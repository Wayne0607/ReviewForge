"""Usage is not progress; token accounting must not reject metered product work."""

from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest
from langchain_core.messages import HumanMessage
from test_discovery_workflow_v6 import batch, lead, no_issue
from test_discovery_workflow_v7 import inputs, patches
from test_investigation_v3 import candidate, source_executor, state
from test_proof_gate import GateLLM
from test_proof_gate import candidate as proof_candidate
from test_proof_gate import ledger as proof_ledger
from test_request_budget import _Model
from test_review_session_v5 import SessionModel, call, packet, receipt
from test_source_audit import audit, verdict

from reviewforge.core.config import PipelineV4Config
from reviewforge.core.events import EventBus
from reviewforge.engine.context_pack import ContextPack
from reviewforge.engine.hypothesis import HypothesisLedger
from reviewforge.engine.investigator import Investigator
from reviewforge.engine.pipeline_v4 import _run_llm_stages
from reviewforge.engine.proof_gate import ProofGate, accepted
from reviewforge.engine.request_budget import (
    BudgetedChatLLM,
    RequestBudget,
    RequestBudgetExceededError,
    budget_snapshot_over_limit,
    external_request_budget_scope,
    plan_request,
)


class ExpensiveModel(SessionModel):
    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        response = super()._generate(messages, stop, run_manager, **kwargs)
        response.generations[0].message.usage_metadata = {
            "input_tokens": 100_000,
            "output_tokens": 10,
            "total_tokens": 100_010,
        }
        return response


async def saved(_snapshot):
    pass


def test_metering_keeps_usage_unknowns_and_frozen_history_without_any_product_ceiling():
    meter = RequestBudget(None, discovery_limit=1, editor_reserve=999_999, adaptive=True)
    meter.allocate(verification=500_000, investigation=500_000, basis={})
    meter.settle(meter.reserve("discovery", 1_000_000), 2_000_000)
    meter.reserve("investigation", 50_000, task_id="task", steps=3)
    restored = RequestBudget(None, saved=meter.snapshot())
    assert restored.reported == 2_000_000 and restored.unreported == 50_000
    assert restored.task_usage["task"]["steps"] == 3
    assert all(restored.available(phase) is None for phase in ("discovery", "investigation", "editor"))
    assert not restored.over_limit and not budget_snapshot_over_limit(restored.snapshot())
    assert restored.snapshot()["allocation_policy"] == "usage-only-v1" and not restored.adaptive
    assert RequestBudget(None, saved=restored.snapshot()).unreported == 50_000
    old = RequestBudget(1000, discovery_limit=500, editor_reserve=100, adaptive=True)
    old.settle(old.reserve("editor", 1000), 1000)
    historical = RequestBudget(None, saved=old.snapshot())
    assert historical.limit == 1000 and historical.adaptive and not historical.metered
    with pytest.raises(RequestBudgetExceededError):
        historical.reserve("editor", 1)


@pytest.mark.asyncio
async def test_finite_financial_grant_survives_metered_product_and_recovery():
    grant, product = RequestBudget(10_000, editor_reserve=0), RequestBudget(None)
    raw = _Model()
    with external_request_budget_scope(grant, identity="paid-grant", on_change=saved):
        model = BudgetedChatLLM(raw, product, phase="discovery")
        plan = plan_request(model, [], max_output_tokens=1000, fallback_available=None)
        assert plan.available_tokens == 10_000
        reservation = product.reserve("discovery", 9000)
        grant_reservation = grant.reserve("editor", 9000)
        with pytest.raises(RequestBudgetExceededError):
            await model.ainvoke([HumanMessage(content="inspect")], max_tokens=2000)
        assert not raw.calls
        product.settle(reservation, 100)
        grant.settle(grant_reservation, 100)
        await model.ainvoke([], max_tokens=2000)
        assert len(raw.calls) == 1 and product.reported == grant.reported == 112
    restored = RequestBudget(None, saved=product.snapshot())
    assert restored.available("editor") == 0
    with pytest.raises(ValueError, match="scope"):
        BudgetedChatLLM(raw, restored, phase="editor")
    with pytest.raises(RequestBudgetExceededError):
        await model.ainvoke([])
    with pytest.raises(ValueError):
        with external_request_budget_scope(RequestBudget(None), identity="unlimited", on_change=saved):
            pass


@pytest.mark.asyncio
async def test_expensive_discovery_field_repair_and_task_all_run_past_old_pr_and_phase_caps():
    review_state, changes, pack = inputs(6, large=True)
    book = HypothesisLedger("integrated", "head", "digest", protocol_version=8)
    phases = []

    def answer(messages, options, turn):
        data = packet(messages)
        if "case_id" in data:
            phases.append("investigation")
            return receipt(
                call(
                    "submit_unknown",
                    {
                        "case_id": data["case_id"],
                        "reason": "Missing contract.",
                        "missing_fact": "The actual consumer contract is unavailable.",
                    },
                )
            )
        if data["session"]["phase"] == "field-repair":
            phases.append("repair")
            return patches(data)
        phases.append("discovery")
        units = data["pending_units"]
        records = [no_issue(unit) for unit in units]
        if units[0]["path"] == "file0.py":
            records[0] = lead(units[0])
            records[-1]["checked"] = "boundary narrative " * 50
        return batch(*records)

    raw = ExpensiveModel(handler=answer)
    runtime = SimpleNamespace(_model_router=SimpleNamespace(get_llm=lambda *_: raw), _events=EventBus(), _db=None)
    publication = await _run_llm_stages(
        runtime,
        review_state,
        changes,
        pack,
        book,
        SimpleNamespace(read=lambda _: "return new_value\n"),
        PipelineV4Config(discovery_max_tokens=1, max_tokens_per_pr=1, investigator_concurrency=1),
        "en",
        {},
    )
    assert "repair" in phases and "investigation" in phases
    assert len(book.no_issue_units) == 5 and len(book.items) == 1 and not book.unresolved_units
    assert not publication.comments
    assert next(iter(book.items.values())).investigation_outcome == "insufficient-evidence"
    assert book.request_budget["limit"] is None and book.request_budget["reported_tokens"] > 250_000
    assert not book.request_budget["denied_requests"] and not book.request_budget["pending"]
    task = next(iter(book.investigation_tasks.values()))
    assert task["token_limit"] is None and task["total_tokens"] > 32_000


@pytest.mark.asyncio
async def test_blind_audit_and_proof_gate_remain_required_after_more_than_old_pr_cap():
    item = proof_candidate()
    book = proof_ledger(item, protocol=8)
    raw = GateLLM(
        replies=[{"audits": [audit()]}, {"reviews": [verdict()]}],
        usage={
            "input_tokens": 150_000,
            "output_tokens": 200,
            "total_tokens": 150_200,
        },
    )
    meter = RequestBudget(None)
    meter.settle(meter.reserve("discovery", 500_000), 500_000)

    async def checkpoint(snapshot):
        book.request_budget = copy.deepcopy(snapshot)

    model = BudgetedChatLLM(raw, meter, phase="editor", on_change=checkpoint)
    await checkpoint(meter.snapshot())
    await ProofGate(model).run(book)
    assert len(raw.calls) == 2 and accepted(item, book)
    assert meter.reported == 800_400 and not meter.over_limit
    assert not budget_snapshot_over_limit(book.request_budget)


@pytest.mark.asyncio
async def test_task_without_financial_scope_is_metered_but_request_output_and_steps_are_finite():
    def answer(messages, options, turn):
        assert 0 < options["max_tokens"] <= 4000
        return receipt(
            call(
                "submit_refuted",
                {
                    "case_id": packet(messages)["case_id"],
                    "reason": "Optional access guards this case.",
                    "premise": "mechanism",
                    "statement": "Optional access prevents this dereference.",
                    "evidence": ["obs_1:e1"],
                },
            )
        )

    model = ExpensiveModel(handler=answer)
    meter = RequestBudget(None)
    result = await Investigator(
        BudgetedChatLLM(model, meter, phase="investigation"), source_executor([]), protocol_version=8, max_steps=2
    ).investigate(candidate(), state(), ContextPack())
    assert result.verdict == "refuted" and result.tokens == meter.reported == 100_010
    assert result.steps == 2 and len(model.calls) == 1


@pytest.mark.asyncio
async def test_finite_grant_still_blocks_blind_audit_and_publication_when_it_is_exhausted():
    item = proof_candidate()
    book = proof_ledger(item, protocol=8)
    meter, grant, raw = RequestBudget(None), RequestBudget(1000, editor_reserve=0), GateLLM()
    grant.settle(grant.reserve("editor", 1000), 1000)

    async def checkpoint(snapshot):
        book.request_budget = snapshot

    with external_request_budget_scope(grant, identity="exhausted", on_change=saved):
        model = BudgetedChatLLM(raw, meter, phase="editor", on_change=checkpoint)
        await checkpoint(meter.snapshot())
        await ProofGate(model).run(book)
    assert not raw.calls and not accepted(item, book) and grant.reported == 1000


@pytest.mark.asyncio
async def test_unknown_attempts_still_survive_a_metered_retry():
    product, raw = RequestBudget(None), _Model(fail=True)
    model = BudgetedChatLLM(raw, product, phase="investigation")
    with pytest.raises(TimeoutError):
        await model.ainvoke([], max_tokens=1000)
    assert product.unreported > 0 and product.serial == 1 and not product.pending
    assert RequestBudget(None, saved=product.snapshot()).unreported == product.unreported


@pytest.mark.asyncio
async def test_source_gap_followup_keeps_original_scope_but_has_no_hidden_task_token_cap():
    from test_evidence_followup import Worker, ready, result, run

    item, book = await ready()
    book.protocol_version = 8
    task = book.investigation_tasks["task"]
    task.update(token_limit=None, budget_policy="usage-only-v1")
    worker = Worker(result(item, tokens=500_000))
    assert await run(worker, book)
    assert worker.calls[0][1]["token_limit"] is None
    assert task["evidence_followup"]["allocated"] == {"tokens": None, "steps": 3}
    assert task["evidence_followup"]["state"] == "completed"
    assert task["total_tokens"] == 500_100 and task["total_steps"] == 3
    assert not await run(worker, book)  # Source collection still cannot restart the original task.


@pytest.mark.asyncio
async def test_actual_source_collection_is_metered_and_still_has_finite_read_rounds():
    model = ExpensiveModel(handler=lambda *_: receipt(call("read_file", {"path": "consumer.ts", "start": 1, "end": 1})))
    meter = RequestBudget(None)
    worker = Investigator(BudgetedChatLLM(model, meter, phase="investigation"), source_executor([]), protocol_version=8)
    result = await worker.collect_evidence(
        candidate(), state(), ContextPack(), token_limit=None, max_steps=1, gaps=["Read the actual consumer contract."]
    )
    assert result.steps == 1 and len(model.calls) == 1 and result.tokens == meter.reported == 100_010
    assert result.outcome == "insufficient-evidence" and result.observations


@pytest.mark.asyncio
async def test_standalone_new_protocol_audit_does_not_fall_back_to_old_phase_ceiling():
    item = proof_candidate()
    book = proof_ledger(item, protocol=8)
    raw = GateLLM(
        replies=[{"audits": [audit()]}, {"reviews": [verdict()]}],
        usage={
            "input_tokens": 150_000,
            "output_tokens": 200,
            "total_tokens": 150_200,
        },
    )
    await ProofGate(raw).run(book)
    assert len(raw.calls) == 2 and accepted(item, book)


@pytest.mark.asyncio
async def test_authorization_identity_is_present_in_the_first_metered_checkpoint_before_any_model_request():
    review_state, changes, pack = inputs(1)
    changes.units.clear()
    book = HypothesisLedger("empty", "head", "digest", protocol_version=8)
    events = EventBus()
    seen = []
    events.subscribe(seen.append)
    raw = _Model()
    runtime = SimpleNamespace(_model_router=SimpleNamespace(get_llm=lambda *_: raw), _events=events, _db=None)
    grant = RequestBudget(10_000, editor_reserve=0)
    with external_request_budget_scope(grant, identity="original-grant", on_change=saved):
        await _run_llm_stages(
            runtime, review_state, changes, pack, book, SimpleNamespace(), PipelineV4Config(), "en", {}, resume=True
        )
    snapshots = [event.data for event in seen if event.event_type == "pipeline_v4.request_budget"]
    assert snapshots[0]["external_budget"]["id"] == "original-grant" and not raw.calls
    restored = RequestBudget(None, saved=snapshots[0])
    with pytest.raises(ValueError, match="scope"):
        restored.bind_current_external()
