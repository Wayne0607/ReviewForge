"""Opt-in question delegation is durable and independently source-audited."""

import asyncio
import json
from dataclasses import replace

import pytest
from langchain_core.messages import AIMessage
from test_v42_fair_runtime import PinnedGateway, finish_unit, receipts
from test_v42_integration import NativeModel, product
from test_v42_question_assistant import answer
from test_v42_stateful_reviewer import call
from test_v42_units_product import FixedBaseGitHub, TwoFileWorkspace, two_file_state

from reviewforge.review_v42.contracts import BindingError, Case, Question, Run, Task
from reviewforge.review_v42.runtime import ReviewRuntime
from reviewforge.review_v42.shared_investigation import SharedInvestigations
from reviewforge.review_v42.store import ReviewStore


def create_pending_delegation(tmp_path, *, with_case=False, fair=False, fine_fence=False):
    region = {"id": "one", "path": "a.py", "side": "head", "start_line": 1, "end_line": 1, "changed_lines": [1]}
    metadata = {
        "changed_regions": [region],
        "workspace_digest": "head-snapshot",
        "base_workspace_digest": "base-snapshot",
        "shared_investigation": "shared-1",
        "question_assistant_policy": "question-delivery-1",
    }
    if fair:
        metadata["unit_scheduler"] = "cooperative-1"
    if fine_fence:
        metadata["question_fencing_policy"] = "question-input-1"
    store = ReviewStore(tmp_path / "board.db", Run("r", "a/b", "base", "head", policy="v42-units-6", metadata=metadata))
    runtime = ReviewRuntime(store, None, PinnedGateway(), object())
    scope = runtime.prepare([region])[0]
    SharedInvestigations(store)
    if with_case:
        old_ref = store.put_evidence(
            b"old source",
            revision="head",
            side="head",
            kind="source",
            path="a.py",
            start_line=1,
            end_line=1,
            metadata={"workspace_digest": "head-snapshot", "tool_name": "read_source", "tool_args": {"path": "a.py"}},
        )
        store.add(
            Case(
                "case",
                store.run.id,
                scope.id,
                trigger="trigger",
                mechanism="mechanism",
                claims=[{"id": "claim", "statement": "claim", "impact": "impact"}],
                evidence_refs=[old_ref.id],
            )
        )
    question = store.add(
        Question(
            "question", store.run.id, "Does the behavior occur?", scope_id=scope.id, case_id="case" if with_case else ""
        )
    )
    review = store.add(Task("delegator", store.run.id, "review", scope.id, scope.revision))
    review = store.acquire_task(review.id, "reviewer", 120)
    store.append_turn_audit(review.id, review.lease_token, "delegate", "request", [])
    response = [
        {
            "type": "ai",
            "data": {
                "content": "",
                "tool_calls": [
                    call("delegate_question", {"question_id": question.id, "reason": "Inspect the source"}, "delegate")
                ],
            },
        }
    ]
    store.append_turn_audit(review.id, review.lease_token, "delegate", "response", response)
    delegated = store.create_question_task(
        review.id,
        review.lease_token,
        question.id,
        question.revision,
        turn_id="delegate",
        tool_call_id="delegate",
        instruction="Inspect the source",
    )
    store.append_turn_audit(review.id, review.lease_token, "delegate", "exchange", response)
    store.release_task(review.id, review.lease_token)
    return store, runtime, region, delegated


class Model:
    def __init__(self):
        self.question_turns = 0

    def bind_tools(self, tools, **kwargs):
        return self

    async def ainvoke(self, messages):
        await asyncio.sleep(0)
        board = json.loads(messages[1].content)
        previous = [json.loads(m.content) for m in messages if m.type == "tool"]
        if "question_id" in board:
            self.question_turns += 1
            if not previous:
                return AIMessage(content="", tool_calls=[call("read_source", {"path": "a.py"}, "question-read")])
            return answer(messages)
        refs = [item["evidence_ref"] for item in previous if "evidence_ref" in item]
        if not refs:
            return AIMessage(content="", tool_calls=[call("read_source", {"path": "a.py"}, "source")])
        if board["unit_role"] == "primary":
            return AIMessage(content="", tool_calls=[finish_unit("one", refs[0])])
        return AIMessage(
            content="",
            tool_calls=[*receipts(board["integration_obligations"], refs), call("finish_integration", {}, "finish")],
        )


class Verifier:
    policy = "v42-independent-1"

    def __init__(self, store):
        self.store, self.revisions = store, []

    async def verify(self, case_id, *, task_id, lease_token):
        case = self.store.get(Case, case_id)
        self.revisions.append(case.revision)
        return [
            self.store.save_verdict(
                case.id,
                claim["id"],
                case.revision,
                self.policy,
                "reject",
                case.evidence_refs,
                {"reason": "independent rejection"},
                task_id=task_id,
                lease_token=lease_token,
            )
            for claim in case.claims
        ]


@pytest.mark.parametrize("fair", [False, True])
@pytest.mark.parametrize("with_case", [False, True])
async def test_restores_pending_delegation_and_rechecks_scope(tmp_path, fair, with_case):
    store, runtime, region, delegated = create_pending_delegation(tmp_path, with_case=with_case, fair=fair)
    model, verifier = Model(), Verifier(store)
    runtime.llm, runtime.verifier = model, verifier
    result = await asyncio.wait_for(runtime.run([region]), 15)
    assert result["status"] == "completed", result
    assert store.get(Question, "question").status == "answered"
    assert store.get(Task, delegated.id).status == "completed" and model.question_turns == 2
    assert all(scope["status"] == "reviewed" for scope in result["scopes"])
    if with_case:
        case = store.get(Case, "case")
        assert len(case.evidence_refs) == 2
        assert case.revision in verifier.revisions
        assert store.get_verdict(case.id, "claim", verifier.policy)["verdict"] == "reject"
    else:
        assert not store.list(Case) and not verifier.revisions


async def test_reviewer_native_delegation_enqueues_and_runs_independent_assistant(tmp_path):
    store, runtime, region, old = create_pending_delegation(tmp_path, fair=True)
    # This test starts with no question or task; retain the same fixed plan.
    # A fresh board is needed because real delegation records are immutable.
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    store = ReviewStore(fresh / "board.db", store.run)
    runtime = ReviewRuntime(store, None, PinnedGateway(), object())
    runtime.prepare([region])
    SharedInvestigations(store)

    class Delegating(Model):
        async def ainvoke(self, messages):
            await asyncio.sleep(0)
            board = json.loads(messages[1].content)
            if "question_id" in board:
                return await super().ainvoke(messages)
            previous = [json.loads(m.content) for m in messages if m.type == "tool"]
            refs = [item["evidence_ref"] for item in previous if "evidence_ref" in item]
            if board["unit_role"] == "integration":
                if not refs:
                    return AIMessage(content="", tool_calls=[call("read_source", {"path": "a.py"}, "integration-read")])
                return AIMessage(
                    content="",
                    tool_calls=[
                        *receipts(board["integration_obligations"], refs),
                        call("finish_integration", {}, "integration-finish"),
                    ],
                )
            questions = store.list(Question)
            if not questions:
                if not refs:
                    return AIMessage(content="", tool_calls=[call("read_source", {"path": "a.py"}, "initial")])
                return AIMessage(
                    content="",
                    tool_calls=[
                        call(
                            "save_question",
                            {
                                "statement": "Does this behavior occur?",
                                "evidence_refs": refs,
                            },
                            "new-question",
                        )
                    ],
                )
            question = questions[0]
            if question.status == "open" and not any(task.kind == "question" for task in store.list(Task)):
                return AIMessage(
                    content="",
                    tool_calls=[
                        call(
                            "delegate_question",
                            {
                                "question_id": question.id,
                                "reason": "Inspect the source independently",
                            },
                            "delegate-native",
                        )
                    ],
                )
            if question.status == "open" or not refs:
                return AIMessage(content="", tool_calls=[call("read_source", {"path": "a.py"}, "own-again")])
            return AIMessage(content="", tool_calls=[finish_unit("one", refs[0])])

    model = Delegating()
    runtime.llm = model
    result = await asyncio.wait_for(runtime.run([region]), 15)
    assert result["status"] == "completed", result
    tasks = [task for task in store.list(Task) if task.kind == "question"]
    assert len(tasks) == 1 and tasks[0].status == "completed"
    assert model.question_turns == 2
    assert store.list(Question)[0].status == "answered"


async def test_cancelled_assistant_reacquires_lease_and_rereads_its_source(tmp_path):
    store, runtime, region, delegated = create_pending_delegation(tmp_path, fair=True)
    entered = asyncio.Event()

    class Interrupted(Model):
        stopped = False

        async def ainvoke(self, messages):
            board = json.loads(messages[1].content)
            if "question_id" in board and self.question_turns == 1 and not self.stopped:
                self.stopped = True
                entered.set()
                await asyncio.Event().wait()
            return await super().ainvoke(messages)

    model = Interrupted()
    runtime.llm, runtime.verifier = model, Verifier(store)
    first = asyncio.create_task(runtime.run([region]))
    await asyncio.wait_for(entered.wait(), 5)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert store.get(Task, delegated.id).status == "pending"
    resumed = await asyncio.wait_for(runtime.run([region]), 15)
    assert resumed["status"] == "completed", resumed
    assert model.question_turns == 3  # First lease's source is not borrowed by the next lease.
    assert store.get(Task, delegated.id).status == "completed"


async def test_question_provider_account_failure_is_not_clean(tmp_path):
    store, runtime, region, delegated = create_pending_delegation(tmp_path)

    class PaymentError(Exception):
        status_code = 402

    class Blocked(Model):
        async def ainvoke(self, messages):
            raise PaymentError()

    runtime.llm, runtime.verifier = Blocked(), Verifier(store)
    result = await asyncio.wait_for(runtime.run([region]), 5)
    assert result["status"] == "provider_blocked"
    assert store.get(Question, "question").status == "open"
    assert store.get(Task, delegated.id).status != "completed"


async def test_existing_case_source_does_not_trigger_new_verification(tmp_path):
    store, runtime, _, delegated = create_pending_delegation(tmp_path, with_case=True)
    old_case = store.get(Case, "case")

    class ExistingSource(Model):
        async def ainvoke(self, messages):
            self.question_turns += 1
            if not any(message.type == "tool" for message in messages):
                return AIMessage(
                    content="", tool_calls=[call("read_evidence", {"ref": old_case.evidence_refs[0]}, "old-source")]
                )
            return answer(messages)

    scheduled = []
    runtime.llm = ExistingSource()
    runtime._enqueue = scheduled.append
    await runtime._answer_audited_question(delegated)
    assert store.get(Task, delegated.id).status == "completed"
    assert store.get(Question, "question").status == "answered"
    assert store.get(Case, "case").revision == old_case.revision
    assert scheduled == []


async def test_scope_change_fences_delegation_without_fabricating_answer(tmp_path):
    store, runtime, _, delegated = create_pending_delegation(tmp_path)
    review = store.acquire_task("delegator", "changed-reviewer", 120)
    store.update_scope_for_task(review.id, review.lease_token, review.input_revision, status="running")
    store.release_task(review.id, review.lease_token)
    runtime.llm = Model()
    await runtime._answer_audited_question(delegated)
    assert store.get(Task, delegated.id).status == "superseded", runtime._errors_real
    assert store.get(Question, "question").status == "open"
    assert runtime.llm.question_turns == 0


async def test_unrelated_navigation_and_checked_region_do_not_fence_assistant(tmp_path):
    store, runtime, _, delegated = create_pending_delegation(tmp_path, fine_fence=True)
    entered, resume = asyncio.Event(), asyncio.Event()

    class Waiting(Model):
        async def ainvoke(self, messages):
            if self.question_turns == 0:
                entered.set()
                await resume.wait()
            return await super().ainvoke(messages)

    runtime.llm = Waiting()
    work = asyncio.create_task(runtime._answer_audited_question(delegated))
    await asyncio.wait_for(entered.wait(), 5)
    review = store.acquire_task("delegator", "active-reviewer", 120)
    ref = store.put_evidence(
        b"region source\n",
        revision="head",
        side="head",
        kind="source",
        path="a.py",
        start_line=1,
        end_line=1,
        metadata={"workspace_digest": "head-snapshot"},
    )
    store.pin_frontier_for_task(review.id, review.lease_token, ref.id)
    payload = {"evidence_ref": ref.id, "content": "region source\n", "start_line": 1, "end_line": 1}
    store.append_turn_audit(
        review.id,
        review.lease_token,
        "decision",
        "request",
        [{"type": "tool", "data": {"content": json.dumps(payload)}}],
    )
    store.append_turn_audit(
        review.id, review.lease_token, "decision", "response", [{"type": "ai", "data": {"content": "checked"}}]
    )
    store.commit_unit_decision(
        review.id,
        review.lease_token,
        review.input_revision,
        "decision",
        checked_region_ids=["one"],
        observations=[{"statement": "Region checked", "evidence_refs": [ref.id], "region_ids": ["one"]}],
        questions=[],
        cases=[],
        unresolved=[],
        turn_id="decision",
    )
    review = store.get(Task, review.id)
    store.merge_working_states(
        review.id,
        review.lease_token,
        review.input_revision,
        [{"question": "Unrelated focus", "observations": [], "open_questions": [], "next_step": "Continue"}],
    )
    store.release_task(review.id, review.lease_token)
    resume.set()
    await asyncio.wait_for(work, 5)
    assert store.get(Task, delegated.id).status == "completed", runtime._errors_real
    assert store.get(Question, "question").status == "answered"


def create_second_delegation(store):
    q1 = store.get(Question, "question")
    q2 = store.add(Question("question-two", store.run.id, "Another question?", scope_id=q1.scope_id))
    review = store.acquire_task("delegator", "active-reviewer", 120)
    store.append_turn_audit(review.id, review.lease_token, "delegate-two", "request", [])
    response = [
        {
            "type": "ai",
            "data": {
                "content": "",
                "tool_calls": [
                    call("delegate_question", {"question_id": q2.id, "reason": "Investigate"}, "delegate-two")
                ],
            },
        }
    ]
    store.append_turn_audit(review.id, review.lease_token, "delegate-two", "response", response)
    second = store.create_question_task(
        review.id,
        review.lease_token,
        q2.id,
        q2.revision,
        turn_id="delegate-two",
        tool_call_id="delegate-two",
        instruction="Investigate",
    )
    store.append_turn_audit(review.id, review.lease_token, "delegate-two", "exchange", response)
    store.release_task(review.id, review.lease_token)
    return q1, q2, second


async def test_independent_assistants_can_commit_without_new_source(tmp_path):
    store, runtime, _, first = create_pending_delegation(tmp_path, fine_fence=True)
    q1, q2, second = create_second_delegation(store)
    entered = {q1.id: asyncio.Event(), q2.id: asyncio.Event()}
    resume = {q1.id: asyncio.Event(), q2.id: asyncio.Event()}

    class Independent(Model):
        async def ainvoke(self, messages):
            ident = json.loads(messages[1].content)["question_id"]
            entered[ident].set()
            await resume[ident].wait()
            return AIMessage(
                content="",
                tool_calls=[
                    call(
                        "answer_question",
                        {"status": "unresolved", "answer": "Additional source is needed", "evidence_refs": []},
                        "unresolved-" + ident,
                    )
                ],
            )

    runtime.llm = Independent()
    jobs = [asyncio.create_task(runtime._answer_audited_question(task)) for task in (first, second)]
    await asyncio.wait_for(asyncio.gather(*(event.wait() for event in entered.values())), 5)
    resume[q1.id].set()
    await asyncio.wait_for(jobs[0], 5)
    resume[q2.id].set()
    await asyncio.wait_for(jobs[1], 5)
    assert store.get(Task, first.id).status == store.get(Task, second.id).status == "completed", runtime._errors_real
    assert store.get(Question, q1.id).status == store.get(Question, q2.id).status == "unresolved"


async def test_new_pending_source_fences_other_assistant(tmp_path):
    store, runtime, _, first = create_pending_delegation(tmp_path, fine_fence=True)
    q1, q2, second = create_second_delegation(store)
    entered, resume = asyncio.Event(), asyncio.Event()

    class NewSource(Model):
        async def ainvoke(self, messages):
            if json.loads(messages[1].content)["question_id"] == q2.id:
                entered.set()
                await resume.wait()
                return AIMessage(
                    content="",
                    tool_calls=[
                        call(
                            "answer_question",
                            {"status": "unresolved", "answer": "Need a source", "evidence_refs": []},
                            "second-answer",
                        )
                    ],
                )
            return await super().ainvoke(messages)

    runtime.llm = NewSource()
    other = asyncio.create_task(runtime._answer_audited_question(second))
    await asyncio.wait_for(entered.wait(), 5)
    await asyncio.wait_for(runtime._answer_audited_question(first), 5)
    assert store.get(Question, q1.id).status == "answered"
    resume.set()
    await asyncio.wait_for(other, 5)
    assert store.get(Task, second.id).status == "superseded", runtime._errors_real
    assert store.get(Question, q2.id).status == "open"
    assert any(item["task_id"] == second.id for item in store.pending_shared_batches(q2.scope_id))


@pytest.mark.parametrize("target", ["question", "case"])
async def test_relevant_question_or_case_change_fences_assistant(tmp_path, target):
    store, runtime, _, delegated = create_pending_delegation(tmp_path, with_case=True, fine_fence=True)
    entered, resume = asyncio.Event(), asyncio.Event()

    class Waiting(Model):
        async def ainvoke(self, messages):
            entered.set()
            await resume.wait()
            return AIMessage(
                content="",
                tool_calls=[
                    call(
                        "answer_question",
                        {"status": "unresolved", "answer": "Need additional source", "evidence_refs": []},
                        "stale-answer",
                    )
                ],
            )

    runtime.llm = Waiting()
    work = asyncio.create_task(runtime._answer_audited_question(delegated))
    await asyncio.wait_for(entered.wait(), 5)
    if target == "question":
        old = store.get(Question, "question")
        store.update(Question, old.id, old.revision, statement="A materially changed question?")
    else:
        old = store.get(Case, "case")
        ref = store.put_evidence(
            b"new case source\n",
            revision="head",
            side="head",
            kind="source",
            path="a.py",
            start_line=1,
            end_line=1,
            metadata={"workspace_digest": "head-snapshot", "tool_name": "read_source", "tool_args": {"path": "a.py"}},
        )
        store.update(Case, old.id, old.revision, evidence_refs=[*old.evidence_refs, ref.id])
    resume.set()
    await asyncio.wait_for(work, 5)
    assert store.get(Task, delegated.id).status == "superseded", runtime._errors_real
    assert store.get(Question, "question").status == "open"
    assert any(item["task_id"] == delegated.id for item in store.pending_shared_batches(old.scope_id))


@pytest.mark.parametrize("fence_phase", ["provider", "response_recorded"])
async def test_scope_fence_during_provider_call_preserves_unexecuted_response(tmp_path, fence_phase):
    store, runtime, region, delegated = create_pending_delegation(tmp_path)
    started, resume = asyncio.Event(), asyncio.Event()

    class InFlight(Model):
        async def ainvoke(self, messages):
            started.set()
            await resume.wait()
            return AIMessage(
                content="",
                tool_calls=[
                    call(
                        "answer_question",
                        {"status": "answered", "answer": "An unverified answer", "evidence_refs": ["invented"]},
                        "in-flight-answer",
                    )
                ],
            )

    runtime.llm = InFlight()
    work = asyncio.create_task(runtime._answer_audited_question(delegated))
    await asyncio.wait_for(started.wait(), 5)

    def fence():
        review = store.acquire_task("delegator", "changed-reviewer", 120)
        store.update_scope_for_task(review.id, review.lease_token, review.input_revision, status="running")
        store.release_task(review.id, review.lease_token)

    if fence_phase == "provider":
        fence()
    else:
        append = store.append_turn_audit

        def append_then_fence(task_id, token, turn_id, phase, messages, **kwargs):
            result = append(task_id, token, turn_id, phase, messages, **kwargs)
            if task_id == delegated.id and phase == "response":
                fence()
            return result

        store.append_turn_audit = append_then_fence
    resume.set()
    await asyncio.wait_for(work, 5)

    assert store.get(Task, delegated.id).status == "superseded", runtime._errors_real
    assert store.get(Question, "question").status == "open"
    audit = store.read_turn_audit(delegated.id)
    responses = [entry for entry in audit if entry["phase"] == "response"]
    assert len(responses) == 1
    assert responses[0]["messages"][0]["data"]["tool_calls"][0]["id"] == "in-flight-answer"
    assert not any(entry["phase"].startswith("tool:") or entry["phase"] == "exchange" for entry in audit)
    question = store.get(Question, "question")
    pending = store.pending_shared_batches(question.scope_id)
    assert any(batch["task_id"] == delegated.id for batch in pending)
    assert runtime._record_incomplete_shared_batches(question.scope_id)
    assert any("in-flight-answer" in error for error in runtime._pending_batch_errors.values())

    class NoReplay(Model):
        async def ainvoke(self, messages):
            pytest.fail("an unresolved native batch cannot be replayed or bypassed")

    runtime.llm = NoReplay()
    result = await asyncio.wait_for(runtime.run([region]), 5)
    assert result["status"] == "partial", result
    assert store.get(Question, "question").status == "open"
    assert any(batch["task_id"] == delegated.id for batch in store.pending_shared_batches(question.scope_id))


@pytest.mark.parametrize("value, shared", [("bad", "shared-1"), ("question-delivery-1", "")])
async def test_invalid_product_option_fails_before_model_call(tmp_path, monkeypatch, value, shared):
    monkeypatch.setenv("REVIEWFORGE_V42_QUESTION_ASSISTANT", value)
    if shared:
        monkeypatch.setenv("REVIEWFORGE_V42_SHARED_INVESTIGATION", shared)
    else:
        monkeypatch.delenv("REVIEWFORGE_V42_SHARED_INVESTIGATION", raising=False)
    model = NativeModel(lambda *_: pytest.fail("invalid option must not call the model"))
    obj, _ = product(tmp_path, monkeypatch, model, model, TwoFileWorkspace(), policy=None)
    with pytest.raises(ValueError, match="unsupported v4.2 question assistant"):
        await obj.run(two_file_state())
    assert not model._inputs


async def test_product_option_is_identity_bound_and_does_not_upgrade_old_run(tmp_path, monkeypatch):
    monkeypatch.setenv("REVIEWFORGE_V42_SHARED_INVESTIGATION", "shared-1")
    monkeypatch.delenv("REVIEWFORGE_V42_QUESTION_ASSISTANT", raising=False)
    model = NativeModel(lambda *_: AIMessage(content="No decision yet"))
    obj, _ = product(tmp_path, monkeypatch, model, model, TwoFileWorkspace(), policy=None)
    obj._gateway._github = FixedBaseGitHub()
    old = await obj.run(two_file_state())
    assert "question_assistant_policy" not in old["review_v42_report"]["run"]["metadata"]
    assert "question_fencing_policy" not in old["review_v42_report"]["run"]["metadata"]
    monkeypatch.setenv("REVIEWFORGE_V42_QUESTION_ASSISTANT", "question-delivery-1")
    new = await obj.run(two_file_state())
    assert new["run_id"] != old["run_id"]
    assert new["review_v42_report"]["run"]["metadata"]["question_assistant_policy"] == "question-delivery-1"
    assert new["review_v42_report"]["run"]["metadata"]["question_fencing_policy"] == "question-input-1"
    assert (await obj.run(two_file_state()))["run_id"] == new["run_id"]


def test_fencing_policy_cannot_be_swapped_on_existing_run(tmp_path):
    store, _, _, _ = create_pending_delegation(tmp_path)
    changed = replace(store.run, metadata={**store.run.metadata, "question_fencing_policy": "question-input-1"})
    with pytest.raises(BindingError, match="different run or snapshot"):
        ReviewStore(store.path, changed)
    invalid = replace(store.run, id="invalid", metadata={**store.run.metadata, "question_fencing_policy": "unknown"})
    with pytest.raises(BindingError, match="unsupported question fencing policy"):
        ReviewStore(tmp_path / "invalid.db", invalid)
