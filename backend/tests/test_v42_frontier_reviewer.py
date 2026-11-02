import asyncio
import json

import pytest
from langchain_core.messages import AIMessage
from test_v42_stateful_reviewer import Gateway, ScriptedModel, call

from reviewforge.review_v42.contracts import Question, Run, Task
from reviewforge.review_v42.reviewer import QuestionAssistant, Reviewer
from reviewforge.review_v42.scope import compile_scopes
from reviewforge.review_v42.store import ReviewStore
from reviewforge.review_v42.tools import EvidenceGateway


def setup_frontier(tmp_path):
    run = Run(id="r", repo="a/b", base="base", head="head", policy="v42-frontier-4")
    store = ReviewStore(tmp_path / "board.db", run)
    scope = compile_scopes(run, [{"id": "one", "path": "a.py"}])[0]
    store.add(scope)
    task = store.add(Task(id="task", run_id="r", kind="review", object_id=scope.id, input_revision=scope.revision))
    task = store.acquire_task(task.id, "tester", 120)
    return store, scope, task


def header(messages):
    return json.loads(messages[2].content)


async def test_alternating_batches_keep_unique_originals_and_native_exchange(tmp_path):
    store, scope, task = setup_frontier(tmp_path)
    gateway = Gateway()

    class Model(ScriptedModel):
        async def ainvoke(self, messages):
            self.inputs.append(messages.copy())
            i = len(self.inputs)
            assert [m.type for m in messages[:3]] == ["system", "human", "human"]
            if i > 1:
                assert messages[3].type == "ai" and all(m.type == "tool" for m in messages[4:])
                previous = {json.loads(m.content)["frontier_entry_id"] for m in messages[4:]}
                assert previous.isdisjoint({item["id"] for item in header(messages)["entries"]})
            if i == 1:
                paths = ["A", "B"]
            elif i == 2:
                assert not header(messages)["entries"]
                paths = ["C", "D"]
            elif i == 3:
                assert {x["path"] for x in header(messages)["entries"]} == {"A", "B"}
                paths = ["A", "B"]
            else:
                assert {x["path"] for x in header(messages)["entries"]} == {"C", "D"}
                assert all(x["content"] == f"BODY_{x['path']}_" + "X" * 12000 for x in header(messages)["entries"])
                return AIMessage(content="", tool_calls=[call("finish_scope", {"checked_regions": ["one"]})])
            return AIMessage(content="", tool_calls=[call("read_source", {"path": p}, p) for p in paths])

    model = Model([])
    reviewer = Reviewer(store, model, EvidenceGateway(store, gateway), task_id=task.id, lease_token=task.lease_token)
    assert await reviewer.run(scope.id) == "reviewed"
    assert len(store.list_frontier(task.id)) == 4
    assert [args["path"] for _, args in gateway.calls] == ["A", "B", "C", "D", "A", "B"]
    audit = store.read_turn_audit(task.id)
    assert len([row for row in audit if row["phase"] == "exchange"]) == 4


async def test_invalid_reference_and_page_are_correctable_without_false_question(tmp_path):
    store, scope, task = setup_frontier(tmp_path)
    ref = store.put_evidence(b"one\ntwo\nthree\n", revision="head", side="head", kind="source", path="a.py")
    model = ScriptedModel(
        [
            [call("read_evidence", {"ref": "missing"})],
            [call("read_evidence", {"ref": ref.id, "start_line": 9, "end_line": 10})],
            [call("read_evidence", {"ref": ref.id, "start_line": 2, "end_line": 2})],
            [call("read_source", {"path": "other"})],
            [call("finish_scope", {"checked_regions": ["one"]})],
        ]
    )
    reviewer = Reviewer(store, model, EvidenceGateway(store, Gateway()), task_id=task.id, lease_token=task.lease_token)
    assert await reviewer.run(scope.id) == "reviewed"
    assert not store.list(Question)
    assert "unknown evidence reference" in next(m.content for m in model.inputs[1] if m.type == "tool")
    assert "outside the stored evidence" in next(m.content for m in model.inputs[2] if m.type == "tool")
    retained = header(model.inputs[4])["entries"]
    page = next(item for item in retained if item["path"] == "a.py")
    assert page["content"] == "two\n" and page["is_page"]
    assert (page["start_line"], page["end_line"]) == (2, 2)
    assert store.read_evidence(ref.id) == b"one\ntwo\nthree\n"


async def test_known_evidence_blob_failure_remains_unresolved(tmp_path):
    store, scope, task = setup_frontier(tmp_path)
    ref = store.put_evidence(b"original", revision="head", side="head", kind="source", path="a.py")
    (store.blob_dir / ref.content_hash).write_bytes(b"corrupt")
    model = ScriptedModel(
        [
            [call("read_evidence", {"ref": ref.id})],
            [call("finish_scope", {"checked_regions": ["one"]})],
        ]
    )
    reviewer = Reviewer(store, model, EvidenceGateway(store, Gateway()), task_id=task.id, lease_token=task.lease_token)
    assert await reviewer.run(scope.id) == "partial"
    question = store.list(Question)[0]
    assert question.owner == "tool_controller" and question.status == "unresolved"
    assert not store.list_frontier(task.id)


async def test_invalid_page_is_rejected_before_attempting_corrupt_blob(tmp_path):
    store, scope, task = setup_frontier(tmp_path)
    ref = store.put_evidence(b"original\n", revision="head", side="head", kind="source", path="a.py")
    (store.blob_dir / ref.content_hash).write_bytes(b"corrupt")

    class Model(ScriptedModel):
        async def ainvoke(self, messages):
            if len(self.inputs) == 1:
                assert not store.list(Question)
            return await super().ainvoke(messages)

    model = Model(
        [
            [call("read_evidence", {"ref": ref.id, "start_line": 0, "end_line": 1})],
            [call("read_evidence", {"ref": ref.id})],
            [call("finish_scope", {"checked_regions": ["one"]})],
        ]
    )
    reviewer = Reviewer(store, model, EvidenceGateway(store, Gateway()), task_id=task.id, lease_token=task.lease_token)
    assert await reviewer.run(scope.id) == "partial"
    assert "valid explicit page" in next(m.content for m in model.inputs[1] if m.type == "tool")
    assert store.list(Question)[0].owner == "tool_controller"


async def test_release_then_reread_repins_original(tmp_path):
    store, scope, task = setup_frontier(tmp_path)
    gateway = Gateway()

    class Model(ScriptedModel):
        async def ainvoke(self, messages):
            self.inputs.append(messages.copy())
            i = len(self.inputs)
            if i == 1:
                return AIMessage(content="", tool_calls=[call("read_source", {"path": "A"})])
            if i == 2:
                receipt = json.loads(next(m.content for m in messages if m.type == "tool"))
                self.entry, self.ref = receipt["frontier_entry_id"], receipt["evidence_ref"]
                return AIMessage(
                    content="",
                    tool_calls=[
                        call(
                            "release_frontier",
                            {
                                "entry_ids": [self.entry],
                                "reason": "resolved",
                                "expected_revision": header(messages)["frontier_revision"],
                            },
                        )
                    ],
                )
            if i == 3:
                assert not header(messages)["entries"] and not store.list_frontier(task.id)
                assert store.read_evidence(self.ref).startswith(b"BODY_A_")
                return AIMessage(content="", tool_calls=[call("read_evidence", {"ref": self.ref})])
            receipt = json.loads(next(m.content for m in messages if m.type == "tool"))
            assert receipt["frontier_entry_id"] == self.entry
            assert store.list_frontier(task.id)[0].id == self.entry
            return AIMessage(content="", tool_calls=[call("finish_scope", {"checked_regions": ["one"]})])

    model = Model([])
    reviewer = Reviewer(store, model, EvidenceGateway(store, gateway), task_id=task.id, lease_token=task.lease_token)
    assert await reviewer.run(scope.id) == "reviewed"


async def test_frontier_exact_session_recovers_after_cancel(tmp_path):
    store, scope, task = setup_frontier(tmp_path)
    gateway = Gateway()

    class Interrupted(ScriptedModel):
        async def ainvoke(self, messages):
            if self.inputs:
                raise asyncio.CancelledError()
            return await super().ainvoke(messages)

    model = Interrupted([[call("read_source", {"path": "A"}), call("read_source", {"path": "B"}, "b")]])
    reviewer = Reviewer(store, model, EvidenceGateway(store, gateway), task_id=task.id, lease_token=task.lease_token)
    with pytest.raises(asyncio.CancelledError):
        await reviewer.run(scope.id)
    resume = ScriptedModel([[call("finish_scope", {"checked_regions": ["one"]})]])
    reviewer = Reviewer(store, resume, EvidenceGateway(store, gateway), task_id=task.id, lease_token=task.lease_token)
    assert await reviewer.run(scope.id) == "reviewed"
    assert [m.type for m in resume.inputs[0][-3:]] == ["ai", "tool", "tool"]
    assert len(store.list_frontier(task.id)) == 2 and len(gateway.calls) == 2


async def test_identical_bytes_from_different_paths_or_sides_do_not_merge(tmp_path):
    store, scope, task = setup_frontier(tmp_path)

    class SameBytes(Gateway):
        async def __call__(self, name, args):
            self.calls.append((name, args))
            side = args.get("side", "head")
            return {"content": "same\n", "revision": side, "side": side, "kind": "source", "path": args["path"]}

    model = ScriptedModel(
        [
            [
                call("read_source", {"path": "a", "side": "head"}, "a"),
                call("read_source", {"path": "b", "side": "head"}, "b"),
                call("read_source", {"path": "a", "side": "base"}, "c"),
            ],
            [call("finish_scope", {"checked_regions": ["one"]})],
        ]
    )
    reviewer = Reviewer(
        store, model, EvidenceGateway(store, SameBytes()), task_id=task.id, lease_token=task.lease_token
    )
    assert await reviewer.run(scope.id) == "reviewed"
    assert {(entry.side, entry.path) for entry in store.list_frontier(task.id)} == {
        ("head", "a"),
        ("head", "b"),
        ("base", "a"),
    }


async def test_cancel_mid_batch_retains_delivered_source_without_fake_exchange(tmp_path):
    store, scope, task = setup_frontier(tmp_path)

    class Interrupted(Gateway):
        async def __call__(self, name, args):
            if args["path"] == "B":
                raise asyncio.CancelledError()
            return await super().__call__(name, args)

    gateway = Interrupted()
    model = ScriptedModel([[call("read_source", {"path": "A"}, "a"), call("read_source", {"path": "B"}, "b")]])
    reviewer = Reviewer(store, model, EvidenceGateway(store, gateway), task_id=task.id, lease_token=task.lease_token)
    with pytest.raises(asyncio.CancelledError):
        await reviewer.run(scope.id)
    resumed = ScriptedModel([[call("finish_scope", {"checked_regions": ["one"]})]])
    reviewer = Reviewer(store, resumed, EvidenceGateway(store, gateway), task_id=task.id, lease_token=task.lease_token)
    assert await reviewer.run(scope.id) == "reviewed"
    assert [m.type for m in resumed.inputs[0]] == ["system", "human", "human"]
    assert [item["path"] for item in header(resumed.inputs[0])["entries"]] == ["A"]
    assert len(gateway.calls) == 1


async def test_context_capacity_is_partial_until_frontier_changes(tmp_path):
    store, scope, task = setup_frontier(tmp_path)

    class CapacityError(Exception):
        code = "context_length_exceeded"

    class Model(ScriptedModel):
        async def ainvoke(self, messages):
            if self.inputs:
                raise CapacityError()
            return await super().ainvoke(messages)

    model = Model([[call("read_source", {"path": "A"})]])
    reviewer = Reviewer(store, model, EvidenceGateway(store, Gateway()), task_id=task.id, lease_token=task.lease_token)
    assert await reviewer.run(scope.id) == "partial" and reviewer.error == "context_length_exceeded"
    entry = store.list_frontier(task.id)[0]
    store.release_frontier_for_task(
        task.id, task.lease_token, [entry.id], "no longer needed", expected_revision=store.frontier_revision(task.id)
    )
    resumed = ScriptedModel([[call("finish_scope", {"checked_regions": ["one"]})]])
    reviewer = Reviewer(
        store, resumed, EvidenceGateway(store, Gateway()), task_id=task.id, lease_token=task.lease_token
    )
    assert await reviewer.run(scope.id) == "reviewed"
    assert header(resumed.inputs[0])["entries"] == []
    assert [m.type for m in resumed.inputs[0][-2:]] == ["ai", "tool"]


async def test_helper_can_correct_unknown_evidence_reference(tmp_path):
    store, scope, task = setup_frontier(tmp_path)
    ref = store.put_evidence(b"real source", revision="head", side="head", kind="source", path="a.py")
    question = store.add_for_task(
        task.id, task.lease_token, Question(id="q", run_id="r", scope_id=scope.id, statement="Why?")
    )
    helper = store.add_for_task(
        task.id,
        task.lease_token,
        Task(id="helper", run_id="r", kind="question", object_id=question.id, input_revision=0),
    )
    helper = store.acquire_task(helper.id, "assistant", 120)
    model = ScriptedModel(
        [
            [call("read_evidence", {"ref": "missing"})],
            [call("read_evidence", {"ref": ref.id})],
            [
                call(
                    "answer_question", {"status": "answered", "answer": "source supports it", "evidence_refs": [ref.id]}
                )
            ],
        ]
    )
    result = await QuestionAssistant(store, model, EvidenceGateway(store, Gateway())).run(helper)
    assert result["status"] == "answered" and result["evidence_refs"] == [ref.id]
