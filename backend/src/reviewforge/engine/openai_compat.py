"""Small compatibility fixes for OpenAI-style chat completion providers.

Some reasoning models return ``reasoning_content`` alongside an assistant
message and require that opaque value to be replayed on the next tool-call
round.  ``langchain-openai`` intentionally ignores unknown response fields,
so the value would otherwise be lost between calls.

This adapter only preserves and replays a field that the provider returned.
It does not enable reasoning or send any provider-specific request option.
"""

from __future__ import annotations

from typing import Any

import openai
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatResult
from langchain_openai import ChatOpenAI


class ReasoningContentChatOpenAI(ChatOpenAI):
    """Preserve opaque ``reasoning_content`` across non-streaming tool rounds."""

    def _create_chat_result(
        self,
        response: dict | openai.BaseModel,
        generation_info: dict | None = None,
    ) -> ChatResult:
        result = super()._create_chat_result(response, generation_info)
        response_dict = response if isinstance(response, dict) else response.model_dump()
        choices = response_dict.get("choices")
        if not isinstance(choices, list):
            return result

        for generation, choice in zip(result.generations, choices, strict=False):
            if not isinstance(choice, dict):
                continue
            raw_message = choice.get("message")
            if (
                isinstance(raw_message, dict)
                and "reasoning_content" in raw_message
                and isinstance(generation.message, AIMessage)
            ):
                generation.message.additional_kwargs["reasoning_content"] = raw_message["reasoning_content"]
        return result

    def _get_request_payload(
        self,
        input_: Any,
        *,
        stop: list[str] | None = None,
        **kwargs: Any,
    ) -> dict:
        source_messages = self._convert_input(input_).to_messages()
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        payload_messages = payload.get("messages")
        if not isinstance(payload_messages, list):
            return payload

        for index, source_message in enumerate(source_messages):
            if not isinstance(source_message, AIMessage):
                continue
            if index >= len(payload_messages):
                break
            request_message = payload_messages[index]
            if not isinstance(request_message, dict) or request_message.get("role") != "assistant":
                continue
            if request_message.get("tool_calls") and request_message.get("content") is None:
                request_message["content"] = ""
            if "reasoning_content" in source_message.additional_kwargs:
                request_message["reasoning_content"] = source_message.additional_kwargs["reasoning_content"]
        return payload
