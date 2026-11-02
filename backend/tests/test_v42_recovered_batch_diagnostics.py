"""Transient native-batch diagnostics track current durable recovery state."""

import asyncio

import pytest
from test_v42_fenced_read_recovery import fenced_batch
from test_v42_shared_rescheduling import CollaborationModel, Source, setup_board
from test_v42_stateful_reviewer import call

from reviewforge.review_v42.contracts import ReviewScope, Task
from reviewforge.review_v42.runtime import ReviewRuntime
from reviewforge.review_v42.shared_investigation import SharedInvestigations


@pytest.mark.parametrize("fair", [False, True])
async def test_pending_read_fenced_aborted_and_rescheduled_in_one_run(tmp_path, fair):
    store, regions, order = setup_board(tmp_path, fair)
    runtime = ReviewRuntime(store, None, Source(), object())
    scopes = runtime.prepare(regions)
    reader = next(scope for scope in scopes if scope.regions[0]["id"] == order[0])
    old = store.add(Task("old-reader", store.run.id, "review", reader.id, reader.revision))
    old = store.acquire_task(old.id, "old", 120)
    store.append_turn_audit(old.id, old.lease_token, "old-turn", "request", [])
    store.append_turn_audit(
        old.id,
        old.lease_token,
        "old-turn",
        "response",
        [{"type": "ai", "data": {"content": "", "tool_calls": [call("read_source", {"path": "a.py"}, "read")]}}],
    )
    store.release_task(old.id, old.lease_token)
    runtime.llm = CollaborationModel(store, order)
    result = await asyncio.wait_for(runtime.run(regions), 15)
    assert result["status"] == "completed", result
    assert result["errors"] == []
    assert store.pending_shared_batches(reader.id) == []
    assert any(event["action"] == "fenced_read_batch_aborted" for event in store.history("Task", old.id))
    assert store.get(ReviewScope, reader.id).status == "reviewed"
    assert any(
        t.kind == "review" and t.object_id == reader.id and t.id != old.id and t.status == "completed"
        for t in store.list(Task)
    )


def test_only_aborted_batch_diagnostic_is_removed_and_real_errors_survive(tmp_path, monkeypatch):
    store, reader, old = fenced_batch(tmp_path, [call("read_source", {"path": "a.py"}, "read")], fence=False)
    store.append_turn_audit(old.id, old.lease_token, "mutating", "request", [], now=1)
    store.append_turn_audit(
        old.id,
        old.lease_token,
        "mutating",
        "response",
        [{"type": "ai", "data": {"content": "", "tool_calls": [call("submit_case", {}, "case")]}}],
        now=1,
    )
    runtime = ReviewRuntime(store, None, Source(), object())
    runtime._errors_real.append("verification case: RuntimeError")
    assert runtime._record_incomplete_shared_batches(reader.id)
    assert any(f"{old.id}/turn" in error for error in runtime._errors)
    assert any(f"{old.id}/mutating" in error for error in runtime._errors)
    maker = next(s for s in store.list(ReviewScope) if s.regions and s.id != reader.id)
    task = store.add(Task("fencer", store.run.id, "review", maker.id, maker.revision))
    task = store.acquire_task(task.id, "fencer", 120, now=0)
    store.append_turn_audit(task.id, task.lease_token, "fence", "request", [], now=1)
    response = [{"type": "ai", "data": {"content": ""}}]
    store.append_turn_audit(task.id, task.lease_token, "fence", "response", response, now=1)
    SharedInvestigations(store).create(
        task.id,
        task.lease_token,
        "question",
        [maker.id, reader.id],
        [maker.regions[0]["id"], reader.regions[0]["id"]],
        turn_id="fence",
        tool_call_id="create",
        now=1,
    )
    store.append_turn_audit(task.id, task.lease_token, "fence", "exchange", response, now=1)
    assert runtime._record_incomplete_shared_batches(reader.id)
    assert not any(f"{old.id}/turn" in error for error in runtime._errors)
    assert any(f"{old.id}/mutating" in error for error in runtime._errors)
    assert "verification case: RuntimeError" in runtime._errors

    original = store.pending_shared_batches
    monkeypatch.setattr(
        store, "pending_shared_batches", lambda *_a, **_kw: (_ for _ in ()).throw(OSError("damaged audit"))
    )
    assert runtime._record_incomplete_shared_batches(reader.id)
    assert any("damaged audit" in error for error in runtime._errors)
    assert any(f"{old.id}/mutating" in error for error in runtime._errors)
    monkeypatch.setattr(store, "pending_shared_batches", original)
    assert runtime._record_incomplete_shared_batches(reader.id)
    assert not any("damaged audit" in error for error in runtime._errors)
    assert any(f"{old.id}/mutating" in error for error in runtime._errors)
    assert "verification case: RuntimeError" in runtime._errors
