"""Durable incremental orchestration of reviewers and independent verification."""

from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any

from reviewforge.review_v42.contracts import (
    BindingError,
    Case,
    LeaseError,
    Question,
    ReviewScope,
    RevisionConflict,
    Task,
)
from reviewforge.review_v42.reviewer import QuestionAssistant, Reviewer, _status_code
from reviewforge.review_v42.scope import compile_scopes
from reviewforge.review_v42.tools import EvidenceGateway


class ReviewRuntime:
    def __init__(self, store: Any, llm: Any, gateway: Any, verifier: Any):
        self.store, self.llm, self.verifier = store, llm, verifier
        self.gateway = gateway if isinstance(gateway, EvidenceGateway) else EvidenceGateway(store, gateway)
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._enqueued: set[str] = set()
        self._blocked = False
        self._blocked_event = asyncio.Event()
        self._errors: list[str] = []

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
        refs = {
            ref
            for q in self.store.list(Question)
            if q.case_id == case.id and q.status == "answered"
            for ref in q.evidence_refs
        }
        if refs - set(case.evidence_refs + case.counter_evidence_refs):
            case = self.store.update(Case, case.id, case.revision, evidence_refs=sorted(set(case.evidence_refs) | refs))
        return case

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

    def _enqueue_question(self, question: Question, reason: str, task_id: str, token: str) -> None:
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

    async def _answer_question(self, task: Task) -> None:
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
            self._errors.append(f"question {question.id}: {type(exc).__name__}")
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
                heartbeat = asyncio.create_task(self._heartbeat(task, 120))
                try:
                    result = await self.verifier.verify(case.id, task_id=task.id, lease_token=task.lease_token)
                    policy = getattr(self.verifier, "policy", "v42-independent-1")
                    if not isinstance(result, list) or len(result) != len(case.claims):
                        raise ValueError("verifier did not deliver every claim")
                    for claim in case.claims:
                        persisted = self.store.get_verdict(case.id, claim["id"], policy)
                        if persisted is None or persisted not in result:
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
                    self._errors.append(f"verification {case.id}: {type(exc).__name__}")
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
            self._errors.append(f"review {scope_id}: lease unavailable")
            return "partial"
        heartbeat = asyncio.create_task(self._heartbeat(task, 120))
        reviewer = Reviewer(
            self.store,
            self.llm,
            self.gateway,
            self._enqueue,
            task_id=task.id,
            lease_token=task.lease_token,
            focus=focus,
            on_question=lambda question, reason: self._enqueue_question(question, reason, task.id, task.lease_token),
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
            if reviewer.error:
                self._errors.append(f"review {scope_id}: {reviewer.error}")
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
                self._errors.append(f"review {scope_id}: could not settle stale task")
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
        return json.dumps([cases, questions, obligations], sort_keys=True, ensure_ascii=False)

    async def run(self, regions: list[dict[str, Any]]) -> dict[str, Any]:
        """Run or resume against an identical complete changed-region manifest."""
        scopes = self.prepare(regions)
        if not scopes:
            return {"status": "partial", "reason": "no changed-region manifest", "scopes": [], "errors": []}
        self._blocked = False
        self._blocked_event.clear()
        self._errors = []
        for case in self.store.list(Case):
            self._enqueue(self._sync_case_evidence(case))
        for task in self.store.list(Task):
            if task.kind == "question" and task.status not in {"completed", "superseded"}:
                if self.store.run.policy == "v42-units-6":
                    self._errors.append(f"question {task.object_id}: delegated investigation requires delivery audit")
                    continue
                if task.id not in self._enqueued:
                    self._enqueued.add(task.id)
                    self._queue.put_nowait(task.id)
        workers = [asyncio.create_task(self._worker()) for _ in range(2)]
        attempted: set[tuple[str, str]] = set()
        try:
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
        status = (
            "provider_blocked"
            if self._blocked
            else (
                "completed"
                if all(s.status == "reviewed" for s in scopes) and not pending and not self._errors and not unresolved
                else "partial"
            )
        )
        return {
            "status": status,
            "scopes": [{"id": s.id, "status": s.status} for s in scopes],
            "cases": [case.id for case in cases],
            "verdicts": verdicts,
            "pending_tasks": pending,
            "errors": self._errors,
        }
