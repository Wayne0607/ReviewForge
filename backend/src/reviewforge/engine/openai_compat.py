"""Small compatibility fixes for OpenAI-style chat completion providers.

Some reasoning models return ``reasoning_content`` alongside an assistant
message and require that opaque value to be replayed on the next tool-call
round.  ``langchain-openai`` intentionally ignores unknown response fields,
so the value would otherwise be lost between calls.

This adapter only preserves and replays a field that the provider returned.
It does not enable reasoning or send any provider-specific request option.
"""

from __future__ import annotations

import copy
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
                and raw_message.get("tool_calls") is not None
                and isinstance(generation.message, AIMessage)
            ):
                # Modern SDK conversion keeps only permissively parsed args.
                # Native result validation must also inspect the exact string
                # for duplicates/completeness; this metadata is never replayed.
                generation.message.additional_kwargs["reviewforge_raw_tool_calls"] = copy.deepcopy(
                    raw_message["tool_calls"]
                )
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


class V42StreamingChatOpenAI(ReasoningContentChatOpenAI):
    """Assemble one v4.2 Chat Completions response from raw SSE frames.

    The SDK's ordinary iterator consumes ``[DONE]`` itself. Reading its SSE
    events lets us distinguish a complete response from an EOF after a partial
    tool call. Tool argument fragments are concatenated without parsing.
    """

    @property
    def v42_stream_transport(self) -> bool:
        return True

    def _get_request_payload(self, input_: Any, *, stop: list[str] | None = None, **kwargs: Any) -> dict:
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        for message in payload.get("messages", []):
            if isinstance(message, dict) and message.get("role") == "assistant":
                for key in ("reasoning_content", "reasoning", "thinking"):
                    message.pop(key, None)
                if isinstance(message.get("content"), list):
                    message["content"] = [
                        block
                        for block in message["content"]
                        if not isinstance(block, dict)
                        or block.get("type") not in {"reasoning", "thinking", "redacted_thinking", "reasoning_content"}
                    ]
        return payload

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs: Any) -> ChatResult:
        import json

        import httpx

        payload = self._get_request_payload(messages, stop=stop, **kwargs)
        if self._use_responses_api(payload):
            raise ValueError("v4.2 streaming requires the Chat Completions API")
        payload["stream"] = True
        options = payload.get("stream_options") or {}
        if not isinstance(options, dict):
            raise ValueError("invalid stream_options")
        payload["stream_options"] = {**options, "include_usage": True}
        response = await self.async_client.create(**payload)
        request = response.response.request
        response_id = None
        model = None
        fingerprint = None
        service_tier = None
        content: list[str] = []
        tools: dict[int, dict[str, Any]] = {}
        finish = None
        usage = None
        done = False

        def protocol_error(message: str) -> ValueError:
            return ValueError("invalid v4.2 streaming response: " + message)

        def unique_pairs(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate SSE object key")
                result[key] = value
            return result

        async with response:
            async for event in response._iter_events():
                if event.data.strip() == "[DONE]":
                    done = True
                    break
                if event.event in ("ping", "heartbeat") or (not event.data and event.event is None):
                    continue
                try:
                    chunk = json.loads(event.data, object_pairs_hook=unique_pairs)
                except json.JSONDecodeError as exc:
                    raise openai.APIConnectionError(
                        message="Malformed or interrupted SSE frame", request=request
                    ) from exc
                except ValueError as exc:
                    raise protocol_error("duplicate SSE object key") from exc
                if not isinstance(chunk, dict):
                    raise protocol_error("non-object event")
                if chunk.get("error") or event.event == "error":
                    error = chunk.get("error") or chunk
                    status = error.get("status", error.get("status_code")) if isinstance(error, dict) else None
                    if type(status) is not int and isinstance(error, dict):
                        code = error.get("code")
                        if type(code) is int or (isinstance(code, str) and code.isdecimal()):
                            status = code
                    if isinstance(status, str) and status.isdecimal():
                        status = int(status)
                    if type(status) is int and 400 <= status <= 599:
                        error_response = httpx.Response(status, request=request)
                        raise openai.APIStatusError("Provider stream error", response=error_response, body=error)
                    raise openai.APIError("Provider stream error", request=request, body=error)
                for key, current in (("id", response_id), ("model", model)):
                    value = chunk.get(key)
                    if value is not None and (not isinstance(value, str) or (current and value != current)):
                        raise protocol_error("inconsistent " + key)
                response_id = chunk.get("id") or response_id
                model = chunk.get("model") or model
                fingerprint = chunk.get("system_fingerprint") or fingerprint
                service_tier = chunk.get("service_tier") or service_tier
                choices = chunk.get("choices")
                if not isinstance(choices, list) or len(choices) > 1:
                    raise protocol_error("expected at most one choice")
                if choices:
                    choice = choices[0]
                    if not isinstance(choice, dict) or choice.get("index", 0) != 0:
                        raise protocol_error("ambiguous choice index")
                    delta = choice.get("delta") or {}
                    if not isinstance(delta, dict):
                        raise protocol_error("invalid delta")
                    if finish is not None and (delta or choice.get("finish_reason") is not None):
                        raise protocol_error("content after completion")
                    role = delta.get("role")
                    if role is not None and role != "assistant":
                        raise protocol_error("unexpected role")
                    text = delta.get("content")
                    if text is not None:
                        if not isinstance(text, str):
                            raise protocol_error("non-text content")
                        content.append(text)
                    chunks = delta.get("tool_calls") or []
                    if not isinstance(chunks, list):
                        raise protocol_error("invalid tool-call deltas")
                    seen_indices = set()
                    for part in chunks:
                        if not isinstance(part, dict) or type(part.get("index")) is not int or part["index"] < 0:
                            raise protocol_error("invalid tool-call index")
                        index = part["index"]
                        if index in seen_indices:
                            raise protocol_error("duplicate tool-call index in event")
                        seen_indices.add(index)
                        item = tools.setdefault(index, {"id": "", "name": "", "arguments": ""})
                        part_id = part.get("id")
                        if part_id is not None:
                            if not isinstance(part_id, str) or (item["id"] and item["id"] != part_id):
                                raise protocol_error("conflicting tool-call ID")
                            item["id"] = part_id
                        if part.get("type") not in (None, "function"):
                            raise protocol_error("unsupported tool-call type")
                        function = part.get("function") or {}
                        if not isinstance(function, dict):
                            raise protocol_error("invalid function delta")
                        for field in ("name", "arguments"):
                            fragment = function.get(field)
                            if fragment is not None:
                                if not isinstance(fragment, str):
                                    raise protocol_error("non-text " + field)
                                item[field] += fragment
                    choice_finish = choice.get("finish_reason")
                    if choice_finish is not None:
                        if not isinstance(choice_finish, str) or not choice_finish:
                            raise protocol_error("invalid finish reason")
                        finish = choice_finish
                if chunk.get("usage") is not None:
                    if not isinstance(chunk["usage"], dict):
                        raise protocol_error("invalid usage")
                    if finish is not None:
                        usage = copy.deepcopy(chunk["usage"])
        if not done or finish is None:
            raise openai.APIConnectionError(message="Incomplete SSE response", request=request)
        if finish == "tool_calls" and tools:
            if sorted(tools) != list(range(len(tools))) or any(not x["id"] or not x["name"] for x in tools.values()):
                raise protocol_error("incomplete tool-call identity")
            if len({x["id"] for x in tools.values()}) != len(tools):
                raise protocol_error("duplicate tool-call ID")
        raw_tools = [
            {"id": item["id"], "type": "function", "function": {"name": item["name"], "arguments": item["arguments"]}}
            for _, item in sorted(tools.items())
        ]
        message: dict[str, Any] = {"role": "assistant", "content": "".join(content)}
        if raw_tools and finish == "tool_calls":
            message["tool_calls"] = raw_tools
        valid_usage = usage is not None and all(
            type(usage.get(key)) is int and usage[key] >= 0 for key in ("prompt_tokens", "completion_tokens")
        )
        safe_usage = copy.deepcopy(usage) if valid_usage else None
        if safe_usage is not None:
            if type(safe_usage.get("total_tokens")) is not int or safe_usage["total_tokens"] < 0:
                safe_usage.pop("total_tokens", None)
            for field in ("prompt_tokens_details", "completion_tokens_details"):
                details = safe_usage.get(field)
                if not isinstance(details, dict):
                    safe_usage.pop(field, None)
                else:
                    safe_usage[field] = {
                        key: value for key, value in details.items() if type(value) is int and value >= 0
                    }
        full_response = {
            "id": response_id or "",
            "model": model or self.model_name,
            "usage": safe_usage,
            "system_fingerprint": fingerprint or "",
            "service_tier": service_tier if isinstance(service_tier, str) else None,
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        }
        result = self._create_chat_result(full_response)
        if usage is not None:
            if valid_usage:
                result.llm_output["token_usage"] = usage
            else:
                # SDK normalization fills absent fields with zero. Retain the
                # raw partial receipt without claiming complete usage.
                result.llm_output["provider_partial_usage"] = usage
        if (
            finish not in {"stop", "tool_calls"}
            or (raw_tools and finish != "tool_calls")
            or (finish == "tool_calls" and not raw_tools)
        ):
            # Preserve reported usage and finish reason but never expose a partial
            # tool call as a successful native action.
            message = result.generations[0].message
            message.tool_calls.clear()
            # Neither native-submission fallback nor a later message reconstruction
            # may reinterpret incomplete raw calls as completed tool actions.
            message.additional_kwargs.pop("tool_calls", None)
            message.additional_kwargs.pop("reviewforge_raw_tool_calls", None)
            if raw_tools:
                message.additional_kwargs["reviewforge_incomplete_tool_calls"] = raw_tools
            message.invalid_tool_calls.append(
                {
                    "name": None,
                    "args": None,
                    "id": None,
                    "error": "incomplete-finish:" + finish,
                    "type": "invalid_tool_call",
                }
            )
        return result
