"""Independent, immutable atomic cases with offline streaming callbacks."""

from __future__ import annotations

import copy
import json

import pytest
from pydantic import ValidationError
from test_discovery_submission import fixture
from test_discovery_workflow_v6 import batch, lead, run
from test_discovery_workspace_v11 import worker
from test_review_session_v5 import SessionModel, call, packet, receipt

from reviewforge.engine.atomic_candidates import bind_atomic_identity
from reviewforge.engine.discovery_workflow_tools import assessment_tools
from reviewforge.engine.discovery_workspace import workspace_action
from reviewforge.engine.hypothesis import HypothesisLedger, HypothesisStatus
from reviewforge.engine.investigation_tasks import build_investigation_tasks
from reviewforge.engine.review_workspace import SharedReviewWorkspace
from reviewforge.engine.session_tools import case_identity


def book(protocol=11):
    return HypothesisLedger("run", "head", "digest", protocol_version=protocol)


def child(unit, name="duplicate-construction"):
    fields = lead(unit)
    fields.pop("kind")
    fields.pop("unit_id")
    return fields | {
        "claim_id": name,
        "claim": "The replacement causes duplicate construction."
        if name == "duplicate-construction"
        else "Documentation is lost.",
        "impact": "The builder executes twice."
        if name == "duplicate-construction"
        else "The documentation is unavailable.",
        "open_question": "Does the caller reconstruct the object?"
        if name == "duplicate-construction"
        else "Where are docs preserved?",
        "refutation": "The existing object is reused."
        if name == "duplicate-construction"
        else "The docs are preserved independently.",
    }


def proposal(unit, *claims):
    return {
        "kind": "atomic_hypotheses",
        "unit_id": unit["unit_id"],
        "proposal_id": "replacement-effects",
        "proposal_summary": "Replacement may cause duplicate construction and documentation loss.",
        "atomic_claims": list(claims) or [child(unit), child(unit, "lost-documentation")],
    }


def candidate_tasks(ledger):
    return build_investigation_tasks(
        list(ledger.items.values()), head_sha=ledger.head_sha, workspace_digest=ledger.workspace_digest
    )


@pytest.mark.asyncio
async def test_one_parent_becomes_two_fixed_cases_with_shared_source_and_independent_outcomes():
    ledger = book()
    saved = []
    callbacks = []

    async def on_update(current):
        saved.append(current.to_dict())

    async def on_candidates(current, identities):
        callbacks.append(identities)
        assert len(identities) == 2
        assert all(current.items[key].observations for key in identities)
        assert all(saved[-1]["items"][key]["observations"] for key in identities)
        tasks = candidate_tasks(current)
        assert len(tasks) == 2 and all(len(task.members) == 1 for task in tasks)
        for identity in identities:
            item = current.items[identity]
            current.apply_verdict(
                identity,
                status=HypothesisStatus.CONFIRMED
                if item.discovery_basis["atomic_origin"]["claim_id"] == "duplicate-construction"
                else HypothesisStatus.UNKNOWN,
                verdict_reason="Offline independent result for this original proof obligation.",
            )

    def answer(messages, *_):
        unit = packet(messages)["pending_units"][0]
        return receipt(
            call("read_file", {"path": "consumer.py", "start": 1, "end": 1}),
            call("submit_assessments", {"records": [proposal(unit)]}),
        )

    model = SessionModel(handler=answer)
    result, ledger = await run(model, count=1, ledger=ledger, on_update=on_update, on_candidates=on_candidates)
    assert len(model.calls) == 1 and len(callbacks) == 1 and result.accepted == 2
    assert len(ledger.items) == 2 and not ledger.unresolved_units
    items = list(ledger.items.values())
    assert len({case_identity(item, "head") for item in items}) == 2
    assert {item.status for item in items} == {HypothesisStatus.CONFIRMED, HypothesisStatus.UNKNOWN}
    origins = [item.discovery_basis["atomic_origin"] for item in items]
    assert len({origin["proposal_identity"] for origin in origins}) == 1
    assert all(origin["trust"] == "untrusted-model-proposal" for origin in origins)
    assert items[0].observations == items[1].observations and items[0].sites == items[1].sites
    assert items[0].discovery_basis["behavior"] == items[1].discovery_basis["behavior"]
    row = next(iter(ledger.discovery_blocks.values()))["units"][items[0].unit_id]
    assert row["finished"] and len(row["atomic_claims"]) == 2
    assert all(entry["status"] == "accepted" and entry["case_identity"] for entry in row["atomic_claims"])


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["schema", "source"])
async def test_invalid_child_is_repairable_without_reinserting_valid_frozen_sibling(invalid):
    ledger = book()
    callbacks = []
    first_identity = ""

    async def accepted(current, identities):
        nonlocal first_identity
        callbacks.append(identities)
        assert len(identities) == 1
        if len(callbacks) == 1:
            first_identity = identities[0]
            current.apply_verdict(
                first_identity, status="confirmed", verdict_reason="Only the first consequence is proven."
            )
        else:
            assert identities[0] != first_identity
            assert current.items[first_identity].status is HypothesisStatus.CONFIRMED
            current.apply_verdict(
                identities[0], status="unknown", verdict_reason="The documentation premise is missing."
            )

    def answer(messages, _options, turn):
        unit = packet(messages)["pending_units"][0]
        second = child(unit, "lost-documentation")
        if turn == 1:
            if invalid == "schema":
                second.pop("impact")
            else:
                second["source_refs"] = ["f999.R1"]
        else:
            assert len(ledger.items) == 1 and ledger.items[first_identity].status is HypothesisStatus.CONFIRMED
            assert (
                "invalid-atomic-claim-schema" in messages[-1].content
                if invalid == "schema"
                else "invalid-assessment-reference" in messages[-1].content
            )
        return batch(proposal(unit, child(unit), second))

    model = SessionModel(handler=answer)
    result, ledger = await run(model, count=1, ledger=ledger, on_candidates=accepted)
    assert len(model.calls) == 2 and result.accepted == 2 and len(callbacks) == 2
    assert len(ledger.items) == 2 and not ledger.unresolved_units
    assert {item.status for item in ledger.items.values()} == {HypothesisStatus.CONFIRMED, HypothesisStatus.UNKNOWN}
    row = next(iter(ledger.discovery_blocks.values()))
    assert any(entry.get("result") == "atomic-claim-already-accepted" for entry in row["workflow"]["feedback"])


@pytest.mark.asyncio
async def test_pending_atomic_repair_survives_resume_and_does_not_reask_closed_child():
    ledger = book()

    async def mark_first(current, identities):
        for identity in identities:
            current.apply_verdict(identity, status="confirmed")

    def interrupted(messages, _options, turn):
        if turn > 1:
            raise RuntimeError("offline interruption")
        unit = packet(messages)["pending_units"][0]
        bad = child(unit, "lost-documentation")
        bad.pop("impact")
        return batch(proposal(unit, child(unit), bad))

    await run(SessionModel(handler=interrupted), count=1, ledger=ledger, on_candidates=mark_first)
    assert len(ledger.items) == 1
    restored = HypothesisLedger.from_dict(ledger.to_dict())
    old_identity = next(iter(restored.items))
    called = []

    async def repaired(current, identities):
        called.extend(identities)
        assert old_identity not in identities and current.items[old_identity].status is HypothesisStatus.CONFIRMED

    def resume(messages, *_):
        unit = packet(messages)["pending_units"][0]
        return batch(proposal(unit, child(unit, "lost-documentation")))

    model = SessionModel(handler=resume)
    await run(model, count=1, ledger=restored, on_candidates=repaired)
    assert len(model.calls) == 1 and len(called) == 1 and len(restored.items) == 2 and not restored.unresolved_units


@pytest.mark.asyncio
async def test_retargeting_accepted_child_is_rejected_then_original_case_can_finish_coverage():
    ledger = book()
    calls = []

    async def accepted(current, identities):
        calls.extend(identities)
        for identity in identities:
            current.apply_verdict(identity, status="unknown")

    def answer(messages, _options, turn):
        unit = packet(messages)["pending_units"][0]
        first, second = child(unit), child(unit, "lost-documentation")
        if turn == 1:
            second.pop("impact")
        elif turn == 2:
            first["claim"] = "A new question after the original was unknown."
        else:
            assert "atomic-claim-frozen" in messages[-1].content
        return batch(proposal(unit, first, second))

    model = SessionModel(handler=answer)
    await run(model, count=1, ledger=ledger, on_candidates=accepted)
    assert len(model.calls) == 3 and len(calls) == len(ledger.items) == 2
    assert all("new question" not in item.claim for item in ledger.items.values())
    assert not ledger.unresolved_units


def test_atomic_schema_only_exists_in_eleven_and_old_single_candidate_remains_valid():
    _, _, block, _ = fixture(1)
    for protocol in range(6, 11):
        tool, schemas = assessment_tools(block, protocol_version=protocol)
        assert set(schemas) == {"hypothesis", "no_issue", "deferred"}
        with pytest.raises(ValidationError):
            tool.args_schema.model_validate({"records": [{"kind": "atomic_hypotheses"}]})
    tool, schemas = assessment_tools(block, protocol_version=11)
    assert "atomic_hypotheses" in schemas
    assert "atomic_claims" in json.dumps(tool.args_schema.model_json_schema())


@pytest.mark.asyncio
async def test_single_candidate_text_with_conjunction_is_not_automatically_split():
    def answer(messages, *_):
        return batch(
            lead(packet(messages)["pending_units"][0]) | {"impact": "duplicate construction and documentation loss"}
        )

    _, ledger = await run(SessionModel(handler=answer), count=1, ledger=book())
    assert len(ledger.items) == 1 and not next(iter(ledger.items.values())).discovery_basis.get("atomic_origin")


@pytest.mark.asyncio
async def test_separate_atomic_cases_cannot_share_a_verdict_even_with_equal_current_wording():
    def answer(messages, *_):
        unit = packet(messages)["pending_units"][0]
        same = child(unit)
        return batch(proposal(unit, same, same | {"claim_id": "second-independent-obligation"}))

    _, ledger = await run(SessionModel(handler=answer), count=1, ledger=book())
    assert len(ledger.items) == len(candidate_tasks(ledger)) == 2
    original = next(iter(ledger.items.values()))
    rewritten = copy.deepcopy(original)
    rewritten.claim = "New question cannot replace an original accepted case"
    with pytest.raises(ValueError, match="atomic-case-frozen"):
        bind_atomic_identity(rewritten, ledger)


@pytest.mark.asyncio
async def test_plain_hypothesis_cannot_forge_controller_atomic_origin():
    def answer(messages, _options, turn):
        raw = lead(packet(messages)["pending_units"][0])
        if turn == 1:
            raw["atomic_origin"] = {"claim_id": "forged"}
        return batch(raw)

    model = SessionModel(handler=answer)
    _, ledger = await run(model, count=1, ledger=book())
    assert len(model.calls) == 2 and len(ledger.items) == 1
    assert not next(iter(ledger.items.values())).discovery_basis.get("atomic_origin")


@pytest.mark.asyncio
async def test_refuted_sibling_handoff_reuses_only_raw_source_and_navigation_without_a_verdict():
    def answer(messages, *_):
        return batch(proposal(packet(messages)["pending_units"][0]))

    _, ledger = await run(SessionModel(handler=answer), count=1, ledger=book())
    first, second = list(ledger.items.values())
    ledger.apply_verdict(
        first.identity, status="refuted", verdict_reason="Counterevidence applies only to this consequence."
    )
    identity = next(
        identity
        for identity, record in ledger.evidence_records.items()
        if record["kind"] == "delivered-source" and record["side"] == "after"
    )
    SharedReviewWorkspace(ledger).publish_progress(
        task_id=first.identity,
        actor="investigation",
        unit_ids=[first.unit_id],
        paths=[first.sites[0].path],
        summary="Inspect the replacement operation from the refuted sibling; check this impact independently.",
        open_questions=[second.open_question],
        next_action="Inspect the immutable source for the separate consequence.",
        evidence_ids=[identity],
    )
    work, store = worker(ledger, task=second.identity)
    view = workspace_action(work, "query_review_workspace", {"paths": [first.sites[0].path], "unit_ids": []}, store)
    assert view["notes"][0]["trust"] == "untrusted-navigation-only"
    workspace_action(work, "read_shared_evidence", {"evidence_ids": [identity]}, store)
    assert work.broker._observations[0].tool == "read_file"
    assert ledger.items[first.identity].status is HypothesisStatus.REFUTED
    assert ledger.items[second.identity].status is HypothesisStatus.OPEN
