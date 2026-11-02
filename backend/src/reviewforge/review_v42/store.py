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
    FrontierEntry,
    IntegrationObligation,
    IntegrationReview,
    LeaseError,
    NavigationItem,
    Question,
    ReviewScope,
    RevisionConflict,
    Run,
    Task,
    UnitPlan,
)

T = TypeVar("T", ReviewScope, Question, Case, Task, EvidenceRef, NavigationItem)
_TYPES = {c.__name__: c for c in (ReviewScope, Question, Case, Task, EvidenceRef, NavigationItem)}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _unit_source_identity(ref: EvidenceRef) -> tuple:
    return (
        ref.revision,
        ref.side,
        ref.kind,
        ref.path,
        ref.start_line,
        ref.end_line,
        ref.content_hash,
        ref.environment,
        ref.metadata.get("source_url") if ref.side == "external" else None,
    )


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
            if run.policy in {"v42-stateful-2", "v42-workspace-3", "v42-frontier-4", "v42-frontier-5", "v42-units-6"}:
                self.audit_dir.mkdir(parents=True, exist_ok=True)
                db.execute(
                    "CREATE TABLE IF NOT EXISTS turn_audit (seq INTEGER PRIMARY KEY AUTOINCREMENT, "
                    "task_id TEXT NOT NULL, turn_id TEXT NOT NULL, phase TEXT NOT NULL, "
                    "content_hash TEXT NOT NULL, UNIQUE(task_id,turn_id,phase))"
                )
            if run.policy in {"v42-workspace-3", "v42-frontier-4", "v42-frontier-5", "v42-units-6"}:
                db.execute(
                    "CREATE TABLE IF NOT EXISTS navigation_aliases ("
                    "scope_id TEXT NOT NULL, fingerprint TEXT NOT NULL, item_id TEXT NOT NULL, "
                    "PRIMARY KEY(scope_id,fingerprint))"
                )

            if run.policy in {"v42-frontier-4", "v42-frontier-5", "v42-units-6"}:
                db.execute(
                    "CREATE TABLE IF NOT EXISTS frontier_entries ("
                    "seq INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, "
                    "entry_id TEXT NOT NULL UNIQUE, payload TEXT NOT NULL)"
                )
                db.execute(
                    "CREATE TABLE IF NOT EXISTS frontier_generations ("
                    "task_id TEXT PRIMARY KEY, revision INTEGER NOT NULL)"
                )
            if run.policy == "v42-units-6":
                db.executescript("""
                    CREATE TABLE IF NOT EXISTS unit_plan (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS integration_review (
                        id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS integration_obligations (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS unit_decisions (
                        decision_id TEXT PRIMARY KEY, scope_id TEXT NOT NULL,
                        request TEXT NOT NULL, result TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS unit_epochs (scope_id TEXT PRIMARY KEY, revision INTEGER NOT NULL);
                    CREATE TABLE IF NOT EXISTS unit_turn_owners (
                        task_id TEXT NOT NULL, turn_id TEXT NOT NULL, lease_hash TEXT NOT NULL,
                        PRIMARY KEY(task_id,turn_id));
                    CREATE TABLE IF NOT EXISTS unit_shared_snapshots (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS unit_pending_sources (
                        scope_id TEXT NOT NULL, region_id TEXT NOT NULL, ref_id TEXT NOT NULL,
                        PRIMARY KEY(scope_id,region_id,ref_id));
                    CREATE TABLE IF NOT EXISTS unit_pending_cases (
                        case_id TEXT PRIMARY KEY, scope_id TEXT NOT NULL, obligation_id TEXT NOT NULL);
                """)

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

    def get_unit_plan(self) -> UnitPlan | None:
        if self.run.policy != "v42-units-6":
            return None
        with self._connect() as db:
            row = db.execute("SELECT payload FROM unit_plan WHERE id=1").fetchone()
        return UnitPlan(**json.loads(row[0])) if row else None

    def pending_unit_sources(self, *, db: sqlite3.Connection | None = None) -> list[dict[str, str]]:
        if self.run.policy != "v42-units-6":
            return []
        if db is None:
            with self._connect() as connection:
                return self.pending_unit_sources(db=connection)
        return [
            {"scope_id": scope, "region_id": region, "ref_id": ref}
            for scope, region, ref in db.execute(
                "SELECT scope_id,region_id,ref_id FROM unit_pending_sources ORDER BY scope_id,region_id,ref_id"
            )
        ]

    def pending_unit_cases(self, *, db: sqlite3.Connection | None = None) -> list[dict[str, str]]:
        if self.run.policy != "v42-units-6":
            return []
        if db is None:
            with self._connect() as connection:
                return self.pending_unit_cases(db=connection)
        return [
            {"case_id": case, "scope_id": scope, "obligation_id": obligation}
            for case, scope, obligation in db.execute(
                "SELECT case_id,scope_id,obligation_id FROM unit_pending_cases ORDER BY case_id"
            )
        ]

    def install_unit_plan(self, primary_scopes: list[ReviewScope], integration_scope: ReviewScope) -> UnitPlan:
        if self.run.policy != "v42-units-6":
            raise ValueError("unit plan requires v42-units-6")
        manifest = self.run.metadata.get("changed_regions")
        head_digest = self.run.metadata.get("workspace_digest")
        base_digest = self.run.metadata.get("base_workspace_digest")
        if not isinstance(manifest, list) or not manifest or not head_digest or not base_digest:
            raise BindingError("unit plan requires fixed nonempty manifest and both workspace identities")
        if any(not isinstance(r, dict) or not isinstance(r.get("id"), str) or not r["id"] for r in manifest):
            raise BindingError("manifest region identity is invalid")
        regions = {r["id"]: r for r in manifest}
        if len(regions) != len(manifest):
            raise BindingError("manifest region IDs must be unique")
        if not primary_scopes or any(not isinstance(s, ReviewScope) for s in primary_scopes):
            raise ValueError("at least one primary unit is required")
        ids = sorted(s.id for s in primary_scopes)
        if len(ids) != len(set(ids)) or integration_scope.id in ids:
            raise ValueError("unit IDs must be unique")
        if (
            integration_scope.run_id != self.run.id
            or integration_scope.regions
            or sorted(integration_scope.dependencies) != ids
            or integration_scope.status != "pending"
            or integration_scope.revision != 0
            or integration_scope.checked_regions
            or integration_scope.case_ids
            or integration_scope.unresolved
            or integration_scope.working_state
        ):
            raise ValueError("integration unit must depend on every primary and have no regions")
        seen = set()
        seen_regions: list[tuple[str, dict[str, Any]]] = []
        transactions: dict[str, str] = {}
        for scope in primary_scopes:
            if (
                scope.run_id != self.run.id
                or not scope.id
                or not scope.regions
                or scope.status != "pending"
                or scope.revision != 0
                or scope.checked_regions
                or scope.case_ids
                or scope.unresolved
                or scope.working_state
            ):
                raise ValueError("primary unit must be new and own regions")
            for region in scope.regions:
                rid = region.get("id") if isinstance(region, dict) else None
                if rid in seen or rid not in regions or regions[rid] != region:
                    raise BindingError("unit plan changes, repeats or invents a manifest region")
                seen.add(rid)
                transaction = region.get("transaction_id")
                if transaction is not None:
                    if not isinstance(transaction, str) or not transaction:
                        raise BindingError("invalid transaction identity")
                    if transaction in transactions and transactions[transaction] != scope.id:
                        raise BindingError("known transaction cannot be split across units")
                    transactions[transaction] = scope.id
                for other_scope, other in seen_regions:
                    if other_scope == scope.id or other.get("path") != region.get("path"):
                        continue
                    for first_key, last_key in (("start_line", "end_line"), ("base_start_line", "base_end_line")):
                        a, b, c, d = (
                            region.get(first_key),
                            region.get(last_key),
                            other.get(first_key),
                            other.get(last_key),
                        )
                        if any(type(v) is not int or v < 1 for v in (a, b, c, d)) or max(a, c) <= min(b, d):
                            raise BindingError("overlapping or unbounded regions cannot be split across units")
                seen_regions.append((scope.id, region))
        if seen != set(regions):
            raise BindingError("unit plan does not cover the fixed manifest")
        plan = UnitPlan(
            self.run.id,
            hashlib.sha256(_json(manifest).encode()).hexdigest(),
            head_digest,
            base_digest,
            ids,
            integration_scope.id,
        )
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT payload FROM unit_plan WHERE id=1").fetchone()
            if existing:
                if existing[0] != _json(asdict(plan)):
                    raise RevisionConflict("different unit plan already installed")
                # The supplied ownership must also match the original installed plan.
                for scope in primary_scopes:
                    row = db.execute(
                        "SELECT payload FROM objects WHERE kind='ReviewScope' AND id=?", (scope.id,)
                    ).fetchone()
                    if row is None or ReviewScope(**json.loads(row[0])).regions != scope.regions:
                        raise RevisionConflict("different unit ownership already installed")
                return plan
            if db.execute("SELECT 1 FROM objects WHERE kind='ReviewScope' LIMIT 1").fetchone():
                raise RevisionConflict("unit plan cannot be installed over pre-existing scopes")
            for scope in [*primary_scopes, integration_scope]:
                if db.execute("SELECT 1 FROM objects WHERE kind='ReviewScope' AND id=?", (scope.id,)).fetchone():
                    raise RevisionConflict("unit scope already exists")
                db.execute("INSERT INTO objects VALUES ('ReviewScope',?,0,?)", (scope.id, _json(asdict(scope))))
                self._event(db, "ReviewScope", scope.id, "add", asdict(scope))
            db.execute("INSERT INTO unit_plan VALUES (1,?)", (_json(asdict(plan)),))
            review = IntegrationReview(
                "integration_" + hashlib.sha256(self.run.id.encode()).hexdigest(), self.run.id, integration_scope.id
            )
            db.execute("INSERT INTO integration_review VALUES (1,?)", (_json(asdict(review)),))
            obligations = [("unit", [sid], "Integrate primary unit " + sid) for sid in ids]
            obligations.append(("cross_unit", ids, "Examine cross-unit behavior and unresolved interactions"))
            for kind, unit_ids, description in obligations:
                oid = "ob_" + hashlib.sha256(_json([self.run.id, kind, unit_ids]).encode()).hexdigest()
                obligation = IntegrationObligation(oid, self.run.id, integration_scope.id, kind, unit_ids, description)
                db.execute("INSERT INTO integration_obligations VALUES (?,?)", (oid, _json(asdict(obligation))))
            for sid in ids:
                db.execute("INSERT INTO unit_epochs VALUES (?,0)", (sid,))
            self._event(db, "UnitPlan", self.run.id, "install", asdict(plan))
            db.commit()
        return plan

    @staticmethod
    def _unit_plan_db(db: sqlite3.Connection) -> UnitPlan:
        row = db.execute("SELECT payload FROM unit_plan WHERE id=1").fetchone()
        if row is None:
            raise BindingError("unit plan is not installed")
        return UnitPlan(**json.loads(row[0]))

    def query_shared(
        self,
        *,
        scope_id: str | None = None,
        path: str | None = None,
        kind: str | None = None,
        cursor: str | None = None,
        limit: int,
    ) -> dict[str, Any]:
        if type(limit) is not int or limit < 1:
            raise ValueError("page limit must be positive")
        filters = {"scope_id": scope_id, "path": path, "kind": kind}
        if any(v is not None and not isinstance(v, str) for v in filters.values()):
            raise ValueError("shared filters must be strings")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            plan = self._unit_plan_db(db)
            if scope_id is not None and scope_id not in {*plan.primary_scope_ids, plan.integration_scope_id}:
                raise BindingError("unknown unit")
            if cursor is not None:
                if not isinstance(cursor, str) or ":" not in cursor:
                    raise ValueError("invalid shared cursor")
                snapshot_id, number = cursor.rsplit(":", 1)
                if not number.isdecimal():
                    raise ValueError("invalid shared cursor")
                offset = int(number)
                row = db.execute("SELECT payload FROM unit_shared_snapshots WHERE id=?", (snapshot_id,)).fetchone()
                if row is None:
                    raise ValueError("unknown shared cursor")
                snapshot = json.loads(row[0])
                if snapshot["filters"] != filters or offset > len(snapshot["items"]):
                    raise ValueError("cursor does not match query")
            else:
                offset = 0
                origins: dict[str, set[str]] = {}
                tasks = {
                    task.id: task.object_id
                    for task in (
                        Task(**json.loads(r[0])) for r in db.execute("SELECT payload FROM objects WHERE kind='Task'")
                    )
                    if task.kind == "review"
                }
                for task_id, payload in db.execute("SELECT task_id,payload FROM frontier_entries"):
                    unit_id = tasks.get(task_id)
                    if unit_id:
                        for ref_id in FrontierEntry(**json.loads(payload)).aliases:
                            origins.setdefault(ref_id, set()).add(unit_id)
                refs = [
                    EvidenceRef(**json.loads(r[0]))
                    for r in db.execute("SELECT payload FROM objects WHERE kind='EvidenceRef' ORDER BY id")
                ]
                ref_paths = {ref.id: ref.path for ref in refs}
                items = []
                for ref in refs:
                    source_scopes = sorted(origins.get(ref.id, set()))
                    if scope_id is not None and scope_id not in source_scopes:
                        continue
                    if path is not None and ref.path != path:
                        continue
                    if kind is not None and kind not in {"evidence", ref.kind}:
                        continue
                    items.append(
                        {
                            "type": "evidence",
                            "id": ref.id,
                            "run_id": ref.run_id,
                            "repo": ref.repo,
                            "revision": ref.revision,
                            "side": ref.side,
                            "kind": ref.kind,
                            "path": ref.path,
                            "start_line": ref.start_line,
                            "end_line": ref.end_line,
                            "origin_scopes": source_scopes,
                        }
                    )
                for (payload,) in db.execute("SELECT payload FROM objects WHERE kind='NavigationItem' ORDER BY id"):
                    item = NavigationItem(**json.loads(payload))
                    if scope_id is not None and item.scope_id != scope_id:
                        continue
                    if path is not None and not any(ref_paths.get(ref) == path for ref in item.evidence_refs):
                        continue
                    if kind is not None and kind not in {"navigation", item.kind}:
                        continue
                    items.append({"type": "navigation", **asdict(item), "trust": "unverified_navigation"})
                snapshot = {"filters": filters, "items": items}
                body = _json(snapshot)
                snapshot_id = hashlib.sha256(body.encode()).hexdigest()
                db.execute("INSERT OR IGNORE INTO unit_shared_snapshots VALUES (?,?)", (snapshot_id, body))
                db.commit()
            end = min(offset + limit, len(snapshot["items"]))
            return {
                "items": snapshot["items"][offset:end],
                "snapshot_id": snapshot_id,
                "next_cursor": f"{snapshot_id}:{end}" if end < len(snapshot["items"]) else None,
            }

    def _obligation_view(self, db: sqlite3.Connection, obligation: IntegrationObligation) -> dict[str, Any]:
        plan = self._unit_plan_db(db)
        epochs = []
        for scope_id in obligation.unit_ids:
            row = db.execute("SELECT revision FROM unit_epochs WHERE scope_id=?", (scope_id,)).fetchone()
            if row is None:
                raise BindingError("obligation references unknown primary unit")
            epochs.append([scope_id, row[0]])
        digest = hashlib.sha256(
            _json(
                [
                    asdict(plan),
                    obligation.id,
                    epochs,
                    sorted(obligation.origin_evidence_refs),
                    sorted(obligation.evidence_refs),
                ]
            ).encode()
        ).hexdigest()
        valid = bool(obligation.receipt_digest == digest and obligation.disposition == "checked" and obligation.reason)
        if obligation.kind == "discovered" and not obligation.origin_evidence_refs:
            valid = False
        if valid:
            try:
                self._require_delivered(db, obligation.task_id, None, obligation.turn_id, set(obligation.evidence_refs))
                if obligation.kind == "discovered":
                    self._require_delivered(
                        db,
                        obligation.origin_task_id,
                        None,
                        obligation.origin_turn_id,
                        set(obligation.origin_evidence_refs),
                    )
            except (BindingError, LeaseError, KeyError, OSError, ValueError, TypeError):
                valid = False
        return {**asdict(obligation), "input_digest": digest, "status": "checked" if valid else "open", "valid": valid}

    def list_integration_obligations(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            self._unit_plan_db(db)
            rows = db.execute("SELECT payload FROM integration_obligations ORDER BY id").fetchall()
            return [self._obligation_view(db, IntegrationObligation(**json.loads(row[0]))) for row in rows]

    def _unit_refs(self, db: sqlite3.Connection, ids: list[str]) -> list[EvidenceRef]:
        if not isinstance(ids, list) or any(not isinstance(r, str) or not r for r in ids):
            raise ValueError("evidence references must be IDs")
        refs = []
        for ref_id in ids:
            row = db.execute("SELECT payload FROM objects WHERE kind='EvidenceRef' AND id=?", (ref_id,)).fetchone()
            if row is None:
                raise BindingError("unknown evidence reference")
            ref = EvidenceRef(**json.loads(row[0]))
            if ref.run_id != self.run.id or ref.repo != self.run.repo or ref.side not in {"head", "base", "external"}:
                raise BindingError("evidence belongs to another run")
            if ref.side != "external" and ref.revision != getattr(self.run, ref.side):
                raise BindingError("evidence belongs to another snapshot")
            if ref.side != "external" and ref.metadata.get("workspace_digest") != self.run.metadata.get(
                "workspace_digest"
            ):
                raise BindingError("evidence belongs to another workspace")
            raw = (self.blob_dir / ref.content_hash).read_bytes()
            if hashlib.sha256(raw).hexdigest() != ref.content_hash:
                raise BindingError("evidence blob hash mismatch")
            refs.append(ref)
        return refs

    @staticmethod
    def _task_frontier_refs(db: sqlite3.Connection, task_id: str) -> set[str]:
        refs = set()
        for (payload,) in db.execute("SELECT payload FROM frontier_entries WHERE task_id=?", (task_id,)):
            refs.update(FrontierEntry(**json.loads(payload)).aliases)
        return refs

    def _require_delivered(
        self,
        db: sqlite3.Connection,
        task_id: str,
        token: str | None,
        turn_id: str,
        ref_ids: set[str],
        *,
        required_ref_ids: set[str] | None = None,
    ) -> dict[str, list[tuple[int, int]]]:
        if not isinstance(turn_id, str) or not turn_id:
            raise BindingError("decision requires the current model turn")
        owner = db.execute(
            "SELECT lease_hash FROM unit_turn_owners WHERE task_id=? AND turn_id=?", (task_id, turn_id)
        ).fetchone()
        if owner is None or (token is not None and owner[0] != hashlib.sha256(token.encode()).hexdigest()):
            raise LeaseError("model turn belongs to another lease")
        rows = dict(
            db.execute(
                "SELECT phase,content_hash FROM turn_audit WHERE task_id=? AND turn_id=? "
                "AND phase IN ('request','response')",
                (task_id, turn_id),
            ).fetchall()
        )
        if set(rows) != {"request", "response"}:
            raise BindingError("decision requires an audited request and successful response")
        payloads = {}
        for phase, digest in rows.items():
            raw = (self.audit_dir / digest).read_bytes()
            if hashlib.sha256(raw).hexdigest() != digest:
                raise BindingError("turn audit blob hash mismatch")
            payloads[phase] = json.loads(raw)
        if not any(
            m.get("type") in {"ai", "AIMessage", "ai_chunk", "AIMessageChunk"}
            for m in payloads["response"]
            if isinstance(m, dict)
        ):
            raise BindingError("turn response is not a visible assistant message")
        delivered: dict[str, list[tuple[int, int]]] = {}

        def record(ref: EvidenceRef, content: bytes, start: int | None, end: int | None) -> None:
            first = start if start is not None else (ref.start_line or 1)
            last = end if end is not None else first + len(content.decode("utf-8").splitlines()) - 1
            delivered.setdefault(ref.id, []).append((first, last))

        for message in payloads["request"]:
            if not isinstance(message, dict) or message.get("type") not in {"human", "tool"}:
                continue
            data = message.get("data")
            content = data.get("content") if isinstance(data, dict) else None
            if not isinstance(content, str):
                continue
            try:
                obj = json.loads(content)
            except (ValueError, TypeError):
                continue
            if not isinstance(obj, dict):
                continue
            if message["type"] == "tool" and isinstance(obj.get("evidence_ref"), str):
                ref_id = obj["evidence_ref"]
                if ref_id not in ref_ids or not isinstance(obj.get("content"), str):
                    continue
                ref = self._unit_refs(db, [ref_id])[0]
                raw = (self.blob_dir / ref.content_hash).read_bytes()
                start, end = obj.get("start_line"), obj.get("end_line")
                try:
                    actual = self._frontier_content(ref, raw, start, end).decode("utf-8")
                except (ValueError, UnicodeError):
                    continue
                if obj["content"] == actual:
                    record(ref, actual.encode("utf-8"), start, end)
            elif message["type"] == "human" and "frontier_revision" in obj and isinstance(obj.get("entries"), list):
                for shown in obj["entries"]:
                    if not isinstance(shown, dict) or not isinstance(shown.get("id"), str):
                        continue
                    row = db.execute(
                        "SELECT payload FROM frontier_entries WHERE task_id=? AND entry_id=?", (task_id, shown["id"])
                    ).fetchone()
                    if row is None:
                        continue
                    entry = FrontierEntry(**json.loads(row[0]))
                    if (
                        not isinstance(shown.get("aliases"), list)
                        or not set(shown["aliases"]) <= set(entry.aliases)
                        or not isinstance(shown.get("content"), str)
                    ):
                        continue
                    for ref_id in ref_ids.intersection(shown["aliases"]):
                        ref = self._unit_refs(db, [ref_id])[0]
                        raw = (self.blob_dir / ref.content_hash).read_bytes()
                        try:
                            actual = self._frontier_content(
                                ref,
                                raw,
                                entry.start_line if entry.is_page else None,
                                entry.end_line if entry.is_page else None,
                            )
                            if (
                                hashlib.sha256(actual).hexdigest() == entry.content_hash
                                and actual.decode("utf-8") == shown["content"]
                            ):
                                record(
                                    ref,
                                    actual,
                                    entry.start_line if entry.is_page else None,
                                    entry.end_line if entry.is_page else None,
                                )
                        except (ValueError, UnicodeError):
                            continue
        required = ref_ids if required_ref_ids is None else required_ref_ids
        if not required <= delivered.keys():
            raise BindingError("source body was not delivered in this successful model turn")
        return delivered

    def _reopen_integration(self, db: sqlite3.Connection, plan: UnitPlan) -> None:
        row = db.execute(
            "SELECT payload FROM objects WHERE kind='ReviewScope' AND id=?", (plan.integration_scope_id,)
        ).fetchone()
        scope = ReviewScope(**json.loads(row[0]))
        if scope.status == "reviewed":
            scope = replace(scope, status="pending", revision=scope.revision + 1)
            db.execute(
                "UPDATE objects SET revision=?,payload=? WHERE kind='ReviewScope' AND id=?",
                (scope.revision, _json(asdict(scope)), scope.id),
            )
            self._event(db, "ReviewScope", scope.id, "integration_reopened", {"reason": "integration inputs changed"})

    def commit_unit_decision(
        self,
        task_id: str,
        token: str,
        expected_scope_revision: int,
        decision_id: str,
        *,
        checked_region_ids: list[str],
        observations: list[dict],
        questions: list[Question],
        cases: list[Case],
        unresolved: list[str],
        turn_id: str,
        finish: bool = False,
        now: float | None = None,
    ) -> dict[str, Any]:
        if not isinstance(decision_id, str) or not decision_id.strip() or type(finish) is not bool:
            raise ValueError("decision requires an ID and boolean finish")
        if any(not isinstance(x, list) for x in (checked_region_ids, observations, questions, cases, unresolved)):
            raise ValueError("decision fields must be lists")
        if any(not isinstance(x, str) or not x for x in checked_region_ids + unresolved):
            raise ValueError("region and unresolved IDs must be nonempty strings")
        if len(set(checked_region_ids)) != len(checked_region_ids):
            raise ValueError("duplicate region IDs")
        if any(not isinstance(q, Question) for q in questions) or any(not isinstance(c, Case) for c in cases):
            raise ValueError("questions and cases must use their contracts")
        request = {
            "checked_region_ids": checked_region_ids,
            "observations": observations,
            "questions": [asdict(x) for x in questions],
            "cases": [asdict(x) for x in cases],
            "unresolved": unresolved,
            "finish": finish,
        }
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            plan = self._unit_plan_db(db)
            row = db.execute("SELECT payload FROM objects WHERE kind='Task' AND id=?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(task_id)
            task = Task(**json.loads(row[0]))
            integration = task.object_id == plan.integration_scope_id
            if task.object_id not in plan.primary_scope_ids and not integration:
                raise BindingError("decision task does not belong to the unit plan")
            if integration and (checked_region_ids or observations or finish):
                raise BindingError("integration decisions may submit only cases or questions")
            self._guard_review_task(db, task_id, token, task.object_id, now)
            old_decision = db.execute(
                "SELECT scope_id,request,result FROM unit_decisions WHERE decision_id=?", (decision_id,)
            ).fetchone()
            if old_decision:
                if old_decision[0] != task.object_id or old_decision[1] != _json(request):
                    raise RevisionConflict("decision ID already used for different content")
                result = json.loads(old_decision[2])
                result["scope"] = ReviewScope(**result["scope"])
                return result
            row = db.execute(
                "SELECT payload FROM objects WHERE kind='ReviewScope' AND id=?", (task.object_id,)
            ).fetchone()
            scope = ReviewScope(**json.loads(row[0]))
            if scope.revision != expected_scope_revision:
                raise RevisionConflict("unit scope revision is stale")
            by_id = {r["id"]: r for r in scope.regions}
            if not set(checked_region_ids) <= set(by_id):
                raise BindingError("region is not owned by this unit")
            historical_observations = []
            for (payload,) in db.execute("SELECT request FROM unit_decisions WHERE scope_id=?", (scope.id,)):
                historical_observations.extend(json.loads(payload)["observations"])
            task_refs = self._task_frontier_refs(db, task_id)
            required_refs = {
                ref
                for observation in observations
                if isinstance(observation, dict)
                for ref in observation.get("evidence_refs", [])
                if isinstance(ref, str)
            }
            for obj in [*questions, *cases]:
                required_refs.update(obj.evidence_refs)
                required_refs.update(getattr(obj, "counter_evidence_refs", []))
                if isinstance(obj, Question):
                    required_refs.update(ref for item in obj.interpretations for ref in item.get("evidence_refs", []))
            for observation in observations:
                if (
                    not isinstance(observation, dict)
                    or set(observation) != {"statement", "evidence_refs", "region_ids"}
                    or not isinstance(observation["statement"], str)
                    or not observation["statement"].strip()
                    or not isinstance(observation["region_ids"], list)
                    or not observation["region_ids"]
                    or any(not isinstance(r, str) or r not in by_id for r in observation["region_ids"])
                    or not observation["evidence_refs"]
                ):
                    raise ValueError("observations require a statement, source references and owned region IDs")
                refs = self._unit_refs(db, observation["evidence_refs"])
                if not set(observation["evidence_refs"]) <= task_refs:
                    raise BindingError("unit observations require sources explicitly read by this task")
                if not refs:
                    raise BindingError("unit observations require sources")
            for rid in checked_region_ids:
                if by_id[rid].get("unsupported"):
                    raise BindingError("checked region requires available original source")
            all_observations = [*historical_observations, *observations]
            candidates = set(required_refs)
            pending_refs = set()
            for region_id in checked_region_ids:
                pending_refs.update(
                    row[0]
                    for row in db.execute(
                        "SELECT ref_id FROM unit_pending_sources WHERE scope_id=? AND region_id=?",
                        (scope.id, region_id),
                    )
                )
            if not pending_refs <= task_refs:
                raise BindingError("reopened regions require explicitly rereading their new sources")
            required_refs.update(pending_refs)
            candidates.update(pending_refs)
            for observation in historical_observations:
                if set(observation["region_ids"]) & set(checked_region_ids):
                    candidates.update(set(observation["evidence_refs"]) & task_refs)
            delivered = self._require_delivered(db, task_id, token, turn_id, candidates, required_ref_ids=required_refs)
            for ref in self._unit_refs(db, list(pending_refs)):
                raw = (self.blob_dir / ref.content_hash).read_bytes()
                try:
                    lines = raw.decode("utf-8").splitlines()
                except UnicodeDecodeError as exc:
                    raise BindingError("new source is not readable UTF-8") from exc
                first = ref.start_line or 1
                last = first + len(lines) - 1
                next_line = first
                for start, end in sorted(delivered.get(ref.id, [])):
                    if start <= next_line <= end:
                        next_line = end + 1
                if next_line <= last:
                    raise BindingError("new source was not fully delivered for reopened regions")
            delivered_refs = set(delivered)
            for rid in checked_region_ids:
                region = by_id[rid]
                coverage: dict[str, list[tuple[int, int]]] = {"head": [], "base": []}
                for observation in all_observations:
                    if rid not in observation["region_ids"]:
                        continue
                    eligible = [ref for ref in observation["evidence_refs"] if ref in delivered]
                    for ref in self._unit_refs(db, eligible):
                        if (
                            ref.kind == "source"
                            and ref.path == region["path"]
                            and ref.side in coverage
                            and ref.metadata.get("navigation_only") is not True
                            and ref.metadata.get("status", "available") in {"available", "success", "ok"}
                            and ref.metadata.get("complete", True) is not False
                        ):
                            coverage[ref.side].extend(delivered.get(ref.id, []))
                side_ok = False
                for side, spans in coverage.items():
                    if (
                        side == "base"
                        and region.get("change") != "deleted"
                        and region.get("side") != "base"
                        and region.get("changed_lines")
                    ):
                        continue
                    if side == "head" and (region.get("change") == "deleted" or region.get("side") == "base"):
                        continue
                    first = region.get("base_start_line" if side == "base" else "start_line")
                    last = region.get("base_end_line" if side == "base" else "end_line")
                    if type(first) is not int or type(last) is not int or first < 1 or last < first:
                        continue
                    next_line = first
                    for start, end in sorted(spans):
                        if start <= next_line <= end:
                            next_line = end + 1
                    if next_line > last:
                        side_ok = True
                        break
                if not side_ok:
                    raise BindingError("delivered source pages do not cover the checked region")
            for obj in [*questions, *cases]:
                if obj.run_id != self.run.id or obj.scope_id != scope.id or obj.revision != 0:
                    raise BindingError("decision object belongs to another unit or is not new")
                if isinstance(obj, Question) and (
                    obj.status not in {"open", "answered", "disputed", "unresolved"}
                    or (obj.status == "answered" and (not obj.answer.strip() or not obj.evidence_refs))
                ):
                    raise ValueError("invalid question decision")
                self._validate_refs(db, obj)
                kind = type(obj).__name__
                payload = _json(asdict(obj))
                old = db.execute("SELECT payload FROM objects WHERE kind=? AND id=?", (kind, obj.id)).fetchone()
                if old and old[0] != payload:
                    raise RevisionConflict("decision object identity conflict")
                if not old:
                    db.execute("INSERT INTO objects VALUES (?,?,0,?)", (kind, obj.id, payload))
                    self._event(db, kind, obj.id, "add", asdict(obj))
                    if isinstance(obj, Question):
                        self._invalidate_question_cases(db, obj.id, obj.case_id)
            checked = {r["id"] for r in scope.checked_regions} | set(checked_region_ids)
            case_ids = list(dict.fromkeys(scope.case_ids + [c.id for c in cases]))
            open_questions = [
                Question(**json.loads(row[0]))
                for row in db.execute("SELECT payload FROM objects WHERE kind='Question'")
            ]
            open_navigation = [
                NavigationItem(**json.loads(row[0]))
                for row in db.execute("SELECT payload FROM objects WHERE kind='NavigationItem'")
            ]
            question_map = {q.id: q for q in open_questions if q.scope_id == scope.id}
            navigation_map = {n.id: n for n in open_navigation if n.scope_id == scope.id and n.kind == "question"}
            current_unresolved = []
            for unresolved_id in dict.fromkeys(scope.unresolved + unresolved):
                question = question_map.get(unresolved_id)
                navigation = navigation_map.get(unresolved_id)
                if question is None and navigation is None:
                    raise BindingError("unresolved reference must identify a question in this unit")
                if (question and question.status in {"open", "disputed", "unresolved"}) or (
                    navigation and navigation.status == "open"
                ):
                    current_unresolved.append(unresolved_id)
            blocked = (
                current_unresolved
                or any(
                    q.scope_id == scope.id and q.status in {"open", "disputed", "unresolved"} for q in open_questions
                )
                or any(n.scope_id == scope.id and n.kind == "question" and n.status == "open" for n in open_navigation)
            )
            status = (
                "reviewed"
                if finish and checked == set(by_id) and not blocked
                else ("partial" if finish else scope.status)
            )
            updated = replace(
                scope,
                checked_regions=[r for r in scope.regions if r["id"] in checked],
                case_ids=case_ids,
                unresolved=current_unresolved,
                status=status,
                revision=scope.revision + 1,
            )
            db.execute(
                "UPDATE objects SET revision=?,payload=? WHERE kind='ReviewScope' AND id=?",
                (updated.revision, _json(asdict(updated)), updated.id),
            )
            for region_id in checked_region_ids:
                db.execute("DELETE FROM unit_pending_sources WHERE scope_id=? AND region_id=?", (scope.id, region_id))
            if (
                status == "reviewed"
                and not db.execute(
                    "SELECT 1 FROM unit_pending_sources WHERE scope_id=? LIMIT 1", (scope.id,)
                ).fetchone()
            ):
                db.execute("DELETE FROM unit_pending_cases WHERE scope_id=? AND obligation_id=''", (scope.id,))
            task = replace(task, input_revision=updated.revision, revision=task.revision + 1)
            db.execute(
                "UPDATE objects SET revision=?,payload=? WHERE kind='Task' AND id=?",
                (task.revision, _json(asdict(task)), task.id),
            )
            substantive = (
                checked != {r["id"] for r in scope.checked_regions}
                or questions
                or cases
                or current_unresolved != scope.unresolved
                or status != scope.status
            )
            if substantive and not integration:
                db.execute("UPDATE unit_epochs SET revision=revision+1 WHERE scope_id=?", (scope.id,))
                self._reopen_integration(db, plan)
            result = {
                "scope": asdict(updated),
                "decision_id": decision_id,
                "observations": observations,
                "task_id": task_id,
                "turn_id": turn_id,
                "delivered_refs": sorted(delivered_refs),
                "delivered_ranges": {key: [list(span) for span in value] for key, value in delivered.items()},
                "question_ids": [q.id for q in questions],
                "case_ids": [c.id for c in cases],
            }
            db.execute(
                "INSERT INTO unit_decisions VALUES (?,?,?,?)", (decision_id, scope.id, _json(request), _json(result))
            )
            self._event(db, "UnitDecision", decision_id, "commit", result)
            db.commit()
            result["scope"] = updated
            return result

    def update_unit_object(
        self,
        task_id: str,
        token: str,
        cls: type,
        object_id: str,
        expected_revision: int,
        *,
        turn_id: str,
        now: float | None = None,
        **changes: Any,
    ) -> Any:
        if cls not in (Question, Case) or {"id", "run_id", "scope_id", "revision"} & changes.keys():
            raise ValueError("invalid unit object update")
        if changes.keys() - {f.name for f in fields(cls)}:
            raise ValueError("unknown unit object fields")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            plan = self._unit_plan_db(db)
            row = db.execute("SELECT payload FROM objects WHERE kind=? AND id=?", (cls.__name__, object_id)).fetchone()
            if row is None:
                raise KeyError(object_id)
            old = cls(**json.loads(row[0]))
            if old.scope_id not in {*plan.primary_scope_ids, plan.integration_scope_id}:
                raise BindingError("object is outside the unit plan")
            self._guard_review_task(db, task_id, token, old.scope_id, now)
            if old.revision != expected_revision:
                raise RevisionConflict("object revision changed")
            if cls is Case and old.trigger and "trigger" in changes and changes["trigger"] != old.trigger:
                raise ValueError("case trigger is immutable")
            if (
                cls is Case
                and set(changes) <= {"status"}
                and db.execute("SELECT 1 FROM verdicts WHERE case_id=? LIMIT 1", (object_id,)).fetchone()
            ):
                raise ValueError("status alone cannot reopen a verified case")
            updated = replace(old, **changes, revision=old.revision + 1)
            self._validate_refs(db, updated)
            refs = set(updated.evidence_refs) | set(getattr(updated, "counter_evidence_refs", []))
            if cls is Question:
                refs.update(ref for item in updated.interpretations for ref in item.get("evidence_refs", []))
                if (
                    not {item["id"] for item in old.interpretations} <= {item["id"] for item in updated.interpretations}
                    or updated.status not in {"open", "answered", "disputed", "unresolved"}
                    or (updated.status == "answered" and (not updated.answer.strip() or not updated.evidence_refs))
                ):
                    raise ValueError("invalid question update")
            self._require_delivered(db, task_id, token, turn_id, refs)
            if not refs <= self._task_frontier_refs(db, task_id):
                raise BindingError("referenced sources require an explicit unit read")
            genuinely_new = []
            if cls is Case:
                old_refs = set(old.evidence_refs + old.counter_evidence_refs)
                old_identities = {_unit_source_identity(ref) for ref in self._unit_refs(db, list(old_refs))}
                for ref in self._unit_refs(db, sorted(refs - old_refs)):
                    identity = _unit_source_identity(ref)
                    if identity not in old_identities:
                        genuinely_new.append(ref.id)
                        old_identities.add(identity)
            db.execute(
                "UPDATE objects SET revision=?,payload=? WHERE kind=? AND id=?",
                (updated.revision, _json(asdict(updated)), cls.__name__, object_id),
            )
            self._event(db, cls.__name__, object_id, "update", {"before": asdict(old), "after": asdict(updated)})
            if cls is Question:
                self._invalidate_question_cases(db, object_id, old.case_id, updated.case_id)
            if old.scope_id in plan.primary_scope_ids:
                db.execute("UPDATE unit_epochs SET revision=revision+1 WHERE scope_id=?", (old.scope_id,))
                self._reopen_integration(db, plan)
                if genuinely_new:
                    row = db.execute(
                        "SELECT payload FROM objects WHERE kind='ReviewScope' AND id=?", (old.scope_id,)
                    ).fetchone()
                    scope = ReviewScope(**json.loads(row[0]))
                    for region in scope.regions:
                        for ref_id in genuinely_new:
                            db.execute(
                                "INSERT OR IGNORE INTO unit_pending_sources VALUES (?,?,?)",
                                (scope.id, region["id"], ref_id),
                            )
                    scope = replace(scope, status="pending", checked_regions=[], revision=scope.revision + 1)
                    db.execute(
                        "UPDATE objects SET revision=?,payload=? WHERE kind='ReviewScope' AND id=?",
                        (scope.revision, _json(asdict(scope)), scope.id),
                    )
                    row = db.execute("SELECT payload FROM objects WHERE kind='Task' AND id=?", (task_id,)).fetchone()
                    task = Task(**json.loads(row[0]))
                    task = replace(task, input_revision=scope.revision, revision=task.revision + 1)
                    db.execute(
                        "UPDATE objects SET revision=?,payload=? WHERE kind='Task' AND id=?",
                        (task.revision, _json(asdict(task)), task.id),
                    )
                    db.execute(
                        "INSERT INTO unit_pending_cases VALUES (?,?,?) "
                        "ON CONFLICT(case_id) DO UPDATE SET scope_id=excluded.scope_id,"
                        "obligation_id=excluded.obligation_id",
                        (old.id, scope.id, ""),
                    )
            elif genuinely_new and old.scope_id == plan.integration_scope_id:
                affected_units = []
                source_paths = {ref.path for ref in self._unit_refs(db, genuinely_new) if ref.path}
                for sid in plan.primary_scope_ids:
                    row = db.execute("SELECT payload FROM objects WHERE kind='ReviewScope' AND id=?", (sid,)).fetchone()
                    unit = ReviewScope(**json.loads(row[0]))
                    if any(region.get("path") in source_paths for region in unit.regions):
                        affected_units.append(sid)
                affected_units = affected_units or list(plan.primary_scope_ids)
                oid = (
                    "ob_"
                    + hashlib.sha256(
                        _json([self.run.id, "case_followup", old.id, updated.revision]).encode()
                    ).hexdigest()
                )
                obligation = IntegrationObligation(
                    oid,
                    self.run.id,
                    plan.integration_scope_id,
                    "case_followup",
                    affected_units,
                    "Investigate new evidence for case " + old.id,
                )
                db.execute("INSERT INTO integration_obligations VALUES (?,?)", (oid, _json(asdict(obligation))))
                db.execute(
                    "INSERT INTO unit_pending_cases VALUES (?,?,?) "
                    "ON CONFLICT(case_id) DO UPDATE SET scope_id=excluded.scope_id,"
                    "obligation_id=excluded.obligation_id",
                    (old.id, plan.integration_scope_id, oid),
                )
                self._event(db, "IntegrationObligation", oid, "case_followup", asdict(obligation))
            db.commit()
            return updated

    def reopen_unit_for_evidence(
        self,
        scope_id: str,
        expected_revision: int,
        evidence_refs: list[str],
        reason: str,
        *,
        case_ids: list[str] | None = None,
        region_ids: list[str] | None = None,
    ) -> ReviewScope:
        """Controller links genuinely new fixed sources to affected work, without erasing history."""
        if not isinstance(reason, str) or not reason.strip() or not evidence_refs:
            raise ValueError("reopening requires new evidence and a reason")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            plan = self._unit_plan_db(db)
            if scope_id not in plan.primary_scope_ids:
                raise BindingError("only a primary unit can be reopened")
            row = db.execute("SELECT payload FROM objects WHERE kind='ReviewScope' AND id=?", (scope_id,)).fetchone()
            scope = ReviewScope(**json.loads(row[0]))
            if scope.revision != expected_revision:
                raise RevisionConflict("unit revision changed")
            refs = self._unit_refs(db, evidence_refs)
            if any(
                ref.kind != "source"
                or ref.side not in {"head", "base"}
                or ref.metadata.get("navigation_only") is True
                or ref.metadata.get("complete", True) is False
                or ref.metadata.get("status", "available") not in {"available", "success", "ok"}
                for ref in refs
            ):
                raise BindingError("reopening requires original fixed source")
            for ref in refs:
                try:
                    (self.blob_dir / ref.content_hash).read_bytes().decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise BindingError("reopening requires readable source") from exc
            previous = set()
            for (payload,) in db.execute("SELECT request FROM unit_decisions WHERE scope_id=?", (scope_id,)):
                for obs in json.loads(payload)["observations"]:
                    previous.update(obs["evidence_refs"])
            for (payload,) in db.execute("SELECT payload FROM objects WHERE kind='Case'"):
                case = Case(**json.loads(payload))
                if case.scope_id == scope_id:
                    previous.update(case.evidence_refs + case.counter_evidence_refs)
            previous_identities = set()
            for ref_id in previous:
                row = db.execute("SELECT payload FROM objects WHERE kind='EvidenceRef' AND id=?", (ref_id,)).fetchone()
                if row:
                    previous_identities.add(_unit_source_identity(EvidenceRef(**json.loads(row[0]))))
            if not any(_unit_source_identity(ref) not in previous_identities for ref in refs):
                raise BindingError("reopening requires new source identity")
            owned = {region["id"] for region in scope.regions}
            affected = (
                set(region_ids)
                if region_ids is not None
                else {region["id"] for region in scope.regions if region["path"] in {ref.path for ref in refs}}
            )
            if not affected or not affected <= owned:
                raise BindingError("reopening requires affected owned region IDs")
            for region_id in affected:
                for ref_id in evidence_refs:
                    db.execute(
                        "INSERT OR IGNORE INTO unit_pending_sources VALUES (?,?,?)", (scope_id, region_id, ref_id)
                    )
            for case_id in case_ids or []:
                row = db.execute("SELECT payload FROM objects WHERE kind='Case' AND id=?", (case_id,)).fetchone()
                if row is None:
                    raise KeyError(case_id)
                old = Case(**json.loads(row[0]))
                if old.scope_id != scope_id:
                    raise BindingError("case belongs to another unit")
                case = replace(
                    old, evidence_refs=list(dict.fromkeys(old.evidence_refs + evidence_refs)), revision=old.revision + 1
                )
                self._validate_refs(db, case)
                db.execute(
                    "UPDATE objects SET revision=?,payload=? WHERE kind='Case' AND id=?",
                    (case.revision, _json(asdict(case)), case.id),
                )
                self._event(db, "Case", case.id, "dependency_changed", {"reason": reason, "refs": evidence_refs})
                db.execute(
                    "INSERT INTO unit_pending_cases VALUES (?,?,?) "
                    "ON CONFLICT(case_id) DO UPDATE SET scope_id=excluded.scope_id,"
                    "obligation_id=excluded.obligation_id",
                    (case.id, scope_id, ""),
                )
            updated = replace(
                scope,
                status="pending",
                revision=scope.revision + 1,
                checked_regions=[r for r in scope.checked_regions if r["id"] not in affected],
            )
            db.execute(
                "UPDATE objects SET revision=?,payload=? WHERE kind='ReviewScope' AND id=?",
                (updated.revision, _json(asdict(updated)), scope_id),
            )
            db.execute("UPDATE unit_epochs SET revision=revision+1 WHERE scope_id=?", (scope_id,))
            self._reopen_integration(db, plan)
            self._event(
                db,
                "ReviewScope",
                scope_id,
                "reopen_for_evidence",
                {"reason": reason, "refs": evidence_refs, "region_ids": sorted(affected), "case_ids": case_ids or []},
            )
            db.commit()
            return updated

    def record_tool_failure_for_task(
        self,
        task_id: str,
        token: str,
        name: str,
        args: dict[str, Any],
        *,
        now: float | None = None,
    ) -> Question:
        if not isinstance(name, str) or not name or not isinstance(args, dict):
            raise ValueError("tool failure needs a name and arguments")
        statement = "Tool unavailable: " + name + " " + json.dumps(args, sort_keys=True, ensure_ascii=False)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            plan = self._unit_plan_db(db)
            row = db.execute("SELECT payload FROM objects WHERE kind='Task' AND id=?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(task_id)
            task = Task(**json.loads(row[0]))
            if task.object_id not in {*plan.primary_scope_ids, plan.integration_scope_id}:
                raise BindingError("tool task is not a unit")
            self._guard_review_task(db, task_id, token, task.object_id, now)
            qid = "q_tool_" + hashlib.sha256(_json([self.run.id, task.object_id, name, args]).encode()).hexdigest()
            row = db.execute("SELECT payload FROM objects WHERE kind='Question' AND id=?", (qid,)).fetchone()
            if row is None:
                question = Question(
                    qid, self.run.id, statement, scope_id=task.object_id, status="unresolved", owner="tool_controller"
                )
                db.execute("INSERT INTO objects VALUES ('Question',?,0,?)", (qid, _json(asdict(question))))
            else:
                old = Question(**json.loads(row[0]))
                if old.status == "unresolved":
                    return old
                question = replace(old, status="unresolved", answer="", revision=old.revision + 1)
                db.execute(
                    "UPDATE objects SET revision=?,payload=? WHERE kind='Question' AND id=?",
                    (question.revision, _json(asdict(question)), qid),
                )
            if task.object_id in plan.primary_scope_ids:
                db.execute("UPDATE unit_epochs SET revision=revision+1 WHERE scope_id=?", (task.object_id,))
                self._reopen_integration(db, plan)
            self._invalidate_question_cases(db, question.id, question.case_id)
            self._event(db, "Question", qid, "tool_unavailable", asdict(question))
            db.commit()
            return question

    def resolve_tool_failure_for_task(
        self,
        task_id: str,
        token: str,
        question_id: str,
        ref_id: str,
        *,
        name: str,
        args: dict[str, Any],
        now: float | None = None,
    ) -> Question:
        statement = "Tool unavailable: " + name + " " + json.dumps(args, sort_keys=True, ensure_ascii=False)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            plan = self._unit_plan_db(db)
            row = db.execute("SELECT payload FROM objects WHERE kind='Question' AND id=?", (question_id,)).fetchone()
            if row is None:
                raise KeyError(question_id)
            question = Question(**json.loads(row[0]))
            self._guard_review_task(db, task_id, token, question.scope_id, now)
            if question.owner != "tool_controller" or question.statement != statement:
                raise BindingError("tool retry does not match the original failure")
            ref = self._unit_refs(db, [ref_id])[0]
            if name == "read_evidence":
                matches = args.get("ref") == ref.id
            else:
                matches = ref.metadata.get("tool_name") == name and ref.metadata.get("tool_args") == args
            if (
                not matches
                or ref.metadata.get("status", "available") not in {"available", "success", "ok", "executed"}
                or ref.kind == "query"
            ):
                raise BindingError("retry did not produce matching available evidence")
            if question.status == "answered" and question.evidence_refs == [ref_id]:
                return question
            question = replace(
                question,
                status="answered",
                answer="Tool became available",
                evidence_refs=[ref_id],
                revision=question.revision + 1,
            )
            db.execute(
                "UPDATE objects SET revision=?,payload=? WHERE kind='Question' AND id=?",
                (question.revision, _json(asdict(question)), question.id),
            )
            if question.scope_id in plan.primary_scope_ids:
                db.execute("UPDATE unit_epochs SET revision=revision+1 WHERE scope_id=?", (question.scope_id,))
                self._reopen_integration(db, plan)
            self._invalidate_question_cases(db, question.id, question.case_id)
            self._event(db, "Question", question.id, "tool_available", asdict(question))
            db.commit()
            return question

    def add_integration_obligation(
        self,
        task_id: str,
        token: str,
        unit_ids: list[str],
        description: str,
        evidence_refs: list[str],
        *,
        turn_id: str,
        now: float | None = None,
    ) -> dict[str, Any]:
        if (
            not isinstance(description, str)
            or not description.strip()
            or not isinstance(unit_ids, list)
            or not unit_ids
        ):
            raise ValueError("obligation requires a description and units")
        if any(not isinstance(x, str) or not x for x in unit_ids) or len(set(unit_ids)) != len(unit_ids):
            raise ValueError("obligation units must be unique IDs")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            plan = self._unit_plan_db(db)
            self._guard_review_task(db, task_id, token, plan.integration_scope_id, now)
            if not set(unit_ids) <= set(plan.primary_scope_ids):
                raise BindingError("obligation references unknown primary")
            refs = self._unit_refs(db, evidence_refs)
            if not refs or any(ref.kind != "source" or ref.side not in {"base", "head"} for ref in refs):
                raise BindingError("a discovered relationship requires original source")
            if not set(evidence_refs) <= self._task_frontier_refs(db, task_id):
                raise BindingError("discovered relationship requires explicit unit reads")
            self._require_delivered(db, task_id, token, turn_id, set(evidence_refs))
            oid = (
                "ob_"
                + hashlib.sha256(
                    _json([self.run.id, sorted(unit_ids), description, sorted(evidence_refs)]).encode()
                ).hexdigest()
            )
            obligation = IntegrationObligation(
                oid,
                self.run.id,
                plan.integration_scope_id,
                "discovered",
                sorted(unit_ids),
                description,
                list(evidence_refs),
                origin_evidence_refs=list(evidence_refs),
                origin_task_id=task_id,
                origin_turn_id=turn_id,
            )
            row = db.execute("SELECT payload FROM integration_obligations WHERE id=?", (oid,)).fetchone()
            if row:
                return self._obligation_view(db, IntegrationObligation(**json.loads(row[0])))
            db.execute("INSERT INTO integration_obligations VALUES (?,?)", (oid, _json(asdict(obligation))))
            self._reopen_integration(db, plan)
            self._event(db, "IntegrationObligation", oid, "add", asdict(obligation))
            view = self._obligation_view(db, obligation)
            db.commit()
            return view

    def register_unit_dependency(
        self,
        task_id: str,
        token: str,
        target_scope_id: str,
        description: str,
        evidence_refs: list[str],
        *,
        turn_id: str,
        now: float | None = None,
    ) -> dict[str, Any]:
        if not isinstance(description, str) or not description.strip():
            raise ValueError("dependency requires a description")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            plan = self._unit_plan_db(db)
            row = db.execute("SELECT payload FROM objects WHERE kind='Task' AND id=?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(task_id)
            task = Task(**json.loads(row[0]))
            if task.object_id not in plan.primary_scope_ids or target_scope_id not in plan.primary_scope_ids:
                raise BindingError("dependency units must be primary units")
            if target_scope_id == task.object_id:
                raise ValueError("dependency requires two distinct units")
            self._guard_review_task(db, task_id, token, task.object_id, now)
            refs = self._unit_refs(db, evidence_refs)
            if not refs or any(ref.kind != "source" or ref.side not in {"base", "head"} for ref in refs):
                raise BindingError("a dependency requires original source")
            if not set(evidence_refs) <= self._task_frontier_refs(db, task_id):
                raise BindingError("dependency requires explicit unit reads")
            self._require_delivered(db, task_id, token, turn_id, set(evidence_refs))
            unit_ids = sorted([task.object_id, target_scope_id])
            oid = (
                "ob_"
                + hashlib.sha256(
                    _json([self.run.id, unit_ids, description, sorted(evidence_refs)]).encode()
                ).hexdigest()
            )
            row = db.execute("SELECT payload FROM integration_obligations WHERE id=?", (oid,)).fetchone()
            if row:
                return self._obligation_view(db, IntegrationObligation(**json.loads(row[0])))
            obligation = IntegrationObligation(
                oid,
                self.run.id,
                plan.integration_scope_id,
                "discovered",
                unit_ids,
                description,
                list(evidence_refs),
                origin_evidence_refs=list(evidence_refs),
                origin_task_id=task_id,
                origin_turn_id=turn_id,
            )
            db.execute("INSERT INTO integration_obligations VALUES (?,?)", (oid, _json(asdict(obligation))))
            self._reopen_integration(db, plan)
            self._event(db, "IntegrationObligation", oid, "discovered", asdict(obligation))
            view = self._obligation_view(db, obligation)
            db.commit()
            return view

    def record_integration_receipt(
        self,
        task_id: str,
        token: str,
        obligation_id: str,
        expected_revision: int,
        input_digest: str,
        *,
        disposition: str,
        evidence_refs: list[str],
        reason: str,
        turn_id: str,
        now: float | None = None,
    ) -> dict[str, Any]:
        if disposition not in {"checked", "unresolved"} or not isinstance(reason, str) or not reason.strip():
            raise ValueError("integration receipt requires a disposition and reason")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            plan = self._unit_plan_db(db)
            self._guard_review_task(db, task_id, token, plan.integration_scope_id, now)
            row = db.execute("SELECT payload FROM integration_obligations WHERE id=?", (obligation_id,)).fetchone()
            if row is None:
                raise KeyError(obligation_id)
            obligation = IntegrationObligation(**json.loads(row[0]))
            view = self._obligation_view(db, obligation)
            if obligation.revision != expected_revision or view["input_digest"] != input_digest:
                raise RevisionConflict("integration obligation inputs changed")
            refs = self._unit_refs(db, evidence_refs)
            self._require_delivered(db, task_id, token, turn_id, set(evidence_refs))
            if disposition == "checked":
                if not set(obligation.origin_evidence_refs) <= set(evidence_refs):
                    raise BindingError("checked relationship must include its original source references")
                if not set(evidence_refs) <= self._task_frontier_refs(db, task_id):
                    raise BindingError("integration requires sources explicitly read by this task")
                for sid in obligation.unit_ids:
                    row = db.execute("SELECT payload FROM objects WHERE kind='ReviewScope' AND id=?", (sid,)).fetchone()
                    scope = ReviewScope(**json.loads(row[0]))
                    paths = {region["path"] for region in scope.regions}
                    if not any(
                        ref.kind == "source"
                        and ref.path in paths
                        and ref.side in {"base", "head"}
                        and ref.metadata.get("navigation_only") is not True
                        and ref.metadata.get("complete", True) is not False
                        and ref.metadata.get("status", "available") in {"available", "success", "ok"}
                        for ref in refs
                    ):
                        raise BindingError("checked integration needs original source for each unit")
            obligation.evidence_refs = list(evidence_refs)
            obligation.disposition = disposition
            obligation.reason = reason
            obligation.task_id, obligation.turn_id = task_id, turn_id
            obligation.revision += 1
            obligation.receipt_digest = self._obligation_view(db, obligation)["input_digest"]
            db.execute(
                "UPDATE integration_obligations SET payload=? WHERE id=?", (_json(asdict(obligation)), obligation.id)
            )
            self._event(db, "IntegrationObligation", obligation.id, "receipt", asdict(obligation))
            view = self._obligation_view(db, obligation)
            db.commit()
            return view

    def finish_integration(
        self,
        task_id: str,
        token: str,
        expected_scope_revision: int,
        now: float | None = None,
    ) -> ReviewScope:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            plan = self._unit_plan_db(db)
            self._guard_review_task(db, task_id, token, plan.integration_scope_id, now)
            row = db.execute(
                "SELECT payload FROM objects WHERE kind='ReviewScope' AND id=?", (plan.integration_scope_id,)
            ).fetchone()
            scope = ReviewScope(**json.loads(row[0]))
            if scope.revision != expected_scope_revision:
                raise RevisionConflict("integration scope revision is stale")
            obligations = [
                self._obligation_view(db, IntegrationObligation(**json.loads(r[0])))
                for r in db.execute("SELECT payload FROM integration_obligations")
            ]
            if not obligations or any(not o["valid"] for o in obligations):
                raise BindingError("integration obligations are unresolved or stale")
            if (
                scope.unresolved
                or any(
                    Question(**json.loads(row[0])).scope_id == scope.id
                    and Question(**json.loads(row[0])).status in {"open", "disputed", "unresolved"}
                    for row in db.execute("SELECT payload FROM objects WHERE kind='Question'")
                )
                or any(
                    NavigationItem(**json.loads(row[0])).scope_id == scope.id
                    and NavigationItem(**json.loads(row[0])).kind == "question"
                    and NavigationItem(**json.loads(row[0])).status == "open"
                    for row in db.execute("SELECT payload FROM objects WHERE kind='NavigationItem'")
                )
            ):
                raise BindingError("integration has unresolved questions")
            for sid in plan.primary_scope_ids:
                row = db.execute("SELECT payload FROM objects WHERE kind='ReviewScope' AND id=?", (sid,)).fetchone()
                if row is None or ReviewScope(**json.loads(row[0])).status != "reviewed":
                    raise BindingError("primary unit is not reviewed")
                for (payload,) in db.execute("SELECT payload FROM objects WHERE kind='Question'"):
                    question = Question(**json.loads(payload))
                    if question.scope_id == sid and question.status in {"open", "disputed", "unresolved"}:
                        raise BindingError("primary unit has unresolved questions")
                for (payload,) in db.execute("SELECT payload FROM objects WHERE kind='NavigationItem'"):
                    item = NavigationItem(**json.loads(payload))
                    if item.scope_id == sid and item.kind == "question" and item.status == "open":
                        raise BindingError("primary unit has unresolved navigation")
            scope = replace(scope, status="reviewed", revision=scope.revision + 1)
            db.execute(
                "UPDATE objects SET revision=?,payload=? WHERE kind='ReviewScope' AND id=?",
                (scope.revision, _json(asdict(scope)), scope.id),
            )
            row = db.execute("SELECT payload FROM objects WHERE kind='Task' AND id=?", (task_id,)).fetchone()
            task = Task(**json.loads(row[0]))
            task = replace(task, input_revision=scope.revision, revision=task.revision + 1)
            db.execute(
                "UPDATE objects SET revision=?,payload=? WHERE kind='Task' AND id=?",
                (task.revision, _json(asdict(task)), task.id),
            )
            self._event(db, "IntegrationReview", scope.id, "finish", {"task_id": task_id})
            db.execute("DELETE FROM unit_pending_cases WHERE scope_id=?", (scope.id,))
            db.commit()
            return scope

    def validate_units_completion(self, *, db: sqlite3.Connection | None = None) -> dict[str, Any]:
        if db is None:
            with self._connect() as connection:
                connection.execute("BEGIN")
                return self.validate_units_completion(db=connection)
        reasons: list[str] = []
        result: dict[str, Any] = {
            "valid": False,
            "plan": None,
            "integration_review": None,
            "obligations": [],
            "owners": {},
            "integration_tasks": [],
            "pending_sources": [],
            "pending_cases": [],
            "reasons": reasons,
        }
        try:
            plan = self._unit_plan_db(db)
            result["plan"] = asdict(plan)
            result["manifest_hash"] = plan.manifest_hash
            result["integration_scope_id"] = plan.integration_scope_id
            result["primary_scope_ids"] = plan.primary_scope_ids
            row = db.execute("SELECT payload FROM integration_review WHERE id=1").fetchone()
            if row is None:
                raise BindingError("independent integration review missing")
            review = IntegrationReview(**json.loads(row[0]))
            result["integration_review"] = asdict(review)
            if review.run_id != self.run.id or review.scope_id != plan.integration_scope_id:
                reasons.append("integration identity mismatch")
            manifest = self.run.metadata.get("changed_regions")
            if (
                not isinstance(manifest, list)
                or hashlib.sha256(_json(manifest).encode()).hexdigest() != plan.manifest_hash
            ):
                reasons.append("manifest mismatch")
                manifest = []
            if plan.workspace_digest != self.run.metadata.get(
                "workspace_digest"
            ) or plan.base_workspace_digest != self.run.metadata.get("base_workspace_digest"):
                reasons.append("workspace mismatch")
            manifest_map = {r["id"]: r for r in manifest}
            for sid in plan.primary_scope_ids:
                row = db.execute("SELECT payload FROM objects WHERE kind='ReviewScope' AND id=?", (sid,)).fetchone()
                if row is None:
                    reasons.append("primary unit missing")
                    continue
                scope = ReviewScope(**json.loads(row[0]))
                for region in scope.regions:
                    rid = region.get("id")
                    if rid in result["owners"] or manifest_map.get(rid) != region:
                        reasons.append("region ownership mismatch")
                    result["owners"][rid] = sid
                if (
                    scope.status != "reviewed"
                    or scope.unresolved
                    or {r["id"] for r in scope.checked_regions} != {r["id"] for r in scope.regions}
                ):
                    reasons.append("primary unit incomplete: " + sid)
                verified_regions = set()
                for request_raw, result_raw in db.execute(
                    "SELECT request,result FROM unit_decisions WHERE scope_id=? ORDER BY rowid", (sid,)
                ):
                    request, decision = json.loads(request_raw), json.loads(result_raw)
                    delivered = self._require_delivered(
                        db, decision["task_id"], None, decision["turn_id"], set(decision["delivered_refs"])
                    )
                    if _json(delivered) != _json(decision["delivered_ranges"]):
                        raise BindingError("unit decision delivered ranges changed")
                    verified_regions.update(request["checked_region_ids"])
                if not {r["id"] for r in scope.checked_regions} <= verified_regions:
                    reasons.append("unit checked regions lack delivery-backed decision: " + sid)
                for (payload,) in db.execute("SELECT payload FROM objects WHERE kind='Question'"):
                    question = Question(**json.loads(payload))
                    if question.scope_id == sid and question.status in {"open", "disputed", "unresolved"}:
                        reasons.append("primary unit has open questions: " + sid)
                for (payload,) in db.execute("SELECT payload FROM objects WHERE kind='NavigationItem'"):
                    item = NavigationItem(**json.loads(payload))
                    if item.scope_id == sid and item.kind == "question" and item.status == "open":
                        reasons.append("primary unit has open navigation: " + sid)
            if set(result["owners"]) != set(manifest_map):
                reasons.append("manifest ownership incomplete")
            row = db.execute(
                "SELECT payload FROM objects WHERE kind='ReviewScope' AND id=?", (plan.integration_scope_id,)
            ).fetchone()
            if (
                row is None
                or ReviewScope(**json.loads(row[0])).status != "reviewed"
                or ReviewScope(**json.loads(row[0])).unresolved
            ):
                reasons.append("integration review incomplete")
            for (payload,) in db.execute("SELECT payload FROM objects WHERE kind='Question'"):
                question = Question(**json.loads(payload))
                if question.scope_id == plan.integration_scope_id and question.status in {
                    "open",
                    "disputed",
                    "unresolved",
                }:
                    reasons.append("integration has unresolved questions")
            for (payload,) in db.execute("SELECT payload FROM objects WHERE kind='NavigationItem'"):
                item = NavigationItem(**json.loads(payload))
                if item.scope_id == plan.integration_scope_id and item.kind == "question" and item.status == "open":
                    reasons.append("integration has unresolved navigation")
            tasks = [Task(**json.loads(row[0])) for row in db.execute("SELECT payload FROM objects WHERE kind='Task'")]
            result["integration_tasks"] = [
                asdict(t) for t in tasks if t.kind == "review" and t.object_id == plan.integration_scope_id
            ]
            if not any(t["status"] == "completed" for t in result["integration_tasks"]):
                reasons.append("integration task not completed")
            result["obligations"] = [
                self._obligation_view(db, IntegrationObligation(**json.loads(row[0])))
                for row in db.execute("SELECT payload FROM integration_obligations ORDER BY id")
            ]
            if not result["obligations"] or any(not o["valid"] for o in result["obligations"]):
                reasons.append("integration obligations are open or stale")
            result["pending_sources"] = self.pending_unit_sources(db=db)
            result["pending_cases"] = self.pending_unit_cases(db=db)
            if result["pending_sources"] or result["pending_cases"]:
                reasons.append("new evidence still requires unit review")
        except (BindingError, LeaseError, KeyError, OSError, ValueError, TypeError) as exc:
            reasons.append(str(exc) or "unit completion validation failed")
        result["valid"] = not reasons
        return result

    def _validate_refs(self, db: sqlite3.Connection, obj: Any) -> None:
        if isinstance(obj, ReviewScope):
            state = obj.working_state
            if not isinstance(state, dict):
                raise ValueError("working_state must be an object")
            if state:
                if self.run.policy not in {
                    "v42-stateful-2",
                    "v42-workspace-3",
                    "v42-frontier-4",
                    "v42-frontier-5",
                    "v42-units-6",
                }:
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
        if self.run.policy == "v42-units-6" and isinstance(obj, (Question, Case)):
            raise BindingError("unit decisions must use audited commit_unit_decision")
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
        if self.run.policy == "v42-units-6":
            raise BindingError("unit object changes require audited update_unit_object")
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
        if (
            isinstance(obj, ReviewScope)
            and self.run.policy in {"v42-workspace-3", "v42-frontier-4", "v42-frontier-5", "v42-units-6"}
            and obj.working_state
        ):
            raise ValueError("policy3 working state must be added through merge_working_states")
        if isinstance(obj, Task) and (
            obj.status != "pending" or obj.owner or obj.lease_token or obj.lease_expires or obj.result
        ):
            raise ValueError("new tasks must start pending without a lease or result")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if (
                isinstance(obj, ReviewScope)
                and self.run.policy == "v42-units-6"
                and db.execute("SELECT 1 FROM unit_plan WHERE id=1").fetchone()
            ):
                raise BindingError("unit scopes are fixed by the installed plan")
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
        if (
            cls is ReviewScope
            and self.run.policy == "v42-units-6"
            and (
                {"regions", "dependencies", "checked_regions", "unresolved"} & changes.keys()
                or changes.get("status") == "reviewed"
            )
        ):
            raise BindingError("unit decisions and completion require their dedicated transaction")
        if (
            cls is ReviewScope
            and self.run.policy in {"v42-workspace-3", "v42-frontier-4", "v42-frontier-5", "v42-units-6"}
            and "working_state" in changes
        ):
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

    def _frontier_task(self, db: sqlite3.Connection, task_id: str) -> Task:
        if self.run.policy not in {"v42-frontier-4", "v42-frontier-5", "v42-units-6"}:
            raise ValueError("frontier requires v42-frontier-4")
        row = db.execute("SELECT payload FROM objects WHERE kind='Task' AND id=?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(task_id)
        task = Task(**json.loads(row[0]))
        if task.run_id != self.run.id or task.kind != "review":
            raise BindingError("frontier requires a review task in this run")
        return task

    @staticmethod
    def _frontier_generation(db: sqlite3.Connection, task_id: str) -> int:
        row = db.execute("SELECT revision FROM frontier_generations WHERE task_id=?", (task_id,)).fetchone()
        return row[0] if row else 0

    @staticmethod
    def _bump_frontier(db: sqlite3.Connection, task_id: str, revision: int) -> None:
        db.execute(
            "INSERT INTO frontier_generations VALUES (?,?) "
            "ON CONFLICT(task_id) DO UPDATE SET revision=excluded.revision",
            (task_id, revision + 1),
        )

    def frontier_revision(self, task_id: str) -> int:
        with self._connect() as db:
            self._frontier_task(db, task_id)
            return self._frontier_generation(db, task_id)

    @staticmethod
    def _frontier_content(ref: EvidenceRef, raw: bytes, start: int | None, end: int | None) -> bytes:
        if (start is None) != (end is None):
            raise ValueError("page requires actual start and end lines")
        if start is None:
            return raw
        if isinstance(start, bool) or isinstance(end, bool) or not isinstance(start, int) or not isinstance(end, int):
            raise ValueError("page lines must be integers")
        try:
            lines = raw.decode("utf-8").splitlines(keepends=True)
        except UnicodeDecodeError as exc:
            raise BindingError("evidence page is not UTF-8") from exc
        first = ref.start_line or 1
        last = first + len(lines) - 1
        if start < first or end < start or end > last:
            raise ValueError("page must be within the actual evidence range")
        return "".join(lines[start - first : end - first + 1]).encode("utf-8")

    def pin_frontier_for_task(
        self,
        task_id: str,
        token: str,
        ref_id: str,
        *,
        start_line: int | None = None,
        end_line: int | None = None,
        expected_revision: int | None = None,
        now: float | None = None,
    ) -> FrontierEntry:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            task = self._frontier_task(db, task_id)
            self._guard_review_task(db, task_id, token, task.object_id, now)
            generation = self._frontier_generation(db, task_id)
            if expected_revision is not None and expected_revision != generation:
                raise RevisionConflict("frontier revision is stale")
            row = db.execute("SELECT payload FROM objects WHERE kind='EvidenceRef' AND id=?", (ref_id,)).fetchone()
            if row is None:
                raise BindingError("unknown evidence reference")
            ref = EvidenceRef(**json.loads(row[0]))
            if ref.run_id != self.run.id or ref.repo != self.run.repo or ref.side not in {"head", "base", "external"}:
                raise BindingError("evidence belongs to a different run or snapshot")
            if ref.side != "external" and ref.revision != getattr(self.run, ref.side):
                raise BindingError("evidence belongs to a different snapshot")
            if ref.metadata.get("status") in {"unavailable", "error", "failed", "unavailable_non_utf8"}:
                raise BindingError("unavailable evidence cannot be pinned")
            raw = (self.blob_dir / ref.content_hash).read_bytes()
            if hashlib.sha256(raw).hexdigest() != ref.content_hash:
                raise BindingError("evidence blob hash mismatch")
            content = self._frontier_content(ref, raw, start_line, end_line)
            is_page = start_line is not None
            actual_start = start_line if is_page else ref.start_line
            actual_end = end_line if is_page else ref.end_line
            digest = hashlib.sha256(content).hexdigest()
            # Query/test results need their invocation identity: equal output does not
            # make two different searches or executions the same source.
            extra = {"workspace_digest": ref.metadata.get("workspace_digest")}
            if ref.kind not in {"source", "diff", "documentation"}:
                extra.update({key: ref.metadata.get(key) for key in ("tool_name", "tool_args")})
            if ref.side == "external":
                extra.update({key: ref.metadata.get(key) for key in ("source_url", "captured_at")})
            identity = {
                "run_id": self.run.id,
                "task_id": task_id,
                "scope_id": task.object_id,
                "repo": ref.repo,
                "source_revision": ref.revision,
                "side": ref.side,
                "path": ref.path,
                "start_line": actual_start,
                "end_line": actual_end,
                "kind": ref.kind,
                "environment": ref.environment,
                "content_hash": digest,
                "source_identity": extra,
            }
            entry_id = "frontier_" + hashlib.sha256(_json(identity).encode()).hexdigest()
            row = db.execute("SELECT payload FROM frontier_entries WHERE entry_id=?", (entry_id,)).fetchone()
            if row:
                entry = FrontierEntry(**json.loads(row[0]))
                changed = ref.id not in entry.aliases or entry.status != "active"
                if changed:
                    if ref.id not in entry.aliases:
                        entry.aliases.append(ref.id)
                    entry.status, entry.release_reason = "active", ""
                    entry.item_revision += 1
                    db.execute(
                        "UPDATE frontier_entries SET payload=? WHERE entry_id=?", (_json(asdict(entry)), entry_id)
                    )
                    self._event(db, "FrontierEntry", entry_id, "pin", asdict(entry))
                    self._bump_frontier(db, task_id, generation)
            else:
                identity.pop("source_identity")
                entry = FrontierEntry(id=entry_id, aliases=[ref.id], is_page=is_page, **identity)
                db.execute(
                    "INSERT INTO frontier_entries(task_id,entry_id,payload) VALUES (?,?,?)",
                    (task_id, entry_id, _json(asdict(entry))),
                )
                self._event(db, "FrontierEntry", entry_id, "pin", asdict(entry))
                self._bump_frontier(db, task_id, generation)
            db.commit()
            return entry

    def list_frontier(self, task_id: str) -> list[FrontierEntry]:
        with self._connect() as db:
            self._frontier_task(db, task_id)
            rows = db.execute(
                "SELECT payload FROM frontier_entries WHERE task_id=? ORDER BY seq", (task_id,)
            ).fetchall()
        entries = [FrontierEntry(**json.loads(row[0])) for row in rows]
        return [entry for entry in entries if entry.status == "active"]

    def list_frontier_page(self, task_id: str, *, cursor: str | None = None, limit: int) -> dict[str, Any]:
        """Caller-sized, resumable directory page; a released cursor remains valid."""
        if type(limit) is not int or limit < 1:
            raise ValueError("limit must be a positive integer")
        with self._connect() as db:
            self._frontier_task(db, task_id)
            seq = 0
            if cursor is not None:
                row = db.execute(
                    "SELECT seq FROM frontier_entries WHERE task_id=? AND entry_id=?", (task_id, cursor)
                ).fetchone()
                if row is None:
                    raise ValueError("invalid frontier cursor")
                seq = row[0]
            rows = db.execute(
                "SELECT payload FROM frontier_entries WHERE task_id=? AND seq>? ORDER BY seq", (task_id, seq)
            )
            selected = []
            for row in rows:
                entry = FrontierEntry(**json.loads(row[0]))
                if entry.status == "active":
                    selected.append(entry)
                    if len(selected) > limit:
                        break
        return {"items": selected[:limit], "next_cursor": selected[limit - 1].id if len(selected) > limit else None}

    def read_frontier_entry(self, task_id: str, entry_id: str) -> bytes:
        with self._connect() as db:
            self._frontier_task(db, task_id)
            row = db.execute(
                "SELECT payload FROM frontier_entries WHERE task_id=? AND entry_id=?", (task_id, entry_id)
            ).fetchone()
        if row is None:
            raise KeyError(entry_id)
        entry = FrontierEntry(**json.loads(row[0]))
        if entry.status != "active" or not entry.aliases:
            raise KeyError(entry_id)
        ref = self.get(EvidenceRef, entry.aliases[0])
        raw = self.read_evidence(ref.id)
        content = self._frontier_content(
            ref, raw, entry.start_line if entry.is_page else None, entry.end_line if entry.is_page else None
        )
        if hashlib.sha256(content).hexdigest() != entry.content_hash:
            raise BindingError("frontier content hash mismatch")
        return content

    def read_frontier_page(self, task_id: str, entry_id: str, *, start_line: int, end_line: int) -> dict[str, Any]:
        """Return an explicit page within the pinned body, never outside it."""
        entries = {entry.id: entry for entry in self.list_frontier(task_id)}
        if entry_id not in entries:
            raise KeyError(entry_id)
        entry = entries[entry_id]
        raw = self.read_frontier_entry(task_id, entry_id)
        if type(start_line) is not int or type(end_line) is not int or start_line < 1 or end_line < start_line:
            raise ValueError("invalid page range")
        try:
            lines = raw.decode("utf-8").splitlines(keepends=True)
        except UnicodeDecodeError as exc:
            raise BindingError("frontier page is not UTF-8") from exc
        first = entry.start_line or 1
        last = first + len(lines) - 1
        if start_line < first or start_line > last:
            raise ValueError("page starts outside the pinned body")
        actual_end = min(end_line, last)
        return {
            "content": "".join(lines[start_line - first : actual_end - first + 1]).encode("utf-8"),
            "start_line": start_line,
            "end_line": actual_end,
            "next_line": actual_end + 1 if actual_end < last else None,
            "complete": start_line == first and actual_end == last,
        }

    def release_frontier_for_task(
        self,
        task_id: str,
        token: str,
        entry_ids: list[str],
        reason: str,
        *,
        expected_revision: int,
        now: float | None = None,
    ) -> list[FrontierEntry]:
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("release requires a reason")
        if not isinstance(entry_ids, list) or not entry_ids or any(not isinstance(x, str) or not x for x in entry_ids):
            raise ValueError("release requires entry ids")
        if len(set(entry_ids)) != len(entry_ids):
            raise ValueError("duplicate entry ids")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            task = self._frontier_task(db, task_id)
            self._guard_review_task(db, task_id, token, task.object_id, now)
            generation = self._frontier_generation(db, task_id)
            if expected_revision != generation:
                raise RevisionConflict("frontier revision is stale")
            entries = []
            for entry_id in entry_ids:
                row = db.execute(
                    "SELECT payload FROM frontier_entries WHERE task_id=? AND entry_id=?", (task_id, entry_id)
                ).fetchone()
                if row is None:
                    raise KeyError(entry_id)
                entry = FrontierEntry(**json.loads(row[0]))
                if entry.status != "active":
                    raise RevisionConflict("frontier entry is already released")
                entries.append(entry)
            for entry in entries:
                entry.status, entry.release_reason = "released", reason
                entry.item_revision += 1
                db.execute("UPDATE frontier_entries SET payload=? WHERE entry_id=?", (_json(asdict(entry)), entry.id))
                self._event(db, "FrontierEntry", entry.id, "release", asdict(entry))
            self._bump_frontier(db, task_id, generation)
            db.commit()
            return entries

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
            if self.run.policy == "v42-units-6" and task.kind == "review":
                plan = self._unit_plan_db(db)
                if task.object_id in {*plan.primary_scope_ids, plan.integration_scope_id}:
                    scope_row = db.execute(
                        "SELECT payload FROM objects WHERE kind='ReviewScope' AND id=?", (task.object_id,)
                    ).fetchone()
                    if scope_row is None or ReviewScope(**json.loads(scope_row[0])).status != "reviewed":
                        raise BindingError("incomplete unit cannot complete its task; release its lease for recovery")
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
        if self.run.policy == "v42-units-6" and (
            {"regions", "dependencies", "checked_regions", "unresolved"} & changes.keys()
            or changes.get("status") == "reviewed"
        ):
            raise BindingError("unit decisions and completion require their dedicated transaction")
        if (
            self.run.policy in {"v42-workspace-3", "v42-frontier-4", "v42-frontier-5", "v42-units-6"}
            and "working_state" in changes
        ):
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
            if self.run.policy == "v42-units-6":
                old_sources = self._unit_refs(db, case.evidence_refs)
                identities = {_unit_source_identity(ref) for ref in old_sources}
                genuinely_new = []
                for ref in self._unit_refs(db, [r for r in evidence_refs if r not in case.evidence_refs]):
                    identity = _unit_source_identity(ref)
                    if identity not in identities:
                        genuinely_new.append(ref.id)
                        identities.add(identity)
                evidence_refs = [*case.evidence_refs, *genuinely_new]
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
            if self.run.policy == "v42-units-6":
                plan = self._unit_plan_db(db)
                if case.scope_id in plan.primary_scope_ids:
                    db.execute("UPDATE unit_epochs SET revision=revision+1 WHERE scope_id=?", (case.scope_id,))
                    self._reopen_integration(db, plan)
                    scope_row = db.execute(
                        "SELECT payload FROM objects WHERE kind='ReviewScope' AND id=?", (case.scope_id,)
                    ).fetchone()
                    scope = ReviewScope(**json.loads(scope_row[0]))
                    for region in scope.regions:
                        for ref_id in genuinely_new:
                            db.execute(
                                "INSERT OR IGNORE INTO unit_pending_sources VALUES (?,?,?)",
                                (scope.id, region["id"], ref_id),
                            )
                    db.execute(
                        "INSERT INTO unit_pending_cases VALUES (?,?,?) "
                        "ON CONFLICT(case_id) DO UPDATE SET scope_id=excluded.scope_id,"
                        "obligation_id=excluded.obligation_id",
                        (case.id, scope.id, ""),
                    )
                    scope = replace(scope, status="pending", checked_regions=[], revision=scope.revision + 1)
                    db.execute(
                        "UPDATE objects SET revision=?,payload=? WHERE kind='ReviewScope' AND id=?",
                        (scope.revision, _json(asdict(scope)), scope.id),
                    )
                    self._event(db, "ReviewScope", scope.id, "reopen_for_verification", {"case_id": case.id})
                elif case.scope_id == plan.integration_scope_id:
                    self._reopen_integration(db, plan)
                    source_paths = {ref.path for ref in self._unit_refs(db, updated.evidence_refs) if ref.path}
                    affected_units = []
                    for sid in plan.primary_scope_ids:
                        row = db.execute(
                            "SELECT payload FROM objects WHERE kind='ReviewScope' AND id=?", (sid,)
                        ).fetchone()
                        unit = ReviewScope(**json.loads(row[0]))
                        if any(region.get("path") in source_paths for region in unit.regions):
                            affected_units.append(sid)
                    affected_units = affected_units or list(plan.primary_scope_ids)
                    oid = (
                        "ob_"
                        + hashlib.sha256(
                            _json([self.run.id, "verification_followup", case.id, updated.revision]).encode()
                        ).hexdigest()
                    )
                    obligation = IntegrationObligation(
                        oid,
                        self.run.id,
                        plan.integration_scope_id,
                        "verification_followup",
                        affected_units,
                        "Investigate independent verification updates for case " + case.id,
                    )
                    db.execute("INSERT INTO integration_obligations VALUES (?,?)", (oid, _json(asdict(obligation))))
                    db.execute(
                        "INSERT INTO unit_pending_cases VALUES (?,?,?) "
                        "ON CONFLICT(case_id) DO UPDATE SET scope_id=excluded.scope_id,"
                        "obligation_id=excluded.obligation_id",
                        (case.id, plan.integration_scope_id, oid),
                    )
                    self._event(db, "IntegrationObligation", oid, "verification_followup", asdict(obligation))
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
        if self.run.policy == "v42-units-6":
            raise BindingError("installed unit ownership cannot be repartitioned")
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
        if self.run.policy not in {
            "v42-stateful-2",
            "v42-workspace-3",
            "v42-frontier-4",
            "v42-frontier-5",
            "v42-units-6",
        }:
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
            if self.run.policy == "v42-units-6":
                lease_hash = hashlib.sha256(token.encode()).hexdigest()
                owner = db.execute(
                    "SELECT lease_hash FROM unit_turn_owners WHERE task_id=? AND turn_id=?", (task_id, turn_id)
                ).fetchone()
                if owner is None:
                    if phase != "request":
                        raise BindingError("turn must start with a request under this lease")
                    db.execute("INSERT INTO unit_turn_owners VALUES (?,?,?)", (task_id, turn_id, lease_hash))
                elif owner[0] != lease_hash:
                    raise LeaseError("turn belongs to a different task lease")
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
        if self.run.policy not in {
            "v42-stateful-2",
            "v42-workspace-3",
            "v42-frontier-4",
            "v42-frontier-5",
            "v42-units-6",
        }:
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
        if self.run.policy not in {"v42-workspace-3", "v42-frontier-4", "v42-frontier-5", "v42-units-6"}:
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
        if self.run.policy not in {"v42-workspace-3", "v42-frontier-4", "v42-frontier-5", "v42-units-6"}:
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
        if self.run.policy not in {"v42-workspace-3", "v42-frontier-4", "v42-frontier-5", "v42-units-6"}:
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
