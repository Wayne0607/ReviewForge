"""Durable incremental orchestration of reviewers and independent verification."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from typing import Any

from reviewforge.review_v42.contracts import (
    BindingError,
    Case,
    EvidenceRef,
    LeaseError,
    Question,
    ReviewScope,
    RevisionConflict,
    Task,
)
from reviewforge.review_v42.reviewer import QuestionAssistant, Reviewer, _status_code
from reviewforge.review_v42.scope import compile_scopes
from reviewforge.review_v42.store import _unit_source_identity
from reviewforge.review_v42.tools import EvidenceGateway


class _FairBoundModel:
    """Share one FIFO model-call lane while reviewers retain their own sessions."""

    def __init__(self, model: Any, runtime: ReviewRuntime, task_id: str, lease_token: str):
        self.model, self.runtime = model, runtime
        self.task_id, self.lease_token = task_id, lease_token

    async def ainvoke(self, messages: Any, *args: Any, **kwargs: Any) -> Any:
        # Give all newly started reviewers a chance to enter the FIFO lock,
        # including when a test or cached provider responds synchronously.
        await asyncio.sleep(0)
        async with self.runtime._model_lane:
            if self.runtime._blocked:
                raise asyncio.CancelledError()
            try:
                task = self.runtime.store.get(Task, self.task_id)
                if task.status != "leased" or task.lease_token != self.lease_token or task.lease_expires <= time.time():
                    raise LeaseError("review lease expired while waiting for the model lane")
                if task.kind == "question":
                    self.runtime.store.assert_question_task_current(task.id, self.lease_token)
                else:
                    scope = self.runtime.store.get(ReviewScope, task.object_id)
                    if task.input_revision != scope.revision:
                        raise RevisionConflict("review input changed while waiting for the model lane")
                return await self.model.ainvoke(messages, *args, **kwargs)
            except Exception as exc:
                if _status_code(exc) in {401, 402, 403}:
                    self.runtime._blocked = True
                    self.runtime._blocked_event.set()
                raise


class _FairModel:
    def __init__(self, model: Any, runtime: ReviewRuntime, task_id: str, lease_token: str):
        self.model, self.runtime = model, runtime
        self.task_id, self.lease_token = task_id, lease_token

    def bind_tools(self, *args: Any, **kwargs: Any) -> _FairBoundModel:
        return _FairBoundModel(self.model.bind_tools(*args, **kwargs), self.runtime, self.task_id, self.lease_token)


class ReviewRuntime:
    def __init__(self, store: Any, llm: Any, gateway: Any, verifier: Any):
        metadata = store.run.metadata
        if "decision_cycle_policy" in metadata and (
            metadata["decision_cycle_policy"] != "decision-cycle-1"
            or store.run.policy != "v42-units-6"
            or metadata.get("shared_investigation") != "shared-1"
            or metadata.get("candidate_draft_policy") != "candidate-draft-1"
            or metadata.get("frontier_projection_policy") != "source-line-union-1"
            or metadata.get("source_line_policy") != "git-lf-1"
            or "question_assistant_policy" in metadata
        ):
            raise ValueError("unsupported v4.2 decision cycle policy")
        if "verification_delivery_policy" in metadata and (
            metadata["verification_delivery_policy"] != "claim-delivery-1"
            or metadata.get("decision_cycle_policy") != "decision-cycle-1"
        ):
            raise ValueError("unsupported v4.2 verification delivery policy")
        if "frontier_projection_policy" in metadata and (
            metadata["frontier_projection_policy"] != "source-line-union-1"
            or store.run.policy != "v42-units-6"
            or metadata.get("shared_investigation") != "shared-1"
            or metadata.get("source_line_policy") != "git-lf-1"
        ):
            raise ValueError("unsupported v4.2 frontier projection policy")
        self.store, self.llm, self.verifier = store, llm, verifier
        self.gateway = gateway if isinstance(gateway, EvidenceGateway) else EvidenceGateway(store, gateway)
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._enqueued: set[str] = set()
        self._blocked = False
        self._blocked_event = asyncio.Event()
        self._errors_real: list[str] = []
        self._pending_batch_errors: dict[tuple[str, str, str], str] = {}
        self._model_lane = asyncio.Lock()
        self._work_changed = asyncio.Event()

    @property
    def _errors(self) -> list[str]:
        return [*self._errors_real, *self._pending_batch_errors.values()]

    def _fair_enabled(self) -> bool:
        return (
            self.store.run.policy == "v42-units-6" and self.store.run.metadata.get("unit_scheduler") == "cooperative-1"
        )

    def _question_enabled(self) -> bool:
        return (
            self.store.run.policy == "v42-units-6"
            and self.store.run.metadata.get("shared_investigation") == "shared-1"
            and self.store.run.metadata.get("question_assistant_policy") == "question-delivery-1"
        )

    def prepare(self, regions: list[dict[str, Any]]) -> list[ReviewScope]:
        """Persist or validate the complete manifest before optional scope partitioning."""
        compiled = compile_scopes(self.store.run, regions)
        if self.store.run.policy == "v42-units-6":
            if not compiled:
                return []
            identity = hashlib.sha256((self.store.run.id + ":units-6:integration").encode()).hexdigest()[:20]
            integration = ReviewScope(
                id="integration_" + identity,
                run_id=self.store.run.id,
                title="Cross-unit integration",
                regions=[],
                dependencies=sorted(scope.id for scope in compiled),
            )
            plan = self.store.install_unit_plan(compiled, integration)
            if sorted(plan.primary_scope_ids) != sorted(scope.id for scope in compiled):
                raise ValueError("work-unit plan differs from the fixed manifest")
            return [self.store.get(ReviewScope, ident) for ident in plan.primary_scope_ids]
        existing = self.store.list(ReviewScope)
        if not existing:
            for scope in compiled:
                self.store.add(scope)
            return compiled
        active = [scope for scope in existing if scope.status != "superseded"]
        expected = {row["id"]: row for scope in compiled for row in scope.regions}
        actual_rows = [row for scope in active for row in scope.regions]
        actual = {row["id"]: row for row in actual_rows}
        if len(actual_rows) != len(actual) or actual != expected:
            raise ValueError("changed-region manifest differs from checkpoint")
        return active

    def partition_scope(self, scope_id: str, groups: list[list[str]]) -> list[ReviewScope]:
        """Explicitly partition a pending scope; the store checks exact ownership atomically."""
        scope = self.store.get(ReviewScope, scope_id)
        by_id = {region["id"]: region for region in scope.regions}
        # Overlapping regions belong to one reviewer even when the caller
        # proposes a split. Unknown ranges are conservatively grouped by path.
        for index, left in enumerate(groups):
            for right in groups[index + 1 :]:
                for left_id in left:
                    for right_id in right:
                        a, b = by_id.get(left_id), by_id.get(right_id)
                        if not a or not b or a.get("path") != b.get("path") or a.get("side") != b.get("side"):
                            continue
                        starts = (a.get("start_line"), b.get("start_line"))
                        ends = (a.get("end_line"), b.get("end_line"))
                        if not all(isinstance(value, int) and value > 0 for value in (*starts, *ends)):
                            raise ValueError("unknown or overlapping regions must share one scope")
                        if max(starts) <= min(ends):
                            raise ValueError("overlapping regions must share one scope")
        children = []
        for group in groups:
            if not group or any(region_id not in by_id for region_id in group):
                raise ValueError("each partition needs existing region IDs")
            key = scope.id + ":" + ":".join(sorted(group))
            children.append(
                ReviewScope(
                    id="scope_" + hashlib.sha256(key.encode()).hexdigest()[:20],
                    run_id=scope.run_id,
                    title=scope.title,
                    regions=[by_id[region_id] for region_id in group],
                    dependencies=list(scope.dependencies),
                )
            )
        self.store.partition_scope(scope.id, scope.revision, children)
        return children

    @staticmethod
    def _case_input(case: Case) -> dict[str, Any]:
        return {
            "trigger": case.trigger,
            "mechanism": case.mechanism,
            "claims": case.claims,
            "evidence_refs": sorted(case.evidence_refs),
            "counter_evidence_refs": sorted(case.counter_evidence_refs),
        }

    def _sync_case_evidence(self, case: Case) -> Case:
        if self._question_enabled():
            # The audited answer transaction binds new case evidence itself.
            return case
        refs = {
            ref
            for q in self.store.list(Question)
            if q.case_id == case.id and q.status == "answered"
            for ref in q.evidence_refs
        }
        if refs - set(case.evidence_refs + case.counter_evidence_refs):
            case = self.store.update(Case, case.id, case.revision, evidence_refs=sorted(set(case.evidence_refs) | refs))
        return case

    def _ack_decision_verification(self, case: Case, task: Task) -> None:
        if self.store.run.metadata.get("decision_cycle_policy") != "decision-cycle-1":
            return
        from reviewforge.review_v42.decision_state import DecisionState

        decisions = DecisionState(self.store)
        for item in decisions.pending_verifications():
            if item["case_id"] == case.id:
                try:
                    decisions.ack_verification(case.id, item["case_revision"], task.id)
                except (BindingError, RevisionConflict):
                    # Another durable transition may supersede this snapshot.
                    # Preserve readiness for the next scan, but do not hide a
                    # binding error on the same current Case and Task.
                    current_case = self.store.get(Case, case.id)
                    current_task = self.store.get(Task, task.id)
                    latest = decisions.pending_verifications()
                    if current_case != case or current_task != task or item not in latest:
                        return
                    raise

    def _enqueue(self, case: Case) -> None:
        # A verdict is never re-sampled on unchanged claims and source evidence,
        # including when a question-only edit has advanced the case revision.
        for previous in self.store.list(Task):
            if (
                previous.kind == "verify"
                and previous.object_id == case.id
                and previous.status == "completed"
                and previous.input_revision != case.revision
                and previous.result.get("case_input") == self._case_input(case)
            ):
                self._ack_decision_verification(case, previous)
                return
            detail = previous.result.get("detail", {}) if previous.status == "superseded" else previous.result
            if (
                previous.kind == "followup"
                and previous.object_id == case.id
                and previous.status in {"completed", "superseded"}
                and detail.get("new_evidence") is False
                and detail.get("claims") == case.claims
                and detail.get("evidence_refs") == sorted(case.evidence_refs + case.counter_evidence_refs)
            ):
                for verified in self.store.list(Task):
                    if (
                        verified.kind == "verify"
                        and verified.object_id == case.id
                        and verified.status == "completed"
                        and verified.result.get("case_input") == self._case_input(case)
                    ):
                        self._ack_decision_verification(case, verified)
                        break
                return
        key = f"{case.id}:{case.revision}"
        task_id = "verify_" + hashlib.sha256(key.encode()).hexdigest()[:24]
        try:
            task = self.store.get(Task, task_id)
        except KeyError:
            task = self.store.add(
                Task(
                    id=task_id, run_id=self.store.run.id, kind="verify", object_id=case.id, input_revision=case.revision
                )
            )
        if task.status != "completed" and task_id not in self._enqueued:
            self._enqueued.add(task_id)
            self._queue.put_nowait(task_id)
        # The durable Task exists before consuming the transactional outbox.
        # A crash before this point is recovered by the normal Case scan.
        self._ack_decision_verification(case, task)

    def _enqueue_question(
        self, question: Question, reason: str, task_id: str, token: str, turn_id: str = "", call_id: str = ""
    ) -> Task:
        if self._question_enabled():
            task = self.store.create_question_task(
                task_id,
                token,
                question.id,
                question.revision,
                turn_id=turn_id,
                tool_call_id=call_id,
                instruction=reason,
            )
            if task.status not in {"completed", "superseded"} and task.id not in self._enqueued:
                self._enqueued.add(task.id)
                self._queue.put_nowait(task.id)
            return task
        key = f"{question.id}:{question.revision}"
        ident = "question_" + hashlib.sha256(key.encode()).hexdigest()[:24]
        try:
            task = self.store.get(Task, ident)
        except KeyError:
            task = self.store.add_for_task(
                task_id,
                token,
                Task(
                    id=ident,
                    run_id=self.store.run.id,
                    kind="question",
                    object_id=question.id,
                    input_revision=question.revision,
                    instruction=reason,
                ),
            )
        if task.status not in {"completed", "superseded"} and ident not in self._enqueued:
            self._enqueued.add(ident)
            self._queue.put_nowait(ident)
        return task

    async def _answer_question(self, task: Task) -> None:
        if self._question_enabled():
            await self._answer_audited_question(task)
            return
        question = self.store.get(Question, task.object_id)
        if question.revision != task.input_revision:
            return
        try:
            task = self.store.acquire_task(task.id, "v42-question-assistant", 120)
        except LeaseError:
            return
        heartbeat = asyncio.create_task(self._heartbeat(task, 120))
        try:
            answer = await QuestionAssistant(self.store, self.llm, self.gateway).run(task)
            self.store.complete_task(task.id, task.lease_token, answer)
            question = self.store.get(Question, task.object_id)
            if question.case_id and question.status == "answered":
                case = self.store.get(Case, question.case_id)
                before = self._case_input(case)
                case = self._sync_case_evidence(case)
                if self._case_input(case) != before:
                    self._enqueue(case)
        except asyncio.CancelledError:
            self.store.release_task(task.id, task.lease_token)
            raise
        except Exception as exc:
            if _status_code(exc) in {401, 402, 403}:
                self._blocked = True
                self._blocked_event.set()
            self._errors_real.append(f"question {question.id}: {type(exc).__name__}")
            try:
                self.store.release_task(task.id, task.lease_token)
            except LeaseError:
                pass
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)

    async def _answer_audited_question(self, task: Task) -> None:
        """A delegated assistant owns only its leased, delivery-audited question."""
        try:
            task = self.store.acquire_task(task.id, "v42-question-assistant", 120)
        except RevisionConflict:
            try:
                self.store.supersede_stale_question_task(task.id)
            except (LeaseError, RevisionConflict):
                pass
            return
        except LeaseError:
            return
        heartbeat = asyncio.create_task(self._heartbeat(task, 120))
        try:
            initial_question = self.store.assert_question_task_current(task.id, task.lease_token)
            case_inputs = {
                case.id: self._case_input(case)
                for case in self.store.list(Case)
                if case.id == initial_question.case_id or initial_question.id in case.question_ids
            }
            llm = _FairModel(self.llm, self, task.id, task.lease_token) if self._fair_enabled() else self.llm
            answer = await QuestionAssistant(self.store, llm, self.gateway).run(task)
            if (
                not isinstance(answer, dict)
                or answer.get("status") not in {"answered", "unresolved"}
                or answer.get("question_id") != task.object_id
            ):
                raise ValueError("question assistant did not commit a complete answer")
            question = self.store.assert_question_task_current(task.id, task.lease_token)
            if question.status != answer["status"] or question.evidence_refs != answer.get("evidence_refs"):
                raise BindingError("question answer differs from its committed state")
            self.store.complete_task(task.id, task.lease_token, answer)
            if question.status == "answered":
                for case in self.store.list(Case):
                    if (case.id == question.case_id or question.id in case.question_ids) and (
                        case_inputs.get(case.id) != self._case_input(case)
                    ):
                        self._enqueue(case)
        except asyncio.CancelledError:
            try:
                self.store.release_task(task.id, task.lease_token)
            except LeaseError:
                pass
            raise
        except RevisionConflict:
            try:
                self.store.settle_question_task_stale(task.id, task.lease_token, {"status": "superseded"})
            except (LeaseError, RevisionConflict):
                self._errors_real.append(f"question {task.object_id}: could not settle stale task")
        except Exception as exc:
            if _status_code(exc) in {401, 402, 403}:
                self._blocked = True
                self._blocked_event.set()
            self._errors_real.append(f"question {task.object_id}: {type(exc).__name__}")
            try:
                self.store.release_task(task.id, task.lease_token)
            except LeaseError:
                pass
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)

    async def _heartbeat(self, task: Task, ttl: float) -> None:
        while True:
            await asyncio.sleep(ttl / 3)
            self.store.renew_task(task.id, task.lease_token, ttl)

    async def _worker(self) -> None:
        while True:
            task_id = await self._queue.get()
            try:
                if self._blocked:
                    continue
                task = self.store.get(Task, task_id)
                if task.kind == "question":
                    await self._answer_question(task)
                    continue
                case = self.store.get(Case, task.object_id)
                if case.revision != task.input_revision:
                    self._enqueue(case)
                    continue
                try:
                    task = self.store.acquire_task(task_id, "v42-verifier", 120)
                except LeaseError:
                    # Another live worker owns this revision. It is still pending.
                    continue
                except RevisionConflict:
                    self._enqueue(self.store.get(Case, task.object_id))
                    continue
                heartbeat = asyncio.create_task(self._heartbeat(task, 120))
                try:
                    result = await self.verifier.verify(case.id, task_id=task.id, lease_token=task.lease_token)
                    policy = getattr(self.verifier, "policy", "v42-independent-1")
                    if self.store.get(Case, case.id).revision != task.input_revision:
                        raise RevisionConflict("case changed before verification completion")
                    if not isinstance(result, list) or len(result) != len(case.claims):
                        raise ValueError("verifier did not deliver every claim")
                    for claim in case.claims:
                        persisted = self.store.get_verdict(case.id, claim["id"], policy)
                        if persisted is None or persisted not in result:
                            if self.store.get(Case, case.id).revision != task.input_revision:
                                raise RevisionConflict("case changed before verification completion")
                            raise ValueError("verdict is not frozen in the store")
                    self.store.complete_task(
                        task.id, task.lease_token, {"verdicts": result, "case_input": self._case_input(case)}
                    )
                except asyncio.CancelledError:
                    try:
                        self.store.release_task(task.id, task.lease_token)
                    except LeaseError:
                        pass
                    raise
                except RevisionConflict:
                    try:
                        self.store.release_task(task.id, task.lease_token)
                    except LeaseError:
                        pass
                    self._enqueue(self.store.get(Case, case.id))
                except Exception as exc:
                    if _status_code(exc) in {401, 402, 403}:
                        self._blocked = True
                        self._blocked_event.set()
                    self._errors_real.append(f"verification {case.id}: {type(exc).__name__}")
                    try:
                        self.store.release_task(task.id, task.lease_token)
                    except LeaseError:
                        pass
                finally:
                    heartbeat.cancel()
                    await asyncio.gather(heartbeat, return_exceptions=True)
            finally:
                self._enqueued.discard(task_id)
                self._queue.task_done()
                if self._fair_enabled():
                    self._work_changed.set()

    async def _drain(self) -> None:
        """Wait for queued work, but wake immediately on an account-level failure."""
        joined = asyncio.create_task(self._queue.join())
        blocked = asyncio.create_task(self._blocked_event.wait())
        try:
            await asyncio.wait({joined, blocked}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            joined.cancel()
            blocked.cancel()
            await asyncio.gather(joined, blocked, return_exceptions=True)

    async def _review_scope(self, scope_id: str, *, focus: str = "", reopen: bool = False) -> str:
        scope = self.store.get(ReviewScope, scope_id)
        if self.store.run.policy == "v42-units-6" and self.store.run.metadata.get("shared_investigation") == "shared-1":
            if self._record_incomplete_shared_batches(scope_id):
                return "partial"
        if scope.status == "reviewed" and not reopen:
            return "reviewed"
        if reopen:
            # A newly materialized verifier gap changes the scope's responsibility;
            # its revision distinguishes this targeted pass from the completed one.
            scope = self.store.update(ReviewScope, scope.id, scope.revision, status="pending")
        task = next(
            (
                t
                for t in self.store.list(Task)
                if t.kind == "review"
                and t.object_id == scope_id
                and t.input_revision == scope.revision
                and t.status != "completed"
            ),
            None,
        )
        if task is None:
            key = f"{scope_id}:{scope.revision}"
            task_id = "review_" + hashlib.sha256(key.encode()).hexdigest()[:24]
            task = self.store.add(
                Task(
                    id=task_id,
                    run_id=self.store.run.id,
                    kind="review",
                    object_id=scope_id,
                    input_revision=scope.revision,
                )
            )
        try:
            task = self.store.acquire_task(task.id, "v42-reviewer", 120)
        except LeaseError:
            self._errors_real.append(f"review {scope_id}: lease unavailable")
            return "partial"
        heartbeat = asyncio.create_task(self._heartbeat(task, 120))
        reviewer_class = Reviewer
        if self.store.run.metadata.get("decision_cycle_policy") == "decision-cycle-1":
            from reviewforge.review_v42.decision_loop import DecisionReviewer

            reviewer_class = DecisionReviewer
        reviewer = reviewer_class(
            self.store,
            _FairModel(self.llm, self, task.id, task.lease_token) if self._fair_enabled() else self.llm,
            self.gateway,
            self._enqueue,
            task_id=task.id,
            lease_token=task.lease_token,
            focus=focus,
            on_question=lambda question, reason, *delivery: self._enqueue_question(
                question, reason, task.id, task.lease_token, *delivery
            ),
        )
        review = asyncio.create_task(reviewer.run(scope_id, reopen=reopen))
        blocked = asyncio.create_task(self._blocked_event.wait())
        try:
            done, _ = await asyncio.wait({review, blocked}, return_when=asyncio.FIRST_COMPLETED)
            if blocked in done and self._blocked:
                review.cancel()
                await asyncio.gather(review, return_exceptions=True)
                self.store.release_task(task.id, task.lease_token)
                return "provider_blocked"
            status = await review
            if self.store.run.metadata.get("shared_investigation") == "shared-1":
                latest_task = self.store.get(Task, task.id)
                latest_scope = self.store.get(ReviewScope, scope_id)
                if latest_task.input_revision != latest_scope.revision:
                    self.store.settle_task_stale(task.id, task.lease_token, {"status": "superseded"})
                    return "superseded"
            if reviewer.error:
                if self.store.run.metadata.get("shared_investigation") == "shared-1" and reviewer.error.startswith(
                    "incomplete_native_batches:"
                ):
                    self._record_incomplete_shared_batches(scope_id)
                else:
                    self._errors_real.append(f"review {scope_id}: {reviewer.error}")
            if status == "reviewed" or (self.store.run.policy != "v42-units-6" and reopen and status == "partial"):
                self.store.complete_task(task.id, task.lease_token, {"status": status, "focus": focus})
            else:
                self.store.release_task(task.id, task.lease_token)
            return status
        except asyncio.CancelledError:
            review.cancel()
            await asyncio.gather(review, return_exceptions=True)
            try:
                self.store.release_task(task.id, task.lease_token)
            except LeaseError:
                pass
            raise
        except (BindingError, RevisionConflict):
            if self.store.run.policy != "v42-units-6":
                raise
            latest_task = self.store.get(Task, task.id)
            latest_scope = self.store.get(ReviewScope, scope_id)
            if latest_task.input_revision == latest_scope.revision:
                raise
            # Verifier evidence can invalidate a running unit while the model is
            # responding. Its expired input must never commit as a completed review.
            try:
                self.store.settle_task_stale(task.id, task.lease_token, {"status": "superseded"})
            except (LeaseError, RevisionConflict):
                self._errors_real.append(f"review {scope_id}: could not settle stale task")
                return "partial"
            return "superseded"
        finally:
            blocked.cancel()
            heartbeat.cancel()
            await asyncio.gather(blocked, heartbeat, return_exceptions=True)

    async def _follow_up(self, *, attempted_inputs: set[str] | None = None) -> bool:
        """Return actionable verifier gaps to their original case once per frozen input."""
        policy = getattr(self.verifier, "policy", "v42-independent-1")
        progressed = False
        for case in self.store.list(Case):
            decisions = [self.store.get_verdict(case.id, claim["id"], policy) for claim in case.claims]
            if not any(d and d["verdict"] == "needs_evidence" for d in decisions):
                continue
            if attempted_inputs is not None:
                key = json.dumps(
                    [
                        case.id,
                        self._case_input(case),
                        sorted(q.id for q in self.store.list(Question) if q.case_id == case.id),
                    ],
                    sort_keys=True,
                    ensure_ascii=False,
                )
                if key in attempted_inputs:
                    continue
                attempted_inputs.add(key)
            follow_id = "follow_" + hashlib.sha256(f"{case.id}:{case.revision}".encode()).hexdigest()[:24]
            try:
                follow = self.store.get(Task, follow_id)
            except KeyError:
                follow = self.store.add(
                    Task(
                        id=follow_id,
                        run_id=self.store.run.id,
                        kind="followup",
                        object_id=case.id,
                        input_revision=case.revision,
                    )
                )
            if follow.status in {"completed", "superseded"}:
                continue
            # Followup ownership is a separate auditable marker; the review task owns scope changes.
            follow = self.store.acquire_task(follow.id, "v42-followup", 120)
            follow_heartbeat = asyncio.create_task(self._heartbeat(follow, 120))
            before = set(case.evidence_refs + case.counter_evidence_refs)
            questions = [q for q in self.store.list(Question) if q.case_id == case.id and q.status == "open"]
            focus = "Resolve these verifier gaps for the original case only: " + "; ".join(
                q.statement for q in questions
            )
            try:
                status = await self._review_scope(case.scope_id, focus=focus, reopen=True)
                if status in {"provider_blocked", "error", "stalled", "interrupted"}:
                    if status == "provider_blocked":
                        self._blocked = True
                        self._blocked_event.set()
                    self.store.release_task(follow.id, follow.lease_token)
                    return False
                current = self.store.get(Case, case.id)
                refs = {ref for q in self.store.list(Question) if q.case_id == case.id for ref in q.evidence_refs}
                new_refs = refs - before
                if new_refs:
                    current = self.store.update(
                        Case, current.id, current.revision, evidence_refs=sorted(set(current.evidence_refs) | new_refs)
                    )
                    progressed = True
                else:
                    for q in self.store.list(Question):
                        if q.case_id == case.id and q.status == "open":
                            self.store.update(
                                Question,
                                q.id,
                                q.revision,
                                status="unresolved",
                                answer="No new evidence was obtained; a new source is needed.",
                            )
                # Scope/question changes can revise the case; this marker intentionally remains
                # bound to the old input and is recorded by releasing if it is now stale.
                current = self.store.get(Case, case.id)
                detail = {
                    "status": status,
                    "new_evidence": bool(new_refs),
                    "claims": current.claims,
                    "evidence_refs": sorted(current.evidence_refs + current.counter_evidence_refs),
                }
                try:
                    self.store.complete_task(follow.id, follow.lease_token, detail)
                except RevisionConflict:
                    self.store.settle_task_stale(follow.id, follow.lease_token, detail)
                if new_refs:
                    self._enqueue(current)
            except BaseException:
                try:
                    self.store.release_task(follow.id, follow.lease_token)
                except LeaseError:
                    pass
                raise
            finally:
                follow_heartbeat.cancel()
                await asyncio.gather(follow_heartbeat, return_exceptions=True)
        return progressed

    def _unit_reopen_input(self, scope_id: str, *, integration: bool = False) -> str:
        """Identify substantive work, excluding lease, timestamps and note wording."""
        cases = sorted(
            ((case.id, self._case_input(case)) for case in self.store.list(Case) if case.scope_id == scope_id),
            key=lambda item: item[0],
        )
        questions = sorted(
            (q.id, q.case_id, q.status, sorted(q.evidence_refs))
            for q in self.store.list(Question)
            if q.scope_id == scope_id
        )
        obligations = (
            sorted((o["id"], o["input_digest"]) for o in self.store.list_integration_obligations())
            if integration
            else []
        )
        inputs = [cases, questions, obligations]
        if self.store.run.metadata.get("shared_investigation") == "shared-1":
            inputs.append(self._shared_unit_input(scope_id, integration=integration))
        return json.dumps(inputs, sort_keys=True, ensure_ascii=False)

    def _shared_unit_input(self, scope_id: str, *, integration: bool) -> dict[str, Any]:
        """Read only relevant shared decisions and original source identities in one snapshot."""
        with self.store._connect() as db:
            db.execute("BEGIN")

            def source_identity(ref_id: str) -> str | None:
                row = db.execute("SELECT payload FROM objects WHERE kind='EvidenceRef' AND id=?", (ref_id,)).fetchone()
                if row is None:
                    return None
                ref = EvidenceRef(**json.loads(row[0]))
                if (
                    ref.run_id != self.store.run.id
                    or ref.repo != self.store.run.repo
                    or ref.kind != "source"
                    or ref.side not in {"head", "base"}
                    or ref.revision != getattr(self.store.run, ref.side)
                    or ref.metadata.get("workspace_digest") != self.store.run.metadata.get("workspace_digest")
                    or ref.metadata.get("navigation_only") is True
                    or ref.metadata.get("complete", True) is not True
                    or ref.metadata.get("status", "available") not in {"available", "success", "ok"}
                ):
                    return None
                return json.dumps(_unit_source_identity(ref), sort_keys=True, ensure_ascii=False)

            pending = sorted(
                (item["scope_id"], item["region_id"], source_identity(item["ref_id"]) or "invalid-source")
                for item in self.store.pending_unit_sources(db=db)
                if integration or item["scope_id"] == scope_id
            )
            exists = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='shared_investigations'"
            ).fetchone()
            items = []
            if exists:
                for ident, payload in db.execute("SELECT id,payload FROM shared_investigations ORDER BY id"):
                    item = json.loads(payload)
                    if not integration and scope_id not in item.get("scope_ids", []):
                        continue
                    refs = {
                        ref
                        for observation in item.get("observations", [])
                        if isinstance(observation, dict)
                        for ref in observation.get("evidence_refs", [])
                        if isinstance(ref, str)
                    }
                    items.append(
                        (
                            ident,
                            item.get("status"),
                            sorted(item.get("scope_ids", [])),
                            sorted(item.get("region_ids", [])),
                            sorted({identity for ref in refs if (identity := source_identity(ref)) is not None}),
                        )
                    )
            return {"investigations": items, "pending_sources": pending}

    def _record_incomplete_shared_batches(self, scope_id: str) -> bool:
        try:
            pending = self.store.pending_shared_batches(scope_id)
        except (BindingError, RevisionConflict, KeyError, OSError, ValueError, TypeError) as exc:
            self._pending_batch_errors[(scope_id, "", "")] = f"review {scope_id}: native batch audit unavailable: {exc}"
            return True
        for batch in pending:
            try:
                self.store.abort_fenced_read_batch(batch["task_id"], batch["turn_id"])
            except (BindingError, RevisionConflict, KeyError, OSError, ValueError, TypeError):
                # Unknown or mutating actions remain explicitly pending.
                pass
        if pending:
            try:
                pending = self.store.pending_shared_batches(scope_id)
            except (BindingError, RevisionConflict, KeyError, OSError, ValueError, TypeError) as exc:
                self._pending_batch_errors[(scope_id, "", "")] = (
                    f"review {scope_id}: native batch audit unavailable: {exc}"
                )
                return True
        current = {}
        for batch in pending:
            calls = ", ".join(
                f"{call.get('name')}({call.get('id')}):{call.get('receipt')}" for call in batch.get("calls", [])
            )
            detail = (
                f"review {scope_id}: incomplete native batch {batch['task_id']}/{batch['turn_id']}: "
                f"{batch.get('reason', 'unfinished')}" + (f"; {calls}" if calls else "")
            )
            current[(scope_id, batch["task_id"], batch["turn_id"])] = detail
        self._pending_batch_errors = {
            **{key: value for key, value in self._pending_batch_errors.items() if key[0] != scope_id},
            **current,
        }
        return bool(pending)

    async def _run_fair_primaries(
        self, scopes: list[ReviewScope], attempted: set[tuple[str, str]], *, initial: bool
    ) -> None:
        """Keep independent leased reviewers runnable while serializing their model calls."""
        active: dict[str, asyncio.Task[str]] = {}
        first = {scope.id for scope in scopes} if initial else set()

        def start(scope: ReviewScope) -> None:
            attempted.add((scope.id, self._unit_reopen_input(scope.id)))
            active[scope.id] = asyncio.create_task(self._review_scope(scope.id))

        try:
            while not self._blocked:
                self._work_changed.clear()
                for ident, job in list(active.items()):
                    if not job.done():
                        continue
                    status = await job
                    del active[ident]
                    for case in self.store.list(Case):
                        self._enqueue(self._sync_case_evidence(case))
                    if status == "provider_blocked":
                        self._blocked = True
                        self._blocked_event.set()
                        break
                if self._blocked:
                    break
                for original in scopes:
                    scope = self.store.get(ReviewScope, original.id)
                    if scope.id in active or scope.status == "reviewed":
                        first.discard(scope.id)
                        continue
                    key = (scope.id, self._unit_reopen_input(scope.id))
                    if scope.id in first or (scope.status == "pending" and key not in attempted):
                        first.discard(scope.id)
                        start(scope)
                if not active:
                    await self._drain()
                    if self._blocked:
                        break
                    # A verifier may have reopened a unit while the queue drained.
                    reopened = any(
                        (scope := self.store.get(ReviewScope, original.id)).status == "pending"
                        and (scope.id, self._unit_reopen_input(scope.id)) not in attempted
                        for original in scopes
                    )
                    if not reopened:
                        break
                    continue
                changed = asyncio.create_task(self._work_changed.wait())
                blocked = asyncio.create_task(self._blocked_event.wait())
                try:
                    await asyncio.wait([*active.values(), changed, blocked], return_when=asyncio.FIRST_COMPLETED)
                finally:
                    changed.cancel()
                    blocked.cancel()
                    await asyncio.gather(changed, blocked, return_exceptions=True)
        finally:
            for job in active.values():
                job.cancel()
            await asyncio.gather(*active.values(), return_exceptions=True)

    async def run(self, regions: list[dict[str, Any]]) -> dict[str, Any]:
        """Run or resume against an identical complete changed-region manifest."""
        scopes = self.prepare(regions)
        if not scopes:
            return {"status": "partial", "reason": "no changed-region manifest", "scopes": [], "errors": []}
        self._blocked = False
        self._blocked_event.clear()
        self._errors_real = []
        self._pending_batch_errors = {}
        if self.store.run.policy == "v42-units-6" and self.store.run.metadata.get("shared_investigation") == "shared-1":
            plan = self.store.get_unit_plan()
            for scope_id in [*plan.primary_scope_ids, plan.integration_scope_id]:
                self._record_incomplete_shared_batches(scope_id)
        for case in self.store.list(Case):
            self._enqueue(self._sync_case_evidence(case))
        for task in self.store.list(Task):
            if task.kind == "question" and task.status not in {"completed", "superseded"}:
                if self.store.run.policy == "v42-units-6" and not self._question_enabled():
                    self._errors_real.append(
                        f"question {task.object_id}: delegated investigation requires delivery audit"
                    )
                    continue
                if task.id not in self._enqueued:
                    self._enqueued.add(task.id)
                    self._queue.put_nowait(task.id)
        workers = [asyncio.create_task(self._worker()) for _ in range(2)]
        attempted: set[tuple[str, str]] = set()
        try:
            if self._question_enabled():
                # Restore already delegated work before starting a new scope
                # lease, which would otherwise fence the saved question input.
                await self._drain()
            if self._blocked:
                pass
            elif self._fair_enabled():
                await self._run_fair_primaries(scopes, attempted, initial=True)
            else:
                for scope in scopes:
                    if self._blocked:
                        break
                    if self.store.run.policy == "v42-units-6":
                        attempted.add((scope.id, self._unit_reopen_input(scope.id)))
                    status = await self._review_scope(scope.id)
                    for case in self.store.list(Case):
                        self._enqueue(self._sync_case_evidence(case))
                    if status == "provider_blocked":
                        self._blocked = True
                        break
            if self._blocked:
                for worker in workers:
                    worker.cancel()
                await asyncio.gather(*workers, return_exceptions=True)
            else:
                await self._drain()
                # If a helper answered after its owner reported a partial scope,
                # return that new fact to the same responsibility instead of
                # requiring the user to start another run.
                handled_questions = set()
                while not self._blocked:
                    answered = [
                        task
                        for task in self.store.list(Task)
                        if task.kind == "question"
                        and task.status == "completed"
                        and task.result.get("status") == "answered"
                        and task.id not in handled_questions
                    ]
                    if not answered:
                        break
                    for task in answered:
                        handled_questions.add(task.id)
                        question = self.store.get(Question, task.object_id)
                        scope = self.store.get(ReviewScope, question.scope_id)
                        if scope.status == "partial":
                            status = await self._review_scope(scope.id)
                            if status == "provider_blocked":
                                self._blocked = True
                                self._blocked_event.set()
                                break
                    await self._drain()
                if not self._blocked and self.store.run.policy != "v42-units-6":
                    while await self._follow_up():
                        await self._drain()
                        if self._blocked:
                            break
                if not self._blocked and self.store.run.policy == "v42-units-6":
                    plan = self.store.get_unit_plan()
                    followup_inputs: set[str] = set()
                    while not self._blocked:
                        await self._drain()
                        if self._blocked:
                            break
                        if await self._follow_up(attempted_inputs=followup_inputs):
                            continue
                        if self._blocked:
                            break
                        primaries = [self.store.get(ReviewScope, ident) for ident in plan.primary_scope_ids]
                        reopened = next(
                            (
                                scope
                                for scope in primaries
                                if scope.status == "pending"
                                and (scope.id, self._unit_reopen_input(scope.id)) not in attempted
                            ),
                            None,
                        )
                        if reopened is not None:
                            if self._fair_enabled():
                                await self._run_fair_primaries(primaries, attempted, initial=False)
                                continue
                            else:
                                attempted.add((reopened.id, self._unit_reopen_input(reopened.id)))
                                status = await self._review_scope(reopened.id)
                        elif primaries and all(scope.status == "reviewed" for scope in primaries):
                            integration = self.store.get(ReviewScope, plan.integration_scope_id)
                            key = (integration.id, self._unit_reopen_input(integration.id, integration=True))
                            if integration.status != "pending" or key in attempted:
                                break
                            attempted.add(key)
                            status = await self._review_scope(integration.id)
                        else:
                            break
                        if status == "provider_blocked":
                            self._blocked = True
                            self._blocked_event.set()
        except asyncio.CancelledError:
            for worker in workers:
                worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            raise
        finally:
            for worker in workers:
                worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
        scopes = [scope for scope in self.store.list(ReviewScope) if scope.status != "superseded"]
        if self.store.run.policy == "v42-units-6" and self.store.run.metadata.get("shared_investigation") == "shared-1":
            for scope in scopes:
                self._record_incomplete_shared_batches(scope.id)
        pending = [
            task.id
            for task in self.store.list(Task)
            if task.status not in {"completed", "superseded"}
            and (
                (task.kind == "verify" and self.store.get(Case, task.object_id).revision == task.input_revision)
                or (
                    task.kind == "question" and self.store.get(Question, task.object_id).revision == task.input_revision
                )
            )
        ]
        cases = self.store.list(Case)
        verdicts = []
        for task in self.store.list(Task):
            if task.kind == "verify" and task.status == "completed":
                case = self.store.get(Case, task.object_id)
                if case.revision == task.input_revision:
                    verdicts.extend(task.result.get("verdicts", []))
        policy = getattr(self.verifier, "policy", "v42-independent-1")
        unresolved = (
            any(v.get("verdict") in {"needs_evidence", "unresolved"} for v in verdicts)
            or any(q.status in {"open", "disputed", "unresolved"} for q in self.store.list(Question))
            or any(
                self.store.get_verdict(case.id, claim["id"], policy) is None for case in cases for claim in case.claims
            )
        )
        draft_policy = self.store.run.metadata.get("candidate_draft_policy")
        pending_drafts: list[str] | None = []
        if draft_policy is not None:
            try:
                if (
                    draft_policy != "candidate-draft-1"
                    or self.store.run.policy != "v42-units-6"
                    or self.store.run.metadata.get("shared_investigation") != "shared-1"
                ):
                    raise ValueError("unsupported candidate draft policy")
                drafts = self.store.pending_candidate_drafts()
                scope_ids = {scope.id for scope in scopes}
                if not isinstance(drafts, list) or any(
                    not isinstance(draft, dict)
                    or not isinstance(draft.get("id"), str)
                    or not draft["id"]
                    or draft.get("run_id") != self.store.run.id
                    or draft.get("scope_id") not in scope_ids
                    or draft.get("status") not in {"pending_validation", "repair_needed"}
                    for draft in drafts
                ):
                    raise ValueError("invalid pending candidate draft view")
                pending_drafts = [draft["id"] for draft in drafts]
                unresolved = unresolved or bool(pending_drafts)
            except (AttributeError, KeyError, TypeError, ValueError, OSError):
                pending_drafts = None
                unresolved = True
                self._errors_real.append("candidate draft state unavailable")
        status = (
            "provider_blocked"
            if self._blocked
            else (
                "completed"
                if all(s.status == "reviewed" for s in scopes) and not pending and not self._errors and not unresolved
                else "partial"
            )
        )
        result = {
            "status": status,
            "scopes": [{"id": s.id, "status": s.status} for s in scopes],
            "cases": [case.id for case in cases],
            "verdicts": verdicts,
            "pending_tasks": pending,
            "errors": self._errors,
        }
        if draft_policy is not None:
            result["pending_candidate_drafts"] = pending_drafts
        return result
