"""Bounded transport recovery preserves request identity and budget boundaries."""

from __future__ import annotations

import asyncio

import httpx
import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import PrivateAttr

from reviewforge.engine.provider_retry import RetryChatLLM, RetryPolicy, current_retry_attempt
from reviewforge.engine.token_tracker import RunContext, TrackedChatLLM


class SequenceLLM(BaseChatModel):
    _results: list = PrivateAttr()
    _calls: list = PrivateAttr(default_factory=list)

    def __init__(self, results):
        super().__init__()
        self._results = list(results)

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        self._calls.append((messages, stop, kwargs, current_retry_attempt()))
        result = self._results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        raise NotImplementedError

    def bind_tools(self, tools, **kwargs):
        return self.bind(tools=tools, **kwargs)

    @property
    def _llm_type(self):
        return "sequence"


def response():
    return ChatResult(
        generations=[ChatGeneration(message=AIMessage(content="ok"))],
        llm_output={"token_usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}},
    )


def status_error(status):
    request = httpx.Request("POST", "https://relay.example/v1/chat/completions")
    reply = httpx.Response(status, request=request)
    return httpx.HTTPStatusError("secret-key-must-not-appear", request=request, response=reply)


@pytest.mark.asyncio
async def test_same_request_retries_at_most_three_times_and_audits_without_error_body(monkeypatch):
    from reviewforge.engine import provider_retry

    delays, audit = [], []

    async def sleep(delay):
        delays.append(delay)

    async def sink(event):
        audit.append(event)

    monkeypatch.setattr(provider_retry.asyncio, "sleep", sleep)
    inner = SequenceLLM([status_error(503), TimeoutError("secret-key-must-not-appear"), response()])
    llm = RetryChatLLM(inner, attempt_sink=sink)
    messages = [HumanMessage(content="fixed prompt")]
    result = await llm._agenerate(messages, ["stop"], max_tokens=100, response_format={"type": "json_object"})
    assert result.generations[0].message.content == "ok"
    assert len(inner._calls) == 3 and delays == [1.0, 2.0]
    assert all(call[:3] == inner._calls[0][:3] for call in inner._calls)
    assert len({call[3]["logical_request_id"] for call in inner._calls}) == 1
    assert [call[3]["attempt"] for call in inner._calls] == [1, 2, 3]
    assert current_retry_attempt() is None
    finished = [event for event in audit if event["phase"] == "finished"]
    assert [event["usage_status"] for event in finished] == ["unknown", "unknown", "reported"]
    assert [event["will_retry"] for event in finished] == [True, True, False]
    assert "secret-key" not in str(audit)


@pytest.mark.asyncio
async def test_exhaustion_keeps_original_provider_error():
    final = status_error(429)
    inner = SequenceLLM([status_error(429), status_error(429), final, response()])
    llm = RetryChatLLM(inner, RetryPolicy(base_delay_seconds=0))
    with pytest.raises(httpx.HTTPStatusError) as caught:
        await llm.ainvoke("same request")
    assert caught.value is final and len(inner._calls) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 422])
async def test_http_rejections_are_not_blindly_retried(status):
    error = status_error(status)
    inner = SequenceLLM([error, response()])
    with pytest.raises(httpx.HTTPStatusError):
        await RetryChatLLM(inner, RetryPolicy(base_delay_seconds=0)).ainvoke("fixed")
    assert len(inner._calls) == 1


@pytest.mark.asyncio
async def test_explicit_relay_404_policy_is_bounded():
    inner = SequenceLLM([status_error(404), status_error(404), status_error(404), response()])
    with pytest.raises(httpx.HTTPStatusError):
        await RetryChatLLM(inner, RetryPolicy(retry_404=True, base_delay_seconds=0)).ainvoke("fixed")
    assert len(inner._calls) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [ValueError("bad parameter"), RuntimeError("local failure")])
async def test_non_provider_and_budget_errors_never_retry(error):
    inner = SequenceLLM([error, response()])
    with pytest.raises(type(error)):
        await RetryChatLLM(inner).ainvoke("fixed")
    assert len(inner._calls) == 1


@pytest.mark.asyncio
async def test_cancellation_interrupts_backoff_without_another_attempt():
    finished = asyncio.Event()
    audit = []

    async def sink(event):
        audit.append(event)
        if event["phase"] == "finished":
            finished.set()

    inner = SequenceLLM([status_error(503), response()])
    llm = RetryChatLLM(inner, RetryPolicy(base_delay_seconds=30, max_delay_seconds=30), attempt_sink=sink)
    task = asyncio.create_task(llm.ainvoke("fixed"))
    await asyncio.wait_for(finished.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    assert len(inner._calls) == 1


@pytest.mark.asyncio
async def test_cancellation_during_provider_call_is_audited_and_not_retried():
    entered = asyncio.Event()
    audit = []

    class WaitingLLM(SequenceLLM):
        async def _agenerate(self, *args, **kwargs):
            entered.set()
            await asyncio.Event().wait()

    async def sink(event):
        audit.append(event)

    task = asyncio.create_task(RetryChatLLM(WaitingLLM([]), attempt_sink=sink).ainvoke("fixed"))
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert audit[-1]["outcome"] == "cancelled" and audit[-1]["usage_status"] == "unknown"


@pytest.mark.asyncio
async def test_success_usage_is_recorded_once_with_tracking_inside_retry():
    usage = []

    async def sink(event):
        usage.append(event)

    raw = SequenceLLM([status_error(500), response()])
    tracked = TrackedChatLLM(raw, RunContext(), "test", usage_sink=sink)
    await RetryChatLLM(tracked, RetryPolicy(base_delay_seconds=0)).ainvoke("fixed")
    assert len(raw._calls) == 2
    assert len(usage) == 1 and usage[0]["total_tokens"] == 10


@pytest.mark.asyncio
async def test_tool_binding_and_json_marker_keep_retry_wrapper():
    inner = SequenceLLM([status_error(502), response()])
    tracked = TrackedChatLLM(inner, RunContext(), "test", request_options={"response_format": {"type": "json_object"}})
    retry = RetryChatLLM(tracked, RetryPolicy(base_delay_seconds=0))
    bound = retry.bind_tools([{"name": "read_file"}])
    assert retry.json_object_output and bound.bound is retry
    await bound.ainvoke("fixed")
    assert len(inner._calls) == 2
    assert inner._calls[0][2] == inner._calls[1][2]
    assert inner._calls[0][2]["tools"] == [{"name": "read_file"}]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_attempts": 4},
        {"max_attempts": 0},
        {"base_delay_seconds": -1},
        {"max_delay_seconds": float("inf")},
        {"retry_404": "false"},
    ],
)
def test_invalid_policy_cannot_remove_bounds(kwargs):
    with pytest.raises(ValueError):
        RetryPolicy(**kwargs)


def test_wrapper_rejects_sdk_retry_stacking():
    from reviewforge.core.config import LLMConfig
    from reviewforge.engine.model_router import ModelRouter

    router = ModelRouter(LLMConfig(api_key="placeholder"))
    with pytest.raises(ValueError, match="SDK retries"):
        RetryChatLLM(router.get_llm("investigator"))
    RetryChatLLM(router.get_llm("investigator", max_retries=0))


@pytest.mark.asyncio
async def test_actual_openai_sdk_attempt_limit_and_unchanged_payload(monkeypatch):
    from reviewforge.core.config import LLMConfig
    from reviewforge.engine import model_router

    requests, audit = [], []

    def respond(request):
        requests.append(request.content)
        if len(requests) < 3:
            return httpx.Response(429 if len(requests) == 1 else 503, json={"error": {"message": "temporary"}})
        return httpx.Response(
            200,
            json={
                "id": "bounded-probe",
                "object": "chat.completion",
                "model": "test-model",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
            },
        )

    async def sink(event):
        audit.append(event)

    http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    original = model_router.ChatOpenAI
    monkeypatch.setattr(model_router, "ChatOpenAI", lambda **kwargs: original(**kwargs, http_async_client=http))
    raw = model_router.ModelRouter(LLMConfig(api_key="placeholder", model="test-model")).get_llm(
        "investigator", max_retries=0
    )
    try:
        result = await RetryChatLLM(raw, RetryPolicy(base_delay_seconds=0), attempt_sink=sink).ainvoke(
            [HumanMessage(content="fixed prompt")], max_tokens=100
        )
        assert result.content == "ok"
        assert len(requests) == 3 and requests[0] == requests[1] == requests[2]
        assert raw.root_async_client.max_retries == 0
        assert len([event for event in audit if event["phase"] == "started"]) == 3
    finally:
        await http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("sdk", ["openai", "anthropic"])
async def test_sdk_connection_errors_are_retryable(sdk):
    import anthropic
    import openai

    request = httpx.Request("POST", "https://relay.example/v1")
    module = openai if sdk == "openai" else anthropic
    inner = SequenceLLM([module.APIConnectionError(request=request), response()])
    await RetryChatLLM(inner, RetryPolicy(base_delay_seconds=0)).ainvoke("fixed")
    assert len(inner._calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("sdk", ["openai", "anthropic"])
@pytest.mark.parametrize("status", [401, 404, 429, 503])
async def test_sdk_status_errors_follow_the_same_finite_policy(sdk, status):
    import anthropic
    import openai

    request = httpx.Request("POST", "https://relay.example/v1")
    reply = httpx.Response(status, request=request)
    module = openai if sdk == "openai" else anthropic
    error = module.APIStatusError("private error body", response=reply, body={})
    inner = SequenceLLM([error, response()])
    wrapped = RetryChatLLM(inner, RetryPolicy(base_delay_seconds=0))
    if status in {429, 503}:
        assert (await wrapped.ainvoke("fixed")).content == "ok"
        assert len(inner._calls) == 2
    else:
        with pytest.raises(module.APIStatusError):
            await wrapped.ainvoke("fixed")
        assert len(inner._calls) == 1


@pytest.mark.asyncio
async def test_successful_empty_output_is_not_semantically_retried():
    empty = ChatResult(generations=[ChatGeneration(message=AIMessage(content=""))])
    inner = SequenceLLM([empty, response()])
    assert (await RetryChatLLM(inner).ainvoke("fixed")).content == ""
    assert len(inner._calls) == 1


@pytest.mark.asyncio
async def test_each_retry_reenters_budget_admission_and_denial_is_terminal():
    from reviewforge.engine.request_budget import (
        BudgetedChatLLM,
        RequestBudget,
        RequestBudgetExceededError,
        estimate_request,
    )

    messages, options = [HumanMessage(content="fixed")], {"max_tokens": 1000}
    reservation = estimate_request(messages, options) + 1000
    budget = RequestBudget(reservation + 1, editor_reserve=0)
    raw = SequenceLLM([status_error(503), response()])
    tracked = TrackedChatLLM(raw, RunContext(), "test")
    admitted = BudgetedChatLLM(tracked, budget, phase="investigation")
    retry = RetryChatLLM(admitted, RetryPolicy(base_delay_seconds=0))
    with pytest.raises(RequestBudgetExceededError):
        await retry.ainvoke(messages, **options)
    assert len(raw._calls) == 1
    assert budget.unreported == reservation and budget.reported == 0
    assert budget.denials == 1 and not budget.pending


@pytest.mark.asyncio
async def test_v3_known_usage_survives_cancellation_during_database_accounting():
    from reviewforge.engine.request_budget import BudgetedChatLLM, RequestBudget

    entered, receipts = asyncio.Event(), []

    class DB:
        async def record_token_usage(self, **kwargs):
            entered.set()
            await asyncio.Event().wait()

    async def receipt_sink(receipt):
        receipts.append(receipt)

    context = RunContext()
    previous_id, previous_db = context.run_id, context.db
    context.set("cancel-receipt-test", DB())
    raw = SequenceLLM([response()])
    budget = RequestBudget(10000, editor_reserve=0)
    admitted = BudgetedChatLLM(raw, budget, phase="investigation")
    tracked = TrackedChatLLM(admitted, context, "test", usage_sink=receipt_sink, receipt_first=True)
    task = asyncio.create_task(RetryChatLLM(tracked).ainvoke("fixed", max_tokens=1000))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        assert len(raw._calls) == 1
        assert len(receipts) == 1 and receipts[0]["total_tokens"] == 10
        assert budget.reported == 10 and budget.unreported == 0 and not budget.pending
    finally:
        task.cancel()
        context.set(previous_id, previous_db)


@pytest.mark.asyncio
@pytest.mark.parametrize("receipt_first, expected", [(False, ["db", "sink"]), (True, ["sink", "db"])])
async def test_receipt_first_is_opt_in_and_never_duplicates_the_usage_sink(receipt_first, expected):
    order = []

    class DB:
        async def record_token_usage(self, **kwargs):
            order.append("db")

    async def receipt_sink(receipt):
        order.append("sink")

    context = RunContext()
    previous_id, previous_db = context.run_id, context.db
    context.set("receipt-order-test", DB())
    try:
        tracked = TrackedChatLLM(
            SequenceLLM([response()]), context, "test", usage_sink=receipt_sink, receipt_first=receipt_first
        )
        await tracked.ainvoke("fixed")
        assert order == expected
    finally:
        context.set(previous_id, previous_db)


def test_nested_retry_wrappers_are_rejected():
    inner = RetryChatLLM(SequenceLLM([response()]))
    with pytest.raises(ValueError, match="cannot be stacked"):
        RetryChatLLM(TrackedChatLLM(inner, RunContext(), "test"))
