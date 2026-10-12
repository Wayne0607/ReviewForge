from __future__ import annotations

import json

import httpx
import pytest
from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI

from reviewforge.engine.hypothesis_generator import HypothesisGenerator
from reviewforge.engine.run_health import RunHealth
from reviewforge.engine.token_tracker import RunContext, TrackedChatLLM


@pytest.mark.asyncio
async def test_unrepairable_json_object_response_remains_a_failure_not_an_empty_review():
    from types import SimpleNamespace

    from langchain_core.messages import AIMessage

    received = []

    async def invoke(messages, **kwargs):
        received.extend(messages)
        return AIMessage(content='{"unrepairable":true}')

    generator = HypothesisGenerator(SimpleNamespace(ainvoke=invoke), json_object_output=True)
    assert await generator._invoke_repair("malformed original response") is None
    assert '{"unrepairable":true}' in received[0].content
    assert received[1].content == "malformed original response"


def test_v2_execution_success_keeps_severe_unproven_claim_visible():
    health = RunHealth.build(protocol_version=2, investigation_failures=0, investigation_unknown_total=1)
    summary = health.apply_to_summary({"tasks_failed": 0})
    assert health.completed
    assert summary["v4_health"]["unknown_claims"] == 1
    assert summary["v4_health"]["execution_failures"] == 0


def test_v2_output_failure_is_incomplete_even_without_an_error_severity_claim():
    health = RunHealth.build(protocol_version=2, investigation_failures=1, investigation_unknown_total=1)
    assert not health.completed
    assert health.apply_to_summary({})["status"] == "partial"


@pytest.mark.asyncio
async def test_closure_request_options_reach_sdk_without_changing_exploration_or_bypassing_usage():
    requests, usage = [], []

    async def handle(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "test",
                "object": "chat.completion",
                "model": "deepseek-test",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": '{"assessment":null}'},
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 1, "total_tokens": 11},
            },
        )

    async def record(value):
        usage.append(value)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        inner = ChatOpenAI(
            model="deepseek-test",
            api_key="test",
            base_url="https://test.example/v1",
            http_async_client=client,
            extra_body={"thinking": {"type": "enabled"}},
            reasoning_effort="low",
            max_retries=0,
        )
        explorer = TrackedChatLLM(inner, RunContext(), "investigator", usage_sink=record)
        closer = TrackedChatLLM(
            inner,
            RunContext(),
            "investigator",
            usage_sink=record,
            request_options={
                "extra_body": {"thinking": {"type": "disabled"}},
                "reasoning_effort": None,
                "response_format": {"type": "json_object"},
            },
        )
        await explorer.ainvoke([HumanMessage(content="Explore")], max_tokens=10)
        await closer.ainvoke([HumanMessage(content="Return JSON")], max_tokens=10)
    assert requests[0]["thinking"]["type"] == "enabled"
    assert requests[0]["reasoning_effort"] == "low"
    assert "response_format" not in requests[0]
    assert requests[1]["thinking"]["type"] == "disabled"
    assert requests[1]["response_format"]["type"] == "json_object"
    assert requests[1].get("reasoning_effort") is None
    assert sum(item["total_tokens"] for item in usage) == 22
    assert inner.extra_body["thinking"]["type"] == "enabled"
