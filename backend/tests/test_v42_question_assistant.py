"""The delegated assistant must independently receive fixed source before answering."""

import json

import pytest
from langchain_core.messages import AIMessage
from test_v42_fair_runtime import PinnedGateway
from test_v42_stateful_reviewer import call

from reviewforge.review_v42.context import ContextBuilder
from reviewforge.review_v42.contracts import Case, Question, ReviewScope, RevisionConflict, Run, Task
from reviewforge.review_v42.publication import PublicationOutbox, _incomplete_batch_impact
from reviewforge.review_v42.reviewer import QuestionAssistant
from reviewforge.review_v42.shared_investigation import SharedInvestigations
from reviewforge.review_v42.store import ReviewStore
from reviewforge.review_v42.tools import EvidenceGateway


def make_question_board(tmp_path, *, fine=False):
    region = {"id": "a", "path": "a.py", "side": "head", "start_line": 1, "end_line": 1, "changed_lines": [1]}
    run = Run(
        "run",
        "a/b",
        "base",
        "head",
        policy="v42-units-6",
        metadata={
            "changed_regions": [region],
            "workspace_digest": "head-snapshot",
            "base_workspace_digest": "base-snapshot",
            "shared_investigation": "shared-1",
            "question_assistant_policy": "question-delivery-1",
        },
    )
    if fine:
        run.metadata["question_fencing_policy"] = "question-input-1"
    store = ReviewStore(tmp_path / "question.sqlite", run)
    store.install_unit_plan(
        [ReviewScope("a", "run", "a", regions=[region])],
        ReviewScope("integration", "run", "integration", dependencies=["a"]),
    )
    SharedInvestigations(store)
    source = store.put_evidence(
        b"changed",
        revision="head",
        side="head",
        kind="source",
        path="a.py",
        start_line=1,
        end_line=1,
        metadata={"workspace_digest": "head-snapshot"},
    )
    case = store.add(
        Case(
            "case",
            "run",
            "a",
            trigger="trigger",
            mechanism="mechanism",
            claims=[{"id": "claim", "statement": "claim", "impact": "impact"}],
            evidence_refs=[source.id],
        )
    )
    question = store.add(Question("question", "run", "Does this behavior occur?", scope_id="a", case_id=case.id))
    cite = {"ref": source.id, "quote": "changed", "start_line": 1, "end_line": 1}
    result = {
        "verdict": "accept",
        "reason": "checked",
        "gaps": [],
        "case_trigger": case.trigger,
        "claim_id": "claim",
        "claim_statement": "claim",
        "facts": {
            name: {"statement": name, "citations": [dict(cite)]}
            for name in ("trigger", "actual", "expected", "change", "impact")
        },
    }
    case = store.get(Case, case.id)
    store.save_verdict(case.id, "claim", case.revision, "v42-independent-1", "accept", [source.id], result)
    review = store.add(Task("review", "run", "review", "a", 0))
    review = store.acquire_task(review.id, "reviewer", 120)
    store.append_turn_audit(review.id, review.lease_token, "delegate", "request", [])
    delegation = call("delegate_question", {"question_id": question.id, "reason": "Check source"}, "delegate")
    response = [{"type": "ai", "data": {"content": "", "tool_calls": [delegation]}}]
    store.append_turn_audit(review.id, review.lease_token, "delegate", "response", response)
    task = store.create_question_task(
        review.id,
        review.lease_token,
        question.id,
        question.revision,
        turn_id="delegate",
        tool_call_id="delegate",
        instruction="Check source",
    )
    store.append_turn_audit(review.id, review.lease_token, "delegate", "exchange", response)
    task = store.acquire_task(task.id, "assistant", 120)
    return store, task


@pytest.fixture
def question_board(tmp_path):
    return make_question_board(tmp_path)


@pytest.fixture
def fine_question_board(tmp_path):
    return make_question_board(tmp_path, fine=True)


class Model:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.inputs = []

    def bind_tools(self, tools, **kwargs):
        return self

    async def ainvoke(self, messages):
        self.inputs.append(list(messages))
        response = next(self.responses)
        return response(messages) if callable(response) else response


def answer(messages):
    ref = next(json.loads(m.content)["evidence_ref"] for m in messages if m.type == "tool")
    return AIMessage(
        content="",
        tool_calls=[
            call(
                "answer_question",
                {"status": "answered", "answer": "The fixed source shows the behavior.", "evidence_refs": [ref]},
                "answer",
            )
        ],
    )


async def test_source_is_received_in_independent_turn_before_answer(question_board):
    store, task = question_board
    model = Model([AIMessage(content="", tool_calls=[call("read_source", {"path": "a.py"}, "read")]), answer])
    result = await QuestionAssistant(store, model, EvidenceGateway(store, PinnedGateway())).run(task)
    assert result["status"] == "answered"
    assert store.get(Question, task.object_id).status == "answered"
    assert store.get(ReviewScope, "a").status == "pending"
    assert any(item["case_id"] == "case" for item in store.pending_unit_cases())
    packet = ContextBuilder(store).verifier("case")
    assert "The fixed source shows the behavior." not in json.dumps(packet)
    assert result["evidence_refs"][0] in store.get(Case, "case").evidence_refs
    phases = [entry["phase"] for entry in store.read_turn_audit(task.id)]
    assert phases.count("request") == phases.count("response") == phases.count("exchange") == 2
    assert any(phase.endswith(":answer") for phase in phases)
    assert not store.pending_shared_batches("a")


async def test_same_response_read_does_not_authorize_answer(question_board):
    store, task = question_board
    # A guessed reference cannot be turned into delivered input by a sibling read.
    existing = store.put_evidence(
        b"known",
        revision="head",
        side="head",
        kind="source",
        path="a.py",
        metadata={"workspace_digest": "head-snapshot"},
    )
    combined = AIMessage(
        content="",
        tool_calls=[
            call("read_evidence", {"ref": existing.id}, "read"),
            call("answer_question", {"status": "answered", "answer": "yes", "evidence_refs": [existing.id]}, "answer"),
        ],
    )
    model = Model(
        [
            combined,
            AIMessage(
                content="",
                tool_calls=[
                    call(
                        "answer_question",
                        {"status": "unresolved", "answer": "need more evidence", "evidence_refs": []},
                        "unresolved",
                    )
                ],
            ),
        ]
    )
    result = await QuestionAssistant(store, model, EvidenceGateway(store, PinnedGateway())).run(task)
    assert result["status"] == "unresolved"
    assert any("error" in json.loads(m.content) for m in model.inputs[1] if m.type == "tool")


async def test_unfinished_question_answer_blocks_its_frozen_comment(question_board, monkeypatch):
    store, task = question_board
    outbox = PublicationOutbox(store)
    frozen = outbox.freeze()
    assert len(frozen) == 1
    source = store.get(Case, "case").evidence_refs[0]
    model = Model(
        [
            AIMessage(
                content="",
                tool_calls=[
                    call(
                        "answer_question", {"status": "answered", "answer": "yes", "evidence_refs": [source]}, "answer"
                    )
                ],
            )
        ]
    )
    original = store.append_turn_audit

    class Crash(BaseException):
        pass

    def crash_after_response(*args, **kwargs):
        result = original(*args, **kwargs)
        if args[3] == "response":
            raise Crash()
        return result

    monkeypatch.setattr(store, "append_turn_audit", crash_after_response)
    with pytest.raises(Crash):
        await QuestionAssistant(store, model, EvidenceGateway(store, PinnedGateway())).run(task)
    pending = store.pending_shared_batches("a")
    assert pending[0]["task_kind"] == "question" and pending[0]["case_id"] == "case"
    assert outbox.freeze() == []
    with pytest.raises(ValueError, match="permission"):
        outbox.prepare(frozen[0]["id"])


def test_question_batch_impact_requires_bound_question_identity():
    entry = {
        "scope_id": "a",
        "task_kind": "question",
        "question_id": "q",
        "case_id": "c",
        "reason": "native exchange not fully persisted",
        "calls": [{"name": "answer_question", "args": {}, "receipt": "outcome_unknown"}],
    }
    assert _incomplete_batch_impact({"a": [entry]}, {"a", "b"}, {"c"}) == ({"a"}, {"c"}, False)
    del entry["question_id"]
    assert _incomplete_batch_impact({"a": [entry]}, {"a", "b"}, {"c"})[2] is True


@pytest.mark.parametrize("bad_ids", [("duplicate", "duplicate"), ("", "read")])
async def test_ambiguous_protocol_rejects_entire_batch_before_any_tool(question_board, bad_ids):
    store, task = question_board
    first = AIMessage(
        content="",
        tool_calls=[
            call("read_source", {"path": "a.py"}, bad_ids[0]),
            call(
                "answer_question", {"status": "unresolved", "answer": "need evidence", "evidence_refs": []}, bad_ids[1]
            ),
        ],
    )
    second = AIMessage(
        content="",
        tool_calls=[
            call("answer_question", {"status": "unresolved", "answer": "need evidence", "evidence_refs": []}, "retry")
        ],
    )
    model = Model([first, second])
    gateway = PinnedGateway()
    assert (await QuestionAssistant(store, model, EvidenceGateway(store, gateway)).run(task))["status"] == "unresolved"
    assert not gateway.calls
    assert "No tool was executed" in model.inputs[1][-1].content


@pytest.mark.parametrize("point", ["response", "feedback", "exchange"])
async def test_protocol_rejection_crashes_are_pending_or_recoverable(question_board, monkeypatch, point):
    store, task = question_board
    bad = AIMessage(
        content="",
        tool_calls=[call("read_source", {"path": "a.py"}, "same"), call("read_source", {"path": "a.py"}, "same")],
    )
    model = Model([bad])
    gateway = PinnedGateway()
    append = store.append_turn_audit
    save = store.save_task_session

    class Crash(BaseException):
        pass

    def audit(*args, **kwargs):
        result = append(*args, **kwargs)
        if args[3] == point:
            raise Crash()
        return result

    def checkpoint(*args, **kwargs):
        result = save(*args, **kwargs)
        if point == "feedback":
            raise Crash()
        return result

    monkeypatch.setattr(store, "append_turn_audit", audit)
    monkeypatch.setattr(store, "save_task_session", checkpoint)
    with pytest.raises(Crash):
        await QuestionAssistant(store, model, EvidenceGateway(store, gateway)).run(task)
    monkeypatch.setattr(store, "append_turn_audit", append)
    monkeypatch.setattr(store, "save_task_session", save)
    assert not gateway.calls
    assert bool(store.pending_shared_batches("a")) == (point != "exchange")
    if point == "exchange":
        corrected = AIMessage(
            content="",
            tool_calls=[
                call(
                    "answer_question",
                    {"status": "unresolved", "answer": "need more evidence", "evidence_refs": []},
                    "retry",
                )
            ],
        )
        recovered = Model([corrected])
        result = await QuestionAssistant(store, recovered, EvidenceGateway(store, gateway)).run(task)
        assert result["status"] == "unresolved"
        assert "No tool was executed" in recovered.inputs[0][-1].content


async def test_new_lease_does_not_replay_old_source_tool_output(question_board, monkeypatch):
    store, task = question_board
    model = Model([AIMessage(content="", tool_calls=[call("read_source", {"path": "a.py"}, "read")])])
    gateway = PinnedGateway()
    append = store.append_turn_audit

    class Crash(BaseException):
        pass

    def interrupt(*args, **kwargs):
        result = append(*args, **kwargs)
        if args[3] == "exchange":
            raise Crash()
        return result

    monkeypatch.setattr(store, "append_turn_audit", interrupt)
    with pytest.raises(Crash):
        await QuestionAssistant(store, model, EvidenceGateway(store, gateway)).run(task)
    monkeypatch.setattr(store, "append_turn_audit", append)
    records = store.read_turn_audit(task.id)
    tool = next(record for record in records if record["phase"].startswith("tool:"))
    ref = json.loads(tool["messages"][0]["data"]["content"])["evidence_ref"]
    store.release_task(task.id, task.lease_token)
    replacement = store.acquire_task(task.id, "replacement", 120)
    retry = AIMessage(
        content="",
        tool_calls=[call("answer_question", {"status": "answered", "answer": "yes", "evidence_refs": [ref]}, "answer")],
    )
    unresolved = AIMessage(
        content="",
        tool_calls=[
            call(
                "answer_question",
                {"status": "unresolved", "answer": "source must be independently reread", "evidence_refs": []},
                "unresolved",
            )
        ],
    )
    second = Model([retry, unresolved])
    result = await QuestionAssistant(store, second, EvidenceGateway(store, gateway)).run(replacement)
    assert result["status"] == "unresolved"
    assert all(message.type != "tool" for message in second.inputs[0])
    assert any("error" in json.loads(message.content) for message in second.inputs[1] if message.type == "tool")


async def test_protocol_feedback_survives_question_task_scope_fence(question_board, monkeypatch):
    store, task = question_board
    gateway = PinnedGateway()
    bad = AIMessage(
        content="",
        tool_calls=[call("read_source", {"path": "a.py"}, "same"), call("read_source", {"path": "a.py"}, "same")],
    )
    append = store.append_turn_audit

    class Crash(BaseException):
        pass

    def interrupt(*args, **kwargs):
        result = append(*args, **kwargs)
        if args[3] == "exchange":
            raise Crash()
        return result

    monkeypatch.setattr(store, "append_turn_audit", interrupt)
    with pytest.raises(Crash):
        await QuestionAssistant(store, Model([bad]), EvidenceGateway(store, gateway)).run(task)
    monkeypatch.setattr(store, "append_turn_audit", append)
    review = store.get(Task, "review")
    scope = store.get(ReviewScope, "a")
    store.update_scope_for_task(review.id, review.lease_token, scope.revision, status="partial")
    store.settle_question_task_stale(task.id, task.lease_token, {"status": "superseded"})
    review = store.get(Task, review.id)
    question = store.get(Question, task.object_id)
    response = [
        {
            "type": "ai",
            "data": {
                "content": "",
                "tool_calls": [
                    call("delegate_question", {"question_id": question.id, "reason": "Check source again"}, "again")
                ],
            },
        }
    ]
    store.append_turn_audit(review.id, review.lease_token, "next", "request", [])
    store.append_turn_audit(review.id, review.lease_token, "next", "response", response)
    new_task = store.create_question_task(
        review.id,
        review.lease_token,
        question.id,
        question.revision,
        turn_id="next",
        tool_call_id="again",
        instruction="Check source again",
    )
    store.append_turn_audit(review.id, review.lease_token, "next", "exchange", response)
    new_task = store.acquire_task(new_task.id, "replacement", 120)
    corrected = AIMessage(
        content="",
        tool_calls=[
            call("answer_question", {"status": "unresolved", "answer": "need source", "evidence_refs": []}, "done")
        ],
    )
    model = Model([corrected])
    result = await QuestionAssistant(store, model, EvidenceGateway(store, gateway)).run(new_task)
    assert result["status"] == "unresolved"
    assert "No tool was executed" in model.inputs[0][-1].content
    assert "read_source" in model.inputs[0][-1].content


async def test_search_navigation_cannot_be_used_as_answer_evidence(question_board):
    store, task = question_board

    class SearchGateway(PinnedGateway):
        async def __call__(self, name, args):
            self.calls.append((name, args))
            return {
                "content": "not found",
                "revision": "head",
                "side": "head",
                "kind": "search",
                "metadata": {"workspace_digest": "head-snapshot", "navigation_only": True},
            }

    first = AIMessage(content="", tool_calls=[call("read_source", {"path": "a.py"}, "search")])
    unresolved = AIMessage(
        content="",
        tool_calls=[
            call(
                "answer_question",
                {"status": "unresolved", "answer": "search is only navigation", "evidence_refs": []},
                "unresolved",
            )
        ],
    )
    model = Model([first, answer, unresolved])
    result = await QuestionAssistant(store, model, EvidenceGateway(store, SearchGateway())).run(task)
    assert result["status"] == "unresolved"
    assert any("error" in json.loads(message.content) for message in model.inputs[2] if message.type == "tool")


@pytest.mark.parametrize("native", [False, True])
async def test_response_after_scope_fence_is_persisted_but_never_executed(question_board, native):
    store, task = question_board
    gateway = PinnedGateway()

    def fence_and_respond(messages):
        review = store.get(Task, "review")
        scope = store.get(ReviewScope, "a")
        store.update_scope_for_task(review.id, review.lease_token, scope.revision, status="partial")
        args = {"status": "unresolved", "answer": "need evidence", "evidence_refs": []}
        kwargs = {}
        if native:
            kwargs["additional_kwargs"] = {
                "tool_calls": [
                    {
                        "id": "answer",
                        "type": "function",
                        "function": {
                            "name": "answer_question",
                            "arguments": json.dumps(args),
                        },
                    }
                ]
            }
        return AIMessage(content="", tool_calls=[call("answer_question", args, "answer")], **kwargs)

    with pytest.raises(RevisionConflict):
        await QuestionAssistant(store, Model([fence_and_respond]), EvidenceGateway(store, gateway)).run(task)
    assert not gateway.calls and store.get(Question, task.object_id).status == "open"
    pending = store.pending_shared_batches("a")
    assert len(pending) == 1 and pending[0]["calls"][0]["name"] == "answer_question"
    audit = store.read_turn_audit(task.id)
    assert any(entry["phase"] == "response" for entry in audit)
    assert not any(entry["phase"].startswith("tool:") or entry["phase"] == "exchange" for entry in audit)


@pytest.mark.parametrize(
    "name,kind,revision,side,path,metadata",
    [
        ("read_source", "mystery", "head", "head", "a.py", {}),
        ("read_source", "navigation_item", "head", "head", "a.py", {}),
        ("read_source", "source", "head", "head", "a.py", {"navigation_only": True}),
        ("search_code", "source", "head", "head", "a.py", {}),
        ("read_source", "documentation", "head", "head", "a.py", {}),
        ("read_source", "test", "head", "head", "a.py", {}),
        ("read_source", "source", "head", "head", "", {}),
        ("read_source", "source", "head", "head", "a.py", {"workspace_digest": "foreign"}),
        ("read_source", "source", "different-head", "head", "a.py", {}),
        ("read_source", "source", "head", "base", "a.py", {}),
    ],
)
async def test_answer_rejects_navigation_forged_kind_tool_or_snapshot(
    question_board, name, kind, revision, side, path, metadata
):
    store, task = question_board

    class Gateway:
        tool_specs = [
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": name,
                    "parameters": {"type": "object", "properties": {}, "additionalProperties": True},
                },
            }
        ]

        async def __call__(self, called, args):
            return {
                "content": "untrusted result",
                "kind": kind,
                "revision": revision,
                "side": side,
                "path": path,
                "metadata": {"workspace_digest": "head-snapshot", "status": "available", **metadata},
            }

    request_args = {"query": "untrusted"} if name == "search_code" else {"path": path or "a.py"}
    first = AIMessage(content="", tool_calls=[call(name, request_args, "source")])
    unresolved = AIMessage(
        content="",
        tool_calls=[
            call(
                "answer_question",
                {"status": "unresolved", "answer": "No eligible original source", "evidence_refs": []},
                "unresolved",
            )
        ],
    )

    def decide(messages):
        payload = next(json.loads(m.content) for m in messages if m.type == "tool")
        return answer(messages) if payload.get("evidence_ref") else unresolved

    model = Model([first, decide, unresolved])
    result = await QuestionAssistant(store, model, EvidenceGateway(store, Gateway())).run(task)
    assert result["status"] == "unresolved"
    assert store.get(Question, task.object_id).status == "unresolved"
    assert any(
        "error" in json.loads(message.content)
        for turn in model.inputs[1:]
        for message in turn
        if message.type == "tool"
    )


@pytest.mark.parametrize("side,revision", [("head", "head"), ("base", "base")])
async def test_answer_accepts_independently_delivered_fixed_source(question_board, side, revision):
    store, task = question_board

    class Gateway(PinnedGateway):
        async def __call__(self, name, args):
            return {
                "content": "line one\nline two\n",
                "kind": "source",
                "side": side,
                "revision": revision,
                "path": "a.py",
                "start_line": 1,
                "end_line": 2,
                "metadata": {"status": "available", "workspace_digest": "head-snapshot"},
            }

    model = Model(
        [AIMessage(content="", tool_calls=[call("read_source", {"path": "a.py", "side": side}, "read")]), answer]
    )
    outcome = await QuestionAssistant(store, model, EvidenceGateway(store, Gateway())).run(task)
    assert outcome["status"] == "answered"
    assert store.get(Question, task.object_id).evidence_refs == outcome["evidence_refs"]


async def test_fine_fencing_unrelated_navigation_does_not_cancel_answer(fine_question_board):
    store, task = fine_question_board

    def navigation_then_answer(messages):
        review = store.get(Task, "review")
        scope = store.get(ReviewScope, "a")
        merged = store.merge_working_states(
            review.id,
            review.lease_token,
            scope.revision,
            [
                {
                    "question": "Other review focus",
                    "observations": [],
                    "open_questions": ["A different question"],
                    "next_step": "Continue separately",
                }
            ],
        )
        assert not merged["errors"]
        return AIMessage(
            content="",
            tool_calls=[
                call(
                    "answer_question",
                    {"status": "unresolved", "answer": "need additional source", "evidence_refs": []},
                    "answer",
                )
            ],
        )

    outcome = await QuestionAssistant(
        store, Model([navigation_then_answer]), EvidenceGateway(store, PinnedGateway())
    ).run(task)
    assert outcome["status"] == "unresolved"
    assert store.get(Question, task.object_id).status == "unresolved"
    assert not store.pending_shared_batches("a")


@pytest.mark.parametrize(
    "change", ["case", "pending_source", "shared_obligation", "integration_obligation", "integration_status"]
)
async def test_fine_fencing_related_change_preserves_unexecuted_response(fine_question_board, change):
    store, task = fine_question_board

    def change_then_answer(messages):
        if change == "case":
            case = store.get(Case, "case")
            store.update(Case, case.id, case.revision, mechanism="A changed mechanism")
        elif change == "pending_source":
            source = store.put_evidence(
                b"new original source",
                revision="head",
                side="head",
                kind="source",
                path="a.py",
                start_line=1,
                end_line=1,
                metadata={"workspace_digest": "head-snapshot"},
            )
            scope = store.get(ReviewScope, "a")
            store.reopen_unit_for_evidence(scope.id, scope.revision, [source.id], "new evidence")
        elif change == "shared_obligation":
            review = store.get(Task, "review")
            store.append_turn_audit(review.id, review.lease_token, "shared", "request", [])
            reply = [{"type": "ai", "data": {"content": ""}}]
            store.append_turn_audit(review.id, review.lease_token, "shared", "response", reply)
            SharedInvestigations(store).create(
                review.id,
                review.lease_token,
                "Related investigation",
                ["a"],
                ["a"],
                turn_id="shared",
                tool_call_id="create",
            )
            store.append_turn_audit(review.id, review.lease_token, "shared", "exchange", reply)
        else:
            integration = store.get(ReviewScope, "integration")
            integrator = store.add(Task("integrator", "run", "review", integration.id, integration.revision))
            integrator = store.acquire_task(integrator.id, "integrator", 120)
            ref_id = store.get(Case, "case").evidence_refs[0]
            store.pin_frontier_for_task(integrator.id, integrator.lease_token, ref_id)
            receipt = {"evidence_ref": ref_id, "content": "changed", "start_line": 1, "end_line": 1}
            store.append_turn_audit(
                integrator.id,
                integrator.lease_token,
                "integration",
                "request",
                [{"type": "tool", "data": {"content": json.dumps(receipt)}}],
            )
            reply = [{"type": "ai", "data": {"content": ""}}]
            store.append_turn_audit(integrator.id, integrator.lease_token, "integration", "response", reply)
            if change == "integration_obligation":
                store.add_integration_obligation(
                    integrator.id,
                    integrator.lease_token,
                    ["a"],
                    "Check the cross-unit consequence",
                    [ref_id],
                    turn_id="integration",
                )
            else:
                obligation = next(item for item in store.list_integration_obligations() if item["kind"] == "unit")
                store.record_integration_receipt(
                    integrator.id,
                    integrator.lease_token,
                    obligation["id"],
                    obligation["revision"],
                    obligation["input_digest"],
                    disposition="checked",
                    evidence_refs=[ref_id],
                    reason="Compared source",
                    turn_id="integration",
                )
            store.append_turn_audit(integrator.id, integrator.lease_token, "integration", "exchange", reply)
        return AIMessage(
            content="",
            tool_calls=[
                call(
                    "answer_question",
                    {"status": "unresolved", "answer": "need more source", "evidence_refs": []},
                    "answer",
                )
            ],
        )

    with pytest.raises(RevisionConflict):
        await QuestionAssistant(store, Model([change_then_answer]), EvidenceGateway(store, PinnedGateway())).run(task)
    assert store.get(Question, task.object_id).status == "open"
    pending = store.pending_shared_batches("a")
    assert any(item.get("task_id") == task.id and item["calls"][0]["name"] == "answer_question" for item in pending)


@pytest.mark.parametrize("kind", ["documentation", "issue", "test"])
async def test_answer_accepts_bound_original_knowledge_and_test_receipts(question_board, kind):
    import hashlib

    store, task = question_board
    name = "run_check" if kind == "test" else "read_knowledge"
    path = (
        "issue:https://github.com/a/b/issues/123"
        if kind == "issue"
        else ("tests/test.js" if kind == "test" else "docs/spec.md")
    )
    args = {"path": path, **({"runtime": "node-test"} if kind == "test" else {})}
    content = "fixed original knowledge"
    revision = "head"
    side = "head"
    metadata = {"workspace_digest": "head-snapshot", "status": "available"}
    receipt = {"status": "success", "path": path, "head_sha": "head", "revision": "head"}
    if kind == "issue":
        side, revision = "external", "2026-01-01T00:00:00Z"
        receipt["revision"] = revision
        metadata.update(
            source_url="https://github.com/a/b/issues/123",
            captured_at="2026-01-02T00:00:00Z",
            historical_head_time=False,
            external_snapshot=True,
        )
    elif kind == "test":
        receipt.update(
            runtime="node-test",
            exit_code=1,
            source_sha256="a" * 64,
            image_id="sha256:" + "b" * 64,
            started_at="2026-01-02T00:00:00Z",
            finished_at="2026-01-02T00:01:00Z",
        )
        content = json.dumps(receipt)
    receipt["content_sha256"] = hashlib.sha256(content.encode()).hexdigest()
    metadata["original_receipt"] = {
        key: value for key, value in receipt.items() if key not in {"started_at", "finished_at"}
    }

    class Gateway:
        tool_specs = [
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": name,
                    "parameters": {"type": "object", "properties": {}, "additionalProperties": True},
                },
            }
        ]

        async def __call__(self, called, arguments):
            return {
                "content": content,
                "kind": kind,
                "revision": revision,
                "side": side,
                "path": path,
                "environment": "node-test" if kind == "test" else "",
                "metadata": metadata,
            }

    model = Model([AIMessage(content="", tool_calls=[call(name, args, "read")]), answer])
    result = await QuestionAssistant(store, model, EvidenceGateway(store, Gateway())).run(task)
    assert result["status"] == "answered"


@pytest.fixture
def fine_shared_question_board(fine_question_board):
    store, old_task = fine_question_board
    review = store.get(Task, "review")
    shared = SharedInvestigations(store)
    response = [{"type": "ai", "data": {"content": ""}}]
    store.append_turn_audit(review.id, review.lease_token, "shared-create", "request", [])
    store.append_turn_audit(review.id, review.lease_token, "shared-create", "response", response)
    item = shared.create(
        review.id, review.lease_token, "Shared inquiry", ["a"], ["a"], turn_id="shared-create", tool_call_id="create"
    )
    store.append_turn_audit(review.id, review.lease_token, "shared-create", "exchange", response)
    store.settle_question_task_stale(old_task.id, old_task.lease_token, {"status": "superseded"})
    item = shared.claim(
        review.id, review.lease_token, item["id"], item["revision"], "investigate", operation_id="claim"
    )
    ref_id = store.get(Case, "case").evidence_refs[0]
    store.pin_frontier_for_task(review.id, review.lease_token, ref_id)
    receipt = {"evidence_ref": ref_id, "content": "changed", "start_line": 1, "end_line": 1}
    store.append_turn_audit(
        review.id,
        review.lease_token,
        "shared-source",
        "request",
        [{"type": "tool", "data": {"content": json.dumps(receipt)}}],
    )
    store.append_turn_audit(review.id, review.lease_token, "shared-source", "response", response)
    item = shared.update(
        review.id,
        review.lease_token,
        item["id"],
        item["revision"],
        question="Shared inquiry",
        observations=[{"statement": "Initial note", "evidence_refs": [ref_id]}],
        open_questions=[],
        status="open",
        reason="record",
        turn_id="shared-source",
        operation_id="initial",
    )
    store.append_turn_audit(review.id, review.lease_token, "shared-source", "exchange", response)
    question = store.get(Question, "question")
    delegate = [
        {
            "type": "ai",
            "data": {
                "content": "",
                "tool_calls": [
                    call(
                        "delegate_question",
                        {"question_id": question.id, "reason": "Investigate after shared setup"},
                        "new-delegate",
                    )
                ],
            },
        }
    ]
    store.append_turn_audit(review.id, review.lease_token, "new-delegate", "request", [])
    store.append_turn_audit(review.id, review.lease_token, "new-delegate", "response", delegate)
    task = store.create_question_task(
        review.id,
        review.lease_token,
        question.id,
        question.revision,
        turn_id="new-delegate",
        tool_call_id="new-delegate",
        instruction="Investigate after shared setup",
    )
    store.append_turn_audit(review.id, review.lease_token, "new-delegate", "exchange", delegate)
    return store, store.acquire_task(task.id, "assistant", 120), item, ref_id


@pytest.mark.parametrize("change", ["wording", "source", "status"])
async def test_shared_investigation_fences_only_substantive_changes(fine_shared_question_board, change):
    store, task, item, ref_id = fine_shared_question_board
    review = store.get(Task, "review")
    shared = SharedInvestigations(store)

    def update_then_answer(messages):
        evidence_id = ref_id
        turn_id = "shared-source"
        if change == "source":
            fresh = store.put_evidence(
                b"another original",
                revision="head",
                side="head",
                kind="source",
                path="a.py",
                start_line=1,
                end_line=1,
                metadata={"workspace_digest": "head-snapshot"},
            )
            evidence_id = fresh.id
            store.pin_frontier_for_task(review.id, review.lease_token, evidence_id)
            receipt = {"evidence_ref": evidence_id, "content": "another original", "start_line": 1, "end_line": 1}
            turn_id = "new-source"
            store.append_turn_audit(
                review.id,
                review.lease_token,
                turn_id,
                "request",
                [{"type": "tool", "data": {"content": json.dumps(receipt)}}],
            )
            store.append_turn_audit(
                review.id, review.lease_token, turn_id, "response", [{"type": "ai", "data": {"content": ""}}]
            )
        shared.update(
            review.id,
            review.lease_token,
            item["id"],
            item["revision"],
            question="Reworded inquiry",
            observations=[{"statement": "Reworded note", "evidence_refs": [evidence_id]}],
            open_questions=[],
            status="unavailable" if change == "status" else "open",
            reason="updated",
            turn_id=turn_id,
            operation_id="update-" + change,
        )
        if change == "source":
            store.append_turn_audit(
                review.id, review.lease_token, turn_id, "exchange", [{"type": "ai", "data": {"content": ""}}]
            )
        return AIMessage(
            content="",
            tool_calls=[
                call(
                    "answer_question",
                    {"status": "unresolved", "answer": "need more evidence", "evidence_refs": []},
                    "answer",
                )
            ],
        )

    assistant = QuestionAssistant(store, Model([update_then_answer]), EvidenceGateway(store, PinnedGateway()))
    if change == "wording":
        assert (await assistant.run(task))["status"] == "unresolved"
    else:
        with pytest.raises(RevisionConflict):
            await assistant.run(task)
        assert store.get(Question, task.object_id).status == "open"
        assert any(item.get("task_id") == task.id for item in store.pending_shared_batches("a"))
