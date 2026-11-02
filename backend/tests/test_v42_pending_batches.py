import json

import pytest
from test_v42_case_counterevidence import board as board

from reviewforge.review_v42.contracts import BindingError, ReviewScope
from reviewforge.review_v42.shared_investigation import SharedInvestigations


def record(store, task, turn, phase, data):
    store.append_turn_audit(task.id, task.lease_token, turn, phase, data, now=2)


def start(store, task, ref, turn, calls, *, native=None):
    payload = {
        "evidence_ref": ref.id,
        "content": store.read_evidence(ref.id).decode(),
        "start_line": ref.start_line,
        "end_line": ref.end_line,
    }
    record(store, task, turn, "request", [{"type": "tool", "data": {"content": json.dumps(payload)}}])
    if native is not None:
        record(store, task, turn, "native", [native])
    record(store, task, turn, "response", [{"type": "ai", "data": {"content": "", "tool_calls": calls}}])


def receipt(store, task, turn, ident, content, index=0):
    record(
        store,
        task,
        turn,
        f"tool:{index}:{ident}",
        [{"type": "tool", "data": {"tool_call_id": ident, "content": json.dumps(content)}}],
    )


def finish(store, task, ref, turn):
    return store.commit_unit_decision(
        task.id,
        task.lease_token,
        store.get(ReviewScope, "a").revision,
        "finish-" + turn,
        checked_region_ids=["a"],
        observations=[{"statement": "checked", "evidence_refs": [ref.id], "region_ids": ["a"]}],
        questions=[],
        cases=[],
        unresolved=[],
        turn_id=turn,
        finish=True,
        now=3,
    )


def test_finish_only_current_batch_then_exchange_required_for_task(board):
    store, task, _, _, _, ref = board
    SharedInvestigations(store)
    calls = [
        {"id": "finish", "name": "submit_unit_decision", "args": {"finish": True}},
        {"id": "read", "name": "read_source", "args": {}},
    ]
    start(store, task, ref, "final", calls)
    receipt(store, task, "final", "read", {"content": "ok"})
    pending = store.pending_shared_batches("a")
    assert [c["receipt"] for c in pending[0]["calls"]] == ["outcome_unknown", "recorded"]
    assert finish(store, task, ref, "final")["scope"].status == "reviewed"
    with pytest.raises(BindingError, match="unfinished native"):
        store.complete_task(task.id, task.lease_token, {}, now=4)
    assert any(x["turn_id"] == "final" for x in store.validate_units_completion()["pending_batches"])
    receipt(store, task, "final", "finish", {"status": "reviewed"}, index=1)
    record(store, task, "final", "exchange", [{"type": "ai", "data": {"content": "done"}}])
    assert not store.pending_shared_batches("a")
    assert store.complete_task(task.id, task.lease_token, {}, now=4).status == "completed"


@pytest.mark.parametrize("mode", ["missing", "error", "duplicate", "invalid_raw"])
def test_finish_cannot_exclude_incomplete_or_failed_sibling_or_invalid_protocol(board, mode):
    store, task, _, _, _, ref = board
    SharedInvestigations(store)
    calls = [
        {"id": "finish", "name": "submit_unit_decision", "args": {"finish": True}},
        {"id": "read", "name": "read_source", "args": {}},
    ]
    if mode == "duplicate":
        calls[1]["id"] = "finish"
    native = None
    if mode == "invalid_raw":
        native = {
            "raw_tool_calls": [
                {
                    "id": "finish",
                    "function": {"name": "submit_unit_decision", "arguments": '{"finish":true,"finish":true}'},
                },
                {"id": "read", "function": {"name": "read_source", "arguments": "{}"}},
            ],
            "invalid_tool_calls": [],
        }
    start(store, task, ref, "final", calls, native=native)
    if mode not in {"missing", "duplicate"}:
        receipt(store, task, "final", "read", {"error": "failed"} if mode == "error" else {"status": "ok"})
    with pytest.raises(BindingError, match="unfinished native"):
        finish(store, task, ref, "final")
    assert store.get(ReviewScope, "a").status != "reviewed"


def test_old_batch_blocks_replacement_lease_and_inflight_request_does_not(board):
    store, task, _, _, _, ref = board
    start(store, task, ref, "pending", [{"id": "x", "name": "read_source", "args": {}}])
    record(store, task, "inflight", "request", [{"type": "human", "data": {"content": "request"}}])
    recovered = store.acquire_task(task.id, "replacement", 100, now=101)
    assert [(x["task_id"], x["turn_id"]) for x in store.pending_shared_batches("a")] == [(task.id, "pending")]
    with pytest.raises(Exception):
        store.append_turn_audit(task.id, task.lease_token, "pending", "exchange", [], now=102)
    assert recovered.lease_token != task.lease_token
    assert not store.pending_shared_batches("b")


def test_corrupted_audit_fails_closed(board):
    store, task, _, _, _, ref = board
    start(store, task, ref, "pending", [{"id": "x", "name": "read_source", "args": {}}])
    with store._connect() as db:
        digest = db.execute(
            "SELECT content_hash FROM turn_audit WHERE task_id=? AND turn_id=? AND phase='response'",
            (task.id, "pending"),
        ).fetchone()[0]
    (store.audit_dir / digest).write_text("corrupt", encoding="utf-8")
    pending = store.pending_shared_batches("a")
    assert len(pending) == 1 and "invalid" in pending[0]["reason"]
