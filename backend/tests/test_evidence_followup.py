"""Followup can add sources once; it cannot buy a new task or revise a verdict."""

import asyncio
import copy
from dataclasses import replace
from types import SimpleNamespace

import pytest
from test_proof_gate import GateLLM, candidate, ledger, observation, review
from test_source_audit import audit

from reviewforge.engine.context_pack import ContextPack
from reviewforge.engine.evidence_followup import run_evidence_followup
from reviewforge.engine.hypothesis import HypothesisLedger, HypothesisStatus
from reviewforge.engine.investigator import InvestigationResult
from reviewforge.engine.proof_gate import ProofGate, completed, proof_fingerprint
from reviewforge.engine.publication_contracts import contract_catalog
from reviewforge.engine.source_audit import current


async def ready(*, aliases=1):
    items = [candidate(f"h_{index}") for index in range(aliases)]
    book = ledger(*items, protocol=4)
    for item in items:
        item.investigation_task_id = "task"
        item.investigation_progress = [{"phase": "original", "event": "read"}]
        item.investigation_steps = 2
    items[0].investigation_tokens = 100
    items[0].investigation_unreported_tokens = 20
    book.investigation_tasks["task"] = {
        "members": [item.identity for item in items],
        "charged_to": items[0].identity,
        "budget_steps_limit": 5,
        "token_limit": 500,
        "total_steps": 2,
        "total_tokens": 100,
        "total_unreported_tokens": 20,
        "attempts": 1,
        "verdict": "confirmed",
        "outcome": "resolved",
    }
    rows = []
    for item in items:
        row = audit(item.id)
        row.update(contracts=[], gaps=["Locate the caller's supported-input requirement."])
        rows.append(row)
    llm = GateLLM(replies=[{"audits": rows}])
    await ProofGate(llm).run(book)
    assert all(completed(item, book) and current(item, book) for item in items)
    return items[0], book


def result(item, *, additions=None, tokens=30, unreported=4, steps=1):
    if additions is None:
        additions = [observation("obs_2", "// caller: absent values must return null\n")]
    return InvestigationResult(
        verdict="refuted",  # A returned claim decision is intentionally ignored.
        reason="cannot rewrite the candidate",
        tokens=tokens,
        unreported_tokens=unreported,
        steps=steps,
        observations=copy.deepcopy(item.observations) + additions,
        progress=copy.deepcopy(item.investigation_progress)
        + [{"phase": "evidence-collection", "event": "budget-final", "budget": {"used_tokens": tokens}}],
    )


class Worker:
    def __init__(self, reply, error=None):
        self.reply = reply
        self.error = error
        self.calls = []

    async def collect_evidence(self, item, state, pack, **kwargs):
        self.calls.append((copy.deepcopy(item), kwargs))
        item.claim = "worker mutation must not escape"
        if self.error is not None:
            raise self.error
        return self.reply

    def evidence_followup_snapshot(self):
        return self.reply


async def run(worker, book, on_update=None):
    return await run_evidence_followup(
        worker, book, SimpleNamespace(head_sha="head"), ContextPack(workspace_digest="workspace"), on_update
    )


@pytest.mark.asyncio
async def test_new_fact_uses_remaining_original_task_once_and_invalidates_old_permission():
    item, book = await ready()
    before = copy.deepcopy(item.to_dict())
    old_audit = copy.deepcopy(book.source_audits[item.id])
    old_review = copy.deepcopy(book.publication_reviews[item.id])
    worker = Worker(result(item))
    checkpoints = []

    async def checkpoint(saved):
        checkpoints.append(copy.deepcopy(saved.to_dict()))
        if len(checkpoints) == 1:
            assert not worker.calls

    assert await run(worker, book, checkpoint)
    assert len(worker.calls) == 1
    assert worker.calls[0][1] == {"token_limit": 380, "max_steps": 3, "gaps": old_audit["facts"]["gaps"]}
    task = book.investigation_tasks["task"]
    saved = task["evidence_followup"]
    assert checkpoints[0]["investigation_tasks"]["task"]["evidence_followup"]["state"] == "started"
    assert saved["state"] == "completed" and saved["new_facts"] == 1
    assert saved["original_audit"] == old_audit and saved["original_review"] == old_review
    assert saved["allocated"] == {"tokens": 380, "steps": 3}
    assert saved["budget_snapshot"] == {"used_tokens": 30}
    assert task["total_tokens"] == item.investigation_tokens == 130
    assert task["total_unreported_tokens"] == item.investigation_unreported_tokens == 24
    assert task["total_steps"] == item.investigation_steps == 3
    assert task["attempts"] == 1 and task["token_limit"] == 500
    assert item.claim == before["claim"] and item.assessment.to_dict() == before["assessment"]
    assert item.status == HypothesisStatus.CONFIRMED and item.verdict_reason == before["verdict_reason"]
    assert not current(item, book) and not completed(item, book)
    assert book.source_audits[item.id] == old_audit and book.publication_reviews[item.id] == old_review
    assert not await run(worker, book)
    restored = HypothesisLedger.from_dict(book.to_dict())
    assert not await run(worker, restored)
    assert len(worker.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("saved_state", ["started", "interrupted", "completed", "rejected"])
async def test_persisted_once_marker_never_reenters(saved_state):
    item, book = await ready()
    book.investigation_tasks["task"]["evidence_followup"] = {"state": saved_state}
    restored = HypothesisLedger.from_dict(book.to_dict())
    worker = Worker(result(item))
    assert not await run(worker, restored) and not worker.calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field",
    ["budget_steps_limit", "token_limit", "total_steps", "total_tokens", "total_unreported_tokens", "members"],
)
async def test_legacy_task_missing_frozen_accounting_cannot_receive_new_allowance(field):
    item, book = await ready()
    del book.investigation_tasks["task"][field]
    worker = Worker(result(item))
    assert not await run(worker, book) and not worker.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("total_tokens", 480), ("total_steps", 5), ("token_limit", True)])
async def test_exhausted_or_invalid_original_allowance_is_not_refilled(field, value):
    item, book = await ready()
    book.investigation_tasks["task"][field] = value
    worker = Worker(result(item))
    assert not await run(worker, book) and not worker.calls


@pytest.mark.asyncio
async def test_equivalent_members_share_sources_with_exactly_one_charge():
    item, book = await ready(aliases=2)
    other = list(book.items.values())[1]
    worker = Worker(result(item))
    assert await run(worker, book)
    assert len(worker.calls) == 1 and other.observations == item.observations
    assert other.investigation_progress == item.investigation_progress
    assert other.investigation_tokens == other.investigation_unreported_tokens == 0
    assert item.investigation_tokens == 130
    assert book.investigation_tasks["task"]["total_tokens"] == 130


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["failed", "duplicate", "duplicate_window", "empty"])
async def test_no_new_source_fact_preserves_original_fingerprint_and_license(kind):
    item, book = await ready()
    fingerprint = proof_fingerprint(item, book.head_sha, book.workspace_digest)
    before = copy.deepcopy(item.observations)
    if kind == "failed":
        addition = replace(observation("obs_2", ""), status="not_found")
    elif kind == "duplicate":
        addition = replace(item.observations[0], id="obs_2", query="different query", result_digest="other")
    elif kind == "duplicate_window":
        item.observations[0] = replace(item.observations[0], excerpt_line_range=(1, 2))
        # Recreate the permit for the extra source metadata before making the call.
        book.source_audits[item.id]["input_fingerprint"] = ""
        book.source_audits[item.id]["fingerprint"] = proof_fingerprint(item, "head", "workspace")
        from reviewforge.engine.source_audit import audit_hash

        book.publication_reviews[item.id].update(
            fingerprint=proof_fingerprint(item, "head", "workspace"), source_audit_hash=audit_hash(item, book)
        )
        fingerprint = proof_fingerprint(item, "head", "workspace")
        before = copy.deepcopy(item.observations)
        addition = replace(item.observations[0], id="obs_2", line_range=(1, 100))
    else:
        addition = observation("obs_2", "  ")
    worker = Worker(result(item, additions=[addition]))
    assert not await run(worker, book)
    assert len(worker.calls) == 1 and completed(item, book) and current(item, book)
    assert item.observations == before
    assert proof_fingerprint(item, "head", "workspace") == fingerprint
    saved = book.investigation_tasks["task"]["evidence_followup"]
    assert saved["state"] == "completed" and saved["new_facts"] == 0
    assert len(saved["observations"]) == 3 and saved["progress"][-1]["event"] == "budget-final"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "damage", ["drop_old", "mutate_old", "wrong_head", "duplicate_id", "progress", "not_observation"]
)
async def test_partial_or_rewritten_returned_sources_are_rejected_but_usage_is_charged(damage):
    item, book = await ready()
    reply = result(item)
    if damage == "drop_old":
        reply.observations.pop(0)
    elif damage == "mutate_old":
        reply.observations[0] = replace(reply.observations[0], excerpt="rewritten")
    elif damage == "wrong_head":
        reply.observations[-1] = replace(reply.observations[-1], sha="other")
    elif damage == "duplicate_id":
        reply.observations[-1] = replace(reply.observations[-1], id="obs_0")
    elif damage == "not_observation":
        reply.observations[-1] = {"id": "not-a-saved-source"}
    else:
        reply.progress.pop(0)
    worker = Worker(reply)
    assert not await run(worker, book)
    assert completed(item, book) and len(item.observations) == 2
    assert item.investigation_tokens == 130
    assert book.investigation_tasks["task"]["evidence_followup"]["state"] == "rejected"


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["accepted", "refuted", "unknown"])
async def test_final_semantic_member_prevents_reopening_entire_shared_task(decision):
    item, book = await ready(aliases=2)
    other = list(book.items.values())[1]
    book.source_audits[other.id]["facts"] = audit(other.id)
    fields = {"accepted": "obligations", "refuted": "counterevidence", "unknown": "missing_obligations"}
    raw = {
        key: value
        for key, value in review(other.id, decision).items()
        if key in {"hypothesis_id", "decision", "reason", fields[decision]}
    }
    if decision == "accepted":
        raw["contract_refs"] = [next(iter(contract_catalog(other, book)))]
    await ProofGate(GateLLM(replies=[{"reviews": [raw]}])).run(book)
    assert completed(other, book) and book.publication_reviews[other.id]["validated"] is True
    assert book.publication_reviews[other.id]["decision"] == decision
    worker = Worker(result(item))
    assert not await run(worker, book) and not worker.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["version", "status", "stale_audit", "stale_review", "gaps", "head", "members"])
async def test_only_current_v4_preflight_with_gaps_and_complete_membership_can_start(damage):
    item, book = await ready()
    if damage == "version":
        book.protocol_version = 3
    elif damage == "status":
        item.status = HypothesisStatus.UNKNOWN
    elif damage == "stale_audit":
        book.source_audits[item.id]["outcome"] = "output-error"
    elif damage == "stale_review":
        book.publication_reviews[item.id]["fingerprint"] = "stale"
    elif damage == "gaps":
        book.source_audits[item.id]["facts"]["gaps"] = []
    elif damage == "head":
        book.head_sha = "other"
    else:
        book.investigation_tasks["task"]["members"].append("missing")
    worker = Worker(result(item))
    assert not await run(worker, book) and not worker.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [RuntimeError("private detail"), asyncio.CancelledError()])
async def test_interrupted_snapshot_accounts_spend_and_persists_without_reentry(error):
    item, book = await ready()
    worker = Worker(result(item), error)
    checkpoints = []

    async def checkpoint(saved):
        checkpoints.append(copy.deepcopy(saved.to_dict()))

    if isinstance(error, asyncio.CancelledError):
        with pytest.raises(asyncio.CancelledError):
            await run(worker, book, checkpoint)
    else:
        assert not await run(worker, book, checkpoint)
    saved = book.investigation_tasks["task"]["evidence_followup"]
    assert saved["state"] == "interrupted" and saved["error_type"] == type(error).__name__
    assert "private detail" not in str(saved)
    assert saved["tokens"] == 30 and saved["unreported_tokens"] == 4 and saved["steps"] == 1
    assert item.investigation_tokens == 130 and len(item.observations) == 2
    assert completed(item, book)
    assert len(checkpoints) == 2
    restored = HypothesisLedger.from_dict(checkpoints[-1])
    assert not await run(worker, restored) and len(worker.calls) == 1


@pytest.mark.asyncio
async def test_checkpoint_failure_prevents_first_paid_call():
    item, book = await ready()
    worker = Worker(result(item))

    async def checkpoint(saved):
        raise OSError("disk unavailable")

    with pytest.raises(OSError):
        await run(worker, book, checkpoint)
    assert not worker.calls
    assert book.investigation_tasks["task"]["evidence_followup"]["state"] == "started"


@pytest.mark.asyncio
@pytest.mark.parametrize("excess", ["tokens", "unknown", "steps"])
async def test_segment_over_original_allocation_is_fully_charged_but_cannot_change_evidence(excess):
    item, book = await ready()
    reply = result(item)
    if excess == "tokens":
        reply.tokens = 380
    elif excess == "unknown":
        reply.unreported_tokens = 351
    else:
        reply.steps = 4
    worker = Worker(reply)
    assert not await run(worker, book) and completed(item, book)
    assert len(item.observations) == 2
    task = book.investigation_tasks["task"]
    assert task["total_tokens"] == 100 + reply.tokens
    assert task["total_unreported_tokens"] == 20 + reply.unreported_tokens
    assert task["total_steps"] == 2 + reply.steps
    assert task["evidence_followup"]["state"] == "rejected"
    assert task["evidence_followup"]["rejection_reason"] == "segment-exceeded-allocated-budget"


@pytest.mark.asyncio
async def test_snapshot_failure_cannot_mask_cancellation_or_allow_reentry():
    item, book = await ready()
    worker = Worker(result(item), asyncio.CancelledError())

    def broken_snapshot():
        raise RuntimeError("private snapshot detail")

    worker.evidence_followup_snapshot = broken_snapshot
    with pytest.raises(asyncio.CancelledError):
        await run(worker, book)
    saved = book.investigation_tasks["task"]["evidence_followup"]
    assert saved["state"] == "interrupted" and saved["usage_unavailable"] is True
    assert saved["snapshot_error_type"] == "RuntimeError"
    assert "private snapshot detail" not in str(saved)
    assert not await run(worker, book) and len(worker.calls) == 1


@pytest.mark.asyncio
async def test_worker_factory_is_lazy_and_retains_compatibility_with_direct_workers():
    item, book = await ready(aliases=2)
    worker = Worker(result(item))
    built = []

    def factory():
        built.append(True)
        return worker

    assert await run(factory, book)
    assert len(built) == len(worker.calls) == 1
    assert not await run(factory, book)
    assert len(built) == 1
