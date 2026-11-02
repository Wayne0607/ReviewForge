import asyncio

from test_v42_reviewer import Model, ai, call, source

from reviewforge.review_v42.contracts import Case, Run, Task
from reviewforge.review_v42.runtime import ReviewRuntime
from reviewforge.review_v42.store import ReviewStore


async def test_resume_does_not_reopen_completed_scope(tmp_path):
    store = ReviewStore(tmp_path / "state.db", Run(id="r", repo="x/y", base="base", head="head"))
    regions = [{"id": "one", "path": "a.py"}]
    model = Model([ai(call("finish_scope", {"checked_regions": ["one"]}))])

    class Verifier:
        pass

    runtime = ReviewRuntime(store, model, source, Verifier())
    assert (await runtime.run(regions))["status"] == "completed"
    assert (await runtime.run(regions))["status"] == "completed"
    assert len(model.inputs) == 1


async def test_candidate_is_queued_without_waiting_for_verifier(tmp_path):
    store = ReviewStore(tmp_path / "state.db", Run(id="r", repo="x/y", base="base", head="head"))
    ref = store.put_evidence(b"src", revision="head", side="head", kind="source", path="a.py")
    case = {"trigger": "x", "behavior": "bad", "impact": "lost", "evidence_refs": [ref.id]}
    started, release = asyncio.Event(), asyncio.Event()

    class Verifier:
        async def verify(self, case_id, **kwargs):
            started.set()
            await release.wait()
            row = store.get(Case, case_id)
            return [
                store.save_verdict(
                    case_id,
                    row.claims[0]["id"],
                    row.revision,
                    "v42-independent-1",
                    "reject",
                    row.evidence_refs + row.counter_evidence_refs,
                    {"reason": "counterexample"},
                    task_id=kwargs["task_id"],
                    lease_token=kwargs["lease_token"],
                )
            ]

    class YieldingModel(Model):
        async def ainvoke(self, messages):
            if self.inputs:
                await asyncio.wait_for(started.wait(), 2)
                release.set()
            return await super().ainvoke(messages)

    model = YieldingModel([ai(call("submit_case", case)), ai(call("finish_scope", {"checked_regions": ["one"]}))])
    result = await ReviewRuntime(store, model, source, Verifier()).run([{"id": "one", "path": "a.py"}])
    assert result["status"] == "completed", result
    assert len(store.list(Case)) == 1
    assert store.list(Task)[0].status == "completed"


async def test_verifier_gap_returns_to_same_case_without_repeated_vote(tmp_path):
    from reviewforge.review_v42.contracts import Question, RevisionConflict

    store = ReviewStore(tmp_path / "state.db", Run(id="r", repo="x/y", base="base", head="head"))
    ref = store.put_evidence(b"src", revision="head", side="head", kind="source", path="a.py")
    data = {"trigger": "x", "behavior": "bad", "impact": "lost", "evidence_refs": [ref.id]}

    class GapVerifier:
        policy = "v42-independent-1"
        calls = 0

        async def verify(self, case_id, **kwargs):
            self.calls += 1
            case = store.get(Case, case_id)
            if not case.question_ids:
                q = Question(id="gap", run_id="r", scope_id=case.scope_id, case_id=case_id, statement="Which contract?")
                store.add(q)
                case = store.get(Case, case_id)
                store.update(Case, case_id, case.revision, question_ids=[q.id])
                raise RevisionConflict("new gap")
            return [
                store.save_verdict(
                    case_id,
                    case.claims[0]["id"],
                    case.revision,
                    self.policy,
                    "needs_evidence",
                    case.evidence_refs,
                    {"gaps": ["Which contract?"]},
                    task_id=kwargs["task_id"],
                    lease_token=kwargs["lease_token"],
                )
            ]

    model = Model(
        [
            ai(call("submit_case", data)),
            ai(call("finish_scope", {"checked_regions": ["one"]})),
            ai(call("save_question", {"question_id": "gap", "statement": "Which contract?", "status": "unresolved"})),
            ai(call("finish_scope", {"checked_regions": ["one"], "unresolved_refs": ["gap"]})),
        ]
    )
    verifier = GapVerifier()
    runtime = ReviewRuntime(store, model, source, verifier)
    regions = [{"id": "one", "path": "a.py"}]
    result = await runtime.run(regions)
    assert result["status"] == "partial"
    assert store.get(Question, "gap").status == "unresolved"
    assert verifier.calls == 2
    # A later run must not reopen the same semantic verdict absent new facts.
    model.turns = iter([ai(call("finish_scope", {"checked_regions": ["one"], "unresolved_refs": ["gap"]}))])
    await runtime.run(regions)
    assert verifier.calls == 2


async def test_delegated_question_uses_separate_leased_assistant(tmp_path):
    import json

    store = ReviewStore(tmp_path / "state.db", Run(id="r", repo="x/y", base="base", head="head"))
    completed = asyncio.Event()

    class Verifier:
        pass

    class DualModel:
        def __init__(self):
            self.review_step = 0
            self.helper_step = 0

        def bind_tools(self, tools, **kwargs):
            return self

        async def ainvoke(self, messages):
            if "Answer this one concrete question" in messages[0].content:
                self.helper_step += 1
                if self.helper_step == 1:
                    return ai(call("read_diff", {"path": "a.py"}))
                result = json.loads(messages[-1].content)
                completed.set()
                return ai(
                    call(
                        "answer_question",
                        {
                            "status": "answered",
                            "answer": "The source says so",
                            "evidence_refs": [result["evidence_ref"]],
                        },
                    )
                )
            self.review_step += 1
            if self.review_step == 1:
                return ai(call("save_question", {"statement": "Which value is used?"}))
            if self.review_step == 2:
                state = json.loads(messages[1].content)
                return ai(
                    call(
                        "delegate_question",
                        {"question_id": state["questions"][0]["id"], "reason": "independent source lookup"},
                    )
                )
            await asyncio.wait_for(completed.wait(), 2)
            return ai(call("finish_scope", {"checked_regions": ["one"]}))

    runtime = ReviewRuntime(store, DualModel(), source, Verifier())
    result = await runtime.run([{"id": "one", "path": "a.py"}])
    assert result["status"] == "completed", result
    tasks = [task for task in store.list(Task) if task.kind == "question"]
    assert len(tasks) == 1 and tasks[0].status == "completed"


async def test_explicit_partition_preserves_manifest_on_resume(tmp_path):
    store = ReviewStore(tmp_path / "state.db", Run(id="r", repo="x/y", base="base", head="head"))
    regions = [{"id": "one", "path": "a.py"}, {"id": "two", "path": "b.py"}]

    class ScopeModel:
        def bind_tools(self, tools, **kwargs):
            return self

        async def ainvoke(self, messages):
            import json

            region_id = json.loads(messages[1].content)["scope"]["regions"][0]["id"]
            return ai(call("finish_scope", {"checked_regions": [region_id]}))

    model = ScopeModel()

    class Verifier:
        pass

    runtime = ReviewRuntime(store, model, source, Verifier())
    parent = runtime.prepare(regions)[0]
    children = runtime.partition_scope(parent.id, [["one"], ["two"]])
    assert len(children) == 2
    assert (await runtime.run(regions))["status"] == "completed"
    assert (await runtime.run(regions))["status"] == "completed"


async def test_interrupted_reviewer_resumes_visible_tool_session(tmp_path):
    import pytest
    from langchain_core.messages import ToolMessage

    store = ReviewStore(tmp_path / "state.db", Run(id="r", repo="x/y", base="base", head="head"))
    regions = [{"id": "one", "path": "a.py"}]
    first = Model([ai(call("read_diff", {"path": "a.py"})), asyncio.CancelledError()])

    class Verifier:
        pass

    with pytest.raises(asyncio.CancelledError):
        await ReviewRuntime(store, first, source, Verifier()).run(regions)

    class ResumeModel(Model):
        async def ainvoke(self, messages):
            assert any(isinstance(message, ToolMessage) and "source" in message.content for message in messages)
            return await super().ainvoke(messages)

    resumed = ResumeModel([ai(call("finish_scope", {"checked_regions": ["one"]}))])
    assert (await ReviewRuntime(store, resumed, source, Verifier()).run(regions))["status"] == "completed"


async def test_provider_failure_cancels_inflight_verifier(tmp_path):
    store = ReviewStore(tmp_path / "state.db", Run(id="r", repo="x/y", base="base", head="head"))
    ref = store.put_evidence(b"src", revision="head", side="head", kind="source", path="a.py")
    data = {"trigger": "x", "behavior": "bad", "impact": "lost", "evidence_refs": [ref.id]}
    started, cancelled = asyncio.Event(), asyncio.Event()

    class BlockedError(Exception):
        status_code = 402

    class Verifier:
        async def verify(self, *args, **kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

    class FailModel(Model):
        async def ainvoke(self, messages):
            if self.inputs:
                await started.wait()
                raise BlockedError()
            return await super().ainvoke(messages)

    model = FailModel([ai(call("submit_case", data))])
    result = await ReviewRuntime(store, model, source, Verifier()).run([{"id": "one", "path": "a.py"}])
    assert result["status"] == "provider_blocked"
    assert cancelled.is_set()
    assert next(t for t in store.list(Task) if t.kind == "verify").status == "pending"


async def test_verifier_provider_failure_cancels_inflight_reviewer(tmp_path):
    store = ReviewStore(tmp_path / "state.db", Run(id="r", repo="x/y", base="base", head="head"))
    ref = store.put_evidence(b"src", revision="head", side="head", kind="source", path="a.py")
    data = {"trigger": "x", "behavior": "bad", "impact": "lost", "evidence_refs": [ref.id]}
    started, cancelled = asyncio.Event(), asyncio.Event()

    class BlockedError(Exception):
        status_code = 402

    class Verifier:
        async def verify(self, *args, **kwargs):
            await started.wait()
            raise BlockedError()

    class WaitingModel(Model):
        async def ainvoke(self, messages):
            if self.inputs:
                started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cancelled.set()
                    raise
            return await super().ainvoke(messages)

    model = WaitingModel([ai(call("submit_case", data))])
    result = await ReviewRuntime(store, model, source, Verifier()).run([{"id": "one", "path": "a.py"}])
    assert result["status"] == "provider_blocked"
    assert cancelled.is_set()
    assert next(t for t in store.list(Task) if t.kind == "review").status == "pending"


async def test_question_only_change_never_revotes_on_resume(tmp_path):
    from reviewforge.review_v42.contracts import Question
    from reviewforge.review_v42.scope import compile_scopes

    store = ReviewStore(tmp_path / "state.db", Run(id="r", repo="x/y", base="base", head="head"))
    regions = [{"id": "one", "path": "a.py"}]
    scope = compile_scopes(store.run, regions)[0]
    scope.status = "reviewed"
    store.add(scope)
    ref = store.put_evidence(b"src", revision="head", side="head", kind="source", path="a.py")
    case = Case(
        id="case",
        run_id="r",
        scope_id=scope.id,
        trigger="x",
        mechanism="m",
        claims=[{"id": "c", "statement": "m", "impact": "y"}],
        evidence_refs=[ref.id],
    )
    store.add(case)
    question = Question(id="q", run_id="r", scope_id=scope.id, case_id=case.id, statement="gap")
    store.add(question)
    case = store.get(Case, case.id)
    verify = Task(id="v", run_id="r", kind="verify", object_id=case.id, input_revision=case.revision)
    store.add(verify)
    verify = store.acquire_task(verify.id, "v", 60)
    verdict = store.save_verdict(
        case.id,
        "c",
        case.revision,
        "v42-independent-1",
        "unresolved",
        [ref.id],
        {"gaps": ["gap"]},
        task_id=verify.id,
        lease_token=verify.lease_token,
    )
    store.complete_task(
        verify.id, verify.lease_token, {"verdicts": [verdict], "case_input": ReviewRuntime._case_input(case)}
    )
    store.update(Question, question.id, question.revision, status="unresolved")

    class NoVote:
        async def verify(self, *args, **kwargs):
            raise AssertionError("question status alone cannot justify another verdict")

    result = await ReviewRuntime(store, Model([]), source, NoVote()).run(regions)
    assert result["status"] == "partial"
    assert len([task for task in store.list(Task) if task.kind == "verify"]) == 1


async def test_followup_new_evidence_reverifies_original_case(tmp_path):
    from reviewforge.review_v42.contracts import Question, RevisionConflict

    store = ReviewStore(tmp_path / "state.db", Run(id="r", repo="x/y", base="base", head="head"))
    ref = store.put_evidence(b"src", revision="head", side="head", kind="source", path="a.py")
    new_ref = store.put_evidence(b"new fact", revision="head", side="head", kind="source", path="b.py")
    data = {"trigger": "x", "behavior": "bad", "impact": "lost", "evidence_refs": [ref.id]}

    class GapVerifier:
        policy = "v42-independent-1"

        async def verify(self, case_id, **kwargs):
            case = store.get(Case, case_id)
            if not case.question_ids:
                q = Question(id="gap", run_id="r", scope_id=case.scope_id, case_id=case_id, statement="Which contract?")
                store.add(q)
                case = store.get(Case, case_id)
                store.update(Case, case_id, case.revision, question_ids=[q.id])
                raise RevisionConflict("new gap")
            verdict = "reject" if new_ref.id in case.evidence_refs else "needs_evidence"
            return [
                store.save_verdict(
                    case_id,
                    case.claims[0]["id"],
                    case.revision,
                    self.policy,
                    verdict,
                    case.evidence_refs,
                    {"gaps": ["contract"]},
                    task_id=kwargs["task_id"],
                    lease_token=kwargs["lease_token"],
                )
            ]

    model = Model(
        [
            ai(call("submit_case", data)),
            ai(call("finish_scope", {"checked_regions": ["one"]})),
            ai(
                call(
                    "save_question",
                    {
                        "question_id": "gap",
                        "statement": "Which contract?",
                        "status": "answered",
                        "answer": "The contract is explicit",
                        "evidence_refs": [new_ref.id],
                    },
                )
            ),
            ai(call("finish_scope", {"checked_regions": ["one"]})),
        ]
    )
    result = await ReviewRuntime(store, model, source, GapVerifier()).run([{"id": "one", "path": "a.py"}])
    case = store.list(Case)[0]
    assert new_ref.id in case.evidence_refs, result
    assert result["status"] == "completed", result
    assert result["verdicts"][0]["verdict"] == "reject"


def test_scope_partition_rejects_overlap(tmp_path):
    import pytest

    store = ReviewStore(tmp_path / "state.db", Run(id="r", repo="x/y", base="base", head="head"))
    regions = [
        {"id": "a", "path": "a.py", "start_line": 1, "end_line": 5},
        {"id": "b", "path": "a.py", "start_line": 4, "end_line": 8},
    ]
    runtime = ReviewRuntime(store, Model([]), source, object())
    scope = runtime.prepare(regions)[0]
    with pytest.raises(ValueError, match="overlapping"):
        runtime.partition_scope(scope.id, [["a"], ["b"]])
