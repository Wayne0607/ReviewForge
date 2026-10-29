"""DeepSeek forced native actions must not enter SDK auto-parse or thinking mode."""

from __future__ import annotations

import json

import httpx
import pytest
from langchain_core.messages import HumanMessage

from reviewforge.engine.discovery_submission import bind_discovery_submission
from reviewforge.engine.llm_protocol_v4 import request_options
from reviewforge.engine.openai_compat import ReasoningContentChatOpenAI
from reviewforge.engine.request_budget import BudgetedChatLLM, RequestBudget
from reviewforge.engine.session_tools import result_tool
from reviewforge.engine.token_tracker import RunContext, TrackedChatLLM


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "agent,closing", [("hypothesis_generator", False), ("investigator", False), ("investigator", True)]
)
async def test_deepseek_native_action_uses_non_thinking_wire_and_keeps_response(agent, closing):
    requests = []

    def server(request):
        body = json.loads(request.content)
        requests.append(body)
        assert body["thinking"] == {"type": "disabled"}
        assert body["tool_choice"] == "required" and "response_format" not in body
        assert body["tools"][0]["function"]["name"] == "submit_unknown"
        return httpx.Response(
            200,
            json={
                "id": "test",
                "object": "chat.completion",
                "created": 0,
                "model": "deepseek-flash",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call",
                                    "type": "function",
                                    "function": {"name": "submit_unknown", "arguments": '{"reason":"missing"}'},
                                }
                            ],
                        },
                    }
                ],
                "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as client:
        raw = ReasoningContentChatOpenAI(
            model="deepseek-flash",
            base_url="https://offline.invalid/v1",
            api_key="fake",
            max_retries=0,
            http_async_client=client,
        )
        meter = RequestBudget(None)
        tracked = TrackedChatLLM(
            BudgetedChatLLM(raw, meter, phase="investigation"),
            RunContext(),
            agent,
            request_options=request_options("deepseek-flash", agent, closing=closing, protocol_version=9),
            deepseek_native_tools=True,
        )
        tool = result_tool("submit_unknown", "Deliver a result", {"reason": (str, ...)})
        bound = bind_discovery_submission(tracked, [tool])
        response = await bound.ainvoke([HumanMessage(content="Return a native action")], max_tokens=100)
    assert response.tool_calls[0]["args"] == {"reason": "missing"}
    assert meter.reported == 110 and len(requests) == 1


@pytest.mark.asyncio
async def test_deepseek_text_only_source_audit_preserves_json_mode():
    requests = []

    def server(request):
        body = json.loads(request.content)
        requests.append(body)
        assert body["response_format"] == {"type": "json_object"}
        assert not body.get("tools") and body["thinking"] == {"type": "disabled"}
        return httpx.Response(
            200,
            json={
                "id": "test",
                "object": "chat.completion",
                "created": 0,
                "model": "deepseek-flash",
                "choices": [
                    {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": '{"audits":[]}'}}
                ],
                "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as client:
        raw = ReasoningContentChatOpenAI(
            model="deepseek-flash",
            base_url="https://offline.invalid/v1",
            api_key="fake",
            max_retries=0,
            http_async_client=client,
        )
        tracked = TrackedChatLLM(
            raw,
            RunContext(),
            "editor",
            deepseek_native_tools=True,
            request_options=request_options("deepseek-flash", "editor", protocol_version=9),
        )
        response = await tracked.ainvoke([HumanMessage(content="Return JSON")], max_tokens=100)
    assert json.loads(response.content) == {"audits": []} and len(requests) == 1


def test_historical_or_non_deepseek_options_are_not_silently_rewritten():
    raw = ReasoningContentChatOpenAI(
        model="fixture", base_url="https://offline.invalid/v1", api_key="fake", max_retries=0
    )
    options = {"response_format": {"type": "json_object"}, "tools": [{"type": "function"}]}
    old = TrackedChatLLM(raw, RunContext(), "test", request_options=options)
    assert old._effective_options({}) == options
