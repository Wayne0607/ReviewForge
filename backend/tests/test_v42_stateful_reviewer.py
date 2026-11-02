import asyncio
import json

import pytest
from langchain_core.messages import AIMessage

from reviewforge.review_v42.contracts import Case, EvidenceRef, Question, Run, Task
from reviewforge.review_v42.reviewer import Reviewer
from reviewforge.review_v42.scope import compile_scopes
from reviewforge.review_v42.store import ReviewStore
from reviewforge.review_v42.tools import EvidenceGateway


def review_state(question="What changed?"):
    return {"question": question, "observations": [], "open_questions": [], "next_step": "Read the relevant source"}


def call(name, args, ident="call"):
    return {"name": name, "args": args, "id": ident}


def setup(tmp_path):
    run = Run(id="r", repo="a/b", base="base", head="head", policy="v42-stateful-2")
    store = ReviewStore(tmp_path / "board.db", run)
    scope = compile_scopes(run, [{"id": "one", "path": "a.py", "patch": "+hello"}])[0]
    store.add(scope)
    task = store.add(Task(id="task", run_id="r", kind="review", object_id=scope.id, input_revision=scope.revision))
    task = store.acquire_task(task.id, "tester", 120)
    return store, scope, task


class Gateway:
    def __init__(self):
        self.calls = []
        self.tool_specs = [
            {
                "type": "function",
                "function": {
                    "name": "read_source",
                    "description": "Read a source",
                    "parameters": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                        "required": ["path"],
                        "additionalProperties": False,
                    },
                },
            }
        ]

    async def __call__(self, name, args):
        self.calls.append((name, args.copy()))
        content = f"BODY_{args['path']}_" + "X" * 12000
        return {"content": content, "revision": "head", "side": "head", "kind": "source", "path": args["path"]}


class LongModel:
    def __init__(self):
        self.inputs = []

    def bind_tools(self, tools, **kwargs):
        schemas = [tool for tool in tools if isinstance(tool, dict)]
        for name in ("read_source", "read_evidence"):
            spec = next(x for x in schemas if x["function"]["name"] == name)
            assert "review_state" in spec["function"]["parameters"]["properties"]
            assert "review_state" not in spec["function"]["parameters"]["required"]
        return self

    async def ainvoke(self, messages):
        self.inputs.append(messages.copy())
        i = len(self.inputs) - 1
        if i:
            recent = next(m for m in messages if m.type == "tool")
            result = json.loads(recent.content)
            assert result["content"] == f"BODY_{i - 1}_" + "X" * 12000
            if i > 1:
                assert f"BODY_{i - 2}_" not in json.dumps([m.content for m in messages])
        if i == 24:
            return AIMessage(content="", tool_calls=[call("finish_scope", {"checked_regions": ["one"]})])
        return AIMessage(
            content="",
            additional_kwargs={"reasoning_content": "hidden-provider-thought"},
            tool_calls=[call("read_source", {"path": str(i), "review_state": review_state()})],
        )


async def test_long_review_uses_latest_exchange_and_audits_complete_history(tmp_path):
    store, scope, task = setup(tmp_path)
    model, gateway = LongModel(), Gateway()
    reviewer = Reviewer(store, model, EvidenceGateway(store, gateway), task_id=task.id, lease_token=task.lease_token)
    assert await reviewer.run(scope.id) == "reviewed"
    assert len(gateway.calls) == 24 and all("review_state" not in args for _, args in gateway.calls)
    refs = store.list(EvidenceRef)
    assert len(refs) == 24 and all("review_state" not in ref.metadata["tool_args"] for ref in refs)
    assert store.read_evidence(refs[0].id).startswith(b"BODY_")
    serialized = [json.dumps([m.content for m in messages]) for messages in model.inputs[1:]]
    assert max(len(text) for text in serialized) < 25000
    audit = store.read_turn_audit(task.id)
    assert len(audit) == 100 and {row["phase"] for row in audit} == {"request", "response", "tool:0:call", "exchange"}
    assert "BODY_0_" in json.dumps(audit) and "BODY_23_" in json.dumps(audit)
    assert "hidden-provider-thought" not in json.dumps(audit)
    assert store.get(type(scope), scope.id).working_state == review_state()
    assert store.load_task_session(task.id)


class ScriptedModel:
    def __init__(self, turns):
        self.turns, self.inputs = iter(turns), []

    def bind_tools(self, tools, **kwargs):
        return self

    async def ainvoke(self, messages):
        self.inputs.append(messages.copy())
        return AIMessage(content="", tool_calls=next(self.turns))


async def test_missing_and_conflicting_state_feedback_is_recoverable(tmp_path):
    store, scope, task = setup(tmp_path)
    gateway = Gateway()
    model = ScriptedModel(
        [
            [call("read_source", {"path": "missing"})],
            [
                call("read_source", {"path": "a", "review_state": review_state()}, "a"),
                call("read_source", {"path": "b", "review_state": review_state("other")}, "b"),
            ],
            [call("read_source", {"path": "invalid", "review_state": {}})],
            [call("read_source", {"path": "ok", "review_state": review_state()})],
            [call("finish_scope", {"checked_regions": ["one"]})],
        ]
    )
    reviewer = Reviewer(store, model, EvidenceGateway(store, gateway), task_id=task.id, lease_token=task.lease_token)
    assert await reviewer.run(scope.id) == "reviewed"
    assert gateway.calls == [("read_source", {"path": "ok"})]
    assert not store.list(Question)
    assert "requires review_state" in next(m.content for m in model.inputs[1] if m.type == "tool")
    assert all("conflicting review_state" in m.content for m in model.inputs[2] if m.type == "tool")
    assert "invalid review_state" in next(m.content for m in model.inputs[3] if m.type == "tool")


async def test_semantic_case_can_submit_without_read_state(tmp_path):
    store, scope, task = setup(tmp_path)
    ref = store.put_evidence(b"source", revision="head", side="head", kind="source", path="a.py")
    model = ScriptedModel(
        [
            [
                call(
                    "submit_case",
                    {"trigger": "when x", "behavior": "fails", "impact": "loss", "evidence_refs": [ref.id]},
                )
            ],
            [call("finish_scope", {"checked_regions": ["one"]})],
        ]
    )
    reviewer = Reviewer(store, model, EvidenceGateway(store, Gateway()), task_id=task.id, lease_token=task.lease_token)
    assert await reviewer.run(scope.id) == "reviewed"
    assert len(store.list(Case)) == 1


async def test_cancelled_session_restores_latest_complete_exchange(tmp_path):
    store, scope, task = setup(tmp_path)
    gateway = Gateway()

    class Interrupted(ScriptedModel):
        async def ainvoke(self, messages):
            if self.inputs:
                raise asyncio.CancelledError()
            return await super().ainvoke(messages)

    first = Interrupted([[call("read_source", {"path": "saved", "review_state": review_state()})]])
    reviewer = Reviewer(store, first, EvidenceGateway(store, gateway), task_id=task.id, lease_token=task.lease_token)
    with pytest.raises(asyncio.CancelledError):
        await reviewer.run(scope.id)
    assert store.load_task_session(task.id)
    resume = ScriptedModel([[call("finish_scope", {"checked_regions": ["one"]})]])
    reviewer = Reviewer(store, resume, EvidenceGateway(store, gateway), task_id=task.id, lease_token=task.lease_token)
    assert await reviewer.run(scope.id) == "reviewed"
    latest = json.loads(next(m.content for m in resume.inputs[0] if m.type == "tool"))
    assert latest["content"] == "BODY_saved_" + "X" * 12000
    assert len(gateway.calls) == 1
    assert "BODY_saved_" in json.dumps(store.read_turn_audit(task.id))


async def test_malformed_native_arguments_are_audited_without_execution(tmp_path):
    store, scope, task = setup(tmp_path)
    gateway = Gateway()
    raw = '{"path":"first","reasoning":"business-value","thinking":"business-value","path":"second"}'

    class Malformed(ScriptedModel):
        async def ainvoke(self, messages):
            self.inputs.append(messages.copy())
            if len(self.inputs) == 1:
                return AIMessage(
                    content="",
                    tool_calls=[call("read_source", {"path": "second"})],
                    additional_kwargs={
                        "reviewforge_raw_tool_calls": [
                            {
                                "id": "call",
                                "type": "function",
                                "function": {"name": "read_source", "arguments": raw},
                            }
                        ],
                        "reasoning_content": "private",
                    },
                )
            return AIMessage(content="", tool_calls=[call("finish_scope", {"checked_regions": ["one"]})])

    model = Malformed([])
    reviewer = Reviewer(store, model, EvidenceGateway(store, gateway), task_id=task.id, lease_token=task.lease_token)
    assert await reviewer.run(scope.id) == "reviewed"
    assert not gateway.calls
    native = next(row for row in store.read_turn_audit(task.id) if row["phase"] == "native")
    assert native["messages"][0]["raw_tool_calls"][0]["function"]["arguments"] == raw
    assert "private" not in json.dumps(native)


async def test_business_arguments_named_reasoning_and_thinking_are_audited_intact(tmp_path):
    store, scope, task = setup(tmp_path)
    gateway = Gateway()
    args = {
        "path": "example",
        "review_state": review_state(),
        "reasoning": "visible business field",
        "thinking": {"reasoning": "nested business field"},
    }
    native_args = json.dumps(args)

    class NativeModel(ScriptedModel):
        async def ainvoke(self, messages):
            self.inputs.append(messages.copy())
            if len(self.inputs) == 1:
                return AIMessage(
                    content=[
                        {"type": "reasoning", "reasoning": "private provider analysis"},
                        {"type": "text", "text": "Visible explanation"},
                    ],
                    tool_calls=[call("read_source", args)],
                    additional_kwargs={
                        "reviewforge_raw_tool_calls": [
                            {
                                "id": "call",
                                "type": "function",
                                "function": {"name": "read_source", "arguments": native_args},
                            }
                        ],
                        "reasoning_content": "private provider analysis",
                    },
                )
            return AIMessage(content="", tool_calls=[call("finish_scope", {"checked_regions": ["one"]})])

    reviewer = Reviewer(
        store, NativeModel([]), EvidenceGateway(store, gateway), task_id=task.id, lease_token=task.lease_token
    )
    assert await reviewer.run(scope.id) == "reviewed"
    assert gateway.calls[0][1] == {key: value for key, value in args.items() if key != "review_state"}
    audit = store.read_turn_audit(task.id)
    response = next(row for row in audit if row["phase"] == "response")
    assert response["messages"][0]["data"]["tool_calls"][0]["args"] == args
    native = next(row for row in audit if row["phase"] == "native")
    assert native["messages"][0]["raw_tool_calls"][0]["function"]["arguments"] == native_args
    assert "private provider analysis" not in json.dumps(audit)
    ref = store.list(EvidenceRef)[0]
    assert ref.metadata["tool_args"]["thinking"] == {"reasoning": "nested business field"}


async def test_cancel_during_read_keeps_prior_exchange_at_new_state_revision(tmp_path):
    store, scope, task = setup(tmp_path)

    class InterruptedGateway(Gateway):
        async def __call__(self, name, args):
            if args["path"] == "cancelled":
                raise asyncio.CancelledError()
            return await super().__call__(name, args)

    gateway = InterruptedGateway()
    model = ScriptedModel(
        [
            [call("read_source", {"path": "saved", "review_state": review_state()})],
            [call("read_source", {"path": "cancelled", "review_state": review_state("New question")})],
        ]
    )
    reviewer = Reviewer(store, model, EvidenceGateway(store, gateway), task_id=task.id, lease_token=task.lease_token)
    with pytest.raises(asyncio.CancelledError):
        await reviewer.run(scope.id)
    resume = ScriptedModel([[call("finish_scope", {"checked_regions": ["one"]})]])
    reviewer = Reviewer(store, resume, EvidenceGateway(store, gateway), task_id=task.id, lease_token=task.lease_token)
    assert await reviewer.run(scope.id) == "reviewed"
    latest = json.loads(next(m.content for m in resume.inputs[0] if m.type == "tool"))
    assert latest["content"] == "BODY_saved_" + "X" * 12000
    assert "New question" in resume.inputs[0][1].content


async def test_failed_read_with_changing_notes_is_not_progress(tmp_path):
    store, scope, task = setup(tmp_path)

    class Unavailable(Gateway):
        async def __call__(self, name, args):
            return {
                "content": "workspace closed",
                "revision": "head",
                "side": "head",
                "kind": "failure",
                "metadata": {"status": "unavailable"},
            }

    model = ScriptedModel(
        [[call("read_source", {"path": "same", "review_state": review_state(str(i))})] for i in range(6)]
    )
    reviewer = Reviewer(
        store, model, EvidenceGateway(store, Unavailable()), task_id=task.id, lease_token=task.lease_token
    )
    assert await reviewer.run(scope.id) == "partial"
    assert len(store.list(Question)) == 1
    assert len(model.inputs) < 6
