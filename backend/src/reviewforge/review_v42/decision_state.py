"""Run-bound decision journal and recovery for the optional decision-cycle profile.

Native attempts and source operations are durable before execution.  Text here is
navigation, never a substitute for the store's source and publication checks.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict
from typing import Any

from .contracts import BindingError, Case, LeaseError, Question, RevisionConflict, Task
from .store import ReviewStore, _json


def _id(prefix: str, values: list[Any]) -> str:
    return prefix + hashlib.sha256(_json(values).encode()).hexdigest()


class DecisionState:
    def __init__(self, store: ReviewStore):
        if store.run.metadata.get("decision_cycle_policy") != "decision-cycle-1":
            raise BindingError("decision cycle is not enabled")
        self.store = store

    def _task(self, db: sqlite3.Connection, task_id: str, token: str, now: float | None) -> Task:
        row = db.execute("SELECT payload FROM objects WHERE kind='Task' AND id=?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(task_id)
        task = Task(**json.loads(row[0]))
        self.store._guard_review_task(db, task_id, token, task.object_id, now)
        return task

    def pending_verifications(self) -> list[dict[str, Any]]:
        with self.store._connect() as db:
            return [
                {"case_id": case_id, "step_id": step_id, "case_revision": revision}
                for case_id, step_id, revision in db.execute(
                    "SELECT case_id,step_id,case_revision FROM decision_verification_queue ORDER BY case_id"
                )
            ]

    def ack_verification(self, case_id: str, case_revision: int, task_id: str) -> None:
        """Acknowledge only a durable matching verification task, never an in-memory enqueue."""
        with self.store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT step_id,case_revision FROM decision_verification_queue WHERE case_id=?", (case_id,)
            ).fetchone()
            if row is None:
                return
            if row[1] != case_revision:
                raise RevisionConflict("verification readiness revision changed")
            task_row = db.execute("SELECT payload FROM objects WHERE kind='Task' AND id=?", (task_id,)).fetchone()
            if task_row is None:
                raise BindingError("verification task is not durable")
            task = Task(**json.loads(task_row[0]))
            case_row = db.execute("SELECT payload FROM objects WHERE kind='Case' AND id=?", (case_id,)).fetchone()
            if case_row is None:
                raise BindingError("candidate case is missing")
            case = Case(**json.loads(case_row[0]))
            semantic_input = {
                "trigger": case.trigger,
                "mechanism": case.mechanism,
                "claims": case.claims,
                "evidence_refs": sorted(case.evidence_refs),
                "counter_evidence_refs": sorted(case.counter_evidence_refs),
            }
            durable_current = task.input_revision == case.revision and task.status != "superseded"
            frozen_equivalent = task.status == "completed" and task.result.get("case_input") == semantic_input
            if (
                task.run_id != self.store.run.id
                or task.kind != "verify"
                or task.object_id != case_id
                or case.revision < case_revision
                or not (durable_current or frozen_equivalent)
            ):
                raise BindingError("verification task does not cover the current candidate")
            db.execute(
                "DELETE FROM decision_verification_queue WHERE case_id=? AND case_revision=?", (case_id, case_revision)
            )
            self.store._event(
                db,
                "DecisionStep",
                row[0],
                "verification_enqueued",
                {"case_id": case_id, "case_revision": case_revision, "task_id": task_id},
            )
            db.commit()

    def list_items(self, task_id: str) -> list[dict[str, Any]]:
        task = self.store.get(Task, task_id)
        with self.store._connect() as db:
            return [
                json.loads(row[0])
                for row in db.execute(
                    "SELECT payload FROM decision_items WHERE scope_id=? ORDER BY rowid", (task.object_id,)
                )
            ]

    def current(self, task_id: str) -> dict[str, Any] | None:
        task = self.store.get(Task, task_id)
        with self.store._connect() as db:
            row = db.execute(
                "SELECT i.payload FROM decision_steps s JOIN decision_items i ON i.id=s.item_id "
                "WHERE s.task_id=? AND i.scope_id=? ORDER BY s.rowid DESC LIMIT 1",
                (task_id, task.object_id),
            ).fetchone()
            return json.loads(row[0]) if row else None

    def record_attempt(
        self, task_id: str, token: str, turn_id: str, call_id: str, args: dict[str, Any], *, now: float | None = None
    ) -> dict[str, Any]:
        """Save an individually identifiable native call before schema validation."""
        with self.store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._task(db, task_id, token, now)
            self.store._require_delivered(db, task_id, token, turn_id, set())
            row = db.execute(
                "SELECT content_hash FROM turn_audit WHERE task_id=? AND turn_id=? AND phase='response'",
                (task_id, turn_id),
            ).fetchone()
            digest = row[0]
            raw = (self.store.audit_dir / digest).read_bytes()
            if hashlib.sha256(raw).hexdigest() != digest:
                raise BindingError("turn audit blob hash mismatch")
            response = json.loads(raw)
            calls = [
                c
                for m in response
                if isinstance(m, dict) and isinstance(m.get("data"), dict)
                for c in m["data"].get("tool_calls", [])
                if isinstance(c, dict) and c.get("id") == call_id
            ]
            if len(calls) != 1 or calls[0].get("name") != "review_step" or calls[0].get("args") != args:
                raise BindingError("decision does not match a unique native call")
            step_id = _id("step_", [self.store.run.id, task_id, turn_id, call_id])
            attempt = {
                "id": step_id,
                "task_id": task_id,
                "turn_id": turn_id,
                "call_id": call_id,
                "args": args,
                "response_hash": digest,
            }
            prior = db.execute("SELECT payload FROM decision_attempts WHERE id=?", (step_id,)).fetchone()
            if prior is not None and json.loads(prior[0]) != attempt:
                raise RevisionConflict("native decision identity has different input")
            if prior is None:
                db.execute(
                    "INSERT INTO decision_attempts VALUES (?,?,?,?,?)",
                    (step_id, task_id, turn_id, call_id, _json(attempt)),
                )
                self.store._event(db, "DecisionAttempt", step_id, "received", attempt)
            db.commit()
            return attempt

    def stage(
        self, task_id: str, token: str, turn_id: str, call_id: str, args: dict[str, Any], *, now: float | None = None
    ) -> dict[str, Any]:
        if not isinstance(args, dict):
            raise ValueError("review_step arguments must be an object")
        with self.store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            task = self._task(db, task_id, token, now)
            self.store._require_delivered(db, task_id, token, turn_id, set())
            row = db.execute(
                "SELECT content_hash FROM turn_audit WHERE task_id=? AND turn_id=? AND phase='response'",
                (task_id, turn_id),
            ).fetchone()
            digest = row[0]
            raw = (self.store.audit_dir / digest).read_bytes()
            if hashlib.sha256(raw).hexdigest() != digest:
                raise BindingError("turn audit blob hash mismatch")
            response = json.loads(raw)
            calls = [
                c
                for m in response
                if isinstance(m, dict) and isinstance(m.get("data"), dict)
                for c in m["data"].get("tool_calls", [])
                if isinstance(c, dict) and c.get("id") == call_id
            ]
            if len(calls) != 1 or calls[0].get("name") != "review_step" or calls[0].get("args") != args:
                raise BindingError("decision does not match a unique native call")
            step_id = _id("step_", [self.store.run.id, task_id, turn_id, call_id])
            attempt = {
                "id": step_id,
                "task_id": task_id,
                "turn_id": turn_id,
                "call_id": call_id,
                "args": args,
                "response_hash": digest,
            }
            prior = db.execute("SELECT payload FROM decision_attempts WHERE id=?", (step_id,)).fetchone()
            if prior is not None and json.loads(prior[0]) != attempt:
                raise RevisionConflict("native decision identity has different input")
            if prior is None:
                db.execute(
                    "INSERT INTO decision_attempts VALUES (?,?,?,?,?)",
                    (step_id, task_id, turn_id, call_id, _json(attempt)),
                )
                self.store._event(db, "DecisionAttempt", step_id, "received", attempt)
            db.commit()
            db.execute("BEGIN IMMEDIATE")
            task = self._task(db, task_id, token, now)
            from reviewforge.engine.native_submission import strict_tool_call

            native_row = db.execute(
                "SELECT content_hash FROM turn_audit WHERE task_id=? AND turn_id=? AND phase='native'",
                (task_id, turn_id),
            ).fetchone()
            native_call = None
            if native_row is not None:
                native_raw = (self.store.audit_dir / native_row[0]).read_bytes()
                if hashlib.sha256(native_raw).hexdigest() != native_row[0]:
                    raise BindingError("native audit blob hash mismatch")
                native = json.loads(native_raw)
                if not isinstance(native, list) or len(native) != 1 or not isinstance(native[0], dict):
                    raise BindingError("invalid native audit")
                raw_calls = native[0].get("raw_tool_calls")
                if raw_calls is not None:
                    if not isinstance(raw_calls, list):
                        raise BindingError("invalid raw calls")
                    matched = [entry for entry in raw_calls if isinstance(entry, dict) and entry.get("id") == call_id]
                    if len(matched) != 1:
                        raise BindingError("ambiguous raw call identity")
                    native_call = matched[0]
            strict_tool_call(calls[0], native_call)
            existing = db.execute("SELECT payload FROM decision_steps WHERE id=?", (step_id,)).fetchone()
            if existing:
                step = json.loads(existing[0])
                if step["args"] != args or step["response_hash"] != digest:
                    raise RevisionConflict("native decision identity has different input")
                return self._with_operations(db, step)
            if set(args) != {
                "work_item_id",
                "expected_revision",
                "question",
                "judgment",
                "evidence_refs",
                "action",
                "payload",
            }:
                raise ValueError("invalid decision fields")
            item_id, rev = args["work_item_id"], args["expected_revision"]
            if not isinstance(item_id, str) or type(rev) is not int or rev < 0:
                raise ValueError("invalid work item identity or revision")
            if (
                not isinstance(args["question"], str)
                or not args["question"].strip()
                or not isinstance(args["judgment"], str)
            ):
                raise ValueError("question and judgment must be text")
            refs = args["evidence_refs"]
            if not isinstance(refs, list) or any(not isinstance(ref, str) or not ref for ref in refs):
                raise ValueError("invalid decision evidence references")
            self.store._require_delivered(db, task_id, token, turn_id, set(refs))
            if not isinstance(args["payload"], dict) or args["action"] not in {
                "request_evidence",
                "offer_candidate",
                "close_question",
                "complete_review",
                "unresolved",
                "delegate",
            }:
                raise ValueError("invalid decision action or payload")
            operations = args["payload"].get("operations", []) if args["action"] == "request_evidence" else []
            if not isinstance(operations, list) or any(
                not isinstance(op, dict)
                or set(op) != {"name", "args"}
                or not isinstance(op["name"], str)
                or not isinstance(op["args"], dict)
                for op in operations
            ):
                raise ValueError("invalid evidence operations")
            if args["action"] == "request_evidence" and not operations:
                raise ValueError("at least one source operation is required")
            if item_id:
                row = db.execute(
                    "SELECT scope_id,revision,payload FROM decision_items WHERE id=?", (item_id,)
                ).fetchone()
                if row is None or row[0] != task.object_id:
                    raise BindingError("unknown or wrong-scope work item")
                if row[1] != rev:
                    raise RevisionConflict("work item revision changed")
                item = json.loads(row[2])
                if item["question"] != args["question"].strip():
                    raise BindingError("existing work item question cannot be replaced; create a new item")
            else:
                if rev != 0:
                    raise RevisionConflict("new work item requires revision zero")
                item_id = _id("work_", [self.store.run.id, task.object_id, task_id, turn_id, call_id])
                qid = _id("question_", [item_id])
                question = Question(
                    qid, self.store.run.id, args["question"].strip(), scope_id=task.object_id, owner=task_id
                )
                db.execute("INSERT INTO objects VALUES ('Question',?,?,?)", (qid, 0, _json(asdict(question))))
                self.store._event(db, "Question", qid, "decision_create", asdict(question))
                plan = self.store._unit_plan_db(db)
                if task.object_id in plan.primary_scope_ids:
                    db.execute("UPDATE unit_epochs SET revision=revision+1 WHERE scope_id=?", (task.object_id,))
                    self.store._reopen_integration(db, plan)
                item = {
                    "id": item_id,
                    "scope_id": task.object_id,
                    "question_id": qid,
                    "question": question.statement,
                    "judgments": [],
                    "revision": 0,
                    "status": "judging",
                }
            if args["action"] == "close_question":
                payload = args["payload"]
                answer, answer_refs = payload.get("answer"), payload.get("evidence_refs")
                if payload.get("question_id") != item["question_id"]:
                    raise BindingError("close must identify this work item's question")
                if (
                    not isinstance(answer, str)
                    or not answer.strip()
                    or not isinstance(answer_refs, list)
                    or not answer_refs
                ):
                    raise ValueError("closing a question requires an answer and original references")
                qrow = db.execute(
                    "SELECT payload FROM objects WHERE kind='Question' AND id=?", (item["question_id"],)
                ).fetchone()
                question = Question(**json.loads(qrow[0]))
                self.store.update_unit_object(
                    task_id,
                    token,
                    Question,
                    question.id,
                    question.revision,
                    turn_id=turn_id,
                    now=now,
                    _db=db,
                    status="answered",
                    answer=answer.strip(),
                    evidence_refs=answer_refs,
                )
            elif args["action"] == "unresolved":
                payload = args["payload"]
                if any(
                    not isinstance(payload.get(key), str) or not payload[key].strip()
                    for key in ("missing_fact", "attempted", "recovery_condition")
                ):
                    raise ValueError("unresolved requires missing fact, attempted paths and recovery condition")
                qrow = db.execute(
                    "SELECT payload FROM objects WHERE kind='Question' AND id=?", (item["question_id"],)
                ).fetchone()
                question = Question(**json.loads(qrow[0]))
                self.store.update_unit_object(
                    task_id,
                    token,
                    Question,
                    question.id,
                    question.revision,
                    turn_id=turn_id,
                    now=now,
                    _db=db,
                    status="unresolved",
                    change_reason=_json(payload),
                )
            if args["judgment"].strip():
                item["judgments"].append(
                    {
                        "id": _id("judgment_", [step_id]),
                        "text": args["judgment"].strip(),
                        "evidence_refs": refs,
                        "turn_id": turn_id,
                    }
                )
            item["revision"] += 1
            item["status"] = (
                "waiting_evidence"
                if operations
                else "answered"
                if args["action"] == "close_question"
                else "unresolved"
                if args["action"] == "unresolved"
                else item["status"]
                if args["action"] == "complete_review"
                else "judging"
            )
            db.execute(
                "INSERT INTO decision_items VALUES (?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                "revision=excluded.revision,payload=excluded.payload",
                (item_id, task.object_id, item["revision"], _json(item)),
            )
            step = {
                "id": step_id,
                "task_id": task_id,
                "turn_id": turn_id,
                "call_id": call_id,
                "work_item_id": item_id,
                "revision": item["revision"],
                "action": args["action"],
                "args": args,
                "response_hash": digest,
                "status": "pending"
                if operations or args["action"] in {"offer_candidate", "complete_review"}
                else "settled",
                "consumed": not bool(operations),
            }
            db.execute(
                "INSERT INTO decision_steps VALUES (?,?,?,?,?,?)",
                (step_id, task_id, turn_id, call_id, item_id, _json(step)),
            )
            for i, op in enumerate(operations):
                op_id = _id("operation_", [step_id, i])
                entry = {"id": op_id, "name": op["name"], "args": op["args"], "status": "pending"}
                db.execute("INSERT INTO decision_operations VALUES (?,?,?,?)", (op_id, step_id, i, _json(entry)))
            self.store._event(db, "DecisionStep", step_id, "stage", step)
            self._task(db, task_id, token, now)
            result = self._with_operations(db, step)
            db.commit()
            return result

    @staticmethod
    def _with_operations(db: sqlite3.Connection, step: dict[str, Any]) -> dict[str, Any]:
        return {
            **step,
            "operations": [
                json.loads(row[0])
                for row in db.execute(
                    "SELECT payload FROM decision_operations WHERE step_id=? ORDER BY ordinal", (step["id"],)
                )
            ],
        }

    def pending(self, task_id: str) -> list[dict[str, Any]]:
        with self.store._connect() as db:
            return [
                self._with_operations(db, step)
                for (payload,) in db.execute(
                    "SELECT payload FROM decision_steps WHERE task_id=? ORDER BY rowid", (task_id,)
                )
                if not (step := json.loads(payload))["consumed"]
            ]

    def claim_operation(
        self, task_id: str, token: str, step_id: str, operation_id: str, *, now: float | None = None
    ) -> dict[str, Any]:
        """Fence an operation before execution; an orphaned claim is outcome-unknown."""
        with self.store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._task(db, task_id, token, now)
            step = db.execute("SELECT task_id,turn_id FROM decision_steps WHERE id=?", (step_id,)).fetchone()
            if step is None or step[0] != task_id:
                raise BindingError("unknown decision step")
            row = db.execute(
                "SELECT payload FROM decision_operations WHERE id=? AND step_id=?", (operation_id, step_id)
            ).fetchone()
            if row is None:
                raise BindingError("unknown source operation")
            op = json.loads(row[0])
            if op["status"] != "pending":
                raise RevisionConflict("source operation already claimed or settled")
            op["status"] = "in_flight"
            op["claim_lease_hash"] = hashlib.sha256(token.encode()).hexdigest()
            db.execute("UPDATE decision_operations SET payload=? WHERE id=?", (_json(op), operation_id))
            self.store._event(db, "DecisionOperation", operation_id, "claim", op)
            db.commit()
            return op

    def record_result(
        self, task_id: str, token: str, step_id: str, operation_id: str, result: Any, *, now: float | None = None
    ) -> dict[str, Any]:
        return self._record(task_id, token, step_id, operation_id, "completed", result, now)

    def mark_unknown(
        self, task_id: str, token: str, step_id: str, operation_id: str, reason: str, *, now: float | None = None
    ) -> dict[str, Any]:
        if not isinstance(reason, str) or not reason:
            raise ValueError("unknown outcome requires a reason")
        return self._record(task_id, token, step_id, operation_id, "unknown", reason, now)

    def _record(
        self, task_id: str, token: str, step_id: str, operation_id: str, status: str, result: Any, now: float | None
    ) -> dict[str, Any]:
        with self.store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._task(db, task_id, token, now)
            step = db.execute("SELECT task_id,turn_id FROM decision_steps WHERE id=?", (step_id,)).fetchone()
            if step is None or step[0] != task_id:
                raise BindingError("unknown decision step")
            row = db.execute(
                "SELECT payload FROM decision_operations WHERE id=? AND step_id=?", (operation_id, step_id)
            ).fetchone()
            if row is None:
                raise BindingError("unknown source operation")
            op = json.loads(row[0])
            if op["status"] != "in_flight":
                if op["status"] == status and op.get("result") == result:
                    return op
                raise RevisionConflict("source operation already settled or outcome unknown")
            if op.get("claim_lease_hash") != hashlib.sha256(token.encode()).hexdigest():
                raise LeaseError("operation was claimed under another lease")
            op.update(status=status, result=result)
            db.execute("UPDATE decision_operations SET payload=? WHERE id=?", (_json(op), operation_id))
            self.store._event(db, "DecisionOperation", operation_id, status, op)
            db.commit()
            return op

    def mark_consumed(
        self, task_id: str, token: str, step_id: str, turn_id: str, *, now: float | None = None
    ) -> dict[str, Any]:
        with self.store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._task(db, task_id, token, now)
            self.store._require_delivered(db, task_id, token, turn_id, set())
            row = db.execute(
                "SELECT payload FROM decision_steps WHERE id=? AND task_id=?", (step_id, task_id)
            ).fetchone()
            if row is None:
                raise BindingError("unknown decision step")
            step = json.loads(row[0])
            ops = self._with_operations(db, step)["operations"]
            if not ops or any(op["status"] != "completed" for op in ops):
                raise BindingError("source batch is not completely recorded")
            expected = {
                "step_id": step_id,
                "results": [{"operation_id": op["id"], "result": op["result"]} for op in ops],
            }
            current = {
                **expected,
                "work_item_id": step["work_item_id"],
                "work_item_revision": step["revision"],
            }
            audit = db.execute(
                "SELECT content_hash FROM turn_audit WHERE task_id=? AND turn_id=? AND phase='request'",
                (task_id, turn_id),
            ).fetchone()
            raw = (self.store.audit_dir / audit[0]).read_bytes()
            if hashlib.sha256(raw).hexdigest() != audit[0]:
                raise BindingError("turn audit blob hash mismatch")
            messages = json.loads(raw)

            def matches(message: dict[str, Any]) -> bool:
                if message.get("type") != "tool" or not isinstance(message.get("data"), dict):
                    return False
                try:
                    receipt = json.loads(message["data"].get("content", ""))
                    return receipt == expected or receipt == current
                except (TypeError, ValueError):
                    return False

            if not any(matches(m) for m in messages if isinstance(m, dict)):
                raise BindingError("complete source receipt was not present in successful request")
            if step["turn_id"] == turn_id:
                raise BindingError("source result cannot be consumed in its producing turn")
            step.update(consumed=True, consumed_turn_id=turn_id)
            db.execute("UPDATE decision_steps SET payload=? WHERE id=?", (_json(step), step_id))
            self.store._event(db, "DecisionStep", step_id, "consume", step)
            db.commit()
            return self._with_operations(db, step)
