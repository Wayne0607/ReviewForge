import asyncio
import copy
import json

import pytest
from langchain_core.messages import AIMessage

from reviewforge.review_v42.contracts import Case, EvidenceRef, Question, Run, Task
from reviewforge.review_v42.reviewer import Reviewer, _protocol_failure
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
    assert "first read call only" in model.inputs[0][0].content
    assert "omit review_state from the other reads" in model.inputs[0][0].content
    assert "requires review_state" in next(m.content for m in model.inputs[1] if m.type == "tool")
    assert all(
        "keep one on the first read and remove the others" in m.content for m in model.inputs[2] if m.type == "tool"
    )
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
    assert len(model.inputs) == 2 and reviewer.error == "repeated_failed_batch"
    exchanges = [row for row in store.read_turn_audit(task.id) if row["phase"] == "exchange"]
    assert len(exchanges) == 2 and all(len(row["messages"]) == 2 for row in exchanges)
    assert store.load_task_session(task.id)


async def test_interleaved_successful_rereads_do_not_abort_later_new_batch_actions(tmp_path):
    store, scope, task = setup(tmp_path)
    raw = Gateway()
    diff_spec = copy.deepcopy(raw.tool_specs[0])
    diff_spec["function"]["name"] = "read_diff"
    raw.tool_specs.append(diff_spec)
    gateway = EvidenceGateway(store, raw)
    refs = {
        name: store.put_evidence(name.encode(), revision="head", side="head", kind="source", path=name).id
        for name in ("A", "B", "C")
    }
    await gateway.execute("read_diff", {"path": "P"})
    raw.calls.clear()
    state = review_state()
    model = ScriptedModel(
        [
            [
                call("read_evidence", {"ref": refs["A"], "review_state": state}, "first_a"),
                call("read_evidence", {"ref": refs["B"]}, "first_b"),
                call("read_diff", {"path": "P"}, "first_p"),
            ],
            [
                call("read_evidence", {"ref": refs["A"], "review_state": state}, "again_a"),
                call("read_evidence", {"ref": refs["C"]}, "new_c"),
                call("read_evidence", {"ref": refs["B"]}, "again_b"),
            ],
            [
                call("read_diff", {"path": "P", "review_state": state}, "again_p"),
                call("read_diff", {"path": "Q"}, "new_q"),
                call("read_diff", {"path": "R"}, "new_r"),
                call("read_diff", {"path": "S"}, "new_s"),
            ],
            [call("finish_scope", {"checked_regions": ["one"]})],
        ]
    )
    reviewer = Reviewer(store, model, gateway, task_id=task.id, lease_token=task.lease_token)
    assert await reviewer.run(scope.id) == "reviewed"
    assert [args["path"] for name, args in raw.calls if name == "read_diff"] == ["P", "P", "Q", "R", "S"]
    third_exchange = [m for m in model.inputs[3] if m.type == "tool"]
    assert [m.tool_call_id for m in third_exchange] == ["again_p", "new_q", "new_r", "new_s"]
    assert all(json.loads(m.content)["content"].startswith("BODY_") for m in third_exchange)
    exchanges = [row for row in store.read_turn_audit(task.id) if row["phase"] == "exchange"]
    assert len(exchanges) == 4 and len(exchanges[2]["messages"]) == 5


async def test_different_failure_batches_do_not_accumulate_as_a_loop(tmp_path):
    store, scope, task = setup(tmp_path)

    class SometimesUnavailable(Gateway):
        async def __call__(self, name, args):
            if args["path"] == "bad":
                return {
                    "content": "workspace unavailable",
                    "revision": "head",
                    "side": "head",
                    "kind": "failure",
                    "metadata": {"status": "unavailable"},
                }
            return await super().__call__(name, args)

    gateway = SometimesUnavailable()
    model = ScriptedModel(
        [
            [call("read_source", {"path": "bad", "review_state": review_state("one")})],
            [call("read_source", {"path": "new", "review_state": review_state("two")})],
            [call("read_source", {"path": "bad", "review_state": review_state("three")})],
            [call("finish_scope", {"checked_regions": ["one"]})],
        ]
    )
    reviewer = Reviewer(store, model, EvidenceGateway(store, gateway), task_id=task.id, lease_token=task.lease_token)
    assert await reviewer.run(scope.id) == "partial"
    assert len(model.inputs) == 4 and reviewer.error is None
    assert len(store.list(Question)) == 1


async def test_same_failed_batch_waits_until_all_sibling_receipts_are_audited(tmp_path):
    store, scope, task = setup(tmp_path)

    class Unavailable(Gateway):
        async def __call__(self, name, args):
            return {
                "content": "workspace unavailable",
                "revision": "head",
                "side": "head",
                "kind": "failure",
                "metadata": {"status": "unavailable"},
            }

    model = ScriptedModel(
        [
            [
                call("read_source", {"path": "a", "review_state": review_state(str(i))}, "a"),
                call("read_source", {"path": "b"}, "b"),
            ]
            for i in range(3)
        ]
    )
    reviewer = Reviewer(
        store, model, EvidenceGateway(store, Unavailable()), task_id=task.id, lease_token=task.lease_token
    )
    assert await reviewer.run(scope.id) == "partial"
    assert len(model.inputs) == 2 and reviewer.error == "repeated_failed_batch"
    exchanges = [row for row in store.read_turn_audit(task.id) if row["phase"] == "exchange"]
    assert len(exchanges) == 2 and all(len(row["messages"]) == 3 for row in exchanges)
    assert len(store.list(Question)) == 2


@pytest.mark.parametrize("failure", ["missing", "conflict", "invalid_state", "unknown_tool", "invalid_params"])
async def test_repeated_identical_protocol_or_parameter_failures_stop_after_full_batch(tmp_path, failure):
    store, scope, task = setup(tmp_path)
    gateway = Gateway()

    def batch(index):
        if failure == "missing":
            return [call("read_source", {"path": "a"}, "a"), call("read_source", {"path": "b"}, "b")]
        if failure == "conflict":
            return [
                call("read_source", {"path": "a", "review_state": review_state(f"note {index}")}, "a"),
                call("read_source", {"path": "b", "review_state": review_state(f"other {index}")}, "b"),
            ]
        if failure == "invalid_state":
            return [call("read_source", {"path": "a", "review_state": {"question": str(index)}}, "a")]
        if failure == "unknown_tool":
            return [call("unknown_action", {"request": "same"}, "a")]
        return [call("save_question", {"statement": ""}, "a")]

    model = ScriptedModel([batch(i) for i in range(4)])
    reviewer = Reviewer(store, model, EvidenceGateway(store, gateway), task_id=task.id, lease_token=task.lease_token)
    assert await reviewer.run(scope.id) == "partial"
    assert reviewer.error == "repeated_failed_batch" and len(model.inputs) == 2
    assert not gateway.calls
    exchanges = [row for row in store.read_turn_audit(task.id) if row["phase"] == "exchange"]
    assert len(exchanges) == 2 and all(len(row["messages"]) == len(batch(0)) + 1 for row in exchanges)


async def test_different_parameter_failure_then_correction_can_continue(tmp_path):
    store, scope, task = setup(tmp_path)
    gateway = Gateway()
    empty_question = {**review_state(), "question": ""}
    model = ScriptedModel(
        [
            [call("read_source", {"path": "a", "review_state": {}})],
            [call("read_source", {"path": "a", "review_state": empty_question})],
            [call("read_source", {"path": "a", "review_state": review_state()})],
            [call("finish_scope", {"checked_regions": ["one"]})],
        ]
    )
    reviewer = Reviewer(store, model, EvidenceGateway(store, gateway), task_id=task.id, lease_token=task.lease_token)
    assert await reviewer.run(scope.id) == "reviewed"
    assert len(model.inputs) == 4 and len(gateway.calls) == 1


async def test_repeated_failure_with_successful_sibling_is_not_a_failed_batch(tmp_path):
    store, scope, task = setup(tmp_path)
    gateway = Gateway()
    model = ScriptedModel(
        [
            [call("read_source", {"path": "a"}, "read")],
            [
                call("read_source", {"path": "a"}, "read"),
                call("save_question", {"statement": "What is the contract?"}, "q"),
            ],
            [call("finish_scope", {"checked_regions": ["one"]})],
        ]
    )
    reviewer = Reviewer(store, model, EvidenceGateway(store, gateway), task_id=task.id, lease_token=task.lease_token)
    assert await reviewer.run(scope.id) == "partial"
    assert reviewer.error is None and len(model.inputs) == 3
    assert len(store.list(Question)) == 1 and store.list(Question)[0].owner != "tool_controller"
    assert not gateway.calls


async def test_repeated_malformed_native_calls_are_protocol_failure_not_provider_failure(tmp_path):
    store, scope, task = setup(tmp_path)
    gateway = Gateway()

    class Malformed(ScriptedModel):
        async def ainvoke(self, messages):
            self.inputs.append(messages.copy())
            if len(self.inputs) > 2:
                return AIMessage(content="", tool_calls=[call("finish_scope", {"checked_regions": ["one"]})])
            state = review_state(f"note {len(self.inputs)}")
            ident = f"call-{len(self.inputs)}"
            if len(self.inputs) == 1:
                raw = '{"path":"a","path":"b","review_state":' + json.dumps(state) + ',"z":1}'
            else:
                raw = '{"z":1,"review_state":' + json.dumps(state) + ',"path":"a","path":"b"}'
            return AIMessage(
                content="",
                tool_calls=[call("read_source", {"path": "b", "review_state": state}, ident)],
                additional_kwargs={
                    "reviewforge_raw_tool_calls": [
                        {
                            "id": ident,
                            "type": "function",
                            "function": {"name": "read_source", "arguments": raw},
                        }
                    ]
                },
            )

    model = Malformed([])
    reviewer = Reviewer(store, model, EvidenceGateway(store, gateway), task_id=task.id, lease_token=task.lease_token)
    assert await reviewer.run(scope.id) == "partial"
    assert reviewer.error == "repeated_failed_batch" and len(model.inputs) == 2
    assert not gateway.calls
    assert len([row for row in store.read_turn_audit(task.id) if row["phase"] == "exchange"]) == 2
    native = [
        row["messages"][0]["raw_tool_calls"][0]["function"]["arguments"]
        for row in store.read_turn_audit(task.id)
        if row["phase"] == "native"
    ]
    assert native[0].startswith('{"path":"a","path":"b"')
    assert native[1].startswith('{"z":1,"review_state":')


def test_native_failure_normalization_preserves_relative_duplicate_order():
    def response(arguments):
        return AIMessage(
            content="",
            additional_kwargs={
                "reviewforge_raw_tool_calls": [
                    {
                        "id": "call",
                        "type": "function",
                        "function": {"name": "read_source", "arguments": arguments},
                    }
                ]
            },
        )

    left = response('{"path":"a","path":"b","z":1}')
    same = response('{"z":1,"path":"a","path":"b"}')
    reversed_duplicates = response('{"z":1,"path":"b","path":"a"}')
    errors = ["invalid-native-tool-call"]
    assert _protocol_failure(left, errors) == _protocol_failure(same, errors)
    assert _protocol_failure(left, errors) != _protocol_failure(reversed_duplicates, errors)
