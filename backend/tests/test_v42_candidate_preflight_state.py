"""Decision-profile candidate preflight failures retain independent recoverable attempts."""

import json

import pytest
from test_v42_decision_state import audit, call
from test_v42_decision_state import board as board

from reviewforge.review_v42.contracts import BindingError, Case, LeaseError, RevisionConflict
from reviewforge.review_v42.decision_state import CorrectableDecisionInputError, DecisionState


def offer(candidate, *, item=None, refs=None):
    return {
        "work_item_id": item["id"] if item else "",
        "expected_revision": item["revision"] if item else 0,
        "question": "What happens?",
        "judgment": "Investigate",
        "evidence_refs": refs or [],
        "action": "offer_candidate",
        "payload": {"candidate": candidate},
    }


def candidate(refs):
    return {"trigger": "action", "behavior": "wrong", "impact": "loss", "evidence_refs": refs}


def stage_attempt(store, token, turn, call_id, args, messages=()):
    audit(store, token, turn, [call(args, call_id)], messages)
    draft = store.stage_candidate_batch("review", token, turn, now=2)[0]
    DecisionState(store).record_attempt("review", token, turn, call_id, args, now=2)
    return draft


def settle(store, token, turn, call_id, phase="schema_validation", errors=None, now=2):
    return store.settle_candidate_preflight_failure(
        "review",
        token,
        turn,
        call_id,
        phase=phase,
        errors=errors or [{"field": "candidate.trigger", "reason": "missing"}],
        now=now,
    )


def test_schema_failure_retains_original_and_allows_explicit_two_target_repair(board):
    store, token = board
    state = DecisionState(store)
    original_args = candidate(["missing"])
    first = offer(original_args)
    first_draft = stage_attempt(store, token, "first", "first-call", first)
    state.stage("review", token, "first", "first-call", first, now=2)
    failed = store.submit_candidate_draft(
        "review", token, first_draft["id"], 0, turn_id="first", call_id="first-call", args=original_args, now=2
    )
    assert failed["status"] == "repair_needed" and failed["revision"] == 1
    item = state.current("review")
    raw_candidate = {
        "behavior": "wrong",
        "impact": "loss",
        "evidence_refs": ["missing"],
        "draft_id": failed["id"],
        "expected_revision": failed["revision"],
    }
    args = offer(raw_candidate, item=item)
    staged = stage_attempt(store, token, "invalid", "invalid-call", args)
    rejected = settle(store, token, "invalid", "invalid-call")
    assert rejected == settle(store, token, "invalid", "invalid-call")
    assert rejected["id"] == staged["id"] and rejected["status"] == "repair_needed" and rejected["revision"] == 1
    assert rejected["requires_resolution"] is True
    assert rejected["original_args"] == raw_candidate and rejected["original_decision_args"] == args
    assert rejected["preflight_target"] == {"status": "known", "draft_id": failed["id"], "revision": 1}
    assert state.current("review") == item and store.get_candidate_draft(failed["id"]) == failed
    with pytest.raises(RevisionConflict):
        settle(store, token, "invalid", "invalid-call", errors=[{"reason": "different"}])
    ref = store.put_evidence(
        b"one\ntwo\n",
        revision="head",
        side="head",
        kind="source",
        path="a.py",
        start_line=1,
        end_line=2,
        metadata={"workspace_digest": "snapshot", "tool_name": "read_source", "tool_args": {"path": "a.py"}},
    )
    store.pin_frontier_for_task("review", token, ref.id, now=2)
    business = candidate([ref.id])
    explicit = {
        **business,
        "draft_id": rejected["id"],
        "expected_revision": 1,
        "candidate_resolution": "apply_to_draft",
        "target_draft_id": failed["id"],
        "target_expected_revision": 1,
    }
    repaired = offer(explicit, item=item, refs=[ref.id])
    receipt = {"evidence_ref": ref.id, "content": "one\ntwo\n", "start_line": 1, "end_line": 2}
    latest = stage_attempt(
        store, token, "repair", "repair-call", repaired, [{"type": "tool", "data": {"content": json.dumps(receipt)}}]
    )
    state.stage("review", token, "repair", "repair-call", repaired, now=2)
    result = store.submit_candidate_draft(
        "review", token, latest["id"], 0, turn_id="repair", call_id="repair-call", args=business, now=2
    )
    assert result["id"] == failed["id"] and result["status"] == "promoted"
    assert store.get_candidate_draft(rejected["id"])["status"] == "applied"
    assert store.get_candidate_draft(latest["id"])["status"] == "applied"
    assert len(store.list(Case)) == 1 and not store.pending_candidate_drafts("a")


def test_stage_unread_ref_and_corrupt_ref_are_distinguished(board):
    store, token = board
    state = DecisionState(store)
    ref = store.put_evidence(
        b"one\ntwo\n",
        revision="head",
        side="head",
        kind="source",
        path="a.py",
        start_line=1,
        end_line=2,
        metadata={"workspace_digest": "snapshot", "tool_name": "read_source"},
    )
    args = offer(candidate([ref.id]), refs=[ref.id])
    draft = stage_attempt(store, token, "unread", "read", args)
    with pytest.raises(CorrectableDecisionInputError) as exc:
        state.stage("review", token, "unread", "read", args, now=2)
    assert exc.value.issues == [{"field": "evidence_refs", "reason": "not_delivered", "evidence_ref": ref.id}]
    result = settle(store, token, "unread", "read", phase="decision_validation", errors=exc.value.issues)
    assert result["id"] == draft["id"] and result["errors"] == exc.value.issues and state.current("review") is None
    args2 = offer(candidate([ref.id]), refs=[ref.id])
    second = stage_attempt(store, token, "corrupt", "other", args2)
    (store.blob_dir / ref.content_hash).write_bytes(b"tampered")
    with pytest.raises(BindingError, match="hash mismatch") as exc:
        state.stage("review", token, "corrupt", "other", args2, now=2)
    assert not isinstance(exc.value, CorrectableDecisionInputError)
    assert store.get_candidate_draft(second["id"])["status"] == "pending_validation"


def test_sibling_attempts_and_failed_settlement_are_isolated(board, monkeypatch):
    store, token = board
    state = DecisionState(store)
    first, second = offer(candidate(["missing1"])), offer(candidate(["missing2"]))
    audit(store, token, "batch", [call(first, "one"), call(second, "two")])
    drafts = store.stage_candidate_batch("review", token, "batch", now=2)
    for ident, args in (("one", first), ("two", second)):
        state.record_attempt("review", token, "batch", ident, args, now=2)
    original = store._event

    def fail(db, kind, ident, action, payload):
        if action == "preflight_rejected":
            raise RuntimeError("simulated crash")
        original(db, kind, ident, action, payload)

    monkeypatch.setattr(store, "_event", fail)
    with pytest.raises(RuntimeError, match="simulated"):
        settle(store, token, "batch", "one")
    assert all(d["status"] == "pending_validation" for d in store.list_candidate_drafts("a"))
    monkeypatch.setattr(store, "_event", original)
    rejected = settle(store, token, "batch", "one")
    other = next(d for d in drafts if d["id"] != rejected["id"])
    assert store.get_candidate_draft(other["id"])["status"] == "pending_validation"
    state.stage("review", token, "batch", "two", second, now=2)
    assert state.current("review")["revision"] == 1
    with pytest.raises(BindingError, match="already staged"):
        settle(store, token, "batch", "two")


def test_lost_lease_or_wrong_profile_cannot_settle(board):
    store, token = board
    args = offer(candidate(["missing"]))
    draft = stage_attempt(store, token, "old", "one", args)
    store.release_task("review", token, now=3)
    new = store.acquire_task("review", "replacement", 100, now=4)
    for lease in (token, new.lease_token):
        with pytest.raises(LeaseError):
            settle(store, lease, "old", "one", now=4)
    assert store.get_candidate_draft(draft["id"])["status"] == "pending_validation"


def test_unknown_target_and_outer_payload_are_preserved_without_guessing(board):
    store, token = board
    args = offer({**candidate(["missing"]), "draft_id": "unrecognized", "expected_revision": 8})
    args["payload"]["draft_id"] = "outer-target"
    draft = stage_attempt(store, token, "unknown", "unknown-call", args)
    rejected = settle(store, token, "unknown", "unknown-call")
    assert rejected["id"] == draft["id"] and rejected["requires_resolution"]
    assert rejected["preflight_target"] == {"status": "unknown"}
    assert rejected["original_decision_args"]["payload"]["draft_id"] == "outer-target"
    assert rejected["original_args"]["draft_id"] == "unrecognized"
    assert store.pending_candidate_drafts("a")[0]["target_resolution"] == "unknown"


def test_decision_attempt_tampering_cannot_change_target_history(board):
    store, token = board
    args = offer(candidate(["missing"]))
    draft = stage_attempt(store, token, "native", "native-call", args)
    with store._connect() as db:
        (raw,) = db.execute("SELECT payload FROM decision_attempts WHERE turn_id='native'").fetchone()
        payload = json.loads(raw)
        payload["args"]["payload"]["draft_id"] = "invented"
        db.execute("UPDATE decision_attempts SET payload=? WHERE turn_id='native'", (json.dumps(payload),))
    with pytest.raises(BindingError, match="audited native call"):
        settle(store, token, "native", "native-call")
    assert store.get_candidate_draft(draft["id"])["status"] == "pending_validation"


def test_missing_reference_is_a_typed_error_but_stale_audit_is_not(board):
    store, token = board
    state = DecisionState(store)
    args = offer(candidate(["missing"]), refs=["missing"])
    draft = stage_attempt(store, token, "missing", "missing-call", args)
    with pytest.raises(CorrectableDecisionInputError) as exc:
        state.stage("review", token, "missing", "missing-call", args, now=2)
    assert exc.value.issues == [{"field": "evidence_refs", "reason": "unknown_id", "evidence_ref": "missing"}]
    result = settle(store, token, "missing", "missing-call", phase="decision_validation", errors=exc.value.issues)
    assert result["id"] == draft["id"] and result["errors"] == exc.value.issues


def test_unknown_id_does_not_mask_corrupt_sibling_source(board):
    store, token = board
    state = DecisionState(store)
    ref = store.put_evidence(
        b"original\n",
        revision="head",
        side="head",
        kind="source",
        path="a.py",
        start_line=1,
        end_line=1,
        metadata={"workspace_digest": "snapshot", "tool_name": "read_source"},
    )
    args = offer(candidate([ref.id]), refs=["ev_missing", ref.id])
    stage_attempt(store, token, "mixed", "mixed-call", args)
    (store.blob_dir / ref.content_hash).write_bytes(b"tampered")
    with pytest.raises(BindingError, match="hash mismatch") as exc:
        state.stage("review", token, "mixed", "mixed-call", args, now=2)
    assert not isinstance(exc.value, CorrectableDecisionInputError)


def test_candidate_submit_keeps_corruption_partial_and_unread_ref_actionable(board):
    store, token = board
    state = DecisionState(store)
    ref = store.put_evidence(
        b"source\n",
        revision="head",
        side="head",
        kind="source",
        path="a.py",
        start_line=1,
        end_line=1,
        metadata={"workspace_digest": "snapshot", "tool_name": "read_source", "tool_args": {"path": "a.py"}},
    )
    store.pin_frontier_for_task("review", token, ref.id, now=2)
    business = candidate([ref.id])
    args = offer(business)
    draft = stage_attempt(store, token, "unread-candidate", "u", args)
    state.stage("review", token, "unread-candidate", "u", args, now=2)
    rejected = store.submit_candidate_draft(
        "review",
        token,
        draft["id"],
        0,
        turn_id="unread-candidate",
        call_id="u",
        args=business,
        now=2,
    )
    assert rejected["errors"] == [
        {
            "field": "evidence_refs",
            "ref": ref.id,
            "reason": "not_delivered",
            "kind": "source",
            "path": "payload.candidate.evidence_refs[0]",
        }
    ]
    second = offer(business, item=state.current("review"))
    corrupt = stage_attempt(store, token, "corrupt-candidate", "bad", second)
    state.stage("review", token, "corrupt-candidate", "bad", second, now=2)
    (store.blob_dir / ref.content_hash).write_bytes(b"tampered")
    with pytest.raises(BindingError, match="hash mismatch"):
        store.submit_candidate_draft(
            "review",
            token,
            corrupt["id"],
            0,
            turn_id="corrupt-candidate",
            call_id="bad",
            args=business,
            now=2,
        )
    assert store.get_candidate_draft(corrupt["id"])["status"] == "pending_validation"
    assert not store.list(Case)


def test_invalid_original_provenance_is_not_downgraded_to_candidate_error(board):
    store, token = board
    state = DecisionState(store)
    ref = store.put_evidence(
        b"content\n",
        revision="head",
        side="head",
        kind="source",
        path="a.py",
        start_line=1,
        end_line=1,
        metadata={"workspace_digest": "snapshot", "tool_name": "read_source", "tool_args": {"path": "other.py"}},
    )
    business = candidate([ref.id])
    args = offer(business)
    draft = stage_attempt(store, token, "invalid-provenance", "p", args)
    state.stage("review", token, "invalid-provenance", "p", args, now=2)
    with pytest.raises(BindingError, match="tool and path"):
        store.submit_candidate_draft(
            "review", token, draft["id"], 0, turn_id="invalid-provenance", call_id="p", args=business, now=2
        )
    assert store.get_candidate_draft(draft["id"])["status"] == "pending_validation"
