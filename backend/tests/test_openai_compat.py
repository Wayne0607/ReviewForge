"""Locked contracts for reasoning-capable OpenAI-compatible providers."""

from __future__ import annotations

import json

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_openai import ChatOpenAI

from reviewforge.engine.model_router import _build_llm
from reviewforge.engine.openai_compat import ReasoningContentChatOpenAI


def _model() -> ReasoningContentChatOpenAI:
    return ReasoningContentChatOpenAI(
        base_url="https://provider.example/v1",
        api_key="test-key",
        model="reasoning-model",
        streaming=False,
    )


def _tool_response(*, reasoning_content: str, call_id: str, argument: int) -> dict:
    return {
        "id": "completion-1",
        "model": "reasoning-model",
        "choices": [
            {
                "index": 0,
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "reasoning_content": reasoning_content,
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": "read_file",
                                "arguments": json.dumps({"line": argument}),
                            },
                        }
                    ],
                },
            }
        ],
    }


def test_reasoning_content_round_trips_from_response_to_next_request() -> None:
    model = _model()
    result = model._create_chat_result(_tool_response(reasoning_content="opaque-first", call_id="call-1", argument=11))
    assistant = result.generations[0].message

    assert isinstance(assistant, AIMessage)
    assert assistant.additional_kwargs["reasoning_content"] == "opaque-first"

    payload = model._get_request_payload([assistant, ToolMessage(content="file contents", tool_call_id="call-1")])
    assert payload["messages"][0]["reasoning_content"] == "opaque-first"


def test_standard_provider_payload_is_identical_to_chat_openai() -> None:
    adapted = _model()
    standard = ChatOpenAI(
        base_url="https://provider.example/v1",
        api_key="test-key",
        model="reasoning-model",
        streaming=False,
    )
    messages = [
        HumanMessage(content="question"),
        AIMessage(content="answer"),
    ]

    assert adapted._get_request_payload(messages) == standard._get_request_payload(messages)


def test_standard_tool_call_content_is_empty_without_reasoning_content() -> None:
    model = _model()
    assistant = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "read_file",
                "args": {"line": 7},
                "id": "call-standard",
                "type": "tool_call",
            }
        ],
    )

    payload = model._get_request_payload([assistant])
    request_message = payload["messages"][0]

    assert "reasoning_content" not in request_message
    assert request_message["content"] == ""
    assert request_message["tool_calls"][0]["id"] == "call-standard"
    assert json.loads(request_message["tool_calls"][0]["function"]["arguments"]) == {"line": 7}


def test_multiple_assistant_positions_preserve_tool_identity_arguments_and_content() -> None:
    model = _model()
    first = (
        model._create_chat_result(_tool_response(reasoning_content="opaque-first", call_id="call-1", argument=11))
        .generations[0]
        .message
    )
    second = (
        model._create_chat_result(_tool_response(reasoning_content="opaque-second", call_id="call-2", argument=22))
        .generations[0]
        .message
    )

    payload = model._get_request_payload(
        [
            HumanMessage(content="inspect"),
            first,
            ToolMessage(content="first result", tool_call_id="call-1"),
            second,
            ToolMessage(content="second result", tool_call_id="call-2"),
        ]
    )
    first_request = payload["messages"][1]
    second_request = payload["messages"][3]

    assert first_request["reasoning_content"] == "opaque-first"
    assert second_request["reasoning_content"] == "opaque-second"
    assert first_request["content"] == ""
    assert second_request["content"] == ""
    assert first_request["tool_calls"][0]["id"] == "call-1"
    assert second_request["tool_calls"][0]["id"] == "call-2"
    assert json.loads(first_request["tool_calls"][0]["function"]["arguments"]) == {"line": 11}
    assert json.loads(second_request["tool_calls"][0]["function"]["arguments"]) == {"line": 22}


def test_model_router_builds_bounded_non_streaming_adapter() -> None:
    model = _build_llm(
        base_url="https://provider.example/v1",
        api_key="test-key",
        model="reasoning-model",
        temperature=0,
    )

    assert isinstance(model, ReasoningContentChatOpenAI)
    assert model.streaming is False
    assert model.request_timeout == 120
    assert model.max_retries == 2
