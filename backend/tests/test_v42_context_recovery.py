import json

import pytest
from langchain_core.messages import ToolMessage
from test_v42_reviewer import Model, ai, call, setup, source

from reviewforge.review_v42.contracts import EvidenceRef, Run
from reviewforge.review_v42.reviewer import Reviewer
from reviewforge.review_v42.store import ReviewStore
from reviewforge.review_v42.tools import EvidenceGateway


class ContextError(Exception):
    code = "context_length_exceeded"
    status_code = 400


class BadRequestError(Exception):
    code = "invalid_argument"
    status_code = 400


async def test_context_overflow_rebuilds_and_preserves_unlinked_evidence(tmp_path):
    store, scope = setup(tmp_path)
    store.update(
        type(scope), scope.id, scope.revision, regions=[{"id": "one", "path": "a.py", "patch": "VERY LARGE PATCH"}]
    )

    async def gateway(name, args):
        return {
            "content": "one\ntwo\nthree\n",
            "revision": "head",
            "side": "head",
            "kind": "source",
            "path": "a.py",
            "start_line": 1,
            "end_line": 3,
        }

    class RecoveryModel:
        def __init__(self):
            self.calls = 0

        def bind_tools(self, tools, **kwargs):
            return self

        async def ainvoke(self, messages):
            self.calls += 1
            if self.calls == 1:
                state = json.loads(messages[1].content)
                assert "patch" not in state["scope"]["regions"][0]
                return ai(call("read_diff", {"path": "a.py"}))
            if self.calls == 2:
                assert any(isinstance(m, ToolMessage) and "three" in m.content for m in messages)
                raise ContextError()
            if self.calls == 3:
                assert not any(isinstance(m, ToolMessage) for m in messages)
                state = json.loads(messages[1].content)
                entries = state["evidence"]
                assert len(entries) == 1
                assert entries[0]["metadata"]["tool_args"] == {"path": "a.py"}
                self.ref = entries[0]["id"]
                return ai(call("read_evidence", {"ref": self.ref, "start_line": 2, "end_line": 2}))
            page = json.loads(messages[-1].content)
            assert page["content"] == "two\n" and page["next_line"] == 3
            return ai(call("finish_scope", {"checked_regions": ["one"]}))

    model = RecoveryModel()
    assert await Reviewer(store, model, EvidenceGateway(store, gateway)).run(scope.id) == "reviewed"
    assert store.read_evidence(model.ref) == b"one\ntwo\nthree\n"


async def test_rebuilt_input_overflow_is_partial_without_repeated_retry(tmp_path):
    store, scope = setup(tmp_path)
    model = Model([ai(call("read_diff", {"path": "a.py"})), ContextError(), ContextError(), ContextError()])
    assert await Reviewer(store, model, EvidenceGateway(store, source)).run(scope.id) == "partial"
    assert len(model.inputs) == 3
    assert store.list(EvidenceRef)


async def test_plain_400_is_not_context_recovery(tmp_path):
    store, scope = setup(tmp_path)
    model = Model([BadRequestError(), ai(call("finish_scope", {"checked_regions": ["one"]}))])
    assert await Reviewer(store, model, EvidenceGateway(store, source)).run(scope.id) == "error"
    assert len(model.inputs) == 1


async def test_alternating_same_actions_and_receipts_stall(tmp_path):
    store, scope = setup(tmp_path)
    a, b = call("read_diff", {"path": "a"}), call("read_diff", {"path": "b"})
    model = Model([ai(a), ai(b), ai(a), ai(b)])
    assert await Reviewer(store, model, EvidenceGateway(store, source)).run(scope.id) == "stalled"
    assert len(model.inputs) == 4


async def test_evidence_page_boundaries_and_full_reread(tmp_path):
    store = ReviewStore(tmp_path / "board.sqlite", Run(id="r", repo="x/y", base="base", head="head"))
    ref = store.put_evidence(
        b"a\nb\nc", revision="head", side="head", kind="source", path="a.py", start_line=10, end_line=12
    )
    gateway = EvidenceGateway(store, source)
    page = await gateway.execute("read_evidence", {"ref": ref.id, "start_line": 11, "end_line": 20})
    assert (page["content"], page["start_line"], page["end_line"], page["next_line"]) == ("b\nc", 11, 12, None)
    assert (await gateway.execute("read_evidence", {"ref": ref.id}))["content"] == "a\nb\nc"
    with pytest.raises(ValueError):
        await gateway.execute("read_evidence", {"ref": ref.id, "start_line": 9, "end_line": 10})


async def test_question_assistant_recovers_and_rereads_unlinked_source(tmp_path):
    from reviewforge.review_v42.contracts import Question, Task
    from reviewforge.review_v42.reviewer import QuestionAssistant

    store, scope = setup(tmp_path)
    question = Question(id="q", run_id="r", scope_id=scope.id, statement="What does the source say?")
    store.add(question)
    task = Task(
        id="assistant",
        run_id="r",
        kind="question",
        object_id=question.id,
        input_revision=question.revision,
        instruction="inspect the fixed source",
    )
    store.add(task)
    task = store.acquire_task(task.id, "helper", 60)

    class AssistantModel:
        def __init__(self):
            self.calls = 0

        def bind_tools(self, tools, **kwargs):
            return self

        async def ainvoke(self, messages):
            self.calls += 1
            if self.calls == 1:
                return ai(call("read_diff", {"path": "a.py"}))
            if self.calls == 2:
                raise ContextError()
            if self.calls == 3:
                state = json.loads(messages[1].content)
                assert state["reason"] == "inspect the fixed source"
                self.ref = state["evidence_directory"][0]["id"]
                return ai(call("read_evidence", {"ref": self.ref}))
            return ai(call("answer_question", {"status": "answered", "answer": "source", "evidence_refs": [self.ref]}))

    model = AssistantModel()
    outcome = await QuestionAssistant(store, model, EvidenceGateway(store, source)).run(task)
    assert outcome["status"] == "answered"
    assert store.get(Question, question.id).evidence_refs == [model.ref]


def test_only_explicit_provider_error_code_enables_capacity_recovery():
    from reviewforge.review_v42.context_recovery import is_context_length_exceeded

    class StructuredError(Exception):
        body = {"error": {"code": "context_length_exceeded"}}

    class MessageOnlyError(Exception):
        body = {"error": {"code": "invalid_argument", "message": "context_length_exceeded"}}

    assert is_context_length_exceeded(StructuredError())
    assert not is_context_length_exceeded(MessageOnlyError())


async def test_new_timestamps_and_temporary_images_are_not_progress(tmp_path):
    store, scope = setup(tmp_path)
    count = 0

    async def ephemeral(name, args):
        nonlocal count
        count += 1
        return {
            "content": json.dumps({"exit_code": 1, "image_id": f"temporary-{count}"}),
            "revision": "head",
            "side": "head",
            "kind": "test",
            "metadata": {"status": "available", "timestamp": count},
        }

    action = call("run_check", {"test_spec": "existing test"})
    assert (
        await Reviewer(store, Model([ai(action), ai(action), ai(action)]), EvidenceGateway(store, ephemeral)).run(
            scope.id
        )
        == "stalled"
    )
    assert count == 3
    assert len(store.list(EvidenceRef)) == 3


async def test_new_distinct_observation_breaks_a_repetition_cycle(tmp_path):
    store, scope = setup(tmp_path)
    a, b, c = (call("read_diff", {"path": path}) for path in ("a", "b", "c"))
    model = Model([ai(a), ai(b), ai(a), ai(c), ai(b), ai(call("finish_scope", {"checked_regions": ["one"]}))])
    assert await Reviewer(store, model, EvidenceGateway(store, source)).run(scope.id) == "reviewed"


async def test_resume_does_not_retry_a_persisted_oversized_projection(tmp_path):
    from reviewforge.review_v42.runtime import ReviewRuntime

    store = ReviewStore(tmp_path / "board.sqlite", Run(id="r", repo="x/y", base="base", head="head"))
    regions = [{"id": "one", "path": "a.py"}]
    model = Model([ai(call("read_diff", {"path": "a.py"})), ContextError(), ContextError()])
    first = await ReviewRuntime(store, model, source, object()).run(regions)
    assert first["status"] == "partial" and len(model.inputs) == 3

    class NeverInvoke:
        def bind_tools(self, tools, **kwargs):
            return self

        async def ainvoke(self, messages):
            raise AssertionError("identical oversized context must not be requested again")

    resumed = await ReviewRuntime(store, NeverInvoke(), source, object()).run(regions)
    assert resumed["status"] == "partial"
    assert any("context_length_exceeded" in error for error in resumed["errors"])


async def test_cancellation_during_tool_does_not_restore_incomplete_native_exchange(tmp_path):
    import asyncio

    from langchain_core.messages import AIMessage

    from reviewforge.review_v42.runtime import ReviewRuntime

    store = ReviewStore(tmp_path / "board.sqlite", Run(id="r", repo="x/y", base="base", head="head"))
    regions = [{"id": "one", "path": "a.py"}]
    entered = asyncio.Event()

    async def interrupted(name, args):
        entered.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(
        ReviewRuntime(store, Model([ai(call("read_diff", {"path": "a.py"}))]), interrupted, object()).run(regions)
    )
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    class ResumeModel:
        def bind_tools(self, tools, **kwargs):
            return self

        async def ainvoke(self, messages):
            assert not any(isinstance(message, AIMessage) and message.tool_calls for message in messages)
            return ai(call("finish_scope", {"checked_regions": ["one"]}))

    resumed = await ReviewRuntime(store, ResumeModel(), source, object()).run(regions)
    assert resumed["status"] == "completed"
