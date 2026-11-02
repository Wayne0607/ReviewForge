import asyncio
import json

import pytest
from langchain_core.messages import AIMessage
from test_v42_stateful_reviewer import Gateway, ScriptedModel, call, review_state

from reviewforge.review_v42.contracts import Case, EvidenceRef, NavigationItem, Question, ReviewScope, Run, Task
from reviewforge.review_v42.reviewer import QuestionAssistant, Reviewer
from reviewforge.review_v42.scope import compile_scopes
from reviewforge.review_v42.store import ReviewStore
from reviewforge.review_v42.tools import EvidenceGateway


def setup_workspace(tmp_path):
    run = Run(id="r", repo="a/b", base="base", head="head", policy="v42-workspace-3")
    store = ReviewStore(tmp_path / "board.db", run)
    scope = compile_scopes(run, [{"id": "one", "path": "a.py"}, {"id": "two", "path": "b.py"}])[0]
    store.add(scope)
    task = store.add(Task(id="task", run_id="r", kind="review", object_id=scope.id, input_revision=scope.revision))
    task = store.acquire_task(task.id, "tester", 120)
    return store, scope, task


async def test_multiple_states_and_empty_snapshot_preserve_open_navigation(tmp_path):
    store, scope, task = setup_workspace(tmp_path)
    source = store.put_evidence(b"prior source", revision="head", side="head", kind="source", path="a.py")
    first = {
        **review_state("handler"),
        "observations": [{"statement": "handler observation", "evidence_refs": [source.id]}],
        "open_questions": ["authorization?", "calendar?", "deduplication?"],
    }
    second = review_state("switch focus")
    third = {**review_state("more"), "open_questions": ["notification?"]}
    model = ScriptedModel(
        [
            [
                call("read_source", {"path": "a", "review_state": first}, "a"),
                call("read_source", {"path": "b", "review_state": second}, "b"),
                call("read_source", {"path": "c", "review_state": third}, "c"),
                call("read_source", {"path": "d"}, "d"),
            ],
            [call("record_region_progress", {"region_ids": ["one"], "action": "checked"})],
            [call("finish_scope", {"checked_regions": ["two"]})],
        ]
    )
    gateway = Gateway()
    reviewer = Reviewer(store, model, EvidenceGateway(store, gateway), task_id=task.id, lease_token=task.lease_token)
    assert await reviewer.run(scope.id) == "partial"
    assert [args["path"] for _, args in gateway.calls] == ["a", "b", "c", "d"]
    assert all("review_state" not in args for _, args in gateway.calls)
    nav = store.list(NavigationItem)
    assert {item.text for item in nav if item.kind == "question"} == {
        "authorization?",
        "calendar?",
        "deduplication?",
        "notification?",
    }
    assert any(item.kind == "observation" and item.text == "handler observation" for item in nav)
    board = json.loads(model.inputs[1][1].content)
    assert board["scope"]["working_state"]["question"] == "more"
    assert len(board["navigation"]) == 5
    finished = store.get(ReviewScope, scope.id)
    assert {region["id"] for region in finished.checked_regions} == {"one", "two"}
    assert set(finished.unresolved) == {item.id for item in nav if item.kind == "question"}
    audit = store.read_turn_audit(task.id)
    final = next(row for row in reversed(audit) if row["phase"] == "exchange")
    receipt = json.loads(final["messages"][-1]["data"]["content"])
    assert set(receipt["unresolved_refs"]) == set(finished.unresolved)


async def test_focal_switch_in_later_turn_does_not_clear_prior_questions_or_observation(tmp_path):
    store, scope, task = setup_workspace(tmp_path)
    source = store.put_evidence(b"source", revision="head", side="head", kind="source", path="a.py")
    first = {
        **review_state("handler"),
        "observations": [{"statement": "handler observation", "evidence_refs": [source.id]}],
        "open_questions": ["authorization?", "calendar?", "deduplication?"],
    }
    model = ScriptedModel(
        [
            [call("read_source", {"path": "a", "review_state": first})],
            [call("read_source", {"path": "b", "review_state": review_state("different focus")})],
            [call("finish_scope", {"checked_regions": ["one", "two"]})],
        ]
    )
    reviewer = Reviewer(store, model, EvidenceGateway(store, Gateway()), task_id=task.id, lease_token=task.lease_token)
    assert await reviewer.run(scope.id) == "partial"
    board = json.loads(model.inputs[2][1].content)
    assert board["scope"]["working_state"]["question"] == "different focus"
    assert {item["text"] for item in board["navigation"]} == {
        "handler observation",
        "authorization?",
        "calendar?",
        "deduplication?",
    }
    assert len(store.get(ReviewScope, scope.id).unresolved) == 3


async def test_bad_state_feedback_does_not_hide_sibling_source_results(tmp_path):
    store, scope, task = setup_workspace(tmp_path)
    gateway = Gateway()
    model = ScriptedModel(
        [
            [
                call("read_source", {"path": "bad-state", "review_state": {}}, "bad"),
                call("read_source", {"path": "valid-state", "review_state": review_state()}, "good"),
                call("read_source", {"path": "no-state"}, "none"),
            ],
            [call("finish_scope", {"checked_regions": ["one", "two"]})],
        ]
    )
    reviewer = Reviewer(store, model, EvidenceGateway(store, gateway), task_id=task.id, lease_token=task.lease_token)
    assert await reviewer.run(scope.id) == "reviewed"
    assert len(gateway.calls) == 3 and len(store.list(EvidenceRef)) == 3
    receipts = {m.tool_call_id: json.loads(m.content) for m in model.inputs[1] if m.type == "tool"}
    assert receipts["bad"]["content"].startswith("BODY_bad-state_")
    assert receipts["bad"]["review_state_error"]
    assert "review_state_error" not in receipts["good"] and "review_state_error" not in receipts["none"]
    assert not store.list(Question)


async def test_invalid_note_does_not_allow_sibling_finish_but_keeps_source(tmp_path):
    store, scope, task = setup_workspace(tmp_path)
    gateway = Gateway()
    model = ScriptedModel(
        [
            [
                call("read_source", {"path": "a", "review_state": {}}, "read"),
                call("finish_scope", {"checked_regions": ["one", "two"]}, "finish"),
            ],
            [call("finish_scope", {"checked_regions": ["one", "two"]})],
        ]
    )
    reviewer = Reviewer(store, model, EvidenceGateway(store, gateway), task_id=task.id, lease_token=task.lease_token)
    assert await reviewer.run(scope.id) == "reviewed"
    receipts = {m.tool_call_id: json.loads(m.content) for m in model.inputs[1] if m.type == "tool"}
    assert receipts["read"]["content"].startswith("BODY_a_") and receipts["read"]["review_state_error"]
    assert "prevents a completion" in receipts["finish"]["error"]
    assert len(gateway.calls) == 1


async def test_valid_state_on_finish_saves_navigation_without_extra_read(tmp_path):
    store, scope, task = setup_workspace(tmp_path)
    source = store.put_evidence(b"source", revision="head", side="head", kind="source", path="a.py")
    state = {
        **review_state(),
        "observations": [{"statement": "observed fact", "evidence_refs": [source.id]}],
        "open_questions": ["still unresolved?"],
    }
    gateway = Gateway()

    class Model(ScriptedModel):
        def bind_tools(self, tools, **kwargs):
            for name in ("read_source", "finish_scope", "submit_case", "record_region_progress"):
                spec = next(tool for tool in tools if tool["function"]["name"] == name)
                assert "review_state" in spec["function"]["parameters"]["properties"]
                assert "review_state" not in spec["function"]["parameters"].get("required", [])
            return self

    model = Model([[call("finish_scope", {"checked_regions": ["one", "two"], "review_state": state})]])
    reviewer = Reviewer(store, model, EvidenceGateway(store, gateway), task_id=task.id, lease_token=task.lease_token)
    assert await reviewer.run(scope.id) == "partial"
    assert not gateway.calls
    assert {item.text for item in store.list(NavigationItem)} == {"observed fact", "still unresolved?"}
    assert len(store.get(ReviewScope, scope.id).unresolved) == 1


async def test_invalid_state_on_semantic_submission_is_rejected_but_source_succeeds(tmp_path):
    store, scope, task = setup_workspace(tmp_path)
    source = store.put_evidence(b"source", revision="head", side="head", kind="source", path="a.py")
    case = {"trigger": "when x", "behavior": "fails", "impact": "loss", "evidence_refs": [source.id]}

    class Model(ScriptedModel):
        async def ainvoke(self, messages):
            if len(self.inputs) == 1:
                assert not store.list(Case)
                receipts = {m.tool_call_id: json.loads(m.content) for m in messages if m.type == "tool"}
                assert receipts["read"]["content"].startswith("BODY_a_")
                assert receipts["case"]["error"] == "invalid review_state"
            return await super().ainvoke(messages)

    model = Model(
        [
            [call("read_source", {"path": "a"}, "read"), call("submit_case", {**case, "review_state": {}}, "case")],
            [call("submit_case", case)],
            [call("finish_scope", {"checked_regions": ["one", "two"]})],
        ]
    )
    gateway = Gateway()
    reviewer = Reviewer(store, model, EvidenceGateway(store, gateway), task_id=task.id, lease_token=task.lease_token)
    assert await reviewer.run(scope.id) == "reviewed"
    assert len(store.list(Case)) == 1 and len(gateway.calls) == 1


async def test_invalid_state_on_finish_cannot_complete_but_source_succeeds(tmp_path):
    store, scope, task = setup_workspace(tmp_path)
    gateway = Gateway()
    model = ScriptedModel(
        [
            [
                call("read_source", {"path": "a"}, "read"),
                call("finish_scope", {"checked_regions": ["one", "two"], "review_state": {}}, "finish"),
            ],
            [call("finish_scope", {"checked_regions": ["one", "two"]})],
        ]
    )
    reviewer = Reviewer(store, model, EvidenceGateway(store, gateway), task_id=task.id, lease_token=task.lease_token)
    assert await reviewer.run(scope.id) == "reviewed"
    receipts = {m.tool_call_id: json.loads(m.content) for m in model.inputs[1] if m.type == "tool"}
    assert receipts["read"]["content"].startswith("BODY_a_")
    assert receipts["finish"]["error"] == "invalid review_state"
    assert len(gateway.calls) == 1


async def test_explicit_resolution_and_incremental_region_progress_complete_scope(tmp_path):
    store, scope, task = setup_workspace(tmp_path)
    gateway = Gateway()

    class Model:
        def __init__(self):
            self.inputs = []

        def bind_tools(self, tools, **kwargs):
            return self

        async def ainvoke(self, messages):
            self.inputs.append(messages.copy())
            if len(self.inputs) == 1:
                state = {**review_state(), "open_questions": ["is it reachable?"]}
                return AIMessage(content="", tool_calls=[call("read_source", {"path": "a", "review_state": state})])
            if len(self.inputs) == 2:
                item = json.loads(messages[1].content)["navigation"][0]
                return AIMessage(
                    content="",
                    tool_calls=[
                        call(
                            "update_navigation",
                            {
                                "item_id": item["id"],
                                "expected_revision": item["revision"],
                                "status": "resolved",
                                "reason": "checked the caller",
                            },
                            "resolve",
                        ),
                        call("record_region_progress", {"region_ids": ["one"], "action": "checked"}, "first"),
                    ],
                )
            if len(self.inputs) == 3:
                return AIMessage(
                    content="",
                    tool_calls=[
                        call("read_source", {"path": "b"}, "read"),
                        call("record_region_progress", {"region_ids": ["two"], "action": "checked"}, "second"),
                    ],
                )
            return AIMessage(content="", tool_calls=[call("finish_scope", {"checked_regions": []})])

    model = Model()
    reviewer = Reviewer(store, model, EvidenceGateway(store, gateway), task_id=task.id, lease_token=task.lease_token)
    assert await reviewer.run(scope.id) == "reviewed"
    assert store.list(NavigationItem)[0].status == "resolved"
    assert {r["id"] for r in store.get(ReviewScope, scope.id).checked_regions} == {"one", "two"}
    assert len(gateway.calls) == 2


async def test_region_retract_requires_reason_and_remains_visible_in_audit(tmp_path):
    store, scope, task = setup_workspace(tmp_path)
    model = ScriptedModel(
        [
            [call("record_region_progress", {"region_ids": ["one"], "action": "checked"})],
            [call("record_region_progress", {"region_ids": ["one"], "action": "retract"})],
            [call("record_region_progress", {"region_ids": ["one"], "action": "retract", "reason": "new dependency"})],
            [call("finish_scope", {"checked_regions": ["two"]})],
        ]
    )
    reviewer = Reviewer(store, model, EvidenceGateway(store, Gateway()), task_id=task.id, lease_token=task.lease_token)
    assert await reviewer.run(scope.id) == "partial"
    assert "requires a reason" in next(m.content for m in model.inputs[2] if m.type == "tool")
    assert [r["id"] for r in store.get(ReviewScope, scope.id).checked_regions] == ["two"]
    assert "new dependency" in json.dumps(store.read_turn_audit(task.id))


async def test_navigation_survives_cancel_during_read_and_recovery(tmp_path):
    store, scope, task = setup_workspace(tmp_path)

    class Interrupted(Gateway):
        async def __call__(self, name, args):
            raise asyncio.CancelledError()

    state = {**review_state(), "open_questions": ["persisted question?"]}
    model = ScriptedModel([[call("read_source", {"path": "a", "review_state": state})]])
    reviewer = Reviewer(
        store, model, EvidenceGateway(store, Interrupted()), task_id=task.id, lease_token=task.lease_token
    )
    with pytest.raises(asyncio.CancelledError):
        await reviewer.run(scope.id)
    resumed = ScriptedModel([[call("finish_scope", {"checked_regions": ["one", "two"]})]])
    reviewer = Reviewer(
        store, resumed, EvidenceGateway(store, Gateway()), task_id=task.id, lease_token=task.lease_token
    )
    assert await reviewer.run(scope.id) == "partial"
    board = json.loads(resumed.inputs[0][1].content)
    assert [item["text"] for item in board["navigation"]] == ["persisted question?"]
    assert len(store.list(NavigationItem)) == 1


async def test_note_wording_cannot_disguise_repeated_source_failures(tmp_path):
    store, scope, task = setup_workspace(tmp_path)

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
                call(
                    "read_source",
                    {
                        "path": "a",
                        "review_state": {**review_state(str(i)), "open_questions": [f"question wording {i}"]},
                    },
                )
            ]
            for i in range(4)
        ]
    )
    reviewer = Reviewer(
        store, model, EvidenceGateway(store, Unavailable()), task_id=task.id, lease_token=task.lease_token
    )
    assert await reviewer.run(scope.id) == "partial"
    assert reviewer.error == "repeated_failed_batch" and len(model.inputs) == 2
    assert len(store.list(Question)) == 1


def other_scope(store):
    scope = store.add(ReviewScope(id="other", run_id="r", title="Other scope", regions=[]))
    task = store.add(Task(id="other-task", run_id="r", kind="review", object_id=scope.id, input_revision=0))
    task = store.acquire_task(task.id, "other-reviewer", 120)
    source = store.put_evidence(b"other source", revision="head", side="head", kind="source", path="other.py")
    state = {
        **review_state(),
        "open_questions": ["Other question?"],
        "observations": [{"statement": "Other observation", "evidence_refs": [source.id]}],
    }
    entries = store.merge_working_states(task.id, task.lease_token, 0, [state])["items"]
    return scope, entries


async def test_cross_scope_navigation_pagination_is_read_only_and_not_case_evidence(tmp_path):
    store, scope, task = setup_workspace(tmp_path)
    other, entries = other_scope(store)

    class Model(ScriptedModel):
        async def ainvoke(self, messages):
            self.inputs.append(messages.copy())
            index = len(self.inputs)
            if index == 1:
                return AIMessage(content="", tool_calls=[call("list_navigation", {"scope_id": other.id, "limit": 1})])
            if index == 2:
                page = json.loads(next(m.content for m in messages if m.type == "tool"))
                assert len(page["items"]) == 1 and page["next_cursor"]
                return AIMessage(
                    content="",
                    tool_calls=[
                        call("list_navigation", {"scope_id": other.id, "limit": 1, "cursor": page["next_cursor"]})
                    ],
                )
            if index == 3:
                page = json.loads(next(m.content for m in messages if m.type == "tool"))
                assert len(page["items"]) == 1 and page["next_cursor"] is None
                return AIMessage(
                    content="",
                    tool_calls=[
                        call(
                            "submit_case",
                            {"trigger": "x", "behavior": "y", "impact": "z", "evidence_refs": [entries[0].id]},
                        )
                    ],
                )
            assert "error" in json.loads(next(m.content for m in messages if m.type == "tool"))
            return AIMessage(content="", tool_calls=[call("finish_scope", {"checked_regions": ["one", "two"]})])

    model = Model([])
    reviewer = Reviewer(store, model, EvidenceGateway(store, Gateway()), task_id=task.id, lease_token=task.lease_token)
    assert await reviewer.run(scope.id) == "reviewed"
    assert not store.list(Case)
    assert all(item.status == "open" for item in store.list(NavigationItem))


async def test_question_assistant_can_read_shared_navigation_but_cannot_cite_it(tmp_path):
    store, scope, task = setup_workspace(tmp_path)
    other, entries = other_scope(store)
    question = store.add_for_task(
        task.id, task.lease_token, Question(id="q", run_id="r", scope_id=scope.id, statement="Why?")
    )
    delegated = store.add_for_task(
        task.id,
        task.lease_token,
        Task(id="helper", run_id="r", kind="question", object_id=question.id, input_revision=0),
    )
    delegated = store.acquire_task(delegated.id, "assistant", 120)
    model = ScriptedModel(
        [
            [call("list_navigation", {"scope_id": other.id})],
            [call("answer_question", {"status": "answered", "answer": "as noted", "evidence_refs": [entries[0].id]})],
            [call("answer_question", {"status": "unresolved", "answer": "need source", "evidence_refs": []})],
        ]
    )
    result = await QuestionAssistant(store, model, EvidenceGateway(store, Gateway())).run(delegated)
    assert result["status"] == "unresolved"
    context = json.loads(model.inputs[0][1].content)
    assert context["scope_id"] == scope.id and other.id in {row["id"] for row in context["scope_index"]}
    receipt = json.loads(next(m.content for m in model.inputs[1] if m.type == "tool"))
    assert {item["id"] for item in receipt["items"]} == {item.id for item in entries}
    assert "not" in receipt["notice"] and "error" in json.loads(
        [m.content for m in model.inputs[2] if m.type == "tool"][-1]
    )


async def test_closed_navigation_merge_returns_closed_status(tmp_path):
    store, scope, task = setup_workspace(tmp_path)
    state = {**review_state(), "open_questions": ["Previously resolved?"]}
    item = store.merge_working_states(task.id, task.lease_token, 0, [state])["items"][0]
    store.update_navigation_for_task(
        task.id, task.lease_token, item.id, item.revision, status="resolved", reason="resolved"
    )
    model = ScriptedModel(
        [
            [call("read_source", {"path": "a", "review_state": state})],
            [call("finish_scope", {"checked_regions": ["one", "two"]})],
        ]
    )
    reviewer = Reviewer(store, model, EvidenceGateway(store, Gateway()), task_id=task.id, lease_token=task.lease_token)
    assert await reviewer.run(scope.id) == "reviewed"
    receipt = json.loads(next(m.content for m in model.inputs[1] if m.type == "tool"))
    assert receipt["navigation_items"][0]["id"] == item.id and receipt["navigation_items"][0]["status"] == "resolved"


async def test_rephrasing_navigation_does_not_disguise_failed_reads(tmp_path):
    store, scope, task = setup_workspace(tmp_path)
    state = {**review_state(), "open_questions": ["Original?"]}
    store.merge_working_states(task.id, task.lease_token, 0, [state])

    class Unavailable(Gateway):
        async def __call__(self, name, args):
            return {
                "content": "unavailable",
                "revision": "head",
                "side": "head",
                "kind": "failure",
                "metadata": {"status": "unavailable"},
            }

    class Model(ScriptedModel):
        async def ainvoke(self, messages):
            self.inputs.append(messages.copy())
            item = json.loads(messages[1].content)["navigation"][0]
            return AIMessage(
                content="",
                tool_calls=[
                    call("read_source", {"path": "a"}, "read"),
                    call(
                        "update_navigation",
                        {
                            "item_id": item["id"],
                            "expected_revision": item["revision"],
                            "text": f"wording {len(self.inputs)}",
                            "reason": "clarify",
                        },
                        "note",
                    ),
                ],
            )

    model = Model([])
    reviewer = Reviewer(
        store, model, EvidenceGateway(store, Unavailable()), task_id=task.id, lease_token=task.lease_token
    )
    assert await reviewer.run(scope.id) == "partial"
    assert reviewer.error == "repeated_failed_batch" and len(model.inputs) == 2
