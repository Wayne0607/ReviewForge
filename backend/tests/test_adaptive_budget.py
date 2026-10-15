"""Offline admission/recovery regressions, never model-quality benchmarks."""

from __future__ import annotations

import asyncio
import copy
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from test_investigation_v3 import RecordingLLM, candidate, source_executor, state
from test_pipeline_v4_integration import _unit
from test_proof_gate import GateLLM
from test_proof_gate import candidate as proven_candidate
from test_proof_gate import ledger as proven_ledger

from reviewforge.core.config import PipelineV4Config, ReviewForgeConfig
from reviewforge.core.events import EventBus
from reviewforge.engine.budget_allocation import verification_forecast
from reviewforge.engine.context_pack import ContextPack
from reviewforge.engine.hypothesis import HypothesisLedger, HypothesisStatus, Observation, Site
from reviewforge.engine.investigation_tasks import build_investigation_tasks
from reviewforge.engine.investigator import Investigator
from reviewforge.engine.pipeline_v4 import _run_llm_stages
from reviewforge.engine.request_budget import (
    BudgetedChatLLM,
    RequestBudget,
    RequestBudgetExceededError,
    investigation_request_scope,
    plan_request,
)
from reviewforge.engine.semantic_diff import SemanticChangeSet


def unknown(item):
    return json.dumps(
        {
            "assessed_trigger": item.trigger,
            "assessment": None,
            "counterevidence": None,
            "reason": "consumer input contract missing",
        }
    )


def test_adaptive_discovery_borrows_only_after_protecting_downstream_and_pending_work():
    budget = RequestBudget(150_000, adaptive=True)
    budget.allocate(verification=12_000, investigation=20_000, basis={"candidates": 2})
    first = budget.reserve("discovery", 90_000)  # More than the old 80k ceiling.
    assert budget.available("discovery") == 28_000
    with pytest.raises(RequestBudgetExceededError):
        budget.reserve("discovery", 28_001)
    assert budget.available("investigation") == 48_000
    budget.settle(first, 85_000)
    assert budget.available("discovery") == 33_000
    budget.allocate(verification=0, investigation=0, basis={"candidates": 0})
    assert budget.available("investigation") == budget.available("editor") == 65_000


def test_restoring_historical_allocation_never_silently_grants_elastic_credit():
    old = RequestBudget(100_000, discovery_limit=20_000, editor_reserve=16_000)
    old.settle(old.reserve("discovery", 10_000), 10_000)
    restored = RequestBudget(250_000, adaptive=True, saved=old.snapshot())
    assert not restored.adaptive
    assert restored.available("discovery") == 10_000
    restored.allocate(verification=0, investigation=0, basis={})
    assert restored.editor_reserve == 16_000 and restored.limit == 100_000
    new = RequestBudget(100_000, adaptive=True)
    new.allocate(verification=7_000, investigation=8_000, basis={})
    resumed = RequestBudget(250_000, adaptive=True, saved=new.snapshot())
    assert resumed.adaptive and resumed.limit == 100_000
    assert RequestBudget(100_000, adaptive=False, saved=new.snapshot()).adaptive


def test_unknown_reconciliation_is_exact_once_and_preserves_old_aggregate_cost():
    budget = RequestBudget(20_000, editor_reserve=0, saved={"unreported_reserved_tokens": 500})
    identity = budget.reserve("investigation", 4_000, task_id="task", steps=2)
    budget.settle(identity, None)
    restored = RequestBudget(20_000, editor_reserve=0, saved=budget.snapshot())
    with pytest.raises(ValueError):
        restored.reconcile_unknown(identity, 0, receipt_ref="")
    restored.reconcile_unknown(identity, 1_200, receipt_ref="verified-provider-receipt:1")
    assert (restored.reported, restored.unreported) == (1_200, 500)
    assert restored.task_usage["task"] == {"reported_tokens": 1200, "unreported_reserved_tokens": 0, "steps": 2}
    with pytest.raises(ValueError):
        restored.reconcile_unknown(identity, 0, receipt_ref="verified-provider-receipt:1")
    other = restored.reserve("investigation", 2_000)
    restored.settle(other, None)
    with pytest.raises(ValueError):
        restored.reconcile_unknown(other, 0, receipt_ref="verified-provider-receipt:1")
    assert restored.unreported == 2_500


def test_interrupted_dispatch_keeps_task_charge_and_steps_across_multiple_restores():
    budget = RequestBudget(20_000, editor_reserve=0, adaptive=True)
    identity = budget.reserve("investigation", 5_000, task_id="task", steps=3)
    restored = RequestBudget(20_000, editor_reserve=0, adaptive=True, saved=budget.snapshot())
    assert restored.unreported_requests[identity]["task_id"] == "task"
    assert restored.task_usage["task"]["steps"] == 3
    assert restored.task_usage["task"]["unreported_reserved_tokens"] == 5_000
    again = RequestBudget(20_000, editor_reserve=0, adaptive=True, saved=restored.snapshot())
    assert again.unreported == 5_000 and again.task_usage == restored.task_usage
    again.reconcile_unknown(identity, 22_000, receipt_ref="verified-overrun")
    assert again.over_limit


def test_verification_forecast_tracks_sources_and_candidates_without_mutating_evidence():
    item = proven_candidate()
    book = proven_ledger(item, protocol=4)
    llm = GateLLM()
    saved = copy.deepcopy(book.to_dict())
    small = verification_forecast(llm, book, [item], language="en")
    expanded = replace(
        item,
        observations=[
            *item.observations,
            Observation(
                id="more",
                tool="read_file",
                query="read_file caller.ts",
                path="caller.ts",
                sha=book.head_sha,
                line_range=(1, 40),
                result_digest="large",
                excerpt="positive source line\n" * 40,
                status="success",
            ),
        ],
    )
    large = verification_forecast(llm, book, [expanded], language="en")
    assert large["verification_tokens"] > small["verification_tokens"] > 0
    second = replace(item, id="second", identity="second")
    two = verification_forecast(llm, book, [item, second], language="en")
    assert two["verification_tokens"] > small["verification_tokens"]
    assert verification_forecast(llm, book, [], language="en")["verification_tokens"] == 0
    assert verification_forecast(llm, book, [], language="en", future_candidate=True)["verification_tokens"] > 0
    assert book.to_dict() == saved and not llm.calls


def test_investigation_allowance_uses_workload_instead_of_severity():
    item = candidate(severity="info")
    llm = RecordingLLM()
    worker = Investigator(llm, source_executor([]), protocol_version=4, adaptive_budget=True)
    small = worker.workload_allowance(item, state(), ContextPack())
    severe = worker.workload_allowance(replace(item, severity="error"), state(), ContextPack())
    larger = worker.workload_allowance(
        replace(item, sites=[*item.sites, Site("caller.ts", 5, "invoke()")]), state(), ContextPack()
    )
    assert small[0] == severe[0] and abs(small[1] - severe[1]) <= 8
    assert larger[0] > small[0] and larger[2] > small[2]
    assert 8_000 <= small[1] <= larger[1] <= 32_000
    assert not llm.calls


@pytest.mark.asyncio
async def test_affordable_real_requests_run_below_old_whole_task_admission_limit():
    items = [candidate(), candidate("second")]
    raw = RecordingLLM(turns=[unknown(item) for item in items], usages=[(100, 20), (100, 20)])
    budget = RequestBudget(23_000, editor_reserve=0, adaptive=True)
    llm = BudgetedChatLLM(raw, budget, phase="investigation")
    book = HypothesisLedger("run", "head", "digest", protocol_version=4)
    for item in items:
        book.upsert(item)
    results = await Investigator(llm, source_executor([]), protocol_version=4).run(
        book,
        state(),
        ContextPack(),
        token_budget=7_000,
        request_budget=budget,
        concurrency=1,
    )
    assert len(raw.calls) == 2
    assert all(result.outcome == "insufficient-evidence" for result in results)
    assert budget.reported == 240 and not budget.pending
    assert sum(row["reported_tokens"] for row in budget.task_usage.values()) == 240
    assert all(task["budget_policy"] == "adaptive-v1" for task in book.investigation_tasks.values())


@pytest.mark.asyncio
async def test_task_count_and_token_exhaustion_are_distinct_from_missing_evidence():
    raw = RecordingLLM()
    budget = RequestBudget(1_000, editor_reserve=0, adaptive=True)
    llm = BudgetedChatLLM(raw, budget, phase="investigation")
    book = HypothesisLedger("run", "head", "digest", protocol_version=4)
    book.upsert(candidate())
    book.upsert(candidate("second"))
    results = await Investigator(llm, source_executor([]), protocol_version=4).run(
        book,
        state(),
        ContextPack(),
        request_budget=budget,
        max_hypotheses_per_pr=1,
    )
    assert results[0].outcome == "budget-exhausted"
    assert results[1].outcome == "policy-deferred" and results[1].reason == "investigation-task-cap"
    assert not raw.calls and not budget.reported


@pytest.mark.asyncio
async def test_task_recovery_uses_pending_receipts_even_without_final_verdict_counters():
    item = candidate()
    book = HypothesisLedger("run", "head", "digest", protocol_version=4)
    book.upsert(item)
    task = build_investigation_tasks([item], head_sha="head", workspace_digest="digest")[0]
    book.investigation_tasks[task.id] = {
        "members": list(task.members),
        "charged_to": item.identity,
        "budget_steps_limit": 4,
        "token_limit": 12_000,
        "total_steps": 0,
        "total_tokens": 0,
        "total_unreported_tokens": 0,
        "budget_policy": "adaptive-v1",
    }
    budget = RequestBudget(250_000, adaptive=True)
    budget.reserve("investigation", 12_000, task_id=task.id, steps=2)
    restored = RequestBudget(250_000, adaptive=True, saved=budget.snapshot())
    raw = RecordingLLM()
    results = await Investigator(
        BudgetedChatLLM(raw, restored, phase="investigation"), source_executor([]), protocol_version=4
    ).run(
        book,
        state(),
        ContextPack(),
        request_budget=restored,
    )
    assert results[0].reason == "original-task-budget-exhausted"
    assert book.investigation_tasks[task.id]["total_unreported_tokens"] == 12_000
    assert not raw.calls


@pytest.mark.asyncio
async def test_verified_reconciliation_updates_original_task_balance_without_double_charge():
    item = candidate()
    book = HypothesisLedger("run", "head", "digest", protocol_version=4)
    book.upsert(item)
    task = build_investigation_tasks([item], head_sha="head", workspace_digest="digest")[0]
    budget = RequestBudget(250_000, editor_reserve=0, adaptive=True)
    identity = budget.reserve("investigation", 12_000, task_id=task.id, steps=2)
    budget.settle(identity, None)
    book.investigation_tasks[task.id] = {
        "members": list(task.members),
        "charged_to": item.identity,
        "budget_steps_limit": 8,
        "token_limit": 32_000,
        "total_steps": 2,
        "total_tokens": 0,
        "total_unreported_tokens": 12_000,
        "budget_policy": "adaptive-v1",
        "request_usage_synced": copy.deepcopy(budget.task_usage[task.id]),
    }
    budget.reconcile_unknown(identity, 1200, receipt_ref="verified-first-attempt")
    raw = RecordingLLM(turns=[unknown(item)], usages=[(100, 20)])
    await Investigator(
        BudgetedChatLLM(raw, budget, phase="investigation"), source_executor([]), protocol_version=4
    ).run(
        book,
        state(),
        ContextPack(),
        request_budget=budget,
    )
    record = book.investigation_tasks[task.id]
    assert record["total_tokens"] == 1320 and record["total_unreported_tokens"] == 0
    assert record["token_limit"] == 32_000 and record["budget_steps_limit"] == 8
    assert budget.reported == 1320 and len(raw.calls) == 1


@pytest.mark.asyncio
async def test_failed_checkpoint_before_dispatch_releases_both_reservations():
    budget = RequestBudget(250_000, adaptive=True)
    raw = RecordingLLM()

    async def failed_checkpoint(snapshot):
        raise OSError("checkpoint unavailable")

    llm = BudgetedChatLLM(raw, budget, phase="investigation", on_change=failed_checkpoint)
    with investigation_request_scope(12_000, task_id="task") as task:
        with pytest.raises(OSError):
            await llm.ainvoke("inspect", max_tokens=1000)
        assert not task.pending and not task.unreported
    assert not raw.calls and not budget.pending and not budget.unreported


@pytest.mark.asyncio
async def test_concurrent_adaptive_tasks_share_physical_capacity_and_keep_verification_floor():
    class WaitingLLM(RecordingLLM):
        async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
            await asyncio.sleep(0.01)
            return self._generate(messages, stop, run_manager, **kwargs)

    items = [candidate(), candidate("second")]
    raw = WaitingLLM(turns=[unknown(items[0])], usages=[(100, 20)])
    probe = Investigator(raw, source_executor([]), protocol_version=4, adaptive_budget=True)
    probe._prepare_investigation(items[0], state(), ContextPack())
    await probe._bootstrap(items[0], ContextPack())
    closer, chat, _ = probe._typed_closing_request(raw, probe._closing_chat([], 32_000, hypothesis=items[0]))
    input_tokens = plan_request(closer, chat, fallback_available=32_000).input_tokens
    budget = RequestBudget(4_000 + input_tokens + 2_048, adaptive=True)
    budget.allocate(verification=4_000, investigation=0, basis={})
    llm = BudgetedChatLLM(raw, budget, phase="investigation")
    book = HypothesisLedger("run", "head", "digest", protocol_version=4)
    for item in items:
        book.upsert(item)
    results = await Investigator(llm, source_executor([]), protocol_version=4).run(
        book,
        state(),
        ContextPack(),
        request_budget=budget,
        concurrency=2,
    )
    assert len(raw.calls) == 1
    assert sorted(result.outcome for result in results) == ["budget-exhausted", "insufficient-evidence"]
    assert budget.reported == 120 and not budget.pending and budget.editor_reserve == 4000


@pytest.mark.asyncio
async def test_pipeline_persists_dynamic_policy_and_releases_empty_verification_reserve():
    # Exercise actual routing/wrappers/checkpoints without any network provider.
    raw = RecordingLLM()
    fake = SimpleNamespace(_model_router=SimpleNamespace(get_llm=lambda name: raw), _events=EventBus(), _db=None)
    book = HypothesisLedger("run", "head", "digest", protocol_version=4)
    book.upsert(candidate())
    raw.turns = [unknown(candidate())]
    raw.usages = [(100, 20)]
    await _run_llm_stages(
        fake,
        state(),
        SemanticChangeSet("owner/repo", 1, "head", units=[]),
        ContextPack(),
        book,
        SimpleNamespace(read=lambda path: "\n" * 11 + "return calendar?.id\n"),
        PipelineV4Config(),
        "en",
        {},
        resume=True,
    )
    assert book.request_budget["allocation_policy"] == "adaptive-v1"
    assert book.request_budget["editor_reserve"] == 0
    assert book.request_budget["reported_tokens"] == 120
    assert next(iter(book.items.values())).status == HypothesisStatus.UNKNOWN
    assert next(iter(book.items.values())).investigation_outcome == "insufficient-evidence"


@pytest.mark.asyncio
async def test_failed_discovery_releases_future_candidate_reserve_after_stage_ends():
    raw = RecordingLLM(turns=["{}"], usages=[(100, 20)])
    fake = SimpleNamespace(_model_router=SimpleNamespace(get_llm=lambda name: raw), _events=EventBus(), _db=None)
    book = HypothesisLedger("run", "head", "digest", protocol_version=4)
    await _run_llm_stages(
        fake,
        state(),
        SemanticChangeSet("owner/repo", 1, "head", units=[_unit("a.ts")]),
        ContextPack(),
        book,
        SimpleNamespace(),
        PipelineV4Config(),
        "en",
        {},
    )
    assert book.unresolved_units and not book.items
    assert book.request_budget["allocation"]["future_candidate"] == 0
    assert book.request_budget["editor_reserve"] == book.request_budget["investigation_reserve"] == 0


@pytest.mark.asyncio
async def test_forecast_follows_actual_task_priority_and_excludes_unselected_work():
    first = candidate()
    deferred = replace(
        candidate("deferred", severity="info"), sites=[Site("other.ts", n, "run()") for n in range(1, 6)]
    )
    raw = RecordingLLM(turns=[unknown(first)], usages=[(100, 20)])
    events = EventBus()
    seen = []
    events.subscribe(seen.append)
    fake = SimpleNamespace(_model_router=SimpleNamespace(get_llm=lambda name: raw), _events=events, _db=None)
    book = HypothesisLedger("run", "head", "digest", protocol_version=4)
    book.upsert(first)
    book.upsert(deferred)
    await _run_llm_stages(
        fake,
        state(),
        SemanticChangeSet("owner/repo", 1, "head", units=[]),
        ContextPack(),
        book,
        SimpleNamespace(read=lambda path: "\n" * 11 + "return calendar?.id\n"),
        PipelineV4Config(investigator_max_hypotheses_per_pr=1),
        "en",
        {},
        resume=True,
    )
    allocations = [event.data["allocation"] for event in seen if event.event_type == "pipeline_v4.request_budget"]
    assert max(row["verification_candidates"] for row in allocations) == 1
    assert book.items[first.identity].investigation_outcome == "insufficient-evidence"
    assert book.items[deferred.identity].investigation_outcome == "policy-deferred"


@pytest.mark.parametrize(
    "configured,expected",
    [("adaptive", "adaptive"), ("fixed", "fixed"), ("metered", "metered"), ("bad", "metered"), ("[]", "metered")],
)
def test_budget_policy_is_explicit_config_and_invalid_values_use_the_current_default(tmp_path, configured, expected):
    path = tmp_path / "reviewforge.yaml"
    path.write_text(f"pipeline_v4:\n  budget_policy: {configured}\n", encoding="utf-8")
    assert ReviewForgeConfig.load(path).pipeline_v4.budget_policy == expected
