import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from reviewforge.review_v42.contracts import (
    BindingError,
    Case,
    LeaseError,
    Question,
    ReviewScope,
    RevisionConflict,
    Run,
    Task,
)
from reviewforge.review_v42.shared_investigation import SharedInvestigations
from reviewforge.review_v42.store import ReviewStore


def make_board(tmp_path, *, fine=False):
    region = {
        "id": "a",
        "path": "a.py",
        "side": "head",
        "start_line": 1,
        "end_line": 2,
        "base_start_line": 1,
        "base_end_line": 2,
        "changed_lines": [1],
        "patch": "",
    }
    metadata = {
        "workspace_digest": "snapshot",
        "base_workspace_digest": "base-snapshot",
        "changed_regions": [region],
        "shared_investigation": "shared-1",
        "question_assistant_policy": "question-delivery-1",
    }
    if fine:
        metadata["question_fencing_policy"] = "question-input-1"
    run = Run(
        "r",
        "org/repo",
        "base",
        "head",
        policy="v42-units-6",
        metadata=metadata,
    )
    store = ReviewStore(tmp_path / "state.db", run)
    store.install_unit_plan(
        [ReviewScope("a", "r", "a", regions=[region])], ReviewScope("i", "r", "i", dependencies=["a"])
    )
    if fine:
        SharedInvestigations(store)
    store.add(Question("q", "r", "why?", scope_id="a"))
    store.add(Task("review", "r", "review", "a", 0))
    token = store.acquire_task("review", "owner", 100, now=0).lease_token
    return store, token


@pytest.fixture
def board(tmp_path):
    return make_board(tmp_path)


@pytest.fixture
def fine_board(tmp_path):
    return make_board(tmp_path, fine=True)


def delegate(store, token):
    call = {
        "name": "delegate_question",
        "args": {"question_id": "q", "reason": "investigate"},
        "id": "d",
        "type": "tool_call",
    }
    store.append_turn_audit("review", token, "delegate", "request", [], now=1)
    store.append_turn_audit(
        "review", token, "delegate", "response", [{"type": "ai", "data": {"content": "", "tool_calls": [call]}}], now=1
    )
    task = store.create_question_task(
        "review", token, "q", 0, turn_id="delegate", tool_call_id="d", instruction="investigate", now=1
    )
    store.append_turn_audit("review", token, "delegate", "exchange", [{"type": "ai", "data": {"content": ""}}], now=1)
    return task


def source(store):
    return store.put_evidence(
        b"one\ntwo\n",
        revision="head",
        side="head",
        kind="source",
        path="a.py",
        start_line=1,
        end_line=2,
        metadata={"workspace_digest": "snapshot", "tool_name": "read_source", "tool_args": {"path": "a.py"}},
    )


def turn(store, task, token, ref, *, turn_id="answer", page=False, include=True):
    messages = []
    if include:
        receipt = {
            "evidence_ref": ref.id,
            "content": "one\n" if page else "one\ntwo\n",
            "start_line": 1,
            "end_line": 1 if page else 2,
        }
        messages.append({"type": "tool", "data": {"content": json.dumps(receipt)}})
    store.append_turn_audit(task.id, token, turn_id, "request", messages, now=2)
    call = {
        "id": "answer",
        "name": "answer_question",
        "args": {"status": "answered", "answer": "yes", "evidence_refs": [ref.id]},
        "type": "tool_call",
    }
    store.append_turn_audit(
        task.id, token, turn_id, "response", [{"type": "ai", "data": {"content": "", "tool_calls": [call]}}], now=2
    )


def answer(store, task, token, ref, turn_id="answer"):
    return store.commit_question_answer(
        task.id, token, 0, turn_id=turn_id, status="answered", answer="yes", evidence_refs=[ref.id], now=3
    )


def test_delegation_delivery_and_unclosed_batch(board):
    store, owner = board
    task = delegate(store, owner)
    assert delegate is not None
    lease = store.acquire_task(task.id, "helper", 100, now=1).lease_token
    ref = source(store)
    store.pin_frontier_for_task(task.id, lease, ref.id, now=2)
    turn(store, task, lease, ref)
    updated = answer(store, task, lease, ref)
    assert updated.status == "answered"
    assert store.get(ReviewScope, "a").status == "pending"
    assert store.pending_unit_sources()[0]["ref_id"] == ref.id
    assert any(batch["task_id"] == task.id for batch in store.pending_shared_batches("a"))
    with pytest.raises(BindingError):
        store.complete_task(task.id, lease, {}, now=4)
    receipt = {"type": "tool", "data": {"tool_call_id": "answer", "content": json.dumps({"status": "answered"})}}
    store.append_turn_audit(task.id, lease, "answer", "tool:0:answer", [receipt], now=4)
    response = {
        "type": "ai",
        "data": {
            "content": "",
            "tool_calls": [
                {
                    "id": "answer",
                    "name": "answer_question",
                    "args": {"status": "answered", "answer": "yes", "evidence_refs": [ref.id]},
                    "type": "tool_call",
                }
            ],
        },
    }
    store.append_turn_audit(task.id, lease, "answer", "exchange", [response, receipt], now=4)
    store.complete_task(task.id, lease, {}, now=4)


@pytest.mark.parametrize("mode", ["not_sent", "page"])
def test_only_current_complete_delivery_is_accepted(board, mode):
    store, owner = board
    task = delegate(store, owner)
    lease = store.acquire_task(task.id, "helper", 100, now=1).lease_token
    ref = source(store)
    store.pin_frontier_for_task(task.id, lease, ref.id, now=2)
    turn(store, task, lease, ref, page=mode == "page", include=mode != "not_sent")
    with pytest.raises(BindingError):
        answer(store, task, lease, ref)
    assert store.get(Question, "q").revision == 0


def test_old_lease_cannot_use_session_or_pins(board):
    store, owner = board
    task = delegate(store, owner)
    old = store.acquire_task(task.id, "one", 2, now=1).lease_token
    ref = source(store)
    store.pin_frontier_for_task(task.id, old, ref.id, now=2)
    store.save_task_session(task.id, old, [{"type": "tool", "data": {"content": "secret"}}], now=2)
    new = store.acquire_task(task.id, "two", 100, now=4).lease_token
    assert store.load_task_session(task.id) == []
    turn(store, task, new, ref)
    with pytest.raises(BindingError):
        answer(store, task, new, ref)
    with pytest.raises(LeaseError):
        store.pin_frontier_for_task(task.id, old, ref.id, now=4)


def test_old_run_cannot_enable_delegate_in_place(board):
    store, _ = board
    with pytest.raises(BindingError):
        ReviewStore(store.path, replace(store.run, metadata={**store.run.metadata, "question_assistant_policy": ""}))


@pytest.mark.parametrize("invalid", [None, "", False, 0, [], {}, "unknown"])
def test_invalid_question_fencing_policy_does_not_create_or_bind_a_store(tmp_path, invalid):
    metadata = {
        "shared_investigation": "shared-1",
        "question_assistant_policy": "question-delivery-1",
        "question_fencing_policy": invalid,
    }
    run = Run("r", "org/repo", "base", "head", policy="v42-units-6", metadata=metadata)
    path = tmp_path / "new-store" / "board.sqlite"
    with pytest.raises(BindingError, match="unsupported question fencing policy"):
        ReviewStore(path, run)
    assert not path.parent.exists()

    # An invalid attempt cannot reserve the location for a later valid run.
    valid = replace(run, metadata={**metadata, "question_fencing_policy": "question-input-1"})
    store = ReviewStore(path, valid)
    assert store.run == valid
    assert ReviewStore(path, valid).run == valid


@pytest.mark.parametrize("missing", ["shared_investigation", "question_assistant_policy", "unit_policy"])
def test_question_input_policy_requires_all_preconditions(tmp_path, missing):
    metadata = {
        "shared_investigation": "shared-1",
        "question_assistant_policy": "question-delivery-1",
        "question_fencing_policy": "question-input-1",
    }
    if missing != "unit_policy":
        metadata.pop(missing)
    run = Run(
        "r", "org/repo", "base", "head", policy="v42" if missing == "unit_policy" else "v42-units-6", metadata=metadata
    )
    path = tmp_path / "unbound" / "board.sqlite"
    with pytest.raises(BindingError, match="unsupported question fencing policy"):
        ReviewStore(path, run)
    assert not path.parent.exists()


def test_missing_question_fencing_key_preserves_existing_scope_fence(board):
    original, owner = board
    assert "question_fencing_policy" not in original.run.metadata
    store = ReviewStore(original.path, original.run)
    task = delegate(store, owner)
    lease = store.acquire_task(task.id, "helper", 100, now=1).lease_token
    store.update_scope_for_task("review", owner, 0, now=2, status="running")
    with pytest.raises(RevisionConflict):
        store.assert_question_task_current(task.id, lease, now=2)

    # Explicit null must not be interpreted as the old checkpoint's missing key.
    changed = replace(store.run, metadata={**store.run.metadata, "question_fencing_policy": None})
    with pytest.raises(BindingError, match="unsupported question fencing policy"):
        ReviewStore(store.path, changed)
    assert ReviewStore(store.path, original.run).get(Task, task.id).input_revision == 0


def test_answer_adds_case_source_and_fences_scope_in_one_transaction(board):
    store, owner = board
    store.add(Case("case", "r", scope_id="a", question_ids=["q"]))
    task = delegate(store, owner)
    lease = store.acquire_task(task.id, "helper", 100, now=1).lease_token
    ref = source(store)
    store.pin_frontier_for_task(task.id, lease, ref.id, now=2)
    turn(store, task, lease, ref)
    answer(store, task, lease, ref)
    case = store.get(Case, "case")
    assert case.evidence_refs == [ref.id] and case.revision == 1
    assert store.pending_unit_cases()[0]["case_id"] == case.id
    with pytest.raises(RevisionConflict):
        store.update_scope_for_task("review", owner, 0, now=4, status="pending")


def test_scope_change_fences_question_and_retains_pending_batch(board):
    store, owner = board
    task = delegate(store, owner)
    lease = store.acquire_task(task.id, "helper", 100, now=1).lease_token
    ref = source(store)
    store.pin_frontier_for_task(task.id, lease, ref.id, now=2)
    turn(store, task, lease, ref)
    store.update_scope_for_task("review", owner, 0, now=3, status="running")
    with pytest.raises(RevisionConflict):
        answer(store, task, lease, ref)
    store.settle_question_task_stale(task.id, lease, {"reason": "scope changed"}, now=3)
    assert any(item["task_id"] == task.id for item in store.pending_shared_batches("a"))


def test_empty_exchange_cannot_hide_unknown_answer_call(board):
    store, owner = board
    task = delegate(store, owner)
    lease = store.acquire_task(task.id, "helper", 100, now=1).lease_token
    ref = source(store)
    turn(store, task, lease, ref)
    store.append_turn_audit(task.id, lease, "answer", "exchange", [{"type": "ai", "data": {"content": ""}}], now=3)
    assert any(item["task_id"] == task.id for item in store.pending_shared_batches("a"))
    with pytest.raises(BindingError):
        store.complete_task(task.id, lease, {}, now=4)


def test_repeat_delegation_is_concurrent_and_idempotent(board):
    store, owner = board
    task = delegate(store, owner)

    def repeat():
        return store.create_question_task(
            "review", owner, "q", 0, turn_id="delegate", tool_call_id="d", instruction="investigate", now=3
        )

    with ThreadPoolExecutor(max_workers=2) as workers:
        assert [item.id for item in workers.map(lambda _: repeat(), range(2))] == [task.id, task.id]
    with pytest.raises(BindingError):
        store.create_question_task(
            "review", owner, "q", 0, turn_id="delegate", tool_call_id="d", instruction="different", now=3
        )


def test_question_protocol_error_feedback_closes_only_rejected_batch(board):
    store, owner = board
    task = delegate(store, owner)
    lease = store.acquire_task(task.id, "helper", 100, now=1).lease_token
    store.append_turn_audit(task.id, lease, "bad", "request", [], now=2)
    store.append_turn_audit(
        task.id, lease, "bad", "native", [{"invalid_tool_calls": [], "protocol_errors": ["bad"]}], now=2
    )
    assert any(item["turn_id"] == "bad" for item in store.pending_shared_batches("a"))
    store.append_turn_audit(task.id, lease, "bad", "response", [{"type": "ai", "data": {"content": ""}}], now=2)
    feedback = {"type": "human", "data": {"content": "Rejected native tool batch bad: No tool was executed; retry."}}
    store.append_turn_audit(task.id, lease, "bad", "exchange", [feedback], now=2)
    assert not any(item["turn_id"] == "bad" for item in store.pending_shared_batches("a"))


def test_unresolved_text_alone_does_not_invalidate_case(board):
    store, owner = board
    store.add(Case("case", "r", scope_id="a", question_ids=["q"]))
    task = delegate(store, owner)
    lease = store.acquire_task(task.id, "helper", 100, now=1).lease_token
    call = {
        "id": "answer",
        "name": "answer_question",
        "type": "tool_call",
        "args": {"status": "unresolved", "answer": "need the contract", "evidence_refs": []},
    }
    store.append_turn_audit(task.id, lease, "unresolved", "request", [], now=2)
    store.append_turn_audit(
        task.id, lease, "unresolved", "response", [{"type": "ai", "data": {"tool_calls": [call]}}], now=2
    )
    store.commit_question_answer(
        task.id,
        lease,
        0,
        turn_id="unresolved",
        status="unresolved",
        answer="need the contract",
        evidence_refs=[],
        now=2,
    )
    assert store.get(Case, "case").revision == 0
    assert store.pending_unit_cases() == []
    assert store.pending_unit_sources() == []


def test_fenced_provider_response_is_preserved_without_executing_calls(board):
    store, owner = board
    task = delegate(store, owner)
    lease = store.acquire_task(task.id, "helper", 100, now=1).lease_token
    store.append_turn_audit(task.id, lease, "in-flight", "request", [], now=2)
    response = [
        {
            "type": "ai",
            "data": {
                "content": "",
                "tool_calls": [
                    {
                        "id": "call",
                        "name": "answer_question",
                        "args": {"status": "unresolved", "answer": "need facts", "evidence_refs": []},
                        "type": "tool_call",
                    }
                ],
            },
        }
    ]
    with pytest.raises(RevisionConflict):
        store.record_fenced_question_response(task.id, lease, "in-flight", response, now=2)
    store.update_scope_for_task("review", owner, 0, now=3, status="running")
    with pytest.raises(RevisionConflict):
        store.append_turn_audit(task.id, lease, "in-flight", "response", response, now=3)
    store.record_fenced_question_response(task.id, lease, "in-flight", response, now=3)
    pending = [item for item in store.pending_shared_batches("a") if item["task_id"] == task.id]
    assert pending[0]["calls"][0]["receipt"] == "outcome_unknown"
    assert store.get(Question, "q").status == "open"
    with pytest.raises(BindingError):
        store.record_fenced_question_response(task.id, "wrong", "in-flight", response, now=3)


def test_delegate_requires_attached_navigation_to_be_merged(board):
    store, owner = board
    state = {"question": "why?", "observations": [], "open_questions": ["what changed?"], "next_step": "read"}
    call = {
        "name": "delegate_question",
        "args": {"question_id": "q", "reason": "investigate", "review_state": state},
        "id": "d",
        "type": "tool_call",
    }
    store.append_turn_audit("review", owner, "with-state", "request", [], now=1)
    store.append_turn_audit(
        "review", owner, "with-state", "response", [{"type": "ai", "data": {"tool_calls": [call]}}], now=1
    )
    with pytest.raises(BindingError):
        store.create_question_task(
            "review", owner, "q", 0, turn_id="with-state", tool_call_id="d", instruction="investigate", now=1
        )
    merged = store.merge_working_states("review", owner, 0, [state], now=1)
    assert not merged["errors"]
    result = store.create_question_task(
        "review", owner, "q", 0, turn_id="with-state", tool_call_id="d", instruction="investigate", now=1
    )
    assert result.object_id == "q"


@pytest.mark.parametrize(
    "kind", ["working_state", "unit_note", "navigation_observation", "turn_audit", "frontier_entry", "mystery"]
)
def test_question_rejects_non_original_kind_even_when_delivered(board, kind):
    store, owner = board
    task = delegate(store, owner)
    lease = store.acquire_task(task.id, "helper", 100, now=1).lease_token
    ref = store.put_evidence(
        b"one\ntwo\n",
        revision="head",
        side="head",
        kind=kind,
        path="a.py",
        start_line=1,
        end_line=2,
        metadata={"workspace_digest": "snapshot", "tool_name": "read_source", "tool_args": {"path": "a.py"}},
    )
    store.pin_frontier_for_task(task.id, lease, ref.id, now=2)
    turn(store, task, lease, ref)
    with pytest.raises(BindingError):
        answer(store, task, lease, ref)


@pytest.mark.parametrize("kind", ["documentation", "issue", "test"])
def test_question_accepts_original_knowledge_and_executed_test(board, kind):
    import hashlib

    store, owner = board
    task = delegate(store, owner)
    lease = store.acquire_task(task.id, "helper", 100, now=1).lease_token
    path = (
        "docs/design.md"
        if kind == "documentation"
        else "issue:https://github.com/org/repo/issues/1"
        if kind == "issue"
        else "tests/test_a.py"
    )
    text = "one\ntwo\n"
    receipt = {"path": path, "status": "success", "head_sha": "head"}
    metadata = {"workspace_digest": "snapshot", "tool_name": "read_knowledge", "tool_args": {"path": path}}
    side, revision = "head", "head"
    if kind == "test":
        receipt.update(
            runtime="python-unittest",
            exit_code=1,
            source_sha256="a" * 64,
            image_id="sha256:" + "b" * 64,
            started_at="2026-01-01T00:00:00Z",
            finished_at="2026-01-01T00:00:01Z",
        )
        metadata.update(tool_name="run_check", tool_args={"path": path, "runtime": "python-unittest"})
        text = json.dumps(receipt)
    else:
        revision = "issue-rev" if kind == "issue" else "head"
        receipt.update(revision=revision, content_sha256=hashlib.sha256(text.encode()).hexdigest())
        if kind == "issue":
            side = "external"
            metadata.update(
                source_url="https://github.com/org/repo/issues/1",
                captured_at="2026-01-01T00:00:00Z",
                historical_head_time=False,
                external_snapshot=True,
            )
    metadata["original_receipt"] = receipt
    ref = store.put_evidence(text.encode(), revision=revision, side=side, kind=kind, path=path, metadata=metadata)
    store.pin_frontier_for_task(task.id, lease, ref.id, now=2)
    request = [{"type": "tool", "data": {"content": json.dumps({"evidence_ref": ref.id, "content": text})}}]
    store.append_turn_audit(task.id, lease, "answer", "request", request, now=2)
    call = {
        "id": "answer",
        "name": "answer_question",
        "args": {"status": "answered", "answer": "yes", "evidence_refs": [ref.id]},
        "type": "tool_call",
    }
    store.append_turn_audit(
        task.id, lease, "answer", "response", [{"type": "ai", "data": {"content": "", "tool_calls": [call]}}], now=2
    )
    assert answer(store, task, lease, ref).status == "answered"


def test_fine_fence_ignores_navigation_but_tracks_related_case(fine_board):
    store, owner = fine_board
    store.add(Case("case", "r", scope_id="a", question_ids=["q"], trigger="trigger"))
    task = delegate(store, owner)
    lease = store.acquire_task(task.id, "helper", 100, now=1).lease_token
    state = {"question": "other topic", "observations": [], "open_questions": [], "next_step": "continue"}
    store.merge_working_states("review", owner, 0, [state], now=2)
    assert store.assert_question_task_current(task.id, lease, now=2).id == "q"
    case = store.get(Case, "case")
    store.update(Case, case.id, case.revision, mechanism="new mechanism")
    with pytest.raises(RevisionConflict):
        store.assert_question_task_current(task.id, lease, now=2)


def test_fine_fence_tracks_shared_investigation_and_unknown_policy(fine_board, tmp_path):
    store, owner = fine_board
    task = delegate(store, owner)
    lease = store.acquire_task(task.id, "helper", 100, now=1).lease_token
    api = SharedInvestigations(store)
    api.create("review", owner, "related?", ["a"], ["a"], turn_id="delegate", tool_call_id="investigate", now=2)
    with pytest.raises(RevisionConflict):
        store.assert_question_task_current(task.id, lease, now=2)
    with pytest.raises(BindingError):
        ReviewStore(
            tmp_path / "invalid.db",
            replace(store.run, id="new", metadata={**store.run.metadata, "question_fencing_policy": "unknown"}),
        )


def test_fine_fence_ignores_shared_note_words_but_not_status(fine_board):
    store, owner = fine_board
    api = SharedInvestigations(store)
    store.append_turn_audit("review", owner, "notes", "request", [], now=1)
    store.append_turn_audit("review", owner, "notes", "response", [{"type": "ai", "data": {"content": ""}}], now=1)
    item = api.create("review", owner, "original?", ["a"], ["a"], turn_id="notes", tool_call_id="create", now=1)
    item = api.claim("review", owner, item["id"], item["revision"], "investigate", operation_id="claim", now=1)
    task = delegate(store, owner)
    lease = store.acquire_task(task.id, "helper", 100, now=1).lease_token
    item = api.update(
        "review",
        owner,
        item["id"],
        item["revision"],
        question="different wording?",
        observations=[],
        open_questions=["clarification"],
        status="open",
        reason="wording",
        turn_id="notes",
        operation_id="words",
        now=2,
    )
    assert store.assert_question_task_current(task.id, lease, now=2).id == "q"
    api.update(
        "review",
        owner,
        item["id"],
        item["revision"],
        question="different wording?",
        observations=[],
        open_questions=["clarification"],
        status="partial",
        reason="not finished",
        turn_id="notes",
        operation_id="status",
        now=2,
    )
    with pytest.raises(RevisionConflict):
        store.assert_question_task_current(task.id, lease, now=2)


@pytest.mark.parametrize("corruption", ["empty_scopes", "bad_region", "unknown_region", "missing_bindings"])
def test_fine_fence_rejects_missing_or_corrupt_dependencies(fine_board, corruption):
    store, owner = fine_board
    api = SharedInvestigations(store)
    store.append_turn_audit("review", owner, "notes", "request", [], now=1)
    store.append_turn_audit("review", owner, "notes", "response", [{"type": "ai", "data": {"content": ""}}], now=1)
    item = api.create("review", owner, "original?", ["a"], ["a"], turn_id="notes", tool_call_id="create", now=1)
    task = delegate(store, owner)
    lease = store.acquire_task(task.id, "helper", 100, now=1).lease_token
    with store._connect() as db:
        if corruption == "missing_bindings":
            db.execute("DELETE FROM question_input_bindings WHERE task_id=?", (task.id,))
        elif corruption == "bad_region":
            payload = json.loads(
                db.execute("SELECT payload FROM objects WHERE kind='ReviewScope' AND id='a'").fetchone()[0]
            )
            payload["regions"][0]["path"] = "unexpected.py"
            db.execute("UPDATE objects SET payload=? WHERE kind='ReviewScope' AND id='a'", (json.dumps(payload),))
        else:
            payload = json.loads(
                db.execute("SELECT payload FROM shared_investigations WHERE id=?", (item["id"],)).fetchone()[0]
            )
            if corruption == "empty_scopes":
                payload["scope_ids"] = []
            else:
                payload["region_ids"] = ["not-in-manifest"]
            db.execute("UPDATE shared_investigations SET payload=? WHERE id=?", (json.dumps(payload), item["id"]))
    with pytest.raises(RevisionConflict):
        store.assert_question_task_current(task.id, lease, now=2)
