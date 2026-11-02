"""Affected unit work resumes in one run after shared investigation changes."""

import asyncio
import hashlib
import json
from collections import Counter

import pytest
from langchain_core.messages import AIMessage
from test_v42_fair_runtime import finish_unit, receipts
from test_v42_stateful_reviewer import Gateway, call

from reviewforge.review_v42.contracts import ReviewScope, Run, Task
from reviewforge.review_v42.runtime import ReviewRuntime
from reviewforge.review_v42.scope import compile_scopes
from reviewforge.review_v42.shared_investigation import SharedInvestigations
from reviewforge.review_v42.store import ReviewStore


def setup_board(tmp_path, fair):
    regions = [
        {
            "id": x,
            "path": f"{x}.py",
            "side": "head",
            "start_line": 1,
            "end_line": 1,
            "base_start_line": 1,
            "base_end_line": 1,
            "changed_lines": [1],
        }
        for x in ("a", "b", "c")
    ]
    metadata = {
        "changed_regions": regions,
        "workspace_digest": "snapshot",
        "base_workspace_digest": "base",
        "shared_investigation": "shared-1",
    }
    if fair:
        metadata["unit_scheduler"] = "cooperative-1"
    run = Run("r", "org/repo", "base", "head", policy="v42-units-6", metadata=metadata)
    store = ReviewStore(tmp_path / "board.db", run)
    scopes = compile_scopes(run, regions)
    return store, regions, [s.regions[0]["id"] for s in scopes]


class Source(Gateway):
    async def __call__(self, name, args):
        self.calls.append((name, args.copy()))
        return {
            "content": f"source {args['path']}\n",
            "revision": "head",
            "side": "head",
            "kind": "source",
            "path": args["path"],
            "start_line": 1,
            "end_line": 1,
            "metadata": {"workspace_digest": "snapshot", "status": "available"},
        }


class CollaborationModel:
    def __init__(self, store, order, *, resolve=True, close_status="resolved"):
        self.store, self.order, self.resolve = store, order, resolve
        self.close_status = close_status
        self.primary = Counter()
        self.created = None
        self.claimed = None
        self.shared_ref = None
        self.integration = 0

    def bind_tools(self, tools, **kwargs):
        return self

    async def ainvoke(self, messages):
        await asyncio.sleep(0)
        board = json.loads(messages[1].content)
        previous = [json.loads(m.content) for m in messages if m.type == "tool"]
        if board["unit_role"] == "integration":
            self.integration += 1
            refs = [x["evidence_ref"] for x in previous if "evidence_ref" in x]
            if not refs:
                return AIMessage(
                    content="",
                    tool_calls=[call("read_source", {"path": f"{key}.py"}, "integration-" + key) for key in self.order],
                )
            return AIMessage(
                content="",
                tool_calls=[
                    *receipts(board["integration_obligations"], refs),
                    call("finish_integration", {}, "integration-finish"),
                ],
            )
        region = board["scope"]["regions"][0]
        key = region["id"]
        self.primary[key] += 1
        assert self.primary[key] <= 20, (key, self.primary, previous)
        maker, affected, unaffected = self.order[2], self.order[0], self.order[1]
        if key == maker:
            step = self.primary[key]
            if step == 1:
                return AIMessage(content="", tool_calls=[call("read_source", {"path": region["path"]}, "own")])
            if step == 2:
                self.shared_ref = next(x["evidence_ref"] for x in previous if "evidence_ref" in x)
                affected_id = next(
                    s.id for s in self.store.list(ReviewScope) if s.regions and s.regions[0]["id"] == affected
                )
                return AIMessage(
                    content="",
                    tool_calls=[
                        call(
                            "create_shared_investigation",
                            {
                                "question": "Do these changes interact?",
                                "scope_ids": [board["scope"]["id"], affected_id],
                                "region_ids": [maker, affected],
                            },
                            "create",
                        )
                    ],
                )
            if step == 3:
                self.created = next(x for x in previous if x.get("type") == "shared_investigation")
                if not self.resolve:
                    return AIMessage(content="", tool_calls=[finish_unit(maker, self.shared_ref)])
                return AIMessage(
                    content="",
                    tool_calls=[
                        call(
                            "claim_shared_investigation",
                            {
                                "investigation_id": self.created["id"],
                                "expected_revision": self.created["revision"],
                                "reason": "Investigate the interaction",
                            },
                            "claim",
                        )
                    ],
                )
            if step == 4:
                self.claimed = next(x for x in previous if x.get("type") == "shared_investigation")
                return AIMessage(
                    content="",
                    tool_calls=[
                        call(
                            "update_shared_investigation",
                            {
                                "investigation_id": self.claimed["id"],
                                "expected_revision": self.claimed["revision"],
                                "question": "Do these changes interact?",
                                "observations": (
                                    [{"statement": "Inspected shared source", "evidence_refs": [self.shared_ref]}]
                                    if self.close_status == "resolved"
                                    else []
                                ),
                                "open_questions": [],
                                "status": self.close_status,
                                "reason": "Inspected the source",
                            },
                            "resolve",
                        )
                    ],
                )
            return AIMessage(content="", tool_calls=[finish_unit(maker, self.shared_ref)])
        assert key in {affected, unaffected}
        if not previous:
            tools = [call("read_source", {"path": region["path"]}, "own")]
            if key == affected and self.claimed is not None:
                tools.append(call("read_evidence", {"ref": self.shared_ref}, "new-shared"))
            return AIMessage(content="", tool_calls=tools)
        ref = next(x["evidence_ref"] for x in previous if x.get("path") == region["path"])
        return AIMessage(content="", tool_calls=[finish_unit(key, ref)])


@pytest.mark.parametrize("fair", [False, True])
@pytest.mark.parametrize("close_status", ["resolved", "retracted"])
async def test_affected_units_reenter_and_unrelated_unit_stays_finished(tmp_path, fair, close_status):
    store, regions, order = setup_board(tmp_path, fair)
    if fair:
        # The first unit fences the second while it waits for its next model turn.
        order = [order[1], order[2], order[0]]
    model = CollaborationModel(store, order, close_status=close_status)
    result = await asyncio.wait_for(ReviewRuntime(store, model, Source(), object()).run(regions), 12)
    assert result["status"] == "completed", result
    assert model.primary[order[0]] >= (3 if fair else 4)
    assert model.primary[order[1]] == 2
    assert model.integration >= 2 and store.pending_unit_sources() == []
    assert all(scope.status == "reviewed" for scope in store.list(ReviewScope))
    affected = next(s for s in store.list(ReviewScope) if s.regions and s.regions[0]["id"] == order[0])
    tasks = [t for t in store.list(Task) if t.kind == "review" and t.object_id == affected.id]
    assert len(tasks) >= 2
    if fair:
        assert any(t.status == "superseded" for t in tasks)


@pytest.mark.parametrize("fair", [False, True])
async def test_unresolved_shared_item_cannot_fake_complete(tmp_path, fair):
    store, regions, order = setup_board(tmp_path, fair)
    model = CollaborationModel(store, order, resolve=False)
    result = await asyncio.wait_for(ReviewRuntime(store, model, Source(), object()).run(regions), 12)
    assert result["status"] == "partial", result
    assert model.integration == 0


async def test_cancellation_recovers_complete_native_exchange_without_replaying_read(tmp_path):
    store, regions, order = setup_board(tmp_path, True)
    entered = asyncio.Event()

    class CancelModel(CollaborationModel):
        blocked = False

        async def ainvoke(self, messages):
            board = json.loads(messages[1].content)
            if (
                board["unit_role"] == "primary"
                and board["scope"]["regions"][0]["id"] == order[2]
                and self.primary[order[2]] == 1
                and not self.blocked
            ):
                self.blocked = True
                entered.set()
                await asyncio.Event().wait()
            return await super().ainvoke(messages)

    model = CancelModel(store, order)
    gateway = Source()
    runtime = ReviewRuntime(store, model, gateway, object())
    first = asyncio.create_task(runtime.run(regions))
    await asyncio.wait_for(entered.wait(), 5)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    maker_path = f"{order[2]}.py"
    before = sum(args.get("path") == maker_path for _, args in gateway.calls)
    assert before == 1
    result = await asyncio.wait_for(ReviewRuntime(store, model, gateway, object()).run(regions), 12)
    assert result["status"] == "completed", result
    # The independent integration task intentionally reads it once more.
    assert sum(args.get("path") == maker_path for _, args in gateway.calls) == before + 1


def test_shared_work_fingerprint_uses_related_status_and_source_not_prose_or_leases(tmp_path):
    store, regions, order = setup_board(tmp_path, False)
    runtime = ReviewRuntime(store, None, None, None)
    scopes = runtime.prepare(regions)
    by_region = {scope.regions[0]["id"]: scope for scope in scopes}
    maker, affected, unrelated = (by_region[key] for key in order)
    before = {scope.id: runtime._unit_reopen_input(scope.id) for scope in scopes}
    task = store.add(Task("maker", store.run.id, "review", maker.id, maker.revision))
    task = store.acquire_task(task.id, "worker", 120, now=0)
    store.append_turn_audit(task.id, task.lease_token, "create", "request", [], now=1)
    store.append_turn_audit(
        task.id, task.lease_token, "create", "response", [{"type": "ai", "data": {"content": ""}}], now=1
    )
    shared = SharedInvestigations(store)
    item = shared.create(
        task.id,
        task.lease_token,
        "Question",
        [maker.id, affected.id],
        [maker.regions[0]["id"], affected.regions[0]["id"]],
        turn_id="create",
        tool_call_id="call",
        now=1,
    )
    created = runtime._unit_reopen_input(affected.id)
    assert created != before[affected.id]
    assert runtime._unit_reopen_input(unrelated.id) == before[unrelated.id]
    item = shared.claim(task.id, task.lease_token, item["id"], item["revision"], "claim", operation_id="claim", now=2)
    assert runtime._unit_reopen_input(affected.id) == created
    store.append_turn_audit(task.id, task.lease_token, "note", "request", [], now=2)
    store.append_turn_audit(
        task.id, task.lease_token, "note", "response", [{"type": "ai", "data": {"content": ""}}], now=2
    )
    item = shared.update(
        task.id,
        task.lease_token,
        item["id"],
        item["revision"],
        question="Rephrased question",
        observations=[],
        open_questions=["A different wording"],
        status="open",
        reason="Update notes",
        turn_id="note",
        operation_id="note",
        now=3,
    )
    assert runtime._unit_reopen_input(affected.id) == created
    assert runtime._unit_reopen_input(unrelated.id) == before[unrelated.id]
    fresh = store.put_evidence(
        b"new source\n",
        revision="head",
        side="head",
        kind="source",
        path=affected.regions[0]["path"],
        start_line=1,
        end_line=1,
        metadata={"workspace_digest": "snapshot"},
    )
    current = store.get(ReviewScope, affected.id)
    store.reopen_unit_for_evidence(affected.id, current.revision, [fresh.id], "new evidence")
    assert runtime._unit_reopen_input(affected.id) != created
    assert runtime._unit_reopen_input(unrelated.id) == before[unrelated.id]


async def test_reviewed_scope_with_unfinished_historical_batch_stays_partial(tmp_path):
    store, regions, order = setup_board(tmp_path, False)
    model = CollaborationModel(store, order)
    runtime = ReviewRuntime(store, model, Source(), object())
    result = await asyncio.wait_for(runtime.run(regions), 12)
    assert result["status"] == "completed", result
    scope = next(s for s in store.list(ReviewScope) if s.regions and s.regions[0]["id"] == order[0])
    task = next(
        t for t in store.list(Task) if t.kind == "review" and t.object_id == scope.id and t.status == "completed"
    )
    response = [
        {
            "type": "ai",
            "data": {"content": "", "tool_calls": [call("read_source", {"path": "a.py"}, "lost")]},
        }
    ]
    # Simulate an already-existing interrupted historical checkpoint from before
    # the transactional finish guard was introduced.
    raw = json.dumps(response).encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()
    (store.audit_dir / digest).write_bytes(raw)
    with store._connect() as db:
        db.execute(
            "INSERT INTO turn_audit(task_id,turn_id,phase,content_hash) VALUES (?,?,?,?)",
            (task.id, "interrupted-turn", "response", digest),
        )
        db.commit()
    calls = sum(model.primary.values())
    assert await runtime._review_scope(scope.id) == "partial"
    assert any(f"{task.id}/interrupted-turn" in error for error in runtime._errors)
    resumed = await asyncio.wait_for(ReviewRuntime(store, model, Source(), object()).run(regions), 12)
    assert resumed["status"] == "partial"
    assert any(f"{task.id}/interrupted-turn" in error for error in resumed["errors"])
    assert sum(model.primary.values()) == calls
