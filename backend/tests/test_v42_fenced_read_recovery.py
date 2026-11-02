"""A fenced reader must not strand a safely abortable native read batch."""

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from langchain_core.messages import AIMessage
from test_v42_fair_runtime import finish_unit, receipts
from test_v42_shared_rescheduling import Source, setup_board
from test_v42_stateful_reviewer import call

from reviewforge.review_v42.contracts import BindingError, LeaseError, Question, ReviewScope, Run, Task
from reviewforge.review_v42.runtime import ReviewRuntime
from reviewforge.review_v42.shared_investigation import SharedInvestigations


async def test_read_await_fenced_by_other_scope_can_be_restarted(tmp_path):
    store, regions, order = setup_board(tmp_path, True)
    runtime = ReviewRuntime(store, None, None, None)
    scopes = runtime.prepare(regions)
    by_region = {scope.regions[0]["id"]: scope for scope in scopes}
    maker, reader = by_region[order[0]], by_region[order[1]]
    entered, resume = asyncio.Event(), asyncio.Event()

    class Gateway(Source):
        async def __call__(self, name, args):
            entered.set()
            await resume.wait()
            return await super().__call__(name, args)

    class Model:
        def bind_tools(self, *args, **kwargs):
            return self

        async def ainvoke(self, messages):
            return AIMessage(content="", tool_calls=[call("read_source", {"path": reader.regions[0]["path"]}, "read")])

    runtime.llm = Model()
    from reviewforge.review_v42.tools import EvidenceGateway

    runtime.gateway = EvidenceGateway(store, Gateway())
    job = asyncio.create_task(runtime._review_scope(reader.id))
    await asyncio.wait_for(entered.wait(), 5)
    task = store.add(Task("maker", store.run.id, "review", maker.id, maker.revision))
    task = store.acquire_task(task.id, "maker", 120)
    store.append_turn_audit(task.id, task.lease_token, "maker-turn", "request", [])
    response = [{"type": "ai", "data": {"content": ""}}]
    store.append_turn_audit(task.id, task.lease_token, "maker-turn", "response", response)
    shared = SharedInvestigations(store)
    item = shared.create(
        task.id,
        task.lease_token,
        "question",
        [maker.id, reader.id],
        [maker.regions[0]["id"], reader.regions[0]["id"]],
        turn_id="maker-turn",
        tool_call_id="create",
    )
    store.append_turn_audit(task.id, task.lease_token, "maker-turn", "exchange", response)
    resume.set()
    assert await asyncio.wait_for(job, 5) == "superseded"
    assert store.get(ReviewScope, reader.id).status == "pending"
    assert not runtime._record_incomplete_shared_batches(reader.id)
    assert store.pending_shared_batches(reader.id) == []
    old_reader = next(t for t in store.list(Task) if t.kind == "review" and t.object_id == reader.id)
    assert any(event["action"] == "fenced_read_batch_aborted" for event in store.history("Task", old_reader.id))
    item = shared.claim(task.id, task.lease_token, item["id"], item["revision"], "take", operation_id="claim")
    shared.update(
        task.id,
        task.lease_token,
        item["id"],
        item["revision"],
        question="question",
        observations=[],
        open_questions=[],
        status="retracted",
        reason="no longer applicable",
        turn_id="maker-turn",
        operation_id="retract",
    )
    store.release_task(task.id, task.lease_token)

    class FinishModel:
        def bind_tools(self, *args, **kwargs):
            return self

        async def ainvoke(self, messages):
            board = json.loads(messages[1].content)
            previous = [json.loads(message.content) for message in messages if message.type == "tool"]
            if board["unit_role"] == "integration":
                refs = [item["evidence_ref"] for item in previous if "evidence_ref" in item]
                if not refs:
                    return AIMessage(
                        content="",
                        tool_calls=[
                            call("read_source", {"path": f"{name}.py"}, f"integration-{name}") for name in order
                        ],
                    )
                return AIMessage(
                    content="",
                    tool_calls=[
                        *receipts(board["integration_obligations"], refs),
                        call("finish_integration", {}, "done"),
                    ],
                )
            region = board["scope"]["regions"][0]
            source = next((item["evidence_ref"] for item in previous if "evidence_ref" in item), None)
            if source is None:
                return AIMessage(content="", tool_calls=[call("read_source", {"path": region["path"]}, "new-read")])
            return AIMessage(content="", tool_calls=[finish_unit(region["id"], source)])

    gateway = Source()
    result = await asyncio.wait_for(ReviewRuntime(store, FinishModel(), gateway, object()).run(regions), 15)
    assert result["status"] == "completed", result
    assert any(args["path"] == reader.regions[0]["path"] for _, args in gateway.calls)
    assert len([t for t in store.list(Task) if t.kind == "review" and t.object_id == reader.id]) >= 2


def fenced_batch(tmp_path, calls, *, receipts_before=(), native=None, fence=True):
    store, regions, order = setup_board(tmp_path, False)
    runtime = ReviewRuntime(store, None, None, None)
    scopes = runtime.prepare(regions)
    maker, reader = scopes[:2]
    SharedInvestigations(store)
    task = store.add(Task("reader", store.run.id, "review", reader.id, reader.revision))
    task = store.acquire_task(task.id, "reader", 120, now=0)
    store.append_turn_audit(task.id, task.lease_token, "turn", "request", [], now=1)
    store.append_turn_audit(
        task.id,
        task.lease_token,
        "turn",
        "response",
        [{"type": "ai", "data": {"content": "", "tool_calls": calls}}],
        now=1,
    )
    if native is not None:
        store.append_turn_audit(task.id, task.lease_token, "turn", "native", [native], now=1)
    for index, ident in enumerate(receipts_before):
        store.append_turn_audit(
            task.id,
            task.lease_token,
            "turn",
            f"tool:{index}:{ident}",
            [{"type": "tool", "data": {"content": json.dumps({"ok": True}), "tool_call_id": ident}}],
            now=1,
        )
    if fence:
        other = store.add(Task("maker", store.run.id, "review", maker.id, maker.revision))
        other = store.acquire_task(other.id, "maker", 120, now=0)
        response = [{"type": "ai", "data": {"content": ""}}]
        store.append_turn_audit(other.id, other.lease_token, "other", "request", [], now=1)
        store.append_turn_audit(other.id, other.lease_token, "other", "response", response, now=1)
        SharedInvestigations(store).create(
            other.id,
            other.lease_token,
            "question",
            [maker.id, reader.id],
            [maker.regions[0]["id"], reader.regions[0]["id"]],
            turn_id="other",
            tool_call_id="create",
            now=1,
        )
        store.append_turn_audit(other.id, other.lease_token, "other", "exchange", response, now=1)
    return store, reader, task


def test_multiple_reads_existing_receipt_idempotence_and_new_lease_delivery(tmp_path):
    calls = [call("read_source", {"path": "a.py"}, "first"), call("read_evidence", {"ref": "ev"}, "second")]
    store, scope, old = fenced_batch(tmp_path, calls, receipts_before=["first"])
    marker = store.abort_fenced_read_batch(old.id, "turn")
    assert marker["calls"] == [
        {"id": "first", "name": "read_source", "receipt": "recorded"},
        {"id": "second", "name": "read_evidence", "receipt": "not_adopted"},
    ]
    assert store.abort_fenced_read_batch(old.id, "turn") == marker
    reopened = type(store)(store.path, store.run)
    assert reopened.pending_shared_batches(scope.id) == []
    with reopened._connect() as db:
        with pytest.raises(LeaseError):
            reopened._require_delivered(db, "new-task", "new-lease", "turn", set())


@pytest.mark.parametrize("other", ["submit_case", "create_shared_investigation", "run_check", "unknown_tool"])
@pytest.mark.parametrize("recorded", [False, True])
def test_mutating_or_unknown_sibling_never_auto_aborts(tmp_path, other, recorded):
    calls = [call("read_source", {"path": "a.py"}, "read"), call(other, {}, "other")]
    store, scope, task = fenced_batch(tmp_path, calls, receipts_before=["read", "other"] if recorded else ["read"])
    with pytest.raises(BindingError):
        store.abort_fenced_read_batch(task.id, "turn")
    assert store.pending_shared_batches(scope.id)


@pytest.mark.parametrize(
    "calls,native",
    [
        ([call("read_source", {}, "x"), call("read_source", {}, "x")], None),
        ([call("read_source", {}, "")], None),
        ([call("read_source", {"review_state": {}}, "x")], None),
        ([call("read_source", {}, "x")], {"raw_tool_calls": [], "invalid_tool_calls": []}),
        ([call("read_source", {}, "x")], {"raw_tool_calls": None, "invalid_tool_calls": [{"id": "bad"}]}),
    ],
)
def test_ambiguous_protocol_or_mutating_review_state_cannot_abort(tmp_path, calls, native):
    store, scope, task = fenced_batch(tmp_path, calls, native=native)
    with pytest.raises((BindingError, ValueError)):
        store.abort_fenced_read_batch(task.id, "turn")
    assert store.pending_shared_batches(scope.id)


def test_unfenced_task_and_old_policy_never_auto_abort(tmp_path):
    store, scope, task = fenced_batch(tmp_path, [call("read_source", {}, "x")], fence=False)
    with pytest.raises(BindingError):
        store.abort_fenced_read_batch(task.id, "turn")
    old = type(store)(tmp_path / "old.sqlite", Run("old", "repo", "base", "head"))
    with pytest.raises(BindingError):
        old.abort_fenced_read_batch("task", "turn")


def test_tool_failure_question_makes_an_unrecorded_read_uncertain(tmp_path):
    calls = [call("read_source", {"path": "a.py"}, "read")]
    store, scope, task = fenced_batch(tmp_path, calls)
    failure = "Tool unavailable: read_source " + json.dumps({"path": "a.py"}, sort_keys=True, ensure_ascii=False)
    store.add(Question("tool-failure", store.run.id, failure, scope_id=scope.id, owner="tool_controller"))
    with pytest.raises(BindingError):
        store.abort_fenced_read_batch(task.id, "turn")
    assert store.pending_shared_batches(scope.id)


def test_abort_transaction_rolls_back_and_corrupt_audit_never_unblocks(tmp_path, monkeypatch):
    store, scope, task = fenced_batch(tmp_path, [call("read_source", {}, "read")])
    original = store._event

    def fail(*args, **kwargs):
        raise OSError("simulated process failure")

    monkeypatch.setattr(store, "_event", fail)
    with pytest.raises(OSError):
        store.abort_fenced_read_batch(task.id, "turn")
    assert store.pending_shared_batches(scope.id)
    monkeypatch.setattr(store, "_event", original)
    marker = store.abort_fenced_read_batch(task.id, "turn")
    assert store.pending_shared_batches(scope.id) == []
    (store.audit_dir / marker["phases"]["response"]).write_bytes(b"corrupt")
    assert store.pending_shared_batches(scope.id)


def test_concurrent_abort_is_atomic_and_emits_one_event(tmp_path):
    store, scope, task = fenced_batch(tmp_path, [call("read_source", {}, "read")])
    with ThreadPoolExecutor(max_workers=2) as pool:
        markers = list(pool.map(lambda _: store.abort_fenced_read_batch(task.id, "turn"), range(2)))
    assert markers[0] == markers[1]
    assert store.pending_shared_batches(scope.id) == []
    assert sum(event["action"] == "fenced_read_batch_aborted" for event in store.history("Task", task.id)) == 1
