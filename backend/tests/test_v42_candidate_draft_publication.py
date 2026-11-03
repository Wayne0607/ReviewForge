"""A pending draft cannot masquerade as a case or authorize stale publication."""

import json
from dataclasses import replace

import pytest
import test_v42_case_counterevidence as case_fixtures
from langchain_core.messages import AIMessage, ToolMessage, messages_to_dict
from test_v42_shared_publication_recovery import interrupted_turn
from test_v42_stateful_reviewer import call

from reviewforge.review_v42.publication import PublicationOutbox

board = case_fixtures.board


@pytest.fixture
def draft_board(tmp_path, monkeypatch):
    original = case_fixtures.ReviewStore

    def opt_in(path, run):
        return original(path, replace(run, metadata={**run.metadata, "candidate_draft_policy": "candidate-draft-1"}))

    monkeypatch.setattr(case_fixtures, "ReviewStore", opt_in)
    return case_fixtures.board.__wrapped__(tmp_path)


def record_repair_needed(store, task, args, turn="draft-turn"):
    native = AIMessage(content="", tool_calls=[call("submit_case", args, "candidate-call")])
    store.append_turn_audit(task.id, task.lease_token, turn, "request", [], now=1)
    store.append_turn_audit(task.id, task.lease_token, turn, "response", messages_to_dict([native]), now=1)
    draft = store.stage_candidate_batch(task.id, task.lease_token, turn, now=1)[0]
    business = {
        key: value
        for key, value in args.items()
        if key
        not in {
            "draft_id",
            "expected_revision",
            "review_state",
            "candidate_resolution",
            "target_draft_id",
            "target_expected_revision",
        }
    }
    result = store.submit_candidate_draft(
        task.id,
        task.lease_token,
        draft["id"],
        draft["revision"],
        turn_id=turn,
        call_id="candidate-call",
        args=business,
        now=1,
    )
    receipt = ToolMessage(content=json.dumps(result), tool_call_id="candidate-call")
    store.append_turn_audit(
        task.id, task.lease_token, turn, "tool:0:candidate-call", messages_to_dict([receipt]), now=1
    )
    store.append_turn_audit(task.id, task.lease_token, turn, "exchange", messages_to_dict([native, receipt]), now=1)
    return result


@pytest.mark.parametrize(
    "mode,blocked",
    [
        ("unrelated", False),
        ("explicit", True),
        ("implicit", True),
        ("unknown_id", True),
        ("missing_input", True),
        ("unknown_attempt", True),
        ("missing_resolution", True),
    ],
)
def test_pending_draft_impact_is_rechecked_during_freeze_and_prepare(board, mode, blocked):
    store, _, _, case, _, _ = board
    store.run.metadata["candidate_draft_policy"] = "candidate-draft-1"
    pending = []
    transactions = []

    def pending_drafts(scope_id=None, *, db=None):
        transactions.append(db is not None and db.in_transaction)
        return pending

    store.pending_candidate_drafts = pending_drafts
    outbox = PublicationOutbox(store)
    comment = next(item for item in outbox.freeze() if item["case_id"] == case.id)
    args = {"trigger": "unrelated trigger", "behavior": "unrelated mechanism"}
    if mode == "explicit":
        args["case_id"] = case.id
    elif mode == "implicit":
        args = {"trigger": "  " + case.trigger + "  ", "behavior": "  " + case.mechanism + "  "}
    elif mode == "unknown_id":
        args["case_id"] = "missing-case"
    elif mode == "missing_input":
        args = {"trigger": ""}
    entry = {
        "id": "draft",
        "scope_id": case.scope_id,
        "run_id": store.run.id,
        "status": "repair_needed",
        "revision": 0,
        "current_args": args,
        "original_args": dict(args),
    }
    if mode in {"unknown_attempt", "missing_resolution"}:
        entry["original_args"] = {"draft_id": "not-trustworthy"}
        if mode == "unknown_attempt":
            entry["target_resolution"] = "unknown"
    pending.append(entry)
    report = outbox.freeze_batch()["report"]
    assert report["status"] == "partial"
    assert report["open_candidate_drafts"][0]["id"] == "draft"
    assert (case.id not in [item["case_id"] for item in report["comments"]]) is blocked
    if blocked:
        with pytest.raises(ValueError, match="permission"):
            outbox.prepare(comment["id"])
    else:
        assert outbox.prepare(comment["id"])["case_id"] == case.id
    assert all(transactions)


def test_missing_or_corrupt_draft_view_fails_closed(board):
    store, _, _, case, _, _ = board
    store.run.metadata["candidate_draft_policy"] = "candidate-draft-1"
    store.pending_candidate_drafts = lambda **kwargs: []
    outbox = PublicationOutbox(store)
    comment = next(item for item in outbox.freeze() if item["case_id"] == case.id)
    store.pending_candidate_drafts = lambda **kwargs: [{"id": "damaged"}]
    report = outbox.freeze_batch()["report"]
    assert report["status"] == "partial" and report["comments"] == []
    assert report["open_candidate_drafts"] is None
    with pytest.raises(ValueError, match="permission"):
        outbox.prepare(comment["id"])


@pytest.mark.parametrize(
    "change", ["missing", "closed", "other_scope", "new_revision", "requires_resolution", "bad_args"]
)
def test_known_repair_target_is_rechecked_in_prepare_transaction(board, change):
    store, _, _, case, _, _ = board
    store.run.metadata["candidate_draft_policy"] = "candidate-draft-1"
    store.pending_candidate_drafts = lambda **kwargs: []
    outbox = PublicationOutbox(store)
    comment = next(item for item in outbox.freeze() if item["case_id"] == case.id)
    args = {"trigger": "unrelated trigger", "behavior": "unrelated mechanism"}
    target = {
        "id": "target",
        "run_id": store.run.id,
        "scope_id": case.scope_id,
        "revision": 1,
        "status": "repair_needed",
        "requires_resolution": False,
        "current_args": args,
        "original_args": args,
    }
    attempt = {
        "id": "attempt",
        "run_id": store.run.id,
        "scope_id": case.scope_id,
        "revision": 0,
        "status": "repair_needed",
        "current_args": args,
        "original_args": {"draft_id": "target", "expected_revision": 1},
        "target_resolution": "known",
    }
    store.pending_candidate_drafts = lambda **kwargs: [attempt]
    missing = {"active": False}

    def get_target(ident, *, db=None):
        assert db is not None and db.in_transaction
        if missing["active"]:
            raise KeyError(ident)
        return target

    store.get_candidate_draft = get_target
    assert any(item["case_id"] == case.id for item in outbox.freeze())
    if change == "closed":
        target["status"] = "promoted"
    elif change == "other_scope":
        target["scope_id"] = "a"
    elif change == "new_revision":
        target["revision"] += 1
    elif change == "requires_resolution":
        target["requires_resolution"] = True
    elif change == "bad_args":
        target["current_args"] = None
    if change == "missing":
        missing["active"] = True
        with pytest.raises(ValueError, match="permission"):
            outbox.prepare(comment["id"])
    else:
        assert outbox.freeze_batch()["report"]["comments"] == []
        with pytest.raises(ValueError, match="permission"):
            outbox.prepare(comment["id"])


def test_known_attempt_cannot_hide_its_target_case(board):
    store, _, _, case, _, _ = board
    store.run.metadata["candidate_draft_policy"] = "candidate-draft-1"
    store.pending_candidate_drafts = lambda **kwargs: []
    outbox = PublicationOutbox(store)
    comment = next(item for item in outbox.freeze() if item["case_id"] == case.id)
    target = {
        "id": "target",
        "run_id": store.run.id,
        "scope_id": case.scope_id,
        "revision": 1,
        "status": "repair_needed",
        "requires_resolution": False,
        "current_args": {"case_id": case.id},
    }
    attempt = {
        "id": "attempt",
        "run_id": store.run.id,
        "scope_id": case.scope_id,
        "revision": 0,
        "status": "repair_needed",
        "current_args": {"trigger": "different", "behavior": "different"},
        "original_args": {"draft_id": "target", "expected_revision": 1},
        "target_resolution": "known",
    }
    # Only the attempt appears in this projection; the target is independently
    # retrieved in the same transaction, not assumed to be another list item.
    store.pending_candidate_drafts = lambda **kwargs: [attempt]
    store.get_candidate_draft = lambda ident, *, db=None: target
    assert outbox.freeze_batch()["report"]["comments"] == []
    with pytest.raises(ValueError, match="permission"):
        outbox.prepare(comment["id"])


def test_unfinished_new_submission_still_blocks_scope_without_replay(board):
    store, _, task, case, _, _ = board
    store.run.metadata["candidate_draft_policy"] = "candidate-draft-1"
    store.pending_candidate_drafts = lambda **kwargs: []
    outbox = PublicationOutbox(store)
    comment = next(item for item in outbox.freeze() if item["case_id"] == case.id)
    interrupted_turn(
        store,
        task,
        "interrupted-draft",
        name="submit_case",
        args={"trigger": case.trigger, "behavior": case.mechanism, "impact": "changed", "evidence_refs": ["missing"]},
    )
    report = outbox.freeze_batch()["report"]
    assert report["status"] == "partial" and report["comments"] == []
    assert report["incomplete_tool_batches"][0]["calls"][0]["name"] == "submit_case"
    with pytest.raises(ValueError, match="permission"):
        outbox.prepare(comment["id"])


@pytest.mark.parametrize("kind,blocked", [("unrelated", False), ("same_case", True)])
def test_real_staged_draft_is_seen_in_freeze_and_prepare(draft_board, kind, blocked):
    store, _, task, case, _, _ = draft_board
    outbox = PublicationOutbox(store)
    frozen = next(item for item in outbox.freeze() if item["case_id"] == case.id)
    args = {
        "trigger": case.trigger if blocked else "a distinct trigger",
        "behavior": case.mechanism if blocked else "a distinct mechanism",
        "impact": "a proposed impact",
        "evidence_refs": ["not-a-real-reference", "another-missing-reference"],
    }
    result = record_repair_needed(store, task, args)
    assert result["status"] == "repair_needed"
    assert len(result["errors"]) >= 2
    report = outbox.freeze_batch()["report"]
    assert report["status"] == "partial"
    assert report["open_candidate_drafts"][0]["id"] == result["id"]
    assert (case.id not in [item["case_id"] for item in report["comments"]]) is blocked
    if blocked:
        with pytest.raises(ValueError, match="permission"):
            outbox.prepare(frozen["id"])
    else:
        assert outbox.prepare(frozen["id"])["case_id"] == case.id


def test_real_unknown_target_attempt_blocks_unrelated_verified_case(draft_board):
    store, _, task, case, _, _ = draft_board
    outbox = PublicationOutbox(store)
    frozen = next(item for item in outbox.freeze() if item["case_id"] == case.id)
    result = record_repair_needed(
        store,
        task,
        {
            "trigger": "unrelated trigger",
            "behavior": "unrelated mechanism",
            "impact": "impact",
            "evidence_refs": ["missing-source"],
            "draft_id": "missing-draft",
            "expected_revision": 0,
        },
    )
    assert result["status"] == "repair_needed" and result["requires_resolution"] is True
    assert store.pending_candidate_drafts()[0]["target_resolution"] == "unknown"
    report = outbox.freeze_batch()["report"]
    assert report["status"] == "partial" and report["comments"] == []
    with pytest.raises(ValueError, match="permission"):
        outbox.prepare(frozen["id"])
