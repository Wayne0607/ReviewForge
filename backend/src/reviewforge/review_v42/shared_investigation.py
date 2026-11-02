"""Opt-in, run-bound shared investigation navigation.

The records here are untrusted notes, not evidence, review completion or a verdict.
Original evidence must still be independently delivered to each consuming task.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import time
import uuid
from typing import Any

from .contracts import BindingError, Case, LeaseError, ReviewScope, RevisionConflict, Task
from .store import ReviewStore, _json


class SharedInvestigations:
    """Shared notes with task-fenced ownership, stored in the existing board DB."""

    def __init__(self, store: ReviewStore):
        if store.run.policy != "v42-units-6" or store.run.metadata.get("shared_investigation") != "shared-1":
            raise BindingError("shared investigations require a new explicitly bound shared-1 run")
        self.store = store
        with store._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS shared_investigations (
                    id TEXT PRIMARY KEY, identity TEXT NOT NULL UNIQUE,
                    revision INTEGER NOT NULL, initial TEXT NOT NULL, payload TEXT NOT NULL,
                    owner_lease_hash TEXT NOT NULL DEFAULT '');
                CREATE TABLE IF NOT EXISTS shared_investigation_operations (
                    operation_id TEXT PRIMARY KEY, request TEXT NOT NULL, result TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS shared_investigation_snapshots (
                    id TEXT PRIMARY KEY, payload TEXT NOT NULL);
            """)

    @staticmethod
    def _clock(now: float | None) -> float:
        value = time.time() if now is None else now
        if not isinstance(value, (float, int)) or not math.isfinite(value):
            raise ValueError("time must be finite")
        return value

    @staticmethod
    def _nonempty(value: Any, label: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{label} must be a nonempty string")
        return value

    @classmethod
    def _strings(cls, value: Any, label: str, *, required: bool = False) -> list[str]:
        if not isinstance(value, list) or (required and not value):
            raise ValueError(f"{label} must be a list")
        for item in value:
            cls._nonempty(item, label)
        if len(value) != len(set(value)):
            raise ValueError(f"{label} must not contain duplicates")
        return sorted(value)

    def _task(self, db: sqlite3.Connection, task_id: str, token: str, now: float) -> Task:
        row = db.execute("SELECT payload FROM objects WHERE kind='Task' AND id=?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(task_id)
        task = Task(**json.loads(row[0]))
        if task.run_id != self.store.run.id:
            raise BindingError("task belongs to another run")
        self.store._guard_review_task(db, task_id, token, task.object_id, now)
        return task

    def _owner_active(self, db: sqlite3.Connection, item: dict[str, Any], lease_hash: str, now: float) -> bool:
        if not item["owner_task_id"] or not lease_hash:
            return False
        row = db.execute("SELECT payload FROM objects WHERE kind='Task' AND id=?", (item["owner_task_id"],)).fetchone()
        if row is None:
            return False
        task = Task(**json.loads(row[0]))
        if (
            task.run_id != self.store.run.id
            or task.kind != "review"
            or task.status != "leased"
            or not task.lease_token
            or task.lease_expires <= now
            or hashlib.sha256(task.lease_token.encode()).hexdigest() != lease_hash
        ):
            return False
        scope = db.execute(
            "SELECT revision FROM objects WHERE kind='ReviewScope' AND id=?", (task.object_id,)
        ).fetchone()
        return task.object_id in item["scope_ids"] and scope is not None and scope[0] == task.input_revision

    def _view(self, db: sqlite3.Connection, item: dict[str, Any], lease_hash: str, now: float) -> dict[str, Any]:
        public = self._public(item)
        public["owner_active"] = self._owner_active(db, item, lease_hash, now)
        if public["owner_active"]:
            row = db.execute(
                "SELECT payload FROM objects WHERE kind='Task' AND id=?", (item["owner_task_id"],)
            ).fetchone()
            public["lease_expires"] = Task(**json.loads(row[0])).lease_expires
        else:
            public["lease_expires"] = 0.0
        return public

    def _scope_regions(self, db: sqlite3.Connection, scope_ids: list[str], region_ids: list[str]) -> None:
        plan = self.store._unit_plan_db(db)
        allowed = {*plan.primary_scope_ids, plan.integration_scope_id}
        if not set(scope_ids) <= allowed:
            raise BindingError("scope is not part of this run's unit plan")
        regions = set()
        for sid in scope_ids:
            row = db.execute("SELECT payload FROM objects WHERE kind='ReviewScope' AND id=?", (sid,)).fetchone()
            if row is None:
                raise BindingError("scope does not exist")
            scope = ReviewScope(**json.loads(row[0]))
            if scope.run_id != self.store.run.id:
                raise BindingError("scope belongs to another run")
            regions.update(r["id"] for r in scope.regions)
        if not set(region_ids) <= regions:
            raise BindingError("regions must belong to the selected scopes")

    @staticmethod
    def _row(db: sqlite3.Connection, investigation_id: str) -> tuple[dict[str, Any], str]:
        row = db.execute(
            "SELECT payload,owner_lease_hash FROM shared_investigations WHERE id=?", (investigation_id,)
        ).fetchone()
        if row is None:
            raise KeyError(investigation_id)
        return json.loads(row[0]), row[1]

    @staticmethod
    def _public(item: dict[str, Any]) -> dict[str, Any]:
        return {**item, "type": "shared_investigation", "trust": "untrusted_navigation"}

    @staticmethod
    def _operation(db: sqlite3.Connection, operation_id: str, request: dict[str, Any]) -> dict[str, Any] | None:
        row = db.execute(
            "SELECT request,result FROM shared_investigation_operations WHERE operation_id=?", (operation_id,)
        ).fetchone()
        if row is None:
            return None
        if row[0] != _json(request):
            raise RevisionConflict("operation ID already used for different input")
        return json.loads(row[1])

    @staticmethod
    def _save_operation(db: sqlite3.Connection, operation_id: str, request: dict[str, Any], result: dict[str, Any]):
        db.execute(
            "INSERT INTO shared_investigation_operations VALUES (?,?,?)", (operation_id, _json(request), _json(result))
        )

    def create(
        self,
        task_id: str,
        token: str,
        question: str,
        scope_ids: list[str],
        region_ids: list[str],
        *,
        turn_id: str,
        tool_call_id: str,
        now: float | None = None,
    ) -> dict[str, Any]:
        self._nonempty(turn_id, "turn_id")
        self._nonempty(tool_call_id, "tool_call_id")
        self._nonempty(question, "question")
        scope_ids = self._strings(scope_ids, "scope_ids", required=True)
        region_ids = self._strings(region_ids, "region_ids", required=True)
        identity = hashlib.sha256(_json([self.store.run.id, task_id, turn_id, tool_call_id]).encode()).hexdigest()
        initial = {"identity": identity, "question": question, "scope_ids": scope_ids, "region_ids": region_ids}
        iid = "investigation-" + identity
        with self.store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            task = self._task(db, task_id, token, self._clock(now))
            self.store._require_delivered(db, task_id, token, turn_id, set())
            self._scope_regions(db, scope_ids, region_ids)
            if task.object_id not in scope_ids:
                raise BindingError("creating task must belong to an associated scope")
            row = db.execute("SELECT initial,payload FROM shared_investigations WHERE id=?", (iid,)).fetchone()
            if row:
                if row[0] != _json(initial):
                    raise RevisionConflict("investigation identity already has different initial input")
                self._task(db, task_id, token, self._clock(now))
                return self._public(json.loads(row[1]))
            item = {
                "id": iid,
                "run_id": self.store.run.id,
                **initial,
                "observations": [],
                "open_questions": [],
                "status": "open",
                "revision": 0,
                "owner_task_id": "",
                "lease_expires": 0.0,
                "reason": "",
            }
            db.execute(
                "INSERT INTO shared_investigations VALUES (?,?,?,?,?,?)",
                (iid, identity, 0, _json(initial), _json(item), ""),
            )
            self.store._invalidate_scopes_for_shared(
                db, scope_ids, iid, "new shared investigation", current_task_id=task_id
            )
            self.store._event(db, "SharedInvestigation", iid, "create", {"task_id": task_id, "item": item})
            self._task(db, task_id, token, self._clock(now))
            db.commit()
            return self._public(item)

    def claim(
        self,
        task_id: str,
        token: str,
        investigation_id: str,
        expected_revision: int,
        reason: str,
        *,
        operation_id: str,
        now: float | None = None,
    ) -> dict[str, Any]:
        self._nonempty(reason, "claim reason")
        self._nonempty(operation_id, "operation_id")
        request = {
            "action": "claim",
            "task_id": task_id,
            "lease": hashlib.sha256(token.encode()).hexdigest(),
            "id": investigation_id,
            "expected_revision": expected_revision,
            "reason": reason,
        }
        with self.store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            at = self._clock(now)
            task = self._task(db, task_id, token, at)
            item, lease_hash = self._row(db, investigation_id)
            if task.object_id not in item["scope_ids"]:
                raise BindingError("task is outside the investigation scopes")
            previous = self._operation(db, operation_id, request)
            if previous is not None:
                if (
                    item["owner_task_id"] != task_id
                    or lease_hash != request["lease"]
                    or not self._owner_active(db, item, lease_hash, self._clock(now))
                    or item["revision"] != previous["revision"]
                ):
                    raise LeaseError("investigation lease is expired or superseded")
                self._task(db, task_id, token, self._clock(now))
                return self._view(db, item, lease_hash, self._clock(now))
            if item["revision"] != expected_revision:
                raise RevisionConflict("investigation revision changed")
            if item["status"] not in {"open", "partial", "unavailable"}:
                raise BindingError("closed investigations cannot be claimed")
            this_hash = request["lease"]
            if self._owner_active(db, item, lease_hash, at) and (
                item["owner_task_id"] != task_id or lease_hash != this_hash
            ):
                raise LeaseError("investigation already has an active owner")
            item.update(
                owner_task_id=task_id,
                lease_expires=task.lease_expires,
                reason=reason,
                revision=item["revision"] + 1,
            )
            db.execute(
                "UPDATE shared_investigations SET revision=?,payload=?,owner_lease_hash=? WHERE id=?",
                (item["revision"], _json(item), this_hash, investigation_id),
            )
            self.store._event(db, "SharedInvestigation", investigation_id, "claim", {"task_id": task_id, "item": item})
            result = self._view(db, item, this_hash, self._clock(now))
            self._save_operation(db, operation_id, request, result)
            self._task(db, task_id, token, self._clock(now))
            if not self._owner_active(db, item, this_hash, self._clock(now)):
                raise LeaseError("investigation lease is expired or superseded")
            db.commit()
            return result

    def update(
        self,
        task_id: str,
        token: str,
        investigation_id: str,
        expected_revision: int,
        *,
        question: str,
        observations: list[dict[str, Any]],
        open_questions: list[str],
        status: str,
        reason: str,
        turn_id: str,
        operation_id: str,
        now: float | None = None,
    ) -> dict[str, Any]:
        self._nonempty(question, "question")
        self._nonempty(reason, "change reason")
        self._nonempty(operation_id, "operation_id")
        open_questions = self._strings(open_questions, "open_questions")
        if status not in {"open", "partial", "unavailable", "resolved", "retracted"}:
            raise ValueError("invalid investigation status")
        if status == "resolved" and open_questions:
            raise ValueError("resolved investigations cannot have open questions")
        if not isinstance(observations, list):
            raise ValueError("observations must be a list")
        if status == "resolved" and not observations:
            raise BindingError("resolved investigations require a source-backed observation")
        normalized = []
        for observation in observations:
            if not isinstance(observation, dict) or set(observation) != {"statement", "evidence_refs"}:
                raise ValueError("observation requires statement and evidence_refs")
            self._nonempty(observation["statement"], "observation statement")
            refs = self._strings(observation["evidence_refs"], "evidence_refs", required=True)
            normalized.append({"statement": observation["statement"], "evidence_refs": refs})
        request = {
            "action": "update",
            "task_id": task_id,
            "lease": hashlib.sha256(token.encode()).hexdigest(),
            "id": investigation_id,
            "expected_revision": expected_revision,
            "question": question,
            "observations": normalized,
            "open_questions": open_questions,
            "status": status,
            "reason": reason,
            "turn_id": turn_id,
        }
        with self.store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            at = self._clock(now)
            self._task(db, task_id, token, at)
            item, lease_hash = self._row(db, investigation_id)
            previous = self._operation(db, operation_id, request)
            if previous is not None:
                if item["revision"] != previous["revision"]:
                    raise RevisionConflict("investigation changed after this operation")
                self._task(db, task_id, token, self._clock(now))
                return previous
            if (
                item["owner_task_id"] != task_id
                or lease_hash != request["lease"]
                or not self._owner_active(db, item, lease_hash, at)
            ):
                raise LeaseError("investigation lease is expired or superseded")
            if item["revision"] != expected_revision:
                raise RevisionConflict("investigation revision changed")
            owner_item, owner_hash = dict(item), lease_hash
            refs = {ref for observation in normalized for ref in observation["evidence_refs"]}
            source_refs = self.store._unit_refs(db, sorted(refs))
            if status == "resolved":
                eligible_sources = set()
                for ref in source_refs:
                    if (
                        ref.kind != "source"
                        or ref.side not in {"base", "head"}
                        or ref.metadata.get("navigation_only") is True
                        or ref.metadata.get("complete", True) is False
                        or ref.metadata.get("status", "available") not in {"available", "success", "ok"}
                        or ref.metadata.get("tool_name") not in {None, "read_source"}
                    ):
                        continue
                    try:
                        (self.store.blob_dir / ref.content_hash).read_bytes().decode("utf-8")
                    except UnicodeDecodeError:
                        continue
                    eligible_sources.add(ref.id)
                if any(not eligible_sources.intersection(obs["evidence_refs"]) for obs in normalized):
                    raise BindingError("each resolved observation needs available original source")
            delivered = self.store._require_delivered(db, task_id, token, turn_id, refs)
            saved_observations = [
                {
                    **observation,
                    "delivered_task_id": task_id,
                    "delivered_turn_id": turn_id,
                    "delivered_ranges": {
                        ref: [list(span) for span in delivered[ref]] for ref in observation["evidence_refs"]
                    },
                }
                for observation in normalized
            ]
            item.update(
                question=question,
                observations=saved_observations,
                open_questions=open_questions,
                status=status,
                reason=reason,
                revision=item["revision"] + 1,
            )
            if status in {"resolved", "retracted"}:
                item.update(owner_task_id="", lease_expires=0.0)
                lease_hash = ""
            db.execute(
                "UPDATE shared_investigations SET revision=?,payload=?,owner_lease_hash=? WHERE id=?",
                (item["revision"], _json(item), lease_hash, investigation_id),
            )
            if status in {"resolved", "retracted"}:
                self.store._apply_shared_resolution(db, investigation_id, current_task_id=task_id)
            self.store._event(
                db,
                "SharedInvestigation",
                investigation_id,
                "update",
                {"task_id": task_id, "turn_id": turn_id, "delivered_ranges": delivered, "item": item},
            )
            result = self._view(db, item, lease_hash, self._clock(now))
            self._save_operation(db, operation_id, request, result)
            self._task(db, task_id, token, self._clock(now))
            if not self._owner_active(db, owner_item, owner_hash, self._clock(now)):
                raise LeaseError("investigation lease is expired or superseded")
            db.commit()
            return result

    def release(
        self,
        task_id: str,
        token: str,
        investigation_id: str,
        expected_revision: int,
        reason: str,
        *,
        operation_id: str,
        now: float | None = None,
    ) -> dict[str, Any]:
        self._nonempty(reason, "handoff reason")
        self._nonempty(operation_id, "operation_id")
        request = {
            "action": "release",
            "task_id": task_id,
            "lease": hashlib.sha256(token.encode()).hexdigest(),
            "id": investigation_id,
            "expected_revision": expected_revision,
            "reason": reason,
        }
        with self.store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            at = self._clock(now)
            self._task(db, task_id, token, at)
            item, lease_hash = self._row(db, investigation_id)
            previous = self._operation(db, operation_id, request)
            if previous is not None:
                if item["revision"] != previous["revision"]:
                    raise RevisionConflict("investigation changed after this operation")
                self._task(db, task_id, token, self._clock(now))
                return previous
            if (
                item["owner_task_id"] != task_id
                or lease_hash != request["lease"]
                or not self._owner_active(db, item, lease_hash, at)
            ):
                raise LeaseError("investigation lease is expired or superseded")
            if item["revision"] != expected_revision:
                raise RevisionConflict("investigation revision changed")
            owner_item, owner_hash = dict(item), lease_hash
            item.update(owner_task_id="", lease_expires=0.0, reason=reason, revision=item["revision"] + 1)
            db.execute(
                "UPDATE shared_investigations SET revision=?,payload=?,owner_lease_hash='' WHERE id=?",
                (item["revision"], _json(item), investigation_id),
            )
            self.store._event(
                db, "SharedInvestigation", investigation_id, "release", {"task_id": task_id, "item": item}
            )
            result = self._view(db, item, "", self._clock(now))
            self._save_operation(db, operation_id, request, result)
            self._task(db, task_id, token, self._clock(now))
            if not self._owner_active(db, owner_item, owner_hash, self._clock(now)):
                raise LeaseError("investigation lease is expired or superseded")
            db.commit()
            return result

    def get(self, task_id: str, token: str, investigation_id: str, *, now: float | None = None) -> dict[str, Any]:
        with self.store._connect() as db:
            db.execute("BEGIN")
            task = self._task(db, task_id, token, self._clock(now))
            item, lease_hash = self._row(db, investigation_id)
            if task.object_id not in item["scope_ids"]:
                raise BindingError("task is outside the investigation scopes")
            result = self._view(db, item, lease_hash, self._clock(now))
            self._task(db, task_id, token, self._clock(now))
            return result

    def list(
        self,
        task_id: str,
        token: str,
        *,
        limit: int,
        cursor: str | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        if type(limit) is not int or limit < 1:
            raise ValueError("limit must be a positive integer")
        with self.store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            at = self._clock(now)
            task = self._task(db, task_id, token, at)
            if cursor is None:
                snapshot_id, offset = uuid.uuid4().hex, 0
                items = [
                    self._view(db, item, lease_hash, at)
                    for payload, lease_hash in db.execute(
                        "SELECT payload,owner_lease_hash FROM shared_investigations ORDER BY id"
                    )
                    if task.object_id in (item := json.loads(payload))["scope_ids"]
                ]
                snapshot = {
                    "run_id": self.store.run.id,
                    "scope_id": task.object_id,
                    "view": "investigations",
                    "items": items,
                }
                db.execute("INSERT INTO shared_investigation_snapshots VALUES (?,?)", (snapshot_id, _json(snapshot)))
            else:
                if not isinstance(cursor, str) or ":" not in cursor:
                    raise ValueError("invalid cursor")
                snapshot_id, number = cursor.rsplit(":", 1)
                if not number.isdecimal():
                    raise ValueError("invalid cursor")
                offset = int(number)
                row = db.execute(
                    "SELECT payload FROM shared_investigation_snapshots WHERE id=?", (snapshot_id,)
                ).fetchone()
                if row is None:
                    raise ValueError("unknown cursor")
                snapshot = json.loads(row[0])
                if (
                    snapshot["run_id"] != self.store.run.id
                    or snapshot["scope_id"] != task.object_id
                    or snapshot.get("view") != "investigations"
                ):
                    raise BindingError("cursor belongs to another run, scope, or view")
                items = snapshot["items"]
                if offset > len(items):
                    raise ValueError("cursor beyond snapshot")
            end = min(offset + limit, len(items))
            self._task(db, task_id, token, self._clock(now))
            db.commit()
            return {
                "items": items[offset:end],
                "next_cursor": f"{snapshot_id}:{end}" if end < len(items) else None,
                "total": len(items),
                "trust": "untrusted_navigation",
                "scope_id": task.object_id,
            }

    def preview(self, task_id: str, token: str, *, limit: int = 20, now: float | None = None) -> dict[str, Any]:
        """A soft seed page; list/get retain the complete paginated workset."""
        if type(limit) is not int or limit < 1:
            raise ValueError("limit must be a positive integer")
        with self.store._connect() as db:
            db.execute("BEGIN")
            task = self._task(db, task_id, token, self._clock(now))
            items = []
            for (payload,) in db.execute("SELECT payload FROM shared_investigations ORDER BY id"):
                item = json.loads(payload)
                if task.object_id not in item["scope_ids"] or item["status"] not in {"open", "partial", "unavailable"}:
                    continue
                refs = sorted({ref for obs in item["observations"] for ref in obs["evidence_refs"]})
                items.append(
                    {
                        "id": item["id"],
                        "question": item["question"],
                        "status": item["status"],
                        "revision": item["revision"],
                        "evidence_refs": refs,
                    }
                )
            self._task(db, task_id, token, self._clock(now))
            return {
                "items": items[:limit],
                "total": len(items),
                "has_more": len(items) > limit,
                "full_view": "list/get",
                "trust": "untrusted_navigation",
            }

    def list_cases(
        self, task_id: str, token: str, *, limit: int, cursor: str | None = None, now: float | None = None
    ) -> dict[str, Any]:
        """Return a paged run-level candidate directory, never verdicts or original bodies."""
        if type(limit) is not int or limit < 1:
            raise ValueError("limit must be a positive integer")
        with self.store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            task = self._task(db, task_id, token, self._clock(now))
            plan = self.store._unit_plan_db(db)
            scopes = {*plan.primary_scope_ids, plan.integration_scope_id}
            if task.object_id not in scopes:
                raise BindingError("task is outside this run's unit plan")
            if cursor is None:
                snapshot_id, offset = uuid.uuid4().hex, 0
                items = []
                for (payload,) in db.execute("SELECT payload FROM objects WHERE kind='Case' ORDER BY id"):
                    case = Case(**json.loads(payload))
                    if case.run_id != self.store.run.id or case.scope_id not in scopes:
                        raise BindingError("case is outside this run's unit plan")
                    self.store._unit_refs(db, case.evidence_refs + case.counter_evidence_refs)
                    items.append(
                        {
                            "id": case.id,
                            "run_id": case.run_id,
                            "scope_id": case.scope_id,
                            "trigger": case.trigger,
                            "claims": case.claims,
                            "revision": case.revision,
                            "evidence_refs": case.evidence_refs,
                            "counter_evidence_refs": case.counter_evidence_refs,
                            "type": "case_directory_entry",
                            "trust": "untrusted_navigation",
                        }
                    )
                snapshot = {"run_id": self.store.run.id, "scope_id": task.object_id, "view": "cases", "items": items}
                db.execute("INSERT INTO shared_investigation_snapshots VALUES (?,?)", (snapshot_id, _json(snapshot)))
            else:
                if not isinstance(cursor, str) or ":" not in cursor:
                    raise ValueError("invalid cursor")
                snapshot_id, number = cursor.rsplit(":", 1)
                if not number.isdecimal():
                    raise ValueError("invalid cursor")
                offset = int(number)
                row = db.execute(
                    "SELECT payload FROM shared_investigation_snapshots WHERE id=?", (snapshot_id,)
                ).fetchone()
                if row is None:
                    raise ValueError("unknown cursor")
                snapshot = json.loads(row[0])
                if (
                    snapshot["run_id"] != self.store.run.id
                    or snapshot["scope_id"] != task.object_id
                    or snapshot.get("view") != "cases"
                ):
                    raise BindingError("cursor belongs to another run, scope, or view")
                items = snapshot["items"]
                if offset > len(items):
                    raise ValueError("cursor beyond snapshot")
            end = min(offset + limit, len(items))
            self._task(db, task_id, token, self._clock(now))
            db.commit()
            return {
                "items": items[offset:end],
                "next_cursor": f"{snapshot_id}:{end}" if end < len(items) else None,
                "total": len(items),
                "trust": "untrusted_navigation",
                "scope_id": task.object_id,
            }
