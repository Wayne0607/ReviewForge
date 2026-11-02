"""Durable, run-bound v4.2 work board and immutable evidence store."""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import time
import uuid
from dataclasses import asdict, fields, replace
from datetime import datetime
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import urlsplit

from .contracts import (
    BindingError,
    Case,
    EvidenceRef,
    LeaseError,
    NavigationItem,
    Question,
    ReviewScope,
    RevisionConflict,
    Run,
    Task,
)

T = TypeVar("T", ReviewScope, Question, Case, Task, EvidenceRef, NavigationItem)
_TYPES = {c.__name__: c for c in (ReviewScope, Question, Case, Task, EvidenceRef, NavigationItem)}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class ReviewStore:
    """A store belongs to one immutable run; operations use short SQLite transactions."""

    def __init__(self, path: str | Path, run: Run):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.blob_dir = self.path.parent / ".review-v42-blobs"
        self.blob_dir.mkdir(parents=True, exist_ok=True)
        self.audit_dir = self.path.parent / ".review-v42-turn-audit"
        self.run = run
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS run (id INTEGER PRIMARY KEY CHECK (id=1), payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS objects (
                    kind TEXT NOT NULL, id TEXT NOT NULL, revision INTEGER NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY (kind,id));
                CREATE TABLE IF NOT EXISTS verdicts (
                    id TEXT PRIMARY KEY, case_id TEXT NOT NULL, claim_id TEXT NOT NULL,
                    revision INTEGER NOT NULL, policy TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS task_sessions (
                    task_id TEXT PRIMARY KEY, input_revision INTEGER NOT NULL, messages TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, object_id TEXT NOT NULL,
                    action TEXT NOT NULL, payload TEXT NOT NULL);
            """)
            row = db.execute("SELECT payload FROM run WHERE id=1").fetchone()
            if row is None:
                db.execute("INSERT INTO run VALUES (1, ?)", (_json(asdict(run)),))
            elif row[0] != _json(asdict(run)):
                raise BindingError("store is bound to a different run or snapshot")
            if run.policy in {"v42-stateful-2", "v42-workspace-3"}:
                self.audit_dir.mkdir(parents=True, exist_ok=True)
                db.execute(
                    "CREATE TABLE IF NOT EXISTS turn_audit (seq INTEGER PRIMARY KEY AUTOINCREMENT, "
                    "task_id TEXT NOT NULL, turn_id TEXT NOT NULL, phase TEXT NOT NULL, "
                    "content_hash TEXT NOT NULL, UNIQUE(task_id,turn_id,phase))"
                )
            if run.policy == "v42-workspace-3":
                db.execute(
                    "CREATE TABLE IF NOT EXISTS navigation_aliases ("
                    "scope_id TEXT NOT NULL, fingerprint TEXT NOT NULL, item_id TEXT NOT NULL, "
                    "PRIMARY KEY(scope_id,fingerprint))"
                )

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.execute("PRAGMA busy_timeout=30000")
        return db

    @staticmethod
    def _event(db: sqlite3.Connection, kind: str, object_id: str, action: str, payload: Any) -> None:
        db.execute(
            "INSERT INTO events (kind,object_id,action,payload) VALUES (?,?,?,?)",
            (kind, object_id, action, _json(payload)),
        )

    def _validate_refs(self, db: sqlite3.Connection, obj: Any) -> None:
        if isinstance(obj, ReviewScope):
            state = obj.working_state
            if not isinstance(state, dict):
                raise ValueError("working_state must be an object")
            if state:
                if self.run.policy not in {"v42-stateful-2", "v42-workspace-3"}:
                    raise ValueError("working_state requires a stateful policy")
                if set(state) != {"question", "observations", "open_questions", "next_step"}:
                    raise ValueError("working_state requires question, observations, open_questions and next_step")
                if any(not isinstance(state[key], str) or not state[key].strip() for key in ("question", "next_step")):
                    raise ValueError("working_state question and next_step must be nonempty strings")
                if not isinstance(state["open_questions"], list) or any(
                    not isinstance(item, str) or not item.strip() for item in state["open_questions"]
                ):
                    raise ValueError("open_questions must be nonempty strings")
                if not isinstance(state["observations"], list):
                    raise ValueError("observations must be a list")
                for item in state["observations"]:
                    if (
                        not isinstance(item, dict)
                        or set(item) != {"statement", "evidence_refs"}
                        or not isinstance(item["statement"], str)
                        or not item["statement"].strip()
                        or not isinstance(item["evidence_refs"], list)
                        or any(not isinstance(ref, str) or not ref for ref in item["evidence_refs"])
                    ):
                        raise ValueError("each observation requires a statement and evidence reference list")
                    for ref in item["evidence_refs"]:
                        if (
                            db.execute("SELECT 1 FROM objects WHERE kind='EvidenceRef' AND id=?", (ref,)).fetchone()
                            is None
                        ):
                            raise BindingError(f"unknown evidence reference: {ref}")
        if isinstance(obj, Case):
            ids = []
            for claim in obj.claims:
                if (
                    not isinstance(claim, dict)
                    or set(claim) - {"id", "statement", "impact"}
                    or not isinstance(claim.get("id"), str)
                    or not claim["id"].strip()
                    or any(not isinstance(value, str) for value in claim.values())
                ):
                    raise ValueError("claims require a unique id and only string id, statement and impact fields")
                ids.append(claim["id"])
            if len(ids) != len(set(ids)):
                raise ValueError("claim ids must be unique")
        if isinstance(obj, Question):
            interpretation_ids = set()
            for interpretation in obj.interpretations:
                if not isinstance(interpretation, dict):
                    raise ValueError("interpretations must be objects")
                if (
                    set(interpretation) - {"id", "text", "status", "reason", "evidence_refs"}
                    or not isinstance(interpretation.get("id"), str)
                    or not interpretation["id"].strip()
                    or any(
                        not isinstance(interpretation[k], str)
                        for k in ("text", "status", "reason")
                        if k in interpretation
                    )
                ):
                    raise ValueError("interpretations require an id and known scalar fields")
                if interpretation["id"] in interpretation_ids:
                    raise ValueError("interpretation ids must be unique")
                interpretation_ids.add(interpretation["id"])
                if interpretation.get("status", "supported") not in {"supported", "disputed", "retracted"}:
                    raise ValueError("invalid interpretation status")
                if interpretation.get("status") == "retracted" and not interpretation.get("reason"):
                    raise ValueError("retracted interpretation requires a reason")
                refs = interpretation.get("evidence_refs", [])
                if not isinstance(refs, list) or any(not isinstance(ref, str) for ref in refs):
                    raise ValueError("interpretation evidence_refs must be a list of ids")
                for ref in refs:
                    if db.execute("SELECT 1 FROM objects WHERE kind='EvidenceRef' AND id=?", (ref,)).fetchone() is None:
                        raise BindingError(f"unknown evidence reference: {ref}")
        for attr in ("evidence_refs", "counter_evidence_refs"):
            for ref in getattr(obj, attr, []):
                if db.execute("SELECT 1 FROM objects WHERE kind='EvidenceRef' AND id=?", (ref,)).fetchone() is None:
                    raise BindingError(f"unknown evidence reference: {ref}")

    @staticmethod
    def _invalidate_question_cases(db: sqlite3.Connection, question_id: str, *case_ids: str) -> None:
        rows = db.execute("SELECT id,payload FROM objects WHERE kind='Case'").fetchall()
        for case_id, payload in rows:
            case = Case(**json.loads(payload))
            if question_id in case.question_ids or case_id in case_ids:
                revised = replace(case, revision=case.revision + 1)
                db.execute(
                    "UPDATE objects SET revision=?,payload=? WHERE kind='Case' AND id=?",
                    (revised.revision, _json(asdict(revised)), case_id),
                )
                ReviewStore._event(db, "Case", case_id, "dependency_changed", {"question_id": question_id})

    @staticmethod
    def _guard_review_task(db: sqlite3.Connection, task_id: str, token: str, scope_id: str, now: float | None) -> None:
        now = time.time() if now is None else now
        if not math.isfinite(now):
            raise ValueError("time must be finite")
        row = db.execute("SELECT payload FROM objects WHERE kind='Task' AND id=?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(task_id)
        task = Task(**json.loads(row[0]))
        if (
            task.kind != "review"
            or task.status != "leased"
            or not token
            or task.lease_token != token
            or task.lease_expires <= now
            or task.object_id != scope_id
        ):
            raise LeaseError("a current review lease for this scope is required")
        row = db.execute("SELECT revision FROM objects WHERE kind='ReviewScope' AND id=?", (scope_id,)).fetchone()
        if row is None or row[0] != task.input_revision:
            raise RevisionConflict("review task scope revision is stale")

    def add_for_task(self, task_id: str, token: str, obj: T, *, now: float | None = None) -> T:
        if isinstance(obj, Task):
            if (
                obj.kind != "question"
                or obj.run_id != self.run.id
                or obj.revision != 0
                or obj.status != "pending"
                or obj.owner
                or obj.lease_token
                or obj.lease_expires
                or obj.result
            ):
                raise ValueError("only a new question task may be delegated")
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute(
                    "SELECT payload FROM objects WHERE kind='Question' AND id=?", (obj.object_id,)
                ).fetchone()
                if row is None:
                    raise KeyError(obj.object_id)
                question = Question(**json.loads(row[0]))
                self._guard_review_task(db, task_id, token, question.scope_id, now)
                if question.status != "open" or question.revision != obj.input_revision:
                    raise RevisionConflict("delegated question is not open at the expected revision")
                row = db.execute("SELECT payload FROM objects WHERE kind='Task' AND id=?", (obj.id,)).fetchone()
                payload = _json(asdict(obj))
                if row and row[0] != payload:
                    raise RevisionConflict("task identity already exists")
                if not row:
                    db.execute("INSERT INTO objects VALUES ('Task',?,0,?)", (obj.id, payload))
                    self._event(db, "Task", obj.id, "add", asdict(obj))
                db.commit()
            return obj
        if not isinstance(obj, (Question, Case)):
            raise TypeError("review tasks may add questions, cases or question tasks")
        return self.add(obj, _task_id=task_id, _lease_token=token, _now=now)

    def update_for_task(
        self,
        task_id: str,
        token: str,
        cls: type[T],
        object_id: str,
        expected_revision: int,
        *,
        now: float | None = None,
        **changes: Any,
    ) -> T:
        if cls not in (Question, Case):
            raise TypeError("review tasks may update questions or cases")
        if self.get(Task, task_id).kind == "question":
            if cls is not Question:
                raise TypeError("question task may update only its own question")
            return self._update_question_task(task_id, token, object_id, expected_revision, now=now, **changes)
        return self.update(cls, object_id, expected_revision, _task_id=task_id, _lease_token=token, _now=now, **changes)

    def add(
        self, obj: T, *, _task_id: str | None = None, _lease_token: str | None = None, _now: float | None = None
    ) -> T:
        kind = type(obj).__name__
        if kind not in _TYPES or obj.run_id != self.run.id:
            raise BindingError("object is not bound to this run")
        if isinstance(obj, EvidenceRef):
            raise BindingError("use put_evidence to create evidence")
        if isinstance(obj, NavigationItem):
            raise BindingError("navigation identities are controller-managed")
        if isinstance(obj, ReviewScope) and self.run.policy == "v42-workspace-3" and obj.working_state:
            raise ValueError("policy3 working state must be added through merge_working_states")
        if isinstance(obj, Task) and (
            obj.status != "pending" or obj.owner or obj.lease_token or obj.lease_expires or obj.result
        ):
            raise ValueError("new tasks must start pending without a lease or result")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if _task_id is not None:
                self._guard_review_task(db, _task_id, _lease_token or "", obj.scope_id, _now)
            self._validate_refs(db, obj)
            payload = _json(asdict(obj))
            old = db.execute("SELECT payload FROM objects WHERE kind=? AND id=?", (kind, obj.id)).fetchone()
            if old:
                if old[0] != payload:
                    raise RevisionConflict(f"{kind} {obj.id} already exists")
                return obj
            if obj.revision != 0:
                raise ValueError("new objects start at revision 0")
            db.execute("INSERT INTO objects VALUES (?,?,?,?)", (kind, obj.id, obj.revision, payload))
            self._event(db, kind, obj.id, "add", asdict(obj))
            if isinstance(obj, Question):
                self._invalidate_question_cases(db, obj.id, obj.case_id)
            db.commit()
        return obj

    def get(self, cls: type[T], object_id: str) -> T:
        if cls.__name__ not in _TYPES:
            raise TypeError("unsupported object type")
        with self._connect() as db:
            row = db.execute("SELECT payload FROM objects WHERE kind=? AND id=?", (cls.__name__, object_id)).fetchone()
        if row is None:
            raise KeyError(object_id)
        return cls(**json.loads(row[0]))

    def list(self, cls: type[T]) -> list[T]:
        if cls.__name__ not in _TYPES:
            raise TypeError("unsupported object type")
        with self._connect() as db:
            rows = db.execute("SELECT payload FROM objects WHERE kind=? ORDER BY id", (cls.__name__,)).fetchall()
        return [cls(**json.loads(row[0])) for row in rows]

    def update(
        self,
        cls: type[T],
        object_id: str,
        expected_revision: int,
        *,
        _task_id: str | None = None,
        _lease_token: str | None = None,
        _now: float | None = None,
        **changes: Any,
    ) -> T:
        if cls not in (ReviewScope, Question, Case):
            raise TypeError("updates are only supported for scopes, questions and cases")
        if cls is ReviewScope and self.run.policy == "v42-workspace-3" and "working_state" in changes:
            raise ValueError("policy3 working state must be added through merge_working_states")
        if {"id", "run_id", "revision"} & changes.keys():
            raise ValueError("identity and revision are controller-managed")
        valid = {f.name for f in fields(cls)}
        if changes.keys() - valid:
            raise ValueError("unknown update fields")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT revision,payload FROM objects WHERE kind=? AND id=?", (cls.__name__, object_id)
            ).fetchone()
            if row is None:
                raise KeyError(object_id)
            if _task_id is not None:
                guarded = cls(**json.loads(row[1]))
                if "scope_id" in changes and changes["scope_id"] != guarded.scope_id:
                    raise BindingError("task cannot transfer an object to another scope")
                self._guard_review_task(db, _task_id, _lease_token or "", guarded.scope_id, _now)
            if row[0] != expected_revision:
                self._event(
                    db,
                    cls.__name__,
                    object_id,
                    "conflict",
                    {
                        "expected_revision": expected_revision,
                        "current_revision": row[0],
                        "attempted_changes": changes,
                    },
                )
                db.commit()
                raise RevisionConflict(f"expected revision {expected_revision}, found {row[0]}")
            old = cls(**json.loads(row[1]))
            if all(getattr(old, key) == value for key, value in changes.items()):
                return old
            if cls is Case:
                if old.trigger and "trigger" in changes and changes["trigger"] != old.trigger:
                    raise ValueError("case trigger is fixed once provided; create a new case")
                if {key for key, value in changes.items() if getattr(old, key) != value} <= {"status"} and db.execute(
                    "SELECT 1 FROM verdicts WHERE case_id=? LIMIT 1", (object_id,)
                ).fetchone():
                    raise ValueError("status alone cannot reopen a verified case")
            if cls is Question and "interpretations" in changes:
                old_ids = {item["id"] for item in old.interpretations}
                new_ids = {item.get("id") for item in changes["interpretations"] if isinstance(item, dict)}
                if not old_ids <= new_ids:
                    raise ValueError("retract interpretations explicitly with an id and reason")
            obj = replace(old, **changes, revision=expected_revision + 1)
            if (
                cls is Question
                and (obj.status == "retracted" or obj.answer_status == "retracted")
                and not obj.change_reason
            ):
                raise ValueError("retraction requires change_reason")
            self._validate_refs(db, obj)
            db.execute(
                "UPDATE objects SET revision=?,payload=? WHERE kind=? AND id=?",
                (obj.revision, _json(asdict(obj)), cls.__name__, object_id),
            )
            self._event(db, cls.__name__, object_id, "update", {"before": asdict(old), "after": asdict(obj)})
            if cls is Question:
                self._invalidate_question_cases(db, object_id, obj.case_id, old.case_id)
            db.commit()
        return obj

    def put_evidence(
        self,
        content: bytes,
        *,
        revision: str,
        side: str,
        kind: str,
        path: str = "",
        start_line: int | None = None,
        end_line: int | None = None,
        environment: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> EvidenceRef:
        if not isinstance(content, bytes):
            raise TypeError("evidence content must be bytes")
        if side not in ("base", "head", "external"):
            raise BindingError("side must be base, head or external")
        if side == "external":
            info = metadata or {}
            source_url, captured_at = info.get("source_url"), info.get("captured_at")
            if not isinstance(revision, str) or not revision.strip() or not isinstance(source_url, str):
                raise BindingError("external evidence requires its own revision and source_url")
            parts = urlsplit(source_url)
            if parts.scheme not in {"http", "https"} or not parts.netloc or parts.username or parts.password:
                raise BindingError("external evidence requires an absolute source URL")
            try:
                captured = datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
            except (AttributeError, TypeError, ValueError) as exc:
                raise BindingError("external evidence requires captured_at timestamp") from exc
            if captured.tzinfo is None or captured.utcoffset() is None:
                raise BindingError("external capture timestamp must include a timezone")
            if info.get("historical_head_time") is not False:
                raise BindingError("external evidence cannot claim to be a head-time snapshot")
        if side != "external" and revision != getattr(self.run, side):
            raise BindingError("evidence revision does not match the selected snapshot")
        if (start_line is None) != (end_line is None) or (
            start_line is not None and (start_line < 1 or end_line < start_line)
        ):
            raise ValueError("invalid source range")
        digest = hashlib.sha256(content).hexdigest()
        identity = {
            "run_id": self.run.id,
            "repo": self.run.repo,
            "revision": revision,
            "side": side,
            "kind": kind,
            "content_hash": digest,
            "path": path,
            "start_line": start_line,
            "end_line": end_line,
            "environment": environment,
            "metadata": metadata or {},
        }
        ref = EvidenceRef(id="ev_" + hashlib.sha256(_json(identity).encode()).hexdigest(), **identity)
        blob = self.blob_dir / digest
        if not blob.exists():
            temp = self.blob_dir / (digest + "." + uuid.uuid4().hex + ".tmp")
            try:
                temp.write_bytes(content)
                os.replace(temp, blob)
            finally:
                temp.unlink(missing_ok=True)
        if hashlib.sha256(blob.read_bytes()).hexdigest() != digest:
            raise BindingError("evidence blob hash mismatch")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            payload = _json(asdict(ref))
            row = db.execute("SELECT payload FROM objects WHERE kind='EvidenceRef' AND id=?", (ref.id,)).fetchone()
            if row and row[0] != payload:
                raise BindingError("evidence identity collision")
            if not row:
                db.execute("INSERT INTO objects VALUES ('EvidenceRef',?,0,?)", (ref.id, payload))
                self._event(db, "EvidenceRef", ref.id, "add", asdict(ref))
            db.commit()
        return ref

    def read_evidence(self, evidence_id: str) -> bytes:
        ref = self.get(EvidenceRef, evidence_id)
        content = (self.blob_dir / ref.content_hash).read_bytes()
        if hashlib.sha256(content).hexdigest() != ref.content_hash:
            raise BindingError("evidence blob hash mismatch")
        return content

    def history(self, kind: str | None = None, object_id: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT seq,kind,object_id,action,payload FROM events WHERE 1=1"
        args: list[str] = []
        if kind is not None:
            sql += " AND kind=?"
            args.append(kind)
        if object_id is not None:
            sql += " AND object_id=?"
            args.append(object_id)
        with self._connect() as db:
            rows = db.execute(sql + " ORDER BY seq", args).fetchall()
        return [dict(seq=s, kind=k, object_id=i, action=a, payload=json.loads(p)) for s, k, i, a, p in rows]

    def acquire_task(self, task_id: str, owner: str, ttl: float, now: float | None = None) -> Task:
        if not owner or not math.isfinite(ttl) or ttl <= 0:
            raise ValueError("owner and positive ttl are required")
        now = time.time() if now is None else now
        if not math.isfinite(now):
            raise ValueError("time must be finite")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload FROM objects WHERE kind='Task' AND id=?", (task_id,)).fetchone()
            if not row:
                raise KeyError(task_id)
            task = Task(**json.loads(row[0]))
            if task.status in {"completed", "superseded"}:
                raise LeaseError("task already settled")
            target_rows = db.execute(
                "SELECT kind,revision FROM objects WHERE id=? AND kind IN ('Case','ReviewScope','Question')",
                (task.object_id,),
            ).fetchall()
            if len(target_rows) != 1 or target_rows[0][1] != task.input_revision:
                raise RevisionConflict("task input revision is stale or ambiguous")
            is_case = target_rows[0][0] == "Case"
            active = db.execute("SELECT payload FROM objects WHERE kind='Task'").fetchall()
            for (payload,) in active:
                other = Task(**json.loads(payload))
                same_input = (other.object_id, other.input_revision) == (task.object_id, task.input_revision)
                if same_input and (is_case or other.kind == task.kind):
                    if (other.status == "completed" and other.kind == task.kind) or (
                        other.status == "leased" and other.lease_expires > now
                    ):
                        raise LeaseError("input already has an active lease")
            if not math.isfinite(now + ttl):
                raise ValueError("lease expiry must be finite")
            task = replace(
                task,
                status="leased",
                owner=owner,
                lease_token=uuid.uuid4().hex,
                lease_expires=now + ttl,
                revision=task.revision + 1,
            )
            db.execute(
                "UPDATE objects SET revision=?,payload=? WHERE kind='Task' AND id=?",
                (task.revision, _json(asdict(task)), task.id),
            )
            self._event(db, "Task", task.id, "acquire", asdict(task))
            db.commit()
        return task

    def complete_task(self, task_id: str, token: str, result: dict[str, Any], now: float | None = None) -> Task:
        now = time.time() if now is None else now
        if not math.isfinite(now):
            raise ValueError("time must be finite")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload FROM objects WHERE kind='Task' AND id=?", (task_id,)).fetchone()
            if not row:
                raise KeyError(task_id)
            task = Task(**json.loads(row[0]))
            if task.status == "completed" and task.lease_token == token and task.result == result:
                return task
            if task.status != "leased" or not token or task.lease_token != token or task.lease_expires <= now:
                raise LeaseError("task lease is expired or superseded")
            rows = db.execute(
                "SELECT revision FROM objects WHERE id=? AND kind IN ('Case','ReviewScope','Question')",
                (task.object_id,),
            ).fetchall()
            if len(rows) != 1 or rows[0][0] != task.input_revision:
                raise RevisionConflict("task input revision is stale or ambiguous")
            task = replace(task, status="completed", result=result, revision=task.revision + 1)
            db.execute(
                "UPDATE objects SET revision=?,payload=? WHERE kind='Task' AND id=?",
                (task.revision, _json(asdict(task)), task.id),
            )
            self._event(db, "Task", task.id, "complete", asdict(task))
            db.commit()
        return task

    def save_verdict(
        self,
        case_id: str,
        claim_id: str,
        expected_revision: int,
        policy: str,
        verdict: str,
        evidence_refs: list[str],
        result: dict[str, Any],
        *,
        task_id: str | None = None,
        lease_token: str | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Freeze one independently produced decision for one immutable claim input."""
        if not policy or verdict not in ("accept", "reject", "needs_evidence", "unresolved"):
            raise ValueError("invalid policy or verdict")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT revision,payload FROM objects WHERE kind='Case' AND id=?", (case_id,)).fetchone()
            if row is None:
                raise KeyError(case_id)
            if row[0] != expected_revision:
                raise RevisionConflict("case revision changed during verification")
            case = Case(**json.loads(row[1]))
            now = time.time() if now is None else now
            if not math.isfinite(now):
                raise ValueError("time must be finite")
            tasks = [
                Task(**json.loads(r[0])) for r in db.execute("SELECT payload FROM objects WHERE kind='Task'").fetchall()
            ]
            verification_tasks = [t for t in tasks if t.object_id == case_id and t.kind in ("verify", "verification")]
            if task_id is not None or lease_token is not None or verification_tasks:
                task = next((t for t in verification_tasks if t.id == task_id), None)
                if (
                    task is None
                    or not lease_token
                    or task.lease_token != lease_token
                    or task.status != "leased"
                    or task.lease_expires <= now
                    or task.input_revision != expected_revision
                ):
                    raise LeaseError("a current verification lease for this case revision is required")
            matching = [c for c in case.claims if c.get("id") == claim_id]
            if len(matching) != 1:
                raise ValueError("claim id must identify exactly one claim")
            expected_refs = set(case.evidence_refs + case.counter_evidence_refs)
            if set(evidence_refs) != expected_refs:
                raise BindingError("verdict must bind all case evidence and counter-evidence")
            sources = []
            for ref_id in sorted(expected_refs):
                row = db.execute("SELECT payload FROM objects WHERE kind='EvidenceRef' AND id=?", (ref_id,)).fetchone()
                if row is None:
                    raise BindingError("unknown evidence")
                ref = EvidenceRef(**json.loads(row[0]))
                content = (self.blob_dir / ref.content_hash).read_bytes()
                if hashlib.sha256(content).hexdigest() != ref.content_hash:
                    raise BindingError("evidence blob hash mismatch")
                sources.append(asdict(ref))
            digest = hashlib.sha256(_json(sources).encode()).hexdigest()
            key = hashlib.sha256(_json([case_id, claim_id, expected_revision, digest, policy]).encode()).hexdigest()
            decision = {
                "run_id": self.run.id,
                "repo": self.run.repo,
                "base": self.run.base,
                "head": self.run.head,
                "case_id": case_id,
                "claim_id": claim_id,
                "case_revision": expected_revision,
                "source_digest": digest,
                "evidence_refs": sorted(expected_refs),
                "policy": policy,
                "verdict": verdict,
                "result": result,
            }
            row = db.execute("SELECT payload FROM verdicts WHERE id=?", (key,)).fetchone()
            if row:
                if json.loads(row[0]) != decision:
                    raise RevisionConflict("a different verdict is already frozen for this input")
                return decision
            db.execute(
                "INSERT INTO verdicts VALUES (?,?,?,?,?,?)",
                (key, case_id, claim_id, expected_revision, policy, _json(decision)),
            )
            self._event(db, "Case", case_id, "verdict", decision)
            db.commit()
        return decision

    def get_verdict(self, case_id: str, claim_id: str, policy: str) -> dict[str, Any] | None:
        """Check case, decision and sources against a single database snapshot."""
        with self._connect() as db:
            db.execute("BEGIN")
            row = db.execute("SELECT payload FROM objects WHERE kind='Case' AND id=?", (case_id,)).fetchone()
            if row is None:
                raise KeyError(case_id)
            case = Case(**json.loads(row[0]))
            rows = db.execute(
                "SELECT payload FROM verdicts WHERE case_id=? AND claim_id=? AND revision=? AND policy=?",
                (case_id, claim_id, case.revision, policy),
            ).fetchall()
            if not rows:
                return None
            if len(rows) != 1:
                raise BindingError("ambiguous verdicts for current input")
            decision = json.loads(rows[0][0])
            sources = []
            for ref_id in decision["evidence_refs"]:
                row = db.execute("SELECT payload FROM objects WHERE kind='EvidenceRef' AND id=?", (ref_id,)).fetchone()
                if row is None:
                    raise BindingError("verdict references missing evidence")
                ref = EvidenceRef(**json.loads(row[0]))
                content = (self.blob_dir / ref.content_hash).read_bytes()
                if hashlib.sha256(content).hexdigest() != ref.content_hash:
                    raise BindingError("evidence blob hash mismatch")
                sources.append(asdict(ref))
            digest = hashlib.sha256(_json(sources).encode()).hexdigest()
            if digest != decision["source_digest"] or set(decision["evidence_refs"]) != set(
                case.evidence_refs + case.counter_evidence_refs
            ):
                raise BindingError("verdict source binding no longer matches")
            return decision

    def renew_task(self, task_id: str, token: str, ttl: float, now: float | None = None) -> Task:
        if not math.isfinite(ttl) or ttl <= 0:
            raise ValueError("positive ttl required")
        return self._change_lease(task_id, token, ttl, False, now)

    def release_task(self, task_id: str, token: str, now: float | None = None) -> Task:
        return self._change_lease(task_id, token, 0, True, now)

    def _change_lease(self, task_id: str, token: str, ttl: float, release: bool, now: float | None) -> Task:
        now = time.time() if now is None else now
        if not math.isfinite(now):
            raise ValueError("time must be finite")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload FROM objects WHERE kind='Task' AND id=?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(task_id)
            task = Task(**json.loads(row[0]))
            if task.status != "leased" or not token or task.lease_token != token or task.lease_expires <= now:
                raise LeaseError("task lease is expired or superseded")
            if not release and not math.isfinite(now + ttl):
                raise ValueError("lease expiry must be finite")
            task = replace(
                task,
                status="pending" if release else "leased",
                owner="" if release else task.owner,
                lease_token="" if release else token,
                lease_expires=0 if release else now + ttl,
                revision=task.revision + 1,
            )
            db.execute(
                "UPDATE objects SET revision=?,payload=? WHERE kind='Task' AND id=?",
                (task.revision, _json(asdict(task)), task.id),
            )
            self._event(db, "Task", task.id, "release" if release else "renew", asdict(task))
            db.commit()
        return task

    def update_scope_for_task(
        self, task_id: str, token: str, expected_revision: int, *, now: float | None = None, **changes: Any
    ) -> ReviewScope:
        """Advance the scope and its review-task input together under the same lease."""
        if self.run.policy == "v42-workspace-3" and "working_state" in changes:
            raise ValueError("policy3 working state must be added through merge_working_states")
        if {"id", "run_id", "revision"} & changes.keys() or changes.keys() - {f.name for f in fields(ReviewScope)}:
            raise ValueError("invalid scope update fields")
        now = time.time() if now is None else now
        if not math.isfinite(now):
            raise ValueError("time must be finite")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload FROM objects WHERE kind='Task' AND id=?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(task_id)
            task = Task(**json.loads(row[0]))
            if (
                task.kind != "review"
                or task.status != "leased"
                or not token
                or task.lease_token != token
                or task.lease_expires <= now
            ):
                raise LeaseError("a current review lease is required")
            row = db.execute(
                "SELECT payload FROM objects WHERE kind='ReviewScope' AND id=?", (task.object_id,)
            ).fetchone()
            if row is None:
                raise KeyError(task.object_id)
            old = ReviewScope(**json.loads(row[0]))
            if old.revision != expected_revision or task.input_revision != expected_revision:
                self._event(
                    db,
                    "ReviewScope",
                    old.id,
                    "conflict",
                    {
                        "expected_revision": expected_revision,
                        "current_revision": old.revision,
                        "task_input_revision": task.input_revision,
                        "attempted_changes": changes,
                    },
                )
                db.commit()
                raise RevisionConflict("review scope or task input revision changed")
            if all(getattr(old, key) == value for key, value in changes.items()):
                return old
            updated = replace(old, **changes, revision=old.revision + 1)
            self._validate_refs(db, updated)
            task = replace(task, input_revision=updated.revision, revision=task.revision + 1)
            db.execute(
                "UPDATE objects SET revision=?,payload=? WHERE kind='ReviewScope' AND id=?",
                (updated.revision, _json(asdict(updated)), updated.id),
            )
            db.execute(
                "UPDATE objects SET revision=?,payload=? WHERE kind='Task' AND id=?",
                (task.revision, _json(asdict(task)), task.id),
            )
            self._event(db, "ReviewScope", updated.id, "update", {"before": asdict(old), "after": asdict(updated)})
            self._event(db, "Task", task.id, "advance_input", {"input_revision": task.input_revision})
            db.commit()
            return updated

    def save_task_session(
        self, task_id: str, token: str, messages: list[dict[str, Any]], *, now: float | None = None
    ) -> None:
        """Checkpoint visible messages; a session never authorizes a semantic result."""
        if not isinstance(messages, list) or any(not isinstance(item, dict) for item in messages):
            raise ValueError("messages must be a list of visible message objects")
        now = time.time() if now is None else now
        if not math.isfinite(now):
            raise ValueError("time must be finite")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload FROM objects WHERE kind='Task' AND id=?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(task_id)
            task = Task(**json.loads(row[0]))
            if task.status != "leased" or not token or task.lease_token != token or task.lease_expires <= now:
                raise LeaseError("a current task lease is required")
            target_kind = {"review": "ReviewScope", "question": "Question"}.get(task.kind)
            row = db.execute(
                "SELECT revision FROM objects WHERE kind=? AND id=?", (target_kind, task.object_id)
            ).fetchone()
            if row is None or row[0] != task.input_revision:
                raise RevisionConflict("task session input revision is stale")
            db.execute(
                "INSERT INTO task_sessions VALUES (?,?,?) ON CONFLICT(task_id) DO UPDATE SET "
                "input_revision=excluded.input_revision, messages=excluded.messages",
                (task.id, task.input_revision, _json(messages)),
            )
            db.commit()

    def load_task_session(self, task_id: str) -> list[dict[str, Any]]:
        with self._connect() as db:
            row = db.execute(
                "SELECT o.payload,s.input_revision,s.messages FROM objects o "
                "JOIN task_sessions s ON s.task_id=o.id WHERE o.kind='Task' AND o.id=?",
                (task_id,),
            ).fetchone()
            if row is None:
                return []
            task = Task(**json.loads(row[0]))
            target_kind = {"review": "ReviewScope", "question": "Question"}.get(task.kind)
            scope = db.execute(
                "SELECT revision FROM objects WHERE kind=? AND id=?", (target_kind, task.object_id)
            ).fetchone()
            if scope is None or scope[0] != task.input_revision or row[1] != task.input_revision:
                return []
            return json.loads(row[2])

    def settle_task_stale(self, task_id: str, token: str, result: dict[str, Any], *, now: float | None = None) -> Task:
        """Close a stale task as superseded; its result is never a verification permit."""
        now = time.time() if now is None else now
        if not math.isfinite(now):
            raise ValueError("time must be finite")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload FROM objects WHERE kind='Task' AND id=?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(task_id)
            task = Task(**json.loads(row[0]))
            if task.status != "leased" or not token or task.lease_token != token or task.lease_expires <= now:
                raise LeaseError("a current task lease is required")
            rows = db.execute(
                "SELECT revision FROM objects WHERE id=? AND kind IN ('Case','ReviewScope','Question')",
                (task.object_id,),
            ).fetchall()
            if len(rows) != 1 or rows[0][0] == task.input_revision:
                raise RevisionConflict("task must refer to an existing newer input revision")
            task = replace(
                task,
                status="superseded",
                result={"current_revision": rows[0][0], "detail": result},
                revision=task.revision + 1,
            )
            db.execute(
                "UPDATE objects SET revision=?,payload=? WHERE kind='Task' AND id=?",
                (task.revision, _json(asdict(task)), task.id),
            )
            self._event(db, "Task", task.id, "superseded", asdict(task))
            db.commit()
            return task

    def record_verification_observations(
        self,
        task_id: str,
        token: str,
        expected_revision: int,
        questions: list[Question],
        evidence_refs: list[str],
        *,
        now: float | None = None,
    ) -> Case:
        """Atomically bind new verifier sources and gaps under the original verification lease."""
        now = time.time() if now is None else now
        if not math.isfinite(now):
            raise ValueError("time must be finite")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload FROM objects WHERE kind='Task' AND id=?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(task_id)
            task = Task(**json.loads(row[0]))
            if (
                task.kind != "verify"
                or task.status != "leased"
                or not token
                or task.lease_token != token
                or task.lease_expires <= now
                or task.input_revision != expected_revision
            ):
                raise LeaseError("a current verification lease for this case revision is required")
            row = db.execute("SELECT payload FROM objects WHERE kind='Case' AND id=?", (task.object_id,)).fetchone()
            if row is None:
                raise KeyError(task.object_id)
            case = Case(**json.loads(row[0]))
            if case.revision != expected_revision:
                raise RevisionConflict("verification input revision is stale")
            question_ids = list(case.question_ids)
            for question in questions:
                if question.run_id != case.run_id or question.case_id != case.id or question.scope_id != case.scope_id:
                    raise BindingError("verification question does not belong to this case and scope")
                if question.revision != 0:
                    raise ValueError("new questions start at revision 0")
                self._validate_refs(db, question)
                payload = _json(asdict(question))
                row = db.execute(
                    "SELECT payload FROM objects WHERE kind='Question' AND id=?", (question.id,)
                ).fetchone()
                if row and row[0] != payload:
                    raise RevisionConflict("verification question identity already exists")
                if not row:
                    db.execute("INSERT INTO objects VALUES ('Question',?,0,?)", (question.id, payload))
                    self._event(db, "Question", question.id, "add", asdict(question))
                if question.id not in question_ids:
                    question_ids.append(question.id)
            if not set(case.evidence_refs) <= set(evidence_refs):
                raise BindingError("verification cannot silently remove case evidence")
            updated = replace(case, evidence_refs=list(evidence_refs), question_ids=question_ids)
            self._validate_refs(db, updated)
            if updated == case:
                return case
            updated = replace(updated, revision=case.revision + 1)
            db.execute(
                "UPDATE objects SET revision=?,payload=? WHERE kind='Case' AND id=?",
                (updated.revision, _json(asdict(updated)), case.id),
            )
            self._event(
                db, "Case", case.id, "verification_observations", {"before": asdict(case), "after": asdict(updated)}
            )
            db.commit()
            return updated

    def _update_question_task(
        self,
        task_id: str,
        token: str,
        question_id: str,
        expected_revision: int,
        *,
        now: float | None = None,
        **changes: Any,
    ) -> Question:
        if {"id", "run_id", "revision", "scope_id", "case_id"} & changes.keys() or changes.keys() - {
            f.name for f in fields(Question)
        }:
            raise ValueError("invalid question update fields")
        now = time.time() if now is None else now
        if not math.isfinite(now):
            raise ValueError("time must be finite")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload FROM objects WHERE kind='Task' AND id=?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(task_id)
            task = Task(**json.loads(row[0]))
            if (
                task.kind != "question"
                or task.object_id != question_id
                or task.status != "leased"
                or not token
                or task.lease_token != token
                or task.lease_expires <= now
            ):
                raise LeaseError("a current lease for this question is required")
            row = db.execute("SELECT payload FROM objects WHERE kind='Question' AND id=?", (question_id,)).fetchone()
            if row is None:
                raise KeyError(question_id)
            old = Question(**json.loads(row[0]))
            if old.revision != expected_revision or task.input_revision != expected_revision:
                raise RevisionConflict("question task input revision is stale")
            if all(getattr(old, key) == value for key, value in changes.items()):
                return old
            if "interpretations" in changes and not {v["id"] for v in old.interpretations} <= {
                v.get("id") for v in changes["interpretations"] if isinstance(v, dict)
            }:
                raise ValueError("retract interpretations explicitly with an id and reason")
            updated = replace(old, **changes, revision=old.revision + 1)
            if (updated.status == "retracted" or updated.answer_status == "retracted") and not updated.change_reason:
                raise ValueError("retraction requires change_reason")
            self._validate_refs(db, updated)
            task = replace(task, input_revision=updated.revision, revision=task.revision + 1)
            db.execute(
                "UPDATE objects SET revision=?,payload=? WHERE kind='Question' AND id=?",
                (updated.revision, _json(asdict(updated)), updated.id),
            )
            db.execute(
                "UPDATE objects SET revision=?,payload=? WHERE kind='Task' AND id=?",
                (task.revision, _json(asdict(task)), task.id),
            )
            self._event(db, "Question", old.id, "update", {"before": asdict(old), "after": asdict(updated)})
            self._event(db, "Task", task.id, "advance_input", {"input_revision": task.input_revision})
            self._invalidate_question_cases(db, old.id, old.case_id)
            db.commit()
            return updated

    def partition_scope(self, scope_id: str, expected_revision: int, children: list[ReviewScope]) -> list[ReviewScope]:
        """Replace an untouched scope with an exact, disjoint partition of its regions."""
        if len(children) < 2:
            raise ValueError("a partition requires at least two child scopes")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload FROM objects WHERE kind='ReviewScope' AND id=?", (scope_id,)).fetchone()
            if row is None:
                raise KeyError(scope_id)
            parent = ReviewScope(**json.loads(row[0]))
            if parent.revision != expected_revision:
                raise RevisionConflict("scope revision changed before partition")
            if (
                parent.status != "pending"
                or parent.checked_regions
                or parent.case_ids
                or parent.unresolved
                or parent.working_state
            ):
                raise ValueError("only an untouched pending scope can be partitioned")
            for kind in ("Task", "Question", "Case"):
                rows = db.execute("SELECT payload FROM objects WHERE kind=?", (kind,)).fetchall()
                for (payload,) in rows:
                    data = json.loads(payload)
                    if data.get("scope_id") == scope_id or (kind == "Task" and data.get("object_id") == scope_id):
                        raise ValueError("scope already has work or a task")
            original = {region.get("id"): region for region in parent.regions}
            if None in original or len(original) != len(parent.regions):
                raise ValueError("parent region ids must be unique")
            seen = set()
            child_ids = set()
            for child in children:
                if (
                    child.run_id != self.run.id
                    or child.id == parent.id
                    or child.id in child_ids
                    or child.status != "pending"
                    or child.revision != 0
                    or not child.regions
                    or child.checked_regions
                    or child.case_ids
                    or child.unresolved
                    or child.working_state
                ):
                    raise ValueError("invalid child scope")
                child_ids.add(child.id)
                if db.execute("SELECT 1 FROM objects WHERE kind='ReviewScope' AND id=?", (child.id,)).fetchone():
                    raise RevisionConflict("child scope already exists")
                for region in child.regions:
                    region_id = region.get("id")
                    if region_id in seen or region_id not in original or region != original[region_id]:
                        raise ValueError("partition repeats, modifies or invents a region")
                    seen.add(region_id)
            if seen != set(original):
                raise ValueError("partition does not cover all regions")
            revised = replace(parent, status="superseded", revision=parent.revision + 1)
            db.execute(
                "UPDATE objects SET revision=?,payload=? WHERE kind='ReviewScope' AND id=?",
                (revised.revision, _json(asdict(revised)), parent.id),
            )
            self._event(db, "ReviewScope", parent.id, "partition", {"children": sorted(child_ids)})
            for child in children:
                db.execute("INSERT INTO objects VALUES ('ReviewScope',?,0,?)", (child.id, _json(asdict(child))))
                self._event(db, "ReviewScope", child.id, "add", asdict(child))
            db.commit()
            return children

    def append_turn_audit(
        self,
        task_id: str,
        token: str,
        turn_id: str,
        phase: str,
        messages: list[dict[str, Any]],
        *,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Append one complete visible exchange, bound to the current review lease."""
        if self.run.policy not in {"v42-stateful-2", "v42-workspace-3"}:
            raise ValueError("turn audit requires a stateful policy")
        if not isinstance(turn_id, str) or not turn_id or not isinstance(phase, str) or not phase:
            raise ValueError("turn_id and phase are required")
        if not isinstance(messages, list) or any(not isinstance(message, dict) for message in messages):
            raise ValueError("visible messages must be a list of objects")

        private_fields = {
            "reasoning",
            "reasoning_content",
            "reasoning_details",
            "thinking",
            "thinking_blocks",
            "redacted_thinking",
        }
        private_blocks = {"reasoning", "reasoning_content", "thinking", "redacted_thinking"}
        for message in messages:
            data = message.get("data")
            body = data if isinstance(data, dict) else message
            if (
                message.get("type") not in ("ai", "AIMessage", "AIMessageChunk", "ai_chunk")
                and message.get("role") != "assistant"
                and body.get("type") not in ("ai", "AIMessage", "AIMessageChunk", "ai_chunk")
                and body.get("role") != "assistant"
            ):
                continue
            # Inspect only protocol metadata positions. Tool arguments and
            # arbitrary visible payloads may legitimately use these names.
            for container in (message, body, body.get("additional_kwargs"), body.get("response_metadata")):
                if isinstance(container, dict) and any(
                    isinstance(key, str) and key.lower() in private_fields for key in container
                ):
                    raise ValueError("provider reasoning is not a visible audit message")
            content = body.get("content")
            blocks = content if isinstance(content, list) else [content]
            if any(
                isinstance(block, dict)
                and isinstance(block.get("type"), str)
                and block["type"].lower() in private_blocks
                for block in blocks
            ):
                raise ValueError("provider reasoning is not a visible audit message")
        payload = _json(messages).encode("utf-8")
        digest = hashlib.sha256(payload).hexdigest()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload FROM objects WHERE kind='Task' AND id=?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(task_id)
            task = Task(**json.loads(row[0]))
            if task.run_id != self.run.id:
                raise BindingError("task belongs to a different run")
            self._guard_review_task(db, task_id, token, task.object_id, now)
            row = db.execute(
                "SELECT seq,content_hash FROM turn_audit WHERE task_id=? AND turn_id=? AND phase=?",
                (task_id, turn_id, phase),
            ).fetchone()
            if row and row[1] != digest:
                raise RevisionConflict("turn phase already has different visible content")
            blob = self.audit_dir / digest
            if blob.exists():
                if hashlib.sha256(blob.read_bytes()).hexdigest() != digest:
                    raise BindingError("turn audit blob hash mismatch")
            else:
                temp = self.audit_dir / (digest + "." + uuid.uuid4().hex + ".tmp")
                try:
                    with temp.open("wb") as stream:
                        stream.write(payload)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(temp, blob)
                finally:
                    temp.unlink(missing_ok=True)
            if row:
                return {"seq": row[0], "task_id": task_id, "turn_id": turn_id, "phase": phase, "content_hash": digest}
            cursor = db.execute(
                "INSERT INTO turn_audit (task_id,turn_id,phase,content_hash) VALUES (?,?,?,?)",
                (task_id, turn_id, phase, digest),
            )
            result = {
                "seq": cursor.lastrowid,
                "task_id": task_id,
                "turn_id": turn_id,
                "phase": phase,
                "content_hash": digest,
            }
            db.commit()
            return result

    def read_turn_audit(self, task_id: str, turn_id: str | None = None) -> list[dict[str, Any]]:
        """Explicitly retrieve complete, hash-checked visible history in append order."""
        if self.run.policy not in {"v42-stateful-2", "v42-workspace-3"}:
            raise ValueError("turn audit requires a stateful policy")
        with self._connect() as db:
            if db.execute("SELECT 1 FROM objects WHERE kind='Task' AND id=?", (task_id,)).fetchone() is None:
                raise KeyError(task_id)
            sql = "SELECT seq,turn_id,phase,content_hash FROM turn_audit WHERE task_id=?"
            args = [task_id]
            if turn_id is not None:
                sql += " AND turn_id=?"
                args.append(turn_id)
            rows = db.execute(sql + " ORDER BY seq", args).fetchall()
        result = []
        for seq, ident, phase, digest in rows:
            payload = (self.audit_dir / digest).read_bytes()
            if hashlib.sha256(payload).hexdigest() != digest:
                raise BindingError("turn audit blob hash mismatch")
            result.append(
                {
                    "seq": seq,
                    "task_id": task_id,
                    "turn_id": ident,
                    "phase": phase,
                    "content_hash": digest,
                    "messages": json.loads(payload),
                }
            )
        return result

    @staticmethod
    def _navigation_fingerprint(scope_id: str, kind: str, text: str, refs: list[str]) -> str:
        return hashlib.sha256(_json([scope_id, kind, text, sorted(set(refs))]).encode("utf-8")).hexdigest()

    def merge_working_states(
        self,
        task_id: str,
        token: str,
        expected_scope_revision: int,
        states: list[dict[str, Any]],
        *,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Merge independent untrusted navigation updates without replacing siblings."""
        if self.run.policy != "v42-workspace-3":
            raise ValueError("navigation requires v42-workspace-3")
        if not isinstance(states, list):
            raise ValueError("states must be a list")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload FROM objects WHERE kind='Task' AND id=?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(task_id)
            task = Task(**json.loads(row[0]))
            if task.run_id != self.run.id:
                raise BindingError("task belongs to a different run")
            self._guard_review_task(db, task_id, token, task.object_id, now)
            row = db.execute(
                "SELECT payload FROM objects WHERE kind='ReviewScope' AND id=?", (task.object_id,)
            ).fetchone()
            if row is None:
                raise KeyError(task.object_id)
            scope = ReviewScope(**json.loads(row[0]))
            if scope.revision != expected_scope_revision or task.input_revision != expected_scope_revision:
                raise RevisionConflict("scope revision changed before navigation merge")
            errors: list[dict[str, Any]] = []
            accepted = []
            for index, state in enumerate(states):
                try:
                    if not isinstance(state, dict) or not state:
                        raise ValueError("review_state must have the required fields")
                    self._validate_refs(db, replace(scope, working_state=state))
                    accepted.append((index, state))
                except (ValueError, BindingError, TypeError) as exc:
                    errors.append({"index": index, "error": str(exc)})
            matched: dict[str, NavigationItem] = {}
            created = False
            focus = scope.working_state
            for index, state in accepted:
                focus = {
                    "question": state["question"],
                    "observations": [],
                    "open_questions": [],
                    "next_step": state["next_step"],
                }
                updates = [("question", question, []) for question in state["open_questions"]]
                for observation in state["observations"]:
                    if not observation["evidence_refs"]:
                        errors.append(
                            {"index": index, "error": "observation ignored because it has no evidence reference"}
                        )
                        continue
                    updates.append(("observation", observation["statement"], observation["evidence_refs"]))
                for kind, text, refs in updates:
                    refs = sorted(set(refs))
                    fingerprint = self._navigation_fingerprint(scope.id, kind, text, refs)
                    row = db.execute(
                        "SELECT item_id FROM navigation_aliases WHERE scope_id=? AND fingerprint=?",
                        (scope.id, fingerprint),
                    ).fetchone()
                    if row:
                        entry = db.execute(
                            "SELECT payload FROM objects WHERE kind='NavigationItem' AND id=?", (row[0],)
                        ).fetchone()
                        if entry is None:
                            raise BindingError("navigation alias references a missing item")
                        item = NavigationItem(**json.loads(entry[0]))
                        if (
                            item.run_id != self.run.id
                            or item.scope_id != scope.id
                            or item.kind != kind
                            or item.status not in {"open", "resolved", "retracted"}
                        ):
                            raise BindingError("navigation alias belongs to a different identity")
                        if (
                            self._navigation_fingerprint(scope.id, item.kind, item.text, item.evidence_refs)
                            != fingerprint
                        ):
                            errors.append(
                                {
                                    "index": index,
                                    "error": f"navigation {item.id} was explicitly corrected; prior wording ignored",
                                }
                            )
                    else:
                        item = NavigationItem(
                            id="nav_" + uuid.uuid4().hex,
                            run_id=self.run.id,
                            scope_id=scope.id,
                            kind=kind,
                            text=text,
                            evidence_refs=refs,
                        )
                        db.execute(
                            "INSERT INTO objects VALUES ('NavigationItem',?,?,?)", (item.id, 0, _json(asdict(item)))
                        )
                        db.execute("INSERT INTO navigation_aliases VALUES (?,?,?)", (scope.id, fingerprint, item.id))
                        self._event(db, "NavigationItem", item.id, "add", {"task_id": task_id, "item": asdict(item)})
                        created = True
                    matched[item.id] = item
            if created or focus != scope.working_state:
                updated = replace(scope, working_state=focus, revision=scope.revision + 1)
                self._validate_refs(db, updated)
                task = replace(task, input_revision=updated.revision, revision=task.revision + 1)
                db.execute(
                    "UPDATE objects SET revision=?,payload=? WHERE kind='ReviewScope' AND id=?",
                    (updated.revision, _json(asdict(updated)), updated.id),
                )
                db.execute(
                    "UPDATE objects SET revision=?,payload=? WHERE kind='Task' AND id=?",
                    (task.revision, _json(asdict(task)), task.id),
                )
                self._event(
                    db,
                    "ReviewScope",
                    scope.id,
                    "navigation_merge",
                    {
                        "task_id": task_id,
                        "before": asdict(scope),
                        "after": asdict(updated),
                        "item_ids": list(matched),
                    },
                )
                scope = updated
            db.commit()
            return {"scope": scope, "items": list(matched.values()), "errors": errors}

    def update_navigation_for_task(
        self,
        task_id: str,
        token: str,
        item_id: str,
        expected_revision: int,
        *,
        reason: str,
        text: str | None = None,
        evidence_refs: list[str] | None = None,
        status: str | None = None,
        now: float | None = None,
    ) -> NavigationItem:
        """Explicitly revise, resolve, retract or reopen one untrusted navigation item."""
        if self.run.policy != "v42-workspace-3":
            raise ValueError("navigation requires v42-workspace-3")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("navigation updates require a reason")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload FROM objects WHERE kind='Task' AND id=?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(task_id)
            task = Task(**json.loads(row[0]))
            self._guard_review_task(db, task_id, token, task.object_id, now)
            row = db.execute("SELECT payload FROM objects WHERE kind='NavigationItem' AND id=?", (item_id,)).fetchone()
            if row is None:
                raise KeyError(item_id)
            item = NavigationItem(**json.loads(row[0]))
            if item.run_id != self.run.id or item.scope_id != task.object_id:
                raise BindingError("navigation item belongs to another run or scope")
            if item.revision != expected_revision:
                raise RevisionConflict("navigation item revision changed")
            new_text = item.text if text is None else text
            new_refs = item.evidence_refs if evidence_refs is None else evidence_refs
            new_status = item.status if status is None else status
            if not isinstance(new_text, str) or not new_text.strip():
                raise ValueError("navigation text must be nonempty")
            if not isinstance(new_refs, list) or any(not isinstance(ref, str) or not ref for ref in new_refs):
                raise ValueError("navigation evidence_refs must be a list of IDs")
            new_refs = sorted(set(new_refs))
            if item.kind == "observation" and not new_refs:
                raise ValueError("an observation needs at least one evidence reference")
            if new_status not in {"open", "resolved", "retracted"}:
                raise ValueError("invalid navigation status")
            for ref in new_refs:
                if db.execute("SELECT 1 FROM objects WHERE kind='EvidenceRef' AND id=?", (ref,)).fetchone() is None:
                    raise BindingError(f"unknown evidence reference: {ref}")
            fingerprint = self._navigation_fingerprint(item.scope_id, item.kind, new_text, new_refs)
            alias = db.execute(
                "SELECT item_id FROM navigation_aliases WHERE scope_id=? AND fingerprint=?",
                (item.scope_id, fingerprint),
            ).fetchone()
            if alias and alias[0] != item.id:
                raise RevisionConflict("corrected navigation duplicates another item")
            if (new_text, new_refs, new_status) == (item.text, item.evidence_refs, item.status):
                return item
            updated = replace(
                item,
                text=new_text,
                evidence_refs=new_refs,
                status=new_status,
                reason=reason,
                revision=item.revision + 1,
            )
            row = db.execute(
                "SELECT payload FROM objects WHERE kind='ReviewScope' AND id=?", (item.scope_id,)
            ).fetchone()
            scope = ReviewScope(**json.loads(row[0]))
            updated_scope = replace(scope, revision=scope.revision + 1)
            updated_task = replace(task, input_revision=updated_scope.revision, revision=task.revision + 1)
            db.execute(
                "UPDATE objects SET revision=?,payload=? WHERE kind='NavigationItem' AND id=?",
                (updated.revision, _json(asdict(updated)), item.id),
            )
            db.execute("INSERT OR IGNORE INTO navigation_aliases VALUES (?,?,?)", (item.scope_id, fingerprint, item.id))
            db.execute(
                "UPDATE objects SET revision=?,payload=? WHERE kind='ReviewScope' AND id=?",
                (updated_scope.revision, _json(asdict(updated_scope)), item.scope_id),
            )
            db.execute(
                "UPDATE objects SET revision=?,payload=? WHERE kind='Task' AND id=?",
                (updated_task.revision, _json(asdict(updated_task)), task.id),
            )
            self._event(
                db,
                "NavigationItem",
                item.id,
                "update",
                {"task_id": task_id, "reason": reason, "before": asdict(item), "after": asdict(updated)},
            )
            self._event(db, "ReviewScope", item.scope_id, "navigation_update", {"task_id": task_id, "item_id": item.id})
            db.commit()
            return updated

    def list_navigation(
        self, scope_id: str, *, status: str | None = "open", cursor: str | None = None, limit: int | None = None
    ) -> dict[str, Any]:
        """Explicit, lossless pagination; None returns the entire current set."""
        if self.run.policy != "v42-workspace-3":
            raise ValueError("navigation requires v42-workspace-3")
        self.get(ReviewScope, scope_id)
        if status not in {None, "open", "resolved", "retracted"}:
            raise ValueError("invalid navigation status")
        if cursor is not None and not isinstance(cursor, str):
            raise ValueError("cursor must be a string")
        if limit is not None and (type(limit) is not int or limit <= 0):
            raise ValueError("page size must be a positive integer")
        items = [
            item
            for item in self.list(NavigationItem)
            if item.scope_id == scope_id
            and (status is None or item.status == status)
            and (cursor is None or item.id > cursor)
        ]
        if limit is None or len(items) <= limit:
            return {"items": items, "next_cursor": None}
        return {"items": items[:limit], "next_cursor": items[limit - 1].id}

    def list_evidence_page(self, *, cursor: str | None = None, limit: int | None = None) -> dict[str, Any]:
        """Page a complete evidence directory without changing source storage."""
        if cursor is not None and not isinstance(cursor, str):
            raise ValueError("cursor must be a string")
        if limit is not None and (type(limit) is not int or limit <= 0):
            raise ValueError("page size must be a positive integer")
        items = [item for item in self.list(EvidenceRef) if cursor is None or item.id > cursor]
        if limit is None or len(items) <= limit:
            return {"items": items, "next_cursor": None}
        return {"items": items[:limit], "next_cursor": items[limit - 1].id}
