from __future__ import annotations

import asyncio
import copy
import json

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import Field

from reviewforge.core.state import StateStore
from reviewforge.engine.context_pack import ContextPack
from reviewforge.engine.hypothesis import Hypothesis, HypothesisLedger, HypothesisStatus, Mechanism, Observation, Site
from reviewforge.engine.investigation_tasks import build_investigation_tasks
from reviewforge.engine.investigator import InvestigationResult, Investigator
from reviewforge.engine.provider_retry import RetryChatLLM, RetryPolicy
from reviewforge.engine.request_budget import BudgetedChatLLM, RequestBudget, RequestPlan, estimate_request


def candidate():
    return Hypothesis(
        id="h_contract",
        identity="unit::wrong-operator::check",
        unit_id="unit",
        mechanism=Mechanism.WRONG_OPERATOR,
        claim="A supported admin is rejected.",
        trigger="An admin calls the operation.",
        impact="The operation returns FORBIDDEN.",
        open_question="What roles does the consumer support?",
        refutation="Only owners may call it.",
        sites=[Site("check.ts", 2, "return isAdmin && isOwner;")],
        severity="error",
        source="generator",
        status=HypothesisStatus.CONFIRMED,
        observations=[
            Observation(
                "obs_4",
                "read_file",
                'read_file {"end": 2, "path": "check.ts", "start": 1}',
                "check.ts",
                (1, 2),
                "head",
                "digest",
                "return isAdmin && isOwner;",
                "success",
            )
        ],
        investigation_progress=[{"phase": "proof-duty", "missing_premise": "contract", "question": "Read consumer"}],
    )


def state():
    return StateStore(repo="o/r", pr_number=1, head_sha="head", file_diffs={"check.ts": "diff"})


def call(name="read_file", **args):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": "call"}])


class Model(BaseChatModel):
    responses: list = Field(default_factory=list)
    calls: list = Field(default_factory=list)
    fail: bool = False
    cancel: bool = False
    missing_usage: bool = False
    metadata_usage: dict = Field(default_factory=dict)
    started: asyncio.Event = Field(default_factory=asyncio.Event)

    @property
    def _llm_type(self):
        return "collection-test"

    def bind_tools(self, tools, **kwargs):
        return self.bind(tools=[convert_to_openai_tool(tool) for tool in tools], **kwargs)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls.append({"messages": list(messages), "options": kwargs})
        if self.fail:
            raise TimeoutError("provider timed out")
        message = self.responses[len(self.calls) - 1].model_copy(deep=True)
        if not self.missing_usage:
            message.usage_metadata = {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120}
        if self.metadata_usage:
            message.response_metadata = {"token_usage": self.metadata_usage}
        return ChatResult(generations=[ChatGeneration(message=message)])

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        if self.cancel:
            self.calls.append({"messages": list(messages), "options": kwargs})
            self.started.set()
            await asyncio.Event().wait()
        return self._generate(messages, stop, run_manager, **kwargs)


def budgeted(model, limit=250_000):
    budget = RequestBudget(limit, editor_reserve=0)
    return BudgetedChatLLM(model, budget, phase="investigation"), budget


@pytest.mark.asyncio
async def test_collection_keeps_original_facts_and_head_without_bootstrap_or_verdict(monkeypatch):
    item = candidate()
    item.observations.append(Observation("obs_20", "grep", "old", "", None, "old", "", "stale", "success"))
    original = copy.deepcopy(item)
    model = Model(responses=[call(path="consumer.ts", start=1, end=2), AIMessage(content='{"verdict":"confirmed"}')])
    llm, budget = budgeted(model)
    executed = []

    async def execute(name, args):
        executed.append((name, args))
        return "if (role !== OWNER) throw Error('owner only');\ncheck(user);"

    def forbidden(*args, **kwargs):
        raise AssertionError("collection must not bootstrap, close, parse or apply a verdict")

    worker = Investigator(llm, execute, protocol_version=4)
    for name in ("_bootstrap", "_close", "_closing_chat", "_parse_verdict", "_finalize"):
        monkeypatch.setattr(worker, name, forbidden)
    result = await worker.collect_evidence(
        item, state(), ContextPack(), token_limit=12_000, max_steps=3, gaps=["No independent role contract."]
    )
    assert item == original
    assert result.verdict == "unknown" and result.assessment is None and result.counterevidence is None
    assert result.strength == "none" and not result.retryable
    assert result.steps == 2 and result.tokens == 240
    assert [obs.id for obs in result.observations] == ["obs_4", "obs_5"]
    assert all(obs.sha == "head" for obs in result.observations)
    assert result.observations[0] == original.observations[0]
    assert result.progress[:1] == original.investigation_progress
    assert len(executed) == 1
    first, second = [json.loads(entry["messages"][-1].content) for entry in model.calls]
    assert first["gaps"] == ["No independent role contract."]
    assert len(first["observations"]) == 1 and len(second["observations"]) == 2
    assert "owner only" in json.dumps(second)
    assert all(entry["options"]["max_tokens"] <= 1000 for entry in model.calls)
    snapshot = result.progress[-1]
    assert snapshot["phase"] == "evidence-collection" and snapshot["event"] == "budget-final"
    assert snapshot["budget"]["limit"] == 12_000
    assert snapshot["budget"]["reported_tokens"] == budget.snapshot()["reported_tokens"] == 240


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response", [call("unknown", path="consumer.ts"), call("read_file"), AIMessage(content='{"verdict":"refuted"}')]
)
async def test_no_legal_tool_never_invents_evidence_or_judgment(response):
    model = Model(responses=[response])
    llm, _ = budgeted(model)

    async def forbidden(*args):
        raise AssertionError("invalid tool must not execute")

    result = await Investigator(llm, forbidden, protocol_version=4).collect_evidence(
        candidate(), state(), ContextPack(), token_limit=12_000, max_steps=3, gaps=["contract missing"]
    )
    assert result.verdict == "unknown" and result.steps == 1
    assert result.observations == candidate().observations
    assert len(model.calls) == 1


@pytest.mark.asyncio
async def test_repeated_query_stops_without_replaying_tools():
    model = Model(responses=[call(path="check.ts", start=1, end=2)])
    llm, _ = budgeted(model)

    async def forbidden(*args):
        raise AssertionError("saved query must not execute again")

    result = await Investigator(llm, forbidden, protocol_version=4).collect_evidence(
        candidate(), state(), ContextPack(), token_limit=12_000, max_steps=3, gaps=["contract missing"]
    )
    assert result.reason == "repeated-evidence" and result.steps == 1
    assert result.observations == candidate().observations


@pytest.mark.asyncio
async def test_explicit_default_arguments_do_not_repeat_saved_query():
    item = candidate()
    item.observations.append(
        Observation("obs_5", "grep", 'grep {"pattern":"consumer"}', "", None, "head", "digest", "", "not_found")
    )
    model = Model(responses=[call("grep", pattern="consumer", glob="", max_hits=10)])
    llm, _ = budgeted(model)

    async def forbidden(*args):
        raise AssertionError("defaults must not make an old query new")

    result = await Investigator(llm, forbidden, protocol_version=4).collect_evidence(
        item, state(), ContextPack(), token_limit=12_000, max_steps=3, gaps=["contract missing"]
    )
    assert result.reason == "repeated-evidence"
    assert result.observations == item.observations


@pytest.mark.asyncio
async def test_explicit_progress_can_read_but_never_exceeds_model_round_limit():
    progress = {
        "progress": {
            "missing_premise": "contract",
            "question": "Read actual consumer",
            "next_evidence": {"tool": "grep", "args": {"pattern": "check"}},
        }
    }
    model = Model(responses=[AIMessage(content=json.dumps(progress))])
    llm, _ = budgeted(model)

    async def execute(name, args):
        assert name == "grep" and args["pattern"] == "check"
        return "- consumer.ts:7: check(user)"

    result = await Investigator(llm, execute, protocol_version=4).collect_evidence(
        candidate(), state(), ContextPack(), token_limit=12_000, max_steps=1, gaps=["contract missing"]
    )
    assert result.steps == len(model.calls) == 1
    assert result.verdict == "unknown" and len(result.observations) == 2
    assert result.observations[-1].tool == "grep"
    assert any(entry.get("next_evidence") == progress["progress"]["next_evidence"] for entry in result.progress)


@pytest.mark.asyncio
async def test_interruption_preserves_public_snapshot_and_unknown_physical_usage():
    model = Model(cancel=True)
    llm, budget = budgeted(model)
    worker = Investigator(llm, None, protocol_version=4)
    task = asyncio.create_task(
        worker.collect_evidence(
            candidate(), state(), ContextPack(), token_limit=12_000, max_steps=3, gaps=["contract missing"]
        )
    )
    await model.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    result = worker.evidence_followup_snapshot()
    assert result.verdict == "unknown" and result.steps == 1 and result.tokens == 0
    assert result.reason == "evidence-collection-interrupted" and not result.retryable
    assert result.unreported_tokens == budget.snapshot()["unreported_reserved_tokens"] > 0
    assert result.progress[-1]["budget"]["pending"] == {}
    assert result.observations == candidate().observations


@pytest.mark.asyncio
@pytest.mark.parametrize("metadata", [{}, {"prompt_tokens": 100}, {"total_tokens": 120}])
async def test_success_without_physical_usage_receipt_keeps_full_reservation_unknown(metadata):
    model = Model(responses=[AIMessage(content="No useful new read.")], missing_usage=True, metadata_usage=metadata)
    llm, budget = budgeted(model)
    result = await Investigator(llm, None, protocol_version=4).collect_evidence(
        candidate(), state(), ContextPack(), token_limit=12_000, max_steps=3, gaps=["contract missing"]
    )
    assert result.steps == 1 and result.tokens == 0
    assert result.unreported_tokens == budget.snapshot()["unreported_reserved_tokens"] > 0
    assert result.progress[-1]["budget"]["unreported_reserved_tokens"] == result.unreported_tokens


@pytest.mark.asyncio
@pytest.mark.parametrize(("tokens", "steps"), [(0, 3), (100, 3), (12_000, 0)])
async def test_no_room_stops_before_model(tokens, steps):
    model = Model()
    llm, _ = budgeted(model)
    result = await Investigator(llm, None, protocol_version=4).collect_evidence(
        candidate(), state(), ContextPack(), token_limit=tokens, max_steps=steps, gaps=["contract missing"]
    )
    assert not model.calls and result.steps == result.tokens == 0
    assert result.verdict == "unknown"
    assert result.observations == candidate().observations


@pytest.mark.asyncio
@pytest.mark.parametrize("output,requests", [(511, 0), (512, 1)])
async def test_collection_requires_at_least_512_output_tokens(monkeypatch, output, requests):
    monkeypatch.setattr(
        "reviewforge.engine.investigator.plan_request", lambda *args, **kwargs: RequestPlan(1_000, 12_000, output)
    )
    model = Model(responses=[AIMessage(content="No useful read.")])
    llm, _ = budgeted(model)
    result = await Investigator(llm, None, protocol_version=4).collect_evidence(
        candidate(), state(), ContextPack(), token_limit=12_000, max_steps=1, gaps=["contract missing"]
    )
    assert len(model.calls) == result.steps == requests
    if requests:
        assert model.calls[0]["options"]["max_tokens"] == output


@pytest.mark.asyncio
async def test_preparation_error_on_reused_worker_never_exposes_previous_usage(monkeypatch):
    model = Model(responses=[AIMessage(content="No useful read.")])
    llm, _ = budgeted(model)
    worker = Investigator(llm, None, protocol_version=4)
    first = await worker.collect_evidence(
        candidate(), state(), ContextPack(), token_limit=12_000, max_steps=1, gaps=["contract missing"]
    )
    assert first.tokens == 120

    def fail(*args):
        raise ValueError("invalid preparation")

    monkeypatch.setattr(worker, "_closing_contract_constraints", fail)
    with pytest.raises(ValueError, match="invalid preparation"):
        await worker.collect_evidence(
            candidate(), state(), ContextPack(), token_limit=12_000, max_steps=1, gaps=["contract missing"]
        )
    snapshot = worker.evidence_followup_snapshot()
    assert snapshot.tokens == snapshot.unreported_tokens == snapshot.steps == 0
    assert snapshot.observations == candidate().observations
    assert snapshot.progress == candidate().investigation_progress


@pytest.mark.asyncio
async def test_physical_retries_share_only_remaining_allowance():
    model = Model(fail=True)
    llm, budget = budgeted(model)
    wrapper = RetryChatLLM(llm, RetryPolicy(max_attempts=3, base_delay_seconds=0))
    result = await Investigator(wrapper, None, protocol_version=4).collect_evidence(
        candidate(), state(), ContextPack(), token_limit=6_000, max_steps=3, gaps=["contract missing"]
    )
    assert 1 <= len(model.calls) < 3
    spent = sum(
        estimate_request(entry["messages"], entry["options"]) + entry["options"]["max_tokens"] for entry in model.calls
    )
    assert result.tokens == 0 and result.unreported_tokens == spent <= 6_000
    assert result.steps == 1 and not result.retryable and result.verdict == "unknown"
    assert result.outcome == "budget-exhausted"
    assert budget.snapshot()["unreported_reserved_tokens"] == spent
    assert result.progress[-1]["budget"]["unreported_reserved_tokens"] == spent


@pytest.mark.asyncio
async def test_new_v4_task_freezes_limits_before_dispatch_and_accumulates(monkeypatch):
    item = candidate()
    item.status = HypothesisStatus.OPEN
    ledger = HypothesisLedger("run", "head", "digest", protocol_version=4)
    ledger.upsert(item)
    checkpoints = []

    async def checkpoint(value):
        checkpoints.append(copy.deepcopy(value.investigation_tasks))

    async def investigate(self, hypothesis, *args, **kwargs):
        frozen = next(iter(ledger.investigation_tasks.values()))
        assert frozen["budget_steps_limit"] == 6 and frozen["token_limit"] == 24_000
        assert checkpoints
        return InvestigationResult("unknown", steps=2, tokens=50, unreported_tokens=80)

    monkeypatch.setattr(Investigator, "investigate", investigate)
    worker = Investigator(None, None, protocol_version=4)
    await worker.run(ledger, state(), ContextPack(), on_update=checkpoint)
    task_id, record = next(iter(ledger.investigation_tasks.items()))
    assert record["total_steps"] == 2 and record["total_unreported_tokens"] == 80
    assert record["total_tokens"] == 50 and record["token_limit"] == 24_000
    ledger.items[item.identity].status = HypothesisStatus.OPEN
    await worker.run(ledger, state(), ContextPack(), on_update=checkpoint)
    assert ledger.investigation_tasks[task_id]["total_steps"] == 4
    assert ledger.investigation_tasks[task_id]["total_tokens"] == 100
    assert ledger.investigation_tasks[task_id]["total_unreported_tokens"] == 160


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol,existing", [(3, False), (4, True)])
async def test_legacy_task_allowance_is_never_guessed(monkeypatch, protocol, existing):
    item = candidate()
    item.status = HypothesisStatus.OPEN
    ledger = HypothesisLedger("run", "head", "digest", protocol_version=protocol)
    ledger.upsert(item)
    if existing:
        task = build_investigation_tasks(ledger.open(), head_sha="head", workspace_digest="digest")[0]
        ledger.investigation_tasks[task.id] = {"total_tokens": 50, "attempts": 1}

    async def investigate(*args, **kwargs):
        return InvestigationResult("unknown", steps=2, tokens=50, unreported_tokens=80)

    monkeypatch.setattr(Investigator, "investigate", investigate)
    await Investigator(None, None, protocol_version=protocol).run(ledger, state(), ContextPack())
    record = next(iter(ledger.investigation_tasks.values()))
    assert not {"token_limit", "budget_steps_limit", "total_steps", "total_unreported_tokens"} & record.keys()
