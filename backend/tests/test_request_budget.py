from __future__ import annotations

import asyncio

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

from reviewforge.engine.request_budget import (
    BudgetedChatLLM,
    RequestBudget,
    RequestBudgetExceededError,
    estimate_request,
    investigation_request_scope,
    plan_request,
)


def test_reservations_protect_editor_and_discovery_allocation():
    budget = RequestBudget(10_000, discovery_limit=3_000, editor_reserve=2_000)
    first = budget.reserve("discovery", 2_000)
    with pytest.raises(RequestBudgetExceededError):
        budget.reserve("discovery", 1_001)
    budget.settle(first, 1_000)
    task = budget.reserve("investigation", 7_000)
    with pytest.raises(RequestBudgetExceededError):
        budget.reserve("investigation", 1)
    editor = budget.reserve("editor", 2_000)
    budget.settle(task, 4_000)
    budget.settle(editor, 1_000)
    assert budget.snapshot()["reported_tokens"] == 6_000
    assert budget.available("investigation") == 2_000


def test_failure_usage_is_unknown_and_resume_cannot_reset_budget():
    budget = RequestBudget(10_000, discovery_limit=8_000, editor_reserve=1_000)
    failed = budget.reserve("investigation", 2_000)
    budget.settle(failed, None)
    budget.reserve("discovery", 3_000)  # interruption with a request in flight
    restored = RequestBudget(20_000, discovery_limit=10_000, editor_reserve=1_000, saved=budget.snapshot())
    snapshot = restored.snapshot()
    assert snapshot["reported_tokens"] == 0
    assert snapshot["unreported_reserved_tokens"] == 5_000
    assert restored.limit == 10_000
    assert restored.available("investigation") == 4_000


def test_provider_overrun_is_recorded_without_clamping_actual_usage():
    budget = RequestBudget(1_000, discovery_limit=500, editor_reserve=100)
    reservation = budget.reserve("investigation", 200)
    budget.settle(reservation, 1_200)
    assert budget.snapshot()["reported_tokens"] == 1_200
    assert budget.over_limit
    with pytest.raises(RequestBudgetExceededError):
        budget.reserve("editor", 1)


def test_opaque_replayed_history_consumes_admission_budget_without_logging_contents():
    plain = AIMessage(content="", tool_calls=[{"id": "read1", "name": "read_file", "args": {"path": "app.py"}}])
    replay = plain.model_copy(update={"additional_kwargs": {"reasoning_content": "opaque" * 2_000}})
    assert estimate_request([replay], {}) - estimate_request([plain], {}) >= 4_000
    assert "opaque" not in str(RequestBudget(10_000).snapshot())


def test_explicit_input_and_output_usage_are_a_receipt_even_without_total():
    from reviewforge.engine.request_budget import _actual_usage

    result = ChatResult(
        generations=[ChatGeneration(message=AIMessage(content="done"))],
        llm_output={"token_usage": {"prompt_tokens": 7, "completion_tokens": 3}},
    )
    assert _actual_usage(result) == 10
    result.llm_output = {"token_usage": {"prompt_tokens": 7}}
    assert _actual_usage(result) is None


class _Model(BaseChatModel):
    calls: list = Field(default_factory=list)
    fail: bool = False

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls.append(kwargs)
        if self.fail:
            raise TimeoutError("provider timed out")
        message = AIMessage(content="done", usage_metadata={"input_tokens": 10, "output_tokens": 2, "total_tokens": 12})
        return ChatResult(generations=[ChatGeneration(message=message)])

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        await asyncio.sleep(0)
        return self._generate(messages, stop, run_manager, **kwargs)

    def bind_tools(self, tools, **kwargs):
        return self.bind(tools=tools, **kwargs)

    @property
    def _llm_type(self):
        return "budget-test"


@pytest.mark.asyncio
async def test_concurrent_requests_reserve_before_provider_and_settle_actuals():
    model = _Model()
    budget = RequestBudget(2_000, discovery_limit=2_000, editor_reserve=0)
    wrapper = BudgetedChatLLM(model, budget, phase="investigation")
    results = await asyncio.gather(
        wrapper.ainvoke([HumanMessage(content="inspect")], max_tokens=1_200),
        wrapper.ainvoke([HumanMessage(content="inspect")], max_tokens=1_200),
        return_exceptions=True,
    )
    assert len(model.calls) == 1
    assert sum(isinstance(item, RequestBudgetExceededError) for item in results) == 1
    assert budget.snapshot()["reported_tokens"] == 12
    assert budget.snapshot()["pending"] == {}


@pytest.mark.asyncio
async def test_failed_attempt_charged_conservatively_and_tools_remain_wrapped():
    model = _Model(fail=True)
    budget = RequestBudget(30_000, discovery_limit=20_000, editor_reserve=0)
    snapshots = []

    async def changed(snapshot):
        snapshots.append(snapshot)

    wrapper = BudgetedChatLLM(model, budget, phase="investigation", on_change=changed)
    with pytest.raises(TimeoutError):
        await wrapper.bind_tools([{"type": "function", "function": {"name": "read"}}]).ainvoke(
            [HumanMessage(content="inspect")], max_tokens=1_000
        )
    assert "tools" in model.calls[0]
    assert snapshots[0]["pending"]
    assert snapshots[-1]["pending"] == {}
    assert snapshots[-1]["reported_tokens"] == 0
    assert snapshots[-1]["unreported_reserved_tokens"] >= 1_000


@pytest.mark.asyncio
async def test_unbounded_editor_call_gets_finite_output_cap():
    model = _Model()
    wrapper = BudgetedChatLLM(model, RequestBudget(20_000, editor_reserve=0), phase="editor")
    await wrapper.ainvoke([HumanMessage(content="write comments")])
    assert model.calls[0]["max_tokens"] == 4_000


@pytest.mark.asyncio
async def test_retries_cannot_reset_the_individual_investigation_allowance():
    from reviewforge.engine.provider_retry import RetryChatLLM, RetryPolicy

    model = _Model(fail=True)
    budget = RequestBudget(50_000, editor_reserve=0)
    wrapped = RetryChatLLM(
        BudgetedChatLLM(model, budget, phase="investigation"),
        RetryPolicy(max_attempts=3, base_delay_seconds=0),
    )
    with investigation_request_scope(7_000) as investigation:
        with pytest.raises(RequestBudgetExceededError):
            await wrapped.ainvoke([HumanMessage(content="inspect")], max_tokens=3_000)
        assert len(model.calls) == 2
        assert 6_000 < investigation.snapshot()["unreported_reserved_tokens"] < 7_000
    assert budget.snapshot()["pending"] == {}
    assert budget.snapshot()["unreported_reserved_tokens"] == investigation.snapshot()["unreported_reserved_tokens"]


@pytest.mark.asyncio
async def test_planning_includes_bound_tools_and_tracked_options_without_reserving():
    from reviewforge.engine.provider_retry import RetryChatLLM, RetryPolicy
    from reviewforge.engine.token_tracker import RunContext, TrackedChatLLM

    model = _Model()
    budget = RequestBudget(30_000, editor_reserve=0)
    options = {"extra_body": {"thinking": {"type": "disabled"}}}
    wrapped = RetryChatLLM(
        TrackedChatLLM(
            BudgetedChatLLM(model, budget, phase="investigation"),
            RunContext(),
            "investigator",
            request_options=options,
        ),
        RetryPolicy(max_attempts=3, base_delay_seconds=0),
    ).bind_tools([{"type": "function", "function": {"name": "read", "description": "source " * 300}}])
    messages = [HumanMessage(content="inspect")]
    before = budget.snapshot()
    plan = plan_request(wrapped, messages, fallback_available=20_000)
    assert budget.snapshot() == before
    await wrapped.ainvoke(messages, max_tokens=plan.max_output_tokens)
    assert plan.input_tokens == estimate_request(messages, model.calls[0])
    assert model.calls[0]["extra_body"] == options["extra_body"]
    assert plan.input_tokens > estimate_request(messages, {"max_tokens": 4_000})


@pytest.mark.asyncio
async def test_closure_reduces_output_to_remaining_task_capacity_using_admission_estimator():
    model = _Model()
    budget = RequestBudget(250_000)
    wrapped = BudgetedChatLLM(model, budget, phase="investigation")
    messages = [HumanMessage(content="source " * 2_850)]
    with investigation_request_scope(16_000) as task:
        receipt = task.reserve("investigation", 6_034)
        task.settle(receipt, 6_034)
        plan = plan_request(wrapped, messages, fallback_available=9_966)
        assert plan.available_tokens == 9_966
        assert 512 <= plan.max_output_tokens < 4_000
        assert plan.input_tokens + plan.max_output_tokens == 9_966
        await wrapped.ainvoke(messages, max_tokens=plan.max_output_tokens)
        assert task.snapshot()["denied_requests"] == 0
        assert task.snapshot()["reported_tokens"] == 6_046


def test_plan_honors_pending_pr_capacity_and_reserves_next_request():
    budget = RequestBudget(20_000, editor_reserve=2_000)
    budget.reserve("investigation", 12_000)
    wrapped = BudgetedChatLLM(_Model(), budget, phase="investigation")
    messages = [HumanMessage(content="inspect")]
    with investigation_request_scope(16_000):
        plan = plan_request(wrapped, messages, fallback_available=16_000, reserve_tokens=3_000)
    assert plan.available_tokens == 6_000
    assert plan.max_output_tokens == 3_000 - plan.input_tokens
    assert budget.snapshot()["request_count"] == 1


@pytest.mark.asyncio
async def test_retries_keep_preplanned_request_identical_when_remaining_capacity_shrinks():
    from reviewforge.engine.provider_retry import RetryChatLLM, RetryPolicy

    model = _Model(fail=True)
    budget = RequestBudget(30_000, editor_reserve=0)
    wrapped = RetryChatLLM(
        BudgetedChatLLM(model, budget, phase="investigation"),
        RetryPolicy(max_attempts=3, base_delay_seconds=0),
    )
    messages = [HumanMessage(content="inspect")]
    plan = plan_request(wrapped, messages, fallback_available=30_000)
    with pytest.raises(TimeoutError):
        await wrapped.ainvoke(messages, max_tokens=plan.max_output_tokens)
    assert len(model.calls) == 3
    assert model.calls[0] == model.calls[1] == model.calls[2]
    assert model.calls[0]["max_tokens"] == plan.max_output_tokens
