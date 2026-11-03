import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from reviewforge.review_v42.contracts import BindingError, Case, LeaseError, ReviewScope, Run, Task
from reviewforge.review_v42.shared_investigation import SharedInvestigations
from reviewforge.review_v42.store import ReviewStore


@pytest.fixture
def board(tmp_path):
    region = {"id": "a", "path": "a.py", "side": "head", "start_line": 1, "end_line": 2, "changed_lines": [1]}
    run = Run(
        "r",
        "org/repo",
        "base",
        "head",
        policy="v42-units-6",
        metadata={
            "workspace_digest": "snapshot",
            "base_workspace_digest": "base-snapshot",
            "changed_regions": [region],
            "shared_investigation": "shared-1",
            "candidate_draft_policy": "candidate-draft-1",
        },
    )
    store = ReviewStore(tmp_path / "state.db", run)
    store.install_unit_plan(
        [ReviewScope("a", "r", "a", regions=[region])], ReviewScope("i", "r", "i", dependencies=["a"])
    )
    SharedInvestigations(store)
    store.add(Task("review", "r", "review", "a", 0))
    token = store.acquire_task("review", "worker", 100, now=0).lease_token
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
    store.pin_frontier_for_task("review", token, ref.id, now=1)
    return store, token, ref


def call(name, call_id, args):
    return {"name": name, "id": call_id, "args": args, "type": "tool_call"}


def turn(store, token, turn_id, calls, refs=(), *, partial=False):
    messages = []
    for ref in refs:
        text = store.read_evidence(ref.id).decode()
        if partial:
            text = "one\n"
        receipt = {"evidence_ref": ref.id, "content": text, "start_line": 1, "end_line": 1 if partial else 2}
        messages.append({"type": "tool", "data": {"content": json.dumps(receipt)}})
    store.append_turn_audit("review", token, turn_id, "request", messages, now=2)
    store.append_turn_audit(
        "review", token, turn_id, "response", [{"type": "ai", "data": {"content": "", "tool_calls": calls}}], now=2
    )


def payload(refs):
    return {"trigger": "user action", "behavior": "wrong result", "impact": "lost work", "evidence_refs": refs}


def test_all_invalid_refs_preserved_then_repaired_atomically(board):
    store, token, ref = board
    original = payload(["ev_missing_one", ref.id, "ev_missing_two"])
    turn(store, token, "one", [call("submit_case", "call-a", original)], [ref])
    draft = store.stage_candidate_batch("review", token, "one", now=2)[0]
    assert store.stage_candidate_batch("review", token, "one", now=2)[0]["id"] == draft["id"]
    failed = store.submit_candidate_draft(
        "review", token, draft["id"], 0, turn_id="one", call_id="call-a", args=original, now=3
    )
    assert failed["status"] == "repair_needed"
    assert {error["ref"] for error in failed["errors"]} == {"ev_missing_one", "ev_missing_two"}
    assert failed["original_args"] == original
    assert store.list(Case) == [] and len(store.pending_candidate_drafts("a")) == 1
    assert (
        store.submit_candidate_draft(
            "review", token, draft["id"], 0, turn_id="one", call_id="call-a", args=original, now=3
        )
        == failed
    )
    repaired = payload([ref.id])
    native = {**repaired, "draft_id": draft["id"], "expected_revision": 1}
    turn(store, token, "two", [call("submit_case", "call-b", native)], [ref])
    attempt = store.stage_candidate_batch("review", token, "two", now=2)[0]
    result = store.submit_candidate_draft(
        "review", token, attempt["id"], 0, turn_id="two", call_id="call-b", args=repaired, now=3
    )
    assert result["status"] == "promoted" and result["revision"] == 2
    assert store.get(Case, result["case_id"]).evidence_refs == [ref.id]
    assert store.pending_candidate_drafts("a") == []
    assert store.history("CandidateDraft", draft["id"])[0]["payload"]["original_args"] == original


def test_partial_or_missing_current_delivery_cannot_promote(board):
    store, token, ref = board
    args = payload([ref.id])
    turn(store, token, "partial", [call("submit_case", "a", args)], [ref], partial=True)
    draft = store.stage_candidate_batch("review", token, "partial", now=2)[0]
    result = store.submit_candidate_draft(
        "review", token, draft["id"], 0, turn_id="partial", call_id="a", args=args, now=3
    )
    assert result["status"] == "repair_needed"
    assert result["errors"][0]["ref"] == ref.id
    assert store.list(Case) == []


def test_retract_requires_original_native_call_and_preserves_history(board):
    store, token, _ = board
    args = payload(["missing"])
    turn(store, token, "first", [call("submit_case", "a", args)])
    draft = store.stage_candidate_batch("review", token, "first", now=2)[0]
    request = {"draft_id": draft["id"], "expected_revision": 0, "reason": "not a defect"}
    turn(store, token, "withdraw", [call("retract_candidate_draft", "b", request)])
    result = store.retract_candidate_draft(
        "review", token, draft["id"], 0, turn_id="withdraw", call_id="b", reason="not a defect", now=3
    )
    assert result["status"] == "retracted" and result["reason"] == "not a defect"
    assert result["original_args"] == args and store.pending_candidate_drafts("a") == []
    assert (
        store.retract_candidate_draft(
            "review", token, draft["id"], 0, turn_id="withdraw", call_id="b", reason="not a defect", now=3
        )
        == result
    )


def test_expired_lease_and_ambiguous_batch_never_stage(board):
    store, token, ref = board
    args = payload([ref.id])
    turn(store, token, "bad", [call("submit_case", "same", args), call("submit_case", "same", args)], [ref])
    with pytest.raises(BindingError):
        store.stage_candidate_batch("review", token, "bad", now=3)
    assert store.list_candidate_drafts("a") == []
    turn(store, token, "good", [call("submit_case", "good", args)], [ref])
    with pytest.raises(LeaseError):
        store.stage_candidate_batch("review", token, "good", now=101)


@pytest.mark.parametrize("invalid", [None, "", False, 0, [], {}, "other"])
def test_invalid_policy_fails_before_creating_store(tmp_path, invalid):
    run = Run("r", "repo", "base", "head", policy="v42-units-6", metadata={"candidate_draft_policy": invalid})
    path = tmp_path / "not-created" / "board.db"
    with pytest.raises(BindingError):
        ReviewStore(path, run)
    assert not path.parent.exists()
    old = replace(run, metadata={})
    assert ReviewStore(path, old).list_candidate_drafts() == []
    assert ReviewStore(path, old).run == old


@pytest.mark.parametrize("policy,shared", [("v42", "shared-1"), ("v42-units-6", ""), ("v42-units-6", None)])
def test_policy_requires_units_and_shared_before_creating_store(tmp_path, policy, shared):
    metadata = {"candidate_draft_policy": "candidate-draft-1", "shared_investigation": shared}
    run = Run("r", "repo", "base", "head", policy=policy, metadata=metadata)
    path = tmp_path / "not-created" / "board.db"
    with pytest.raises(BindingError):
        ReviewStore(path, run)
    assert not path.parent.exists()


def test_promotion_and_draft_close_roll_back_together(board, monkeypatch):
    store, token, ref = board
    args = payload([ref.id])
    turn(store, token, "commit", [call("submit_case", "a", args)], [ref])
    draft = store.stage_candidate_batch("review", token, "commit", now=2)[0]
    original = store.commit_unit_decision

    def fail_after_case(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("injected interruption before draft close")

    monkeypatch.setattr(store, "commit_unit_decision", fail_after_case)
    with pytest.raises(RuntimeError, match="injected interruption"):
        store.submit_candidate_draft("review", token, draft["id"], 0, turn_id="commit", call_id="a", args=args, now=3)
    assert store.list(Case) == []
    assert store.get_candidate_draft(draft["id"])["status"] == "pending_validation"
    monkeypatch.setattr(store, "commit_unit_decision", original)
    result = store.submit_candidate_draft(
        "review", token, draft["id"], 0, turn_id="commit", call_id="a", args=args, now=3
    )
    assert result["status"] == "promoted" and len(store.list(Case)) == 1


def test_existing_case_update_rolls_back_with_draft_close(board, monkeypatch):
    store, token, ref = board
    existing = store.add(
        Case(
            "case",
            store.run.id,
            scope_id="a",
            trigger="user action",
            mechanism="wrong result",
            claims=[{"id": "old", "statement": "wrong result", "impact": "other impact"}],
            evidence_refs=[ref.id],
        )
    )
    args = payload([ref.id])
    turn(store, token, "update", [call("submit_case", "a", args)], [ref])
    draft = store.stage_candidate_batch("review", token, "update", now=2)[0]
    original = store.update_unit_object

    def fail_after_update(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("interruption after existing case update")

    monkeypatch.setattr(store, "update_unit_object", fail_after_update)
    with pytest.raises(RuntimeError, match="interruption"):
        store.submit_candidate_draft("review", token, draft["id"], 0, turn_id="update", call_id="a", args=args, now=3)
    assert store.get(Case, existing.id) == existing
    assert store.get_candidate_draft(draft["id"])["status"] == "pending_validation"
    monkeypatch.setattr(store, "update_unit_object", original)
    result = store.submit_candidate_draft(
        "review", token, draft["id"], 0, turn_id="update", call_id="a", args=args, now=3
    )
    assert result["case_id"] == existing.id and result["case_revision"] == 1
    assert len(store.get(Case, existing.id).claims) == 2


def test_competing_repairs_cas_cannot_create_two_cases(board):
    store, token, ref = board
    original = payload(["bad"])
    turn(store, token, "origin", [call("submit_case", "origin", original)], [ref])
    draft = store.stage_candidate_batch("review", token, "origin", now=2)[0]
    store.submit_candidate_draft(
        "review", token, draft["id"], 0, turn_id="origin", call_id="origin", args=original, now=3
    )
    fixed = payload([ref.id])
    native = {**fixed, "draft_id": draft["id"], "expected_revision": 1}
    attempts = {}
    for name in ("one", "two"):
        turn(store, token, name, [call("submit_case", name, native)], [ref])
        attempts[name] = store.stage_candidate_batch("review", token, name, now=2)[0]

    def attempt(name):
        return store.submit_candidate_draft(
            "review", token, attempts[name]["id"], 0, turn_id=name, call_id=name, args=fixed, now=3
        )["status"]

    with ThreadPoolExecutor(max_workers=2) as workers:
        assert sorted(workers.map(attempt, ("one", "two"))) == ["promoted", "repair_needed"]
    assert len(store.pending_candidate_drafts("a")) == 1
    assert len(store.list(Case)) == 1


def test_invalid_review_state_cannot_bypass_native_candidate_validation(board):
    store, token, ref = board
    args = payload([ref.id])
    native = {**args, "review_state": {"question": "broken"}}
    turn(store, token, "invalid-state", [call("submit_case", "a", native)], [ref])
    draft = store.stage_candidate_batch("review", token, "invalid-state", now=2)[0]
    result = store.submit_candidate_draft(
        "review", token, draft["id"], 0, turn_id="invalid-state", call_id="a", args=args, now=3
    )
    assert result["status"] == "repair_needed"
    assert any(error["field"] == "review_state" for error in result["errors"])
    assert store.list(Case) == []


def test_old_lease_and_old_turn_cannot_authorize_repair(board):
    store, token, ref = board
    original = payload(["missing"])
    turn(store, token, "first", [call("submit_case", "a", original)], [ref])
    draft = store.stage_candidate_batch("review", token, "first", now=2)[0]
    store.submit_candidate_draft("review", token, draft["id"], 0, turn_id="first", call_id="a", args=original, now=3)
    new_token = store.acquire_task("review", "replacement", 100, now=101).lease_token
    with pytest.raises(LeaseError):
        store.submit_candidate_draft(
            "review", token, draft["id"], 0, turn_id="first", call_id="a", args=original, now=102
        )
    with pytest.raises(LeaseError):
        store.submit_candidate_draft(
            "review", new_token, draft["id"], 0, turn_id="first", call_id="a", args=original, now=102
        )
    assert store.get_candidate_draft(draft["id"])["status"] == "repair_needed"


def test_bad_audit_hash_fails_closed_without_draft(board):
    store, token, ref = board
    turn(store, token, "bad-hash", [call("submit_case", "a", payload([ref.id]))], [ref])
    with store._connect() as db:
        digest = db.execute(
            "SELECT content_hash FROM turn_audit WHERE task_id='review' AND turn_id='bad-hash' AND phase='response'"
        ).fetchone()[0]
    (store.audit_dir / digest).write_bytes(b"corrupt")
    with pytest.raises(BindingError, match="hash mismatch"):
        store.stage_candidate_batch("review", token, "bad-hash", now=2)
    assert store.list_candidate_drafts() == []


def test_open_draft_prevents_checked_unit_from_being_reviewed(board):
    store, token, ref = board
    args = payload(["missing"])
    origin = call("submit_case", "a", args)
    turn(store, token, "first", [origin], [ref])
    draft = store.stage_candidate_batch("review", token, "first", now=2)[0]
    error = store.submit_candidate_draft(
        "review", token, draft["id"], 0, turn_id="first", call_id="a", args=args, now=3
    )
    response = {"type": "ai", "data": {"content": "", "tool_calls": [origin]}}
    receipt = {"type": "tool", "data": {"tool_call_id": "a", "content": json.dumps({"status": error["status"]})}}
    store.append_turn_audit("review", token, "first", "tool:0:a", [receipt], now=3)
    store.append_turn_audit("review", token, "first", "exchange", [response, receipt], now=3)
    terminal = call("submit_unit_decision", "finish", {"finish": True})
    turn(store, token, "finish", [terminal], [ref])
    result = store.commit_unit_decision(
        "review",
        token,
        0,
        "finish-decision",
        checked_region_ids=["a"],
        observations=[{"statement": "read region", "region_ids": ["a"], "evidence_refs": [ref.id]}],
        questions=[],
        cases=[],
        unresolved=[],
        turn_id="finish",
        finish=True,
        now=3,
    )
    assert result["scope"].status == "partial"
    view = store.validate_units_completion()
    assert not view["valid"] and view["pending_candidate_drafts"][0]["id"] == draft["id"]


def test_unknown_target_keeps_attempt_and_requires_explicit_resolution(board):
    store, token, ref = board
    args = payload(["missing-a", "missing-b"])
    native = {**args, "draft_id": "made-up", "expected_revision": 9}
    turn(store, token, "unknown", [call("submit_case", "x", native)], [ref])
    attempt = store.stage_candidate_batch("review", token, "unknown", now=2)[0]
    failed = store.submit_candidate_draft(
        "review", token, attempt["id"], 0, turn_id="unknown", call_id="x", args=args, now=3
    )
    assert failed["requires_resolution"] and failed["original_args"] == native
    assert {e.get("ref") for e in failed["errors"] if e.get("ref")} == {"missing-a", "missing-b"}
    assert store.pending_candidate_drafts("a")[0]["target_resolution"] == "unknown"
    fixed = payload([ref.id])
    no_resolution = {**fixed, "draft_id": attempt["id"], "expected_revision": 1}
    turn(store, token, "missing-choice", [call("submit_case", "y", no_resolution)], [ref])
    new_attempt = store.stage_candidate_batch("review", token, "missing-choice", now=2)[0]
    rejected = store.submit_candidate_draft(
        "review", token, new_attempt["id"], 0, turn_id="missing-choice", call_id="y", args=fixed, now=3
    )
    assert rejected["status"] == "repair_needed"
    assert store.get_candidate_draft(attempt["id"]) == failed
    explicit = {**fixed, "draft_id": attempt["id"], "expected_revision": 1, "candidate_resolution": "new_case"}
    turn(store, token, "explicit", [call("submit_case", "z", explicit)], [ref])
    final_attempt = store.stage_candidate_batch("review", token, "explicit", now=2)[0]
    promoted = store.submit_candidate_draft(
        "review", token, final_attempt["id"], 0, turn_id="explicit", call_id="z", args=fixed, now=3
    )
    assert promoted["status"] == "promoted" and store.get_candidate_draft(final_attempt["id"])["status"] == "applied"
    assert store.get_candidate_draft(new_attempt["id"])["status"] == "repair_needed"
    assert len(store.list(Case)) == 1


def test_explicit_target_atomic_resolution_and_rollback(board, monkeypatch):
    store, token, ref = board
    initial = payload(["absent"])
    turn(store, token, "old", [call("submit_case", "old", initial)])
    target = store.stage_candidate_batch("review", token, "old", now=2)[0]
    store.submit_candidate_draft("review", token, target["id"], 0, turn_id="old", call_id="old", args=initial, now=3)
    invalid = {**initial, "draft_id": "no-such", "expected_revision": 4}
    turn(store, token, "bad-target", [call("submit_case", "bad", invalid)])
    broken = store.stage_candidate_batch("review", token, "bad-target", now=2)[0]
    store.submit_candidate_draft(
        "review", token, broken["id"], 0, turn_id="bad-target", call_id="bad", args=initial, now=3
    )
    fixed = payload([ref.id])
    native = {
        **fixed,
        "draft_id": broken["id"],
        "expected_revision": 1,
        "candidate_resolution": "apply_to_draft",
        "target_draft_id": target["id"],
        "target_expected_revision": 1,
    }
    turn(store, token, "apply", [call("submit_case", "apply", native)], [ref])
    attempt = store.stage_candidate_batch("review", token, "apply", now=2)[0]
    original = store.commit_unit_decision

    def interrupt(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("after case before links")

    monkeypatch.setattr(store, "commit_unit_decision", interrupt)
    with pytest.raises(RuntimeError):
        store.submit_candidate_draft(
            "review", token, attempt["id"], 0, turn_id="apply", call_id="apply", args=fixed, now=3
        )
    assert store.list(Case) == []
    assert store.get_candidate_draft(target["id"])["revision"] == 1
    assert store.get_candidate_draft(broken["id"])["status"] == "repair_needed"
    assert store.get_candidate_draft(attempt["id"])["status"] == "pending_validation"
    monkeypatch.setattr(store, "commit_unit_decision", original)
    result = store.submit_candidate_draft(
        "review", token, attempt["id"], 0, turn_id="apply", call_id="apply", args=fixed, now=3
    )
    assert result["id"] == target["id"] and result["status"] == "promoted"
    assert store.get_candidate_draft(broken["id"])["status"] == "applied"
    assert store.get_candidate_draft(attempt["id"])["status"] == "applied"
    assert not store.pending_candidate_drafts("a")


def test_closed_target_does_not_erase_attempt(board):
    store, token, _ = board
    args = payload(["missing"])
    turn(store, token, "original", [call("submit_case", "a", args)])
    target = store.stage_candidate_batch("review", token, "original", now=2)[0]
    withdraw = {"draft_id": target["id"], "expected_revision": 0, "reason": "withdrawn"}
    turn(store, token, "close", [call("retract_candidate_draft", "b", withdraw)])
    store.retract_candidate_draft(
        "review", token, target["id"], 0, turn_id="close", call_id="b", reason="withdrawn", now=3
    )
    native = {**args, "draft_id": target["id"], "expected_revision": 0}
    turn(store, token, "late", [call("submit_case", "c", native)])
    attempt = store.stage_candidate_batch("review", token, "late", now=2)[0]
    result = store.submit_candidate_draft(
        "review", token, attempt["id"], 0, turn_id="late", call_id="c", args=args, now=3
    )
    assert result["requires_resolution"] and result["original_args"] == native
    assert store.get_candidate_draft(target["id"])["status"] == "retracted"
    assert store.pending_candidate_drafts("a")[0]["target_resolution"] == "unknown"


def test_same_batch_conflicting_repairs_retains_second_payload(board):
    store, token, ref = board
    bad = payload(["missing"])
    turn(store, token, "origin-two", [call("submit_case", "origin", bad)])
    target = store.stage_candidate_batch("review", token, "origin-two", now=2)[0]
    store.submit_candidate_draft(
        "review", token, target["id"], 0, turn_id="origin-two", call_id="origin", args=bad, now=3
    )
    first = payload([ref.id])
    second = {**first, "impact": "different consequences"}
    calls = [
        call("submit_case", "first", {**first, "draft_id": target["id"], "expected_revision": 1}),
        call("submit_case", "second", {**second, "draft_id": target["id"], "expected_revision": 1}),
    ]
    turn(store, token, "same-batch", calls, [ref])
    staged = store.stage_candidate_batch("review", token, "same-batch", now=2)
    assert len(staged) == 2 and len({item["id"] for item in staged}) == 2
    assert all(
        item["target_resolution"] == "known"
        for item in store.pending_candidate_drafts("a")
        if item["id"] in {x["id"] for x in staged}
    )
    assert (
        store.submit_candidate_draft(
            "review", token, staged[0]["id"], 0, turn_id="same-batch", call_id="first", args=first, now=3
        )["status"]
        == "promoted"
    )
    result = store.submit_candidate_draft(
        "review", token, staged[1]["id"], 0, turn_id="same-batch", call_id="second", args=second, now=3
    )
    assert result["requires_resolution"] and result["current_args"] == second
    assert store.pending_candidate_drafts("a")[0]["target_resolution"] == "unknown"
    assert len(store.list(Case)) == 1


def test_other_scope_target_is_never_modified(board):
    store, token, _ = board
    store.add(Task("integration-review", "r", "review", "i", 0))
    other_token = store.acquire_task("integration-review", "worker-i", 100, now=0).lease_token
    args = payload(["missing"])
    store.append_turn_audit("integration-review", other_token, "other", "request", [], now=2)
    store.append_turn_audit(
        "integration-review",
        other_token,
        "other",
        "response",
        [{"type": "ai", "data": {"content": "", "tool_calls": [call("submit_case", "other", args)]}}],
        now=2,
    )
    target = store.stage_candidate_batch("integration-review", other_token, "other", now=2)[0]
    native = {**args, "draft_id": target["id"], "expected_revision": 0}
    turn(store, token, "wrong-scope", [call("submit_case", "wrong", native)])
    attempt = store.stage_candidate_batch("review", token, "wrong-scope", now=2)[0]
    result = store.submit_candidate_draft(
        "review", token, attempt["id"], 0, turn_id="wrong-scope", call_id="wrong", args=args, now=3
    )
    assert result["requires_resolution"] and result["original_args"] == native
    assert store.get_candidate_draft(target["id"]) == target
    assert store.pending_candidate_drafts("a")[0]["target_resolution"] == "unknown"


def test_replay_after_repair_commit_returns_original_result(board):
    store, token, ref = board
    bad = payload(["missing"])
    turn(store, token, "replay-original", [call("submit_case", "a", bad)])
    original = store.stage_candidate_batch("review", token, "replay-original", now=2)[0]
    store.submit_candidate_draft(
        "review", token, original["id"], 0, turn_id="replay-original", call_id="a", args=bad, now=3
    )
    fixed = payload([ref.id])
    native = {**fixed, "draft_id": original["id"], "expected_revision": 1}
    turn(store, token, "replay-repair", [call("submit_case", "b", native)], [ref])
    attempt = store.stage_candidate_batch("review", token, "replay-repair", now=2)[0]
    result = store.submit_candidate_draft(
        "review", token, attempt["id"], 0, turn_id="replay-repair", call_id="b", args=fixed, now=3
    )
    assert store.stage_candidate_batch("review", token, "replay-repair", now=2)[0]["status"] == "applied"
    replay = store.submit_candidate_draft(
        "review", token, attempt["id"], 0, turn_id="replay-repair", call_id="b", args=fixed, now=3
    )
    assert replay == result and len(store.list(Case)) == 1


def test_explicit_new_never_merges_same_text_into_existing_case(board):
    store, token, ref = board
    existing = store.add(
        Case(
            "existing",
            store.run.id,
            scope_id="a",
            trigger="user action",
            mechanism="wrong result",
            claims=[{"id": "old", "statement": "wrong result", "impact": "old"}],
            evidence_refs=[ref.id],
        )
    )
    fixed = payload([ref.id])
    native = {**fixed, "draft_id": "unavailable", "expected_revision": 0}
    turn(store, token, "lost-target", [call("submit_case", "a", native)], [ref])
    broken = store.stage_candidate_batch("review", token, "lost-target", now=2)[0]
    store.submit_candidate_draft(
        "review", token, broken["id"], 0, turn_id="lost-target", call_id="a", args=fixed, now=3
    )
    chosen = {**fixed, "draft_id": broken["id"], "expected_revision": 1, "candidate_resolution": "new_case"}
    turn(store, token, "explicit-new", [call("submit_case", "b", chosen)], [ref])
    attempt = store.stage_candidate_batch("review", token, "explicit-new", now=2)[0]
    result = store.submit_candidate_draft(
        "review", token, attempt["id"], 0, turn_id="explicit-new", call_id="b", args=fixed, now=3
    )
    assert result["status"] == "promoted" and result["case_id"] != existing.id
    assert store.get(Case, existing.id) == existing
    assert len(store.list(Case)) == 2
