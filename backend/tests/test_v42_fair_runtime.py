import asyncio
import json

import pytest
from langchain_core.messages import AIMessage
from test_v42_stateful_reviewer import Gateway, call

from reviewforge.review_v42.contracts import Case, ReviewScope, Run, Task
from reviewforge.review_v42.runtime import ReviewRuntime
from reviewforge.review_v42.store import ReviewStore


def make_run(tmp_path, *, fair=True):
    regions = [
        {"id": "a", "path": "a.py", "side": "head", "start_line": 1, "end_line": 1, "changed_lines": [1]},
        {"id": "b", "path": "b.py", "side": "head", "start_line": 1, "end_line": 1, "changed_lines": [1]},
    ]
    meta = {"changed_regions": regions, "workspace_digest": "head-snapshot", "base_workspace_digest": "base-snapshot"}
    if fair:
        meta["unit_scheduler"] = "cooperative-1"
    run = Run(id="r", repo="a/b", base="base", head="head", policy="v42-units-6", metadata=meta)
    return ReviewStore(tmp_path / "board.db", run), regions


class PinnedGateway(Gateway):
    async def __call__(self, name, args):
        result = await super().__call__(name, args)
        result["metadata"] = {"workspace_digest": "head-snapshot", "status": "available"}
        return result


def finish_unit(region_id, ref):
    return call(
        "submit_unit_decision",
        {
            "checked_region_ids": [region_id],
            "observations": [{"statement": "Checked original", "evidence_refs": [ref], "region_ids": [region_id]}],
            "unresolved_refs": [],
            "finish": True,
        },
        "finish-" + region_id,
    )


def receipts(obligations, refs):
    return [
        call(
            "record_integration_receipt",
            {
                "obligation_id": o["id"],
                "disposition": "checked",
                "evidence_refs": refs,
                "reason": "Compared sources",
            },
            o["id"],
        )
        for o in obligations
    ]


async def test_fifo_reviews_b_and_verifies_while_a_is_still_active(tmp_path):
    store, regions = make_run(tmp_path)
    verified = asyncio.Event()

    class Verifier:
        policy = "v42-independent-1"

        async def verify(self, case_id, *, task_id, lease_token):
            case = store.get(Case, case_id)
            assert any(
                s.regions and s.regions[0]["id"] == "a" and s.status == "running" for s in store.list(ReviewScope)
            )
            result = store.save_verdict(
                case_id,
                case.claims[0]["id"],
                case.revision,
                self.policy,
                "reject",
                case.evidence_refs,
                {"reason": "counterexample"},
                task_id=task_id,
                lease_token=lease_token,
            )
            verified.set()
            return [result]

    class Model:
        def __init__(self):
            self.order = []
            self.active = 0
            self.peak = 0

        def bind_tools(self, tools, **kwargs):
            return self

        async def ainvoke(self, messages):
            self.active += 1
            self.peak = max(self.peak, self.active)
            try:
                await asyncio.sleep(0.001)
                board = json.loads(messages[1].content)
                previous = [json.loads(m.content) for m in messages if m.type == "tool"]
                if board["unit_role"] == "integration":
                    if not previous:
                        return AIMessage(
                            content="",
                            tool_calls=[
                                call("read_source", {"path": "a.py"}, "a"),
                                call("read_source", {"path": "b.py"}, "b"),
                            ],
                        )
                    refs = [r["evidence_ref"] for r in previous if "evidence_ref" in r]
                    return AIMessage(
                        content="",
                        tool_calls=[
                            *receipts(board["integration_obligations"], refs),
                            call("finish_integration", {}, "finish"),
                        ],
                    )
                region = board["scope"]["regions"][0]
                key, path = region["id"], region["path"]
                self.order.append(key)
                if not previous or (key == "a" and not verified.is_set()):
                    return AIMessage(content="", tool_calls=[call("read_source", {"path": path}, "read-" + key)])
                ref = next(r["evidence_ref"] for r in previous if "evidence_ref" in r)
                if key == "b":
                    return AIMessage(
                        content="",
                        tool_calls=[
                            call(
                                "submit_case",
                                {
                                    "trigger": "trigger",
                                    "behavior": "mechanism",
                                    "impact": "impact",
                                    "evidence_refs": [ref],
                                },
                                "case",
                            ),
                            finish_unit(key, ref),
                        ],
                    )
                return AIMessage(content="", tool_calls=[finish_unit(key, ref)])
            finally:
                self.active -= 1

    model = Model()
    result = await asyncio.wait_for(ReviewRuntime(store, model, PinnedGateway(), Verifier()).run(regions), 8)
    assert result["status"] == "completed", result
    assert verified.is_set() and model.peak == 1
    assert set(model.order[:2]) == {"a", "b"}
    assert model.order.index("b", model.order.index("b") + 1) < len(model.order) - 1
    assert len([t for t in store.list(Task) if t.kind == "review"]) == 3


async def test_account_failure_wakes_queued_reviewers_without_more_calls(tmp_path):
    store, regions = make_run(tmp_path)

    class PaymentError(Exception):
        status_code = 402

    class Model:
        calls = 0

        def bind_tools(self, tools, **kwargs):
            return self

        async def ainvoke(self, messages):
            self.calls += 1
            await asyncio.sleep(0.001)
            raise PaymentError()

    model = Model()
    result = await asyncio.wait_for(ReviewRuntime(store, model, PinnedGateway(), object()).run(regions), 5)
    assert result["status"] == "provider_blocked" and model.calls == 1
    assert all(t.status != "leased" for t in store.list(Task) if t.kind == "review")
    assert all(row["status"] != "checked" for row in store.list_integration_obligations())


async def test_cancel_and_resume_keeps_native_exchange_without_replaying_reads(tmp_path):
    store, regions = make_run(tmp_path)
    entered = asyncio.Event()
    gateway = PinnedGateway()

    class PausingModel:
        def bind_tools(self, tools, **kwargs):
            return self

        async def ainvoke(self, messages):
            board = json.loads(messages[1].content)
            previous = [m for m in messages if m.type == "tool"]
            if previous:
                entered.set()
                await asyncio.Event().wait()
            region = board["scope"]["regions"][0]
            return AIMessage(
                content="", tool_calls=[call("read_source", {"path": region["path"]}, "read-" + region["id"])]
            )

    run_task = asyncio.create_task(ReviewRuntime(store, PausingModel(), gateway, object()).run(regions))
    await asyncio.wait_for(entered.wait(), 5)
    run_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run_task
    assert len(gateway.calls) == 2
    assert all(t.status != "leased" for t in store.list(Task) if t.kind == "review")

    class ResumeModel:
        def __init__(self):
            self.resumed = set()

        def bind_tools(self, tools, **kwargs):
            return self

        async def ainvoke(self, messages):
            board = json.loads(messages[1].content)
            previous = [json.loads(m.content) for m in messages if m.type == "tool"]
            if board["unit_role"] == "primary":
                assert [m.type for m in messages[-2:]] == ["ai", "tool"]
                region_id = board["scope"]["regions"][0]["id"]
                self.resumed.add(region_id)
                return AIMessage(content="", tool_calls=[finish_unit(region_id, previous[0]["evidence_ref"])])
            if not previous:
                return AIMessage(
                    content="",
                    tool_calls=[call("read_source", {"path": "a.py"}, "a"), call("read_source", {"path": "b.py"}, "b")],
                )
            refs = [r["evidence_ref"] for r in previous if "evidence_ref" in r]
            return AIMessage(
                content="",
                tool_calls=[
                    *receipts(board["integration_obligations"], refs),
                    call("finish_integration", {}, "finish"),
                ],
            )

    model = ResumeModel()
    result = await asyncio.wait_for(ReviewRuntime(store, model, gateway, object()).run(regions), 6)
    assert result["status"] == "completed", result
    assert model.resumed == {"a", "b"}
    assert len(gateway.calls) == 4  # Two original unit reads and two separate integration reads.


async def test_absent_flag_keeps_first_unit_sequential(tmp_path):
    store, regions = make_run(tmp_path, fair=False)
    entered = asyncio.Event()

    class Model:
        seen = []

        def bind_tools(self, tools, **kwargs):
            return self

        async def ainvoke(self, messages):
            board = json.loads(messages[1].content)
            region = board["scope"]["regions"][0]
            self.seen.append(region["id"])
            if len(self.seen) == 2:
                entered.set()
                await asyncio.Event().wait()
            return AIMessage(content="", tool_calls=[call("read_source", {"path": region["path"]}, "read")])

    model = Model()
    run_task = asyncio.create_task(ReviewRuntime(store, model, PinnedGateway(), object()).run(regions))
    await asyncio.wait_for(entered.wait(), 5)
    assert model.seen[0] == model.seen[1]
    assert len([t for t in store.list(Task) if t.kind == "review"]) == 1
    run_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run_task


async def test_verifier_account_failure_cancels_active_reviewers(tmp_path):
    store, regions = make_run(tmp_path)

    class AccountError(Exception):
        status_code = 401

    class Verifier:
        async def verify(self, case_id, *, task_id, lease_token):
            raise AccountError()

    class Model:
        def bind_tools(self, tools, **kwargs):
            return self

        async def ainvoke(self, messages):
            await asyncio.sleep(0.001)
            board = json.loads(messages[1].content)
            previous = [json.loads(m.content) for m in messages if m.type == "tool"]
            region = board["scope"]["regions"][0]
            if region["id"] == "a" or not previous:
                return AIMessage(
                    content="", tool_calls=[call("read_source", {"path": region["path"]}, "read-" + region["id"])]
                )
            ref = next(r["evidence_ref"] for r in previous if "evidence_ref" in r)
            return AIMessage(
                content="",
                tool_calls=[
                    call(
                        "submit_case",
                        {"trigger": "trigger", "behavior": "mechanism", "impact": "impact", "evidence_refs": [ref]},
                        "case",
                    ),
                    finish_unit("b", ref),
                ],
            )

    result = await asyncio.wait_for(ReviewRuntime(store, Model(), PinnedGateway(), Verifier()).run(regions), 6)
    assert result["status"] == "provider_blocked"
    assert all(t.status != "leased" for t in store.list(Task))
    assert all(row["status"] != "checked" for row in store.list_integration_obligations())


async def test_verifier_reopens_finished_b_while_a_still_reads(tmp_path):
    from reviewforge.review_v42.contracts import RevisionConflict

    store, regions = make_run(tmp_path)
    b_rechecked = asyncio.Event()

    class Verifier:
        policy = "v42-independent-1"
        calls = 0

        async def verify(self, case_id, *, task_id, lease_token):
            self.calls += 1
            case = store.get(Case, case_id)
            assert any(
                s.regions and s.regions[0]["id"] == "a" and s.status == "running" for s in store.list(ReviewScope)
            )
            if self.calls == 1:
                for _ in range(500):
                    if store.get(ReviewScope, case.scope_id).status == "reviewed":
                        break
                    await asyncio.sleep(0.001)
                assert store.get(ReviewScope, case.scope_id).status == "reviewed"
                ref = store.put_evidence(
                    b"new source\n",
                    revision="head",
                    side="head",
                    kind="source",
                    path="b.py",
                    metadata={"workspace_digest": "head-snapshot", "status": "available"},
                )
                store.record_verification_observations(
                    task_id, lease_token, case.revision, [], case.evidence_refs + [ref.id]
                )
                raise RevisionConflict("new evidence")
            return [
                store.save_verdict(
                    case_id,
                    case.claims[0]["id"],
                    case.revision,
                    self.policy,
                    "reject",
                    case.evidence_refs,
                    {"reason": "counterexample"},
                    task_id=task_id,
                    lease_token=lease_token,
                )
            ]

    class Model:
        def __init__(self):
            self.b_reopened = False

        def bind_tools(self, tools, **kwargs):
            return self

        async def ainvoke(self, messages):
            await asyncio.sleep(0.001)
            board = json.loads(messages[1].content)
            previous = [json.loads(m.content) for m in messages if m.type == "tool"]
            if board["unit_role"] == "integration":
                if not previous:
                    return AIMessage(
                        content="",
                        tool_calls=[
                            call("read_source", {"path": "a.py"}, "a"),
                            call("read_source", {"path": "b.py"}, "b"),
                        ],
                    )
                refs = [r["evidence_ref"] for r in previous if "evidence_ref" in r]
                return AIMessage(
                    content="",
                    tool_calls=[
                        *receipts(board["integration_obligations"], refs),
                        call("finish_integration", {}, "finish"),
                    ],
                )
            region = board["scope"]["regions"][0]
            key = region["id"]
            if key == "a" and (not previous or not b_rechecked.is_set()):
                return AIMessage(content="", tool_calls=[call("read_source", {"path": "a.py"}, "read-a")])
            if key == "b" and board["cases"] and not previous:
                self.b_reopened = True
                return AIMessage(
                    content="",
                    tool_calls=[call("read_evidence", {"ref": board["cases"][0]["evidence_refs"][-1]}, "new")],
                )
            if not previous:
                return AIMessage(content="", tool_calls=[call("read_source", {"path": region["path"]}, "read-" + key)])
            ref = next(r["evidence_ref"] for r in previous if "evidence_ref" in r)
            if key == "b" and not board["cases"]:
                return AIMessage(
                    content="",
                    tool_calls=[
                        call(
                            "submit_case",
                            {"trigger": "trigger", "behavior": "mechanism", "impact": "impact", "evidence_refs": [ref]},
                            "case",
                        ),
                        finish_unit("b", ref),
                    ],
                )
            if key == "b":
                b_rechecked.set()
            return AIMessage(content="", tool_calls=[finish_unit(key, ref)])

    model, verifier = Model(), Verifier()
    result = await asyncio.wait_for(ReviewRuntime(store, model, PinnedGateway(), verifier).run(regions), 8)
    assert result["status"] == "completed", result
    assert model.b_reopened and b_rechecked.is_set() and verifier.calls == 2


async def test_model_lane_checks_stale_review_before_provider_call(tmp_path):
    from reviewforge.review_v42.contracts import RevisionConflict
    from reviewforge.review_v42.runtime import _FairBoundModel

    store, regions = make_run(tmp_path)
    runtime = ReviewRuntime(store, object(), PinnedGateway(), object())
    scope = runtime.prepare(regions)[0]
    task = store.add(Task(id="leased", run_id="r", kind="review", object_id=scope.id, input_revision=scope.revision))
    task = store.acquire_task(task.id, "owner", 120)

    class Model:
        calls = 0

        async def ainvoke(self, messages):
            self.calls += 1
            return AIMessage(content="")

    model = Model()
    bound = _FairBoundModel(model, runtime, task.id, task.lease_token)
    await runtime._model_lane.acquire()
    waiter = asyncio.create_task(bound.ainvoke([]))
    await asyncio.sleep(0)
    store.update(ReviewScope, scope.id, scope.revision, status="running")
    runtime._model_lane.release()
    with pytest.raises(RevisionConflict):
        await waiter
    assert model.calls == 0


async def test_unfinished_unit_prevents_integration_even_if_sibling_finishes(tmp_path):
    store, regions = make_run(tmp_path)

    class Model:
        def bind_tools(self, tools, **kwargs):
            return self

        async def ainvoke(self, messages):
            board = json.loads(messages[1].content)
            assert board["unit_role"] == "primary"
            previous = [json.loads(m.content) for m in messages if m.type == "tool"]
            region = board["scope"]["regions"][0]
            if region["id"] == "a":
                return AIMessage(
                    content="",
                    tool_calls=[
                        call(
                            "submit_unit_decision",
                            {"checked_region_ids": [], "observations": [], "unresolved_refs": [], "finish": True},
                            "incomplete",
                        )
                    ],
                )
            if not previous:
                return AIMessage(content="", tool_calls=[call("read_source", {"path": "b.py"}, "read")])
            return AIMessage(content="", tool_calls=[finish_unit("b", previous[0]["evidence_ref"])])

    result = await asyncio.wait_for(ReviewRuntime(store, Model(), PinnedGateway(), object()).run(regions), 5)
    assert result["status"] == "partial"
    integration_id = store.get_unit_plan().integration_scope_id
    assert store.get(ReviewScope, integration_id).status == "pending"
    assert all(t.object_id != integration_id for t in store.list(Task) if t.kind == "review")
