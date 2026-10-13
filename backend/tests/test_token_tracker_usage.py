"""Response delivery and conservative charging do not require usage metadata."""

from unittest.mock import AsyncMock

import httpx
import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from reviewforge.engine.openai_compat import ReasoningContentChatOpenAI
from reviewforge.engine.provider_retry import RetryChatLLM, RetryPolicy
from reviewforge.engine.request_budget import (
    BudgetedChatLLM,
    RequestBudget,
    estimate_request,
    investigation_request_scope,
)
from reviewforge.engine.token_tracker import RunContext, TrackedChatLLM


class ResultModel(BaseChatModel):
    result: ChatResult

    def _generate(self, *args, **kwargs):
        return self.result

    async def _agenerate(self, *args, **kwargs):
        return self.result

    @property
    def _llm_type(self):
        return "saved-result"


@pytest.mark.asyncio
@pytest.mark.parametrize("usage", [None, {}])
@pytest.mark.parametrize("receipt_first", [False, True])
async def test_unreported_usage_returns_original_result_without_known_receipt(usage, receipt_first):
    result = ChatResult(
        generations=[ChatGeneration(message=AIMessage(content="A complete response"))],
        llm_output={"token_usage": usage, "model_name": "test"},
    )
    db, sink, ctx = AsyncMock(), AsyncMock(), RunContext()
    ctx.set("run", db)
    try:
        tracked = TrackedChatLLM(ResultModel(result=result), ctx, "test", usage_sink=sink, receipt_first=receipt_first)
        returned = await tracked._agenerate([HumanMessage(content="request")])
        assert returned is result
        assert returned.llm_output == {"token_usage": usage, "model_name": "test"}
        db.record_token_usage.assert_not_awaited()
        sink.assert_not_awaited()
    finally:
        ctx.set("", None)


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["token_usage", "message_usage"])
@pytest.mark.parametrize("total", [0, 17])
async def test_reported_usage_keeps_known_receipt_and_message_fallback(source, total):
    message = AIMessage(content="A complete response")
    usage = None
    if source == "token_usage":
        usage = {"prompt_tokens": total, "completion_tokens": 0, "total_tokens": total}
    else:
        message.usage_metadata = {"input_tokens": total, "output_tokens": 0, "total_tokens": total}
    result = ChatResult(generations=[ChatGeneration(message=message)], llm_output={"token_usage": usage})
    db, sink, ctx = AsyncMock(), AsyncMock(), RunContext()
    ctx.set("run", db)
    try:
        tracked = TrackedChatLLM(ResultModel(result=result), ctx, "test", usage_sink=sink)
        assert await tracked._agenerate([HumanMessage(content="request")]) is result
        sink.assert_awaited_once_with(
            {"prompt_tokens": total, "completion_tokens": 0, "total_tokens": total, "model": ""}
        )
        if total:
            db.record_token_usage.assert_awaited_once()
            assert db.record_token_usage.await_args.kwargs["total_tokens"] == total
        else:
            db.record_token_usage.assert_not_awaited()
    finally:
        ctx.set("", None)


@pytest.mark.asyncio
@pytest.mark.parametrize("usage", ["absent", None, {"prompt_tokens": 11, "completion_tokens": 6, "total_tokens": 17}])
async def test_sdk_native_response_survives_missing_usage_with_one_conservatively_charged_request(usage):
    # Preserve the exact provider string, including formatting. The tracker is
    # not allowed to repair/reparse tool arguments while handling accounting.
    raw_calls = [{"id": "call", "type": "function", "function": {"name": "submit", "arguments": '{ "answer": "ok" }'}}]
    body = {
        "id": "offline",
        "model": "offline",
        "object": "chat.completion",
        "created": 1,
        "choices": [
            {
                "index": 0,
                "finish_reason": "tool_calls",
                "message": {"role": "assistant", "content": "", "tool_calls": raw_calls},
            }
        ],
    }
    if usage != "absent":
        body["usage"] = usage
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json=body)

    db, sink, attempts, ctx = AsyncMock(), AsyncMock(), [], RunContext()

    async def attempt(event):
        attempts.append(event)

    ctx.set("run", db)
    budget = RequestBudget(20000, editor_reserve=0)
    messages = [HumanMessage(content="request")]
    expected_reserve = estimate_request(messages, {"max_tokens": 400}) + 400
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            raw = ReasoningContentChatOpenAI(
                api_key="offline-only",
                base_url="https://example.invalid/v1",
                model="offline",
                max_retries=0,
                http_async_client=client,
                http_socket_options=(),
            )
            tracked = TrackedChatLLM(
                BudgetedChatLLM(raw, budget, phase="investigation"), ctx, "test", usage_sink=sink, receipt_first=True
            )
            retrying = RetryChatLLM(tracked, RetryPolicy(max_attempts=3), attempt_sink=attempt)
            with investigation_request_scope(10000) as task:
                result = await retrying.ainvoke(messages, max_tokens=400)
            assert result.additional_kwargs["reviewforge_raw_tool_calls"] == raw_calls
            assert result.tool_calls[0]["args"] == {"answer": "ok"}
        assert len(requests) == 1
        assert len(attempts) == 2 and attempts[-1]["outcome"] == "success" and not attempts[-1]["will_retry"]
        assert budget.snapshot()["pending"] == {} and task.snapshot()["pending"] == {}
        if isinstance(usage, dict):
            assert budget.reported == task.reported == 17
            assert budget.unreported == task.unreported == 0
            sink.assert_awaited_once()
            db.record_token_usage.assert_awaited_once()
            assert attempts[-1]["usage_status"] == "reported"
        else:
            assert budget.reported == task.reported == 0
            assert budget.unreported == task.unreported == expected_reserve
            sink.assert_not_awaited()
            db.record_token_usage.assert_not_awaited()
            assert result.usage_metadata is None
            assert attempts[-1]["usage_status"] == "unknown"
    finally:
        ctx.set("", None)
