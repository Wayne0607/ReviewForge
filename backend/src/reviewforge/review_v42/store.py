"""Durable, run-bound v4.2 work board and immutable evidence store."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import time
import uuid
from contextlib import nullcontext
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
from .line_semantics import source_lines, source_slice

T = TypeVar("T", ReviewScope, Question, Case, Task, EvidenceRef, NavigationItem)
_TYPES = {c.__name__: c for c in (ReviewScope, Question, Case, Task, EvidenceRef, NavigationItem)}
_VERIFICATION_DELIVERY_POLICY = "claim-delivery-1"


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


def _install_verified_blob(temp: Path, destination: Path, digest: str, *, label: str) -> None:
    """Atomically install without replacing a reader's existing immutable blob."""
    try:
        os.link(temp, destination)
    except OSError as install_error:
        try:
            current = destination.read_bytes()
        except OSError:
            raise install_error
        if hashlib.sha256(current).hexdigest() != digest:
            raise BindingError(f"{label} blob hash mismatch") from install_error
        return
    if hashlib.sha256(destination.read_bytes()).hexdigest() != digest:
        raise BindingError(f"{label} blob hash mismatch")


class _VerificationSources:
    """Expose only transaction-validated originals to the verifier's citation validator."""

    def __init__(self, run: Run):
        self.run = run
        self.refs: dict[str, EvidenceRef] = {}
        self.blobs: dict[str, bytes] = {}

    def get(self, cls: type[EvidenceRef], ref_id: str) -> EvidenceRef:
        if cls is not EvidenceRef or ref_id not in self.refs:
            raise BindingError("citation does not bind an original source in this delivery")
        return self.refs[ref_id]

    def read_evidence(self, ref_id: str) -> bytes:
        return self.blobs[ref_id]

    def for_claim(self, observed: list[str]) -> _VerificationSources:
        sources = _VerificationSources(self.run)
        sources.refs = {ref_id: self.refs[ref_id] for ref_id in observed}
        sources.blobs = {ref_id: self.blobs[ref_id] for ref_id in observed}
        return sources


class ReviewStore:
    """A store belongs to one immutable run; operations use short SQLite transactions."""

    def __init__(self, path: str | Path, run: Run):
        self.run = run
        fencing = run.metadata.get("question_fencing_policy")
        if "question_fencing_policy" in run.metadata and (
            fencing != "question-input-1" or not self._question_policy_enabled()
        ):
            raise BindingError("unsupported question fencing policy")
        candidate_policy = run.metadata.get("candidate_draft_policy")
        if "candidate_draft_policy" in run.metadata and (
            candidate_policy != "candidate-draft-1"
            or run.policy != "v42-units-6"
            or run.metadata.get("shared_investigation") != "shared-1"
        ):
            raise BindingError("unsupported candidate draft policy")
        projection = run.metadata.get("frontier_projection_policy")
        if "frontier_projection_policy" in run.metadata and (
            projection != "source-line-union-1"
            or run.policy != "v42-units-6"
            or run.metadata.get("shared_investigation") != "shared-1"
            or run.metadata.get("source_line_policy") != "git-lf-1"
        ):
            raise BindingError("unsupported frontier projection policy")
        decision_policy = run.metadata.get("decision_cycle_policy")
        if "decision_cycle_policy" in run.metadata and (
            decision_policy != "decision-cycle-1"
            or run.policy != "v42-units-6"
            or run.metadata.get("shared_investigation") != "shared-1"
            or run.metadata.get("source_line_policy") != "git-lf-1"
            or projection != "source-line-union-1"
            or candidate_policy != "candidate-draft-1"
            or "question_assistant_policy" in run.metadata
        ):
            raise BindingError("unsupported decision cycle profile")
        if "verification_delivery_policy" in run.metadata and (
            run.metadata["verification_delivery_policy"] != _VERIFICATION_DELIVERY_POLICY
            or decision_policy != "decision-cycle-1"
            or run.policy != "v42-units-6"
        ):
            raise BindingError("unsupported verification delivery profile")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.blob_dir = self.path.parent / ".review-v42-blobs"
        self.blob_dir.mkdir(parents=True, exist_ok=True)
        self.audit_dir = self.path.parent / ".review-v42-turn-audit"
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
            if decision_policy == "decision-cycle-1":
                db.executescript("""
                    CREATE TABLE IF NOT EXISTS decision_attempts (
                        id TEXT PRIMARY KEY, task_id TEXT NOT NULL, turn_id TEXT NOT NULL, call_id TEXT NOT NULL,
                        payload TEXT NOT NULL, UNIQUE(task_id,turn_id,call_id));
                    CREATE TABLE IF NOT EXISTS decision_items (
                        id TEXT PRIMARY KEY, scope_id TEXT NOT NULL, revision INTEGER NOT NULL, payload TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS decision_steps (
                        id TEXT PRIMARY KEY, task_id TEXT NOT NULL, turn_id TEXT NOT NULL, call_id TEXT NOT NULL,
                        item_id TEXT NOT NULL, payload TEXT NOT NULL, UNIQUE(task_id,turn_id,call_id));
                    CREATE TABLE IF NOT EXISTS decision_operations (
                        id TEXT PRIMARY KEY, step_id TEXT NOT NULL, ordinal INTEGER NOT NULL, payload TEXT NOT NULL,
                        UNIQUE(step_id,ordinal));
                    CREATE TABLE IF NOT EXISTS decision_verification_queue (
                        case_id TEXT PRIMARY KEY, step_id TEXT NOT NULL, case_revision INTEGER NOT NULL);
                """)
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
                if run.metadata.get("shared_investigation") == "shared-1":
                    db.execute(
                        "CREATE TABLE IF NOT EXISTS case_counterevidence_links ("
                        "case_id TEXT NOT NULL, source_identity TEXT NOT NULL, payload TEXT NOT NULL, "
                        "PRIMARY KEY(case_id,source_identity))"
                    )
                    db.execute(
                        "CREATE TABLE IF NOT EXISTS unit_aborted_read_batches ("
                        "task_id TEXT NOT NULL, turn_id TEXT NOT NULL, scope_id TEXT NOT NULL, payload TEXT NOT NULL, "
                        "PRIMARY KEY(task_id,turn_id))"
                    )
                    if candidate_policy == "candidate-draft-1":
                        db.execute(
                            "CREATE TABLE IF NOT EXISTS candidate_drafts ("
                            "id TEXT PRIMARY KEY, scope_id TEXT NOT NULL, revision INTEGER NOT NULL, "
                            "status TEXT NOT NULL, payload TEXT NOT NULL)"
                        )
                        db.execute(
                            "CREATE TABLE IF NOT EXISTS candidate_draft_operations ("
                            "id TEXT PRIMARY KEY, request TEXT NOT NULL, result TEXT NOT NULL)"
                        )
                    if run.metadata.get("question_assistant_policy") == "question-delivery-1":
                        db.execute(
                            "CREATE TABLE IF NOT EXISTS question_delegations ("
                            "task_id TEXT PRIMARY KEY, scope_id TEXT NOT NULL, scope_revision INTEGER NOT NULL, "
                            "creator_task_id TEXT NOT NULL, turn_id TEXT NOT NULL, call_id TEXT NOT NULL, "
                            "request TEXT NOT NULL, UNIQUE(creator_task_id,turn_id,call_id))"
                        )
                        db.execute(
                            "CREATE TABLE IF NOT EXISTS question_decisions ("
                            "task_id TEXT NOT NULL, turn_id TEXT NOT NULL, request TEXT NOT NULL, "
                            "result TEXT NOT NULL, PRIMARY KEY(task_id,turn_id))"
                        )
                        db.execute(
                            "CREATE TABLE IF NOT EXISTS question_lease_refs ("
                            "task_id TEXT NOT NULL, lease_hash TEXT NOT NULL, ref_id TEXT NOT NULL, "
                            "PRIMARY KEY(task_id,lease_hash,ref_id))"
                        )
                        db.execute(
                            "CREATE TABLE IF NOT EXISTS question_session_owners "
                            "(task_id TEXT PRIMARY KEY, lease_hash TEXT NOT NULL)"
                        )
                        if fencing == "question-input-1":
                            db.execute(
                                "CREATE TABLE IF NOT EXISTS question_input_bindings "
                                "(task_id TEXT PRIMARY KEY, digest TEXT NOT NULL)"
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

    def pending_shared_batches(
        self,
        scope_id: str,
        *,
        db: sqlite3.Connection | None = None,
        exclude_task_id: str | None = None,
        exclude_turn_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """List visible model responses whose complete native exchange was not persisted.

        No prior action is re-executed or attributed to a replacement lease.
        Exclusion is solely for the current finish operation; all sibling calls
        must already have durable receipts and there must be exactly one finish.
        """
        if self.run.policy != "v42-units-6" or self.run.metadata.get("shared_investigation") != "shared-1":
            return []
        if (exclude_task_id is None) != (exclude_turn_id is None):
            raise ValueError("task and turn must both identify the current finish")
        if db is None:
            with self._connect() as connection:
                connection.execute("BEGIN")
                return self.pending_shared_batches(
                    scope_id, db=connection, exclude_task_id=exclude_task_id, exclude_turn_id=exclude_turn_id
                )
        plan = self._unit_plan_db(db)
        if scope_id not in {*plan.primary_scope_ids, plan.integration_scope_id}:
            raise BindingError("scope is outside the current unit plan")

        def audit(digest: str) -> list[dict[str, Any]]:
            raw = (self.audit_dir / digest).read_bytes()
            if hashlib.sha256(raw).hexdigest() != digest:
                raise BindingError("turn audit blob hash mismatch")
            value = json.loads(raw)
            if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
                raise BindingError("turn audit has invalid visible messages")
            return value

        result = []
        tasks = [Task(**json.loads(row[0])) for row in db.execute("SELECT payload FROM objects WHERE kind='Task'")]
        for task in tasks:
            if task.run_id != self.run.id:
                continue
            if task.kind == "review":
                relevant = task.object_id == scope_id
            elif task.kind == "question" and self._question_policy_enabled():
                delegation = db.execute(
                    "SELECT scope_id FROM question_delegations WHERE task_id=?", (task.id,)
                ).fetchone()
                relevant = delegation is None or delegation[0] == scope_id
            else:
                relevant = False
            if not relevant:
                continue
            turns: dict[str, dict[str, str]] = {}
            for turn_id, phase, digest in db.execute(
                "SELECT turn_id,phase,content_hash FROM turn_audit WHERE task_id=? ORDER BY seq", (task.id,)
            ):
                turns.setdefault(turn_id, {})[phase] = digest
            for turn_id, phases in turns.items():
                if "response" not in phases:
                    if task.kind == "question" and "native" in phases:
                        result.append(
                            {
                                "scope_id": scope_id,
                                "task_id": task.id,
                                "task_kind": "question",
                                "turn_id": turn_id,
                                "calls": [],
                                "reason": "turn audit unavailable or invalid: native phase has no response",
                            }
                        )
                    continue
                entry: dict[str, Any] = {
                    "scope_id": scope_id,
                    "task_id": task.id,
                    "turn_id": turn_id,
                    "calls": [],
                    "reason": "native exchange not fully persisted",
                }
                if task.kind == "question":
                    entry["task_kind"] = "question"
                    qrow = db.execute(
                        "SELECT payload FROM objects WHERE kind='Question' AND id=?", (task.object_id,)
                    ).fetchone()
                    if delegation and qrow:
                        question = Question(**json.loads(qrow[0]))
                        if question.run_id == self.run.id and question.scope_id == delegation[0]:
                            entry.update(question_id=question.id, case_id=question.case_id)
                try:
                    response = audit(phases["response"])
                    question_exchange = None
                    if "exchange" in phases:
                        question_exchange = audit(phases["exchange"])
                        if task.kind != "question":
                            continue
                        if len(question_exchange) == 1 and question_exchange[0].get("type") == "human":
                            data = question_exchange[0].get("data")
                            content = data.get("content") if isinstance(data, dict) else None
                            if (
                                isinstance(content, str)
                                and content.startswith("Rejected native tool batch " + turn_id + ":")
                                and "No tool was executed" in content
                                and not any(phase.startswith("tool:") for phase in phases)
                            ):
                                native = audit(phases["native"]) if "native" in phases else []
                                recorded_error = (
                                    len(native) == 1
                                    and isinstance(native[0], dict)
                                    and bool(native[0].get("invalid_tool_calls") or native[0].get("protocol_errors"))
                                )
                                body = response[0].get("data", {}) if len(response) == 1 else {}
                                visible_calls = body.get("tool_calls", []) if isinstance(body, dict) else []
                                ids = [call.get("id") for call in visible_calls if isinstance(call, dict)]
                                ambiguous = (
                                    len(ids) != len(visible_calls)
                                    or any(not isinstance(ident, str) or not ident.strip() for ident in ids)
                                    or len(ids) != len(set(ids))
                                )
                                if recorded_error or ambiguous:
                                    continue
                    aborted = db.execute(
                        "SELECT scope_id,payload FROM unit_aborted_read_batches WHERE task_id=? AND turn_id=?",
                        (task.id, turn_id),
                    ).fetchone()
                    if aborted:
                        marker = json.loads(aborted[1])
                        current_scope = db.execute(
                            "SELECT revision FROM objects WHERE kind='ReviewScope' AND id=?", (scope_id,)
                        ).fetchone()
                        if (
                            not isinstance(marker, dict)
                            or aborted[0] != scope_id
                            or marker.get("phases") != phases
                            or marker.get("input_revision") != task.input_revision
                            or current_scope is None
                            or current_scope[0] == task.input_revision
                        ):
                            raise BindingError("aborted read batch binding changed")
                        for digest in phases.values():
                            audit(digest)
                        continue
                    calls = []
                    if len(response) != 1 or response[0].get("type") not in {
                        "ai",
                        "AIMessage",
                        "ai_chunk",
                        "AIMessageChunk",
                    }:
                        raise BindingError("native response is incomplete or ambiguous")
                    for message in response:
                        if message.get("type") not in {"ai", "AIMessage", "ai_chunk", "AIMessageChunk"}:
                            continue
                        body = message.get("data")
                        if not isinstance(body, dict):
                            raise BindingError("invalid visible assistant response")
                        tool_calls = body.get("tool_calls", [])
                        if not isinstance(tool_calls, list):
                            raise BindingError("invalid visible tool calls")
                        for call in tool_calls:
                            if not isinstance(call, dict):
                                raise BindingError("invalid visible tool call")
                            calls.append({"id": call.get("id"), "name": call.get("name"), "args": call.get("args")})
                    from reviewforge.engine.native_submission import strict_tool_call

                    ids = [call["id"] for call in calls]
                    if any(not isinstance(ident, str) or not ident.strip() for ident in ids) or len(ids) != len(
                        set(ids)
                    ):
                        raise BindingError("ambiguous native tool identities")
                    raw_calls = None
                    if "native" in phases:
                        native = audit(phases["native"])
                        if (
                            len(native) != 1
                            or not isinstance(native[0].get("invalid_tool_calls"), list)
                            or native[0]["invalid_tool_calls"]
                            or native[0].get("protocol_errors")
                        ):
                            raise BindingError("invalid native tool calls")
                        raw_calls = native[0].get("raw_tool_calls")
                        if raw_calls is not None and (not isinstance(raw_calls, list) or len(raw_calls) != len(calls)):
                            raise BindingError("native call count differs")
                    for call in calls:
                        raw = None
                        if raw_calls is not None:
                            matches = [
                                item for item in raw_calls if isinstance(item, dict) and item.get("id") == call["id"]
                            ]
                            if len(matches) != 1:
                                raise BindingError("ambiguous native tool identities")
                            raw = matches[0]
                        strict_tool_call(call, raw)
                    receipts = set()
                    receipt_messages = {}
                    failed_receipts = set()
                    for phase, digest in phases.items():
                        if not phase.startswith("tool:"):
                            continue
                        parts = phase.split(":", 2)
                        if len(parts) != 3 or not parts[1].isdigit() or not parts[2]:
                            raise BindingError("invalid tool receipt identity")
                        messages = audit(digest)
                        matched = [
                            message
                            for message in messages
                            if message.get("type") == "tool"
                            and isinstance(message.get("data"), dict)
                            and message["data"].get("tool_call_id") == parts[2]
                        ]
                        if not matched:
                            raise BindingError("tool receipt does not match its identity")
                        for message in matched:
                            receipt_messages.setdefault(parts[2], []).append(message)
                            data = message["data"]
                            content = data.get("content")
                            try:
                                content = json.loads(content) if isinstance(content, str) else content
                            except ValueError:
                                content = None
                            if (
                                data.get("status") == "error"
                                or not isinstance(content, dict)
                                or (
                                    isinstance(content, dict)
                                    and (
                                        "error" in content
                                        or "review_state_error" in content
                                        or content.get("status") in {"error", "failed", "unavailable"}
                                        or (
                                            isinstance(content.get("metadata"), dict)
                                            and content["metadata"].get("status") in {"error", "failed", "unavailable"}
                                        )
                                    )
                                )
                            ):
                                failed_receipts.add(parts[2])
                        receipts.add(parts[2])
                    entry["calls"] = [
                        {**call, "receipt": "recorded" if call["id"] in receipts else "outcome_unknown"}
                        for call in calls
                    ]
                    if question_exchange is not None:
                        exchange_ids = {
                            message.get("data", {}).get("tool_call_id")
                            for message in question_exchange
                            if message.get("type") == "tool" and isinstance(message.get("data"), dict)
                        }
                        if not all(call["id"] in receipts and call["id"] in exchange_ids for call in calls) or not any(
                            message.get("type") in {"ai", "AIMessage", "ai_chunk", "AIMessageChunk"}
                            for message in question_exchange
                        ):
                            raise BindingError("question exchange is missing native calls or receipts")
                        if response[0] not in question_exchange:
                            raise BindingError("question exchange differs from its native response")
                        for call in calls:
                            if not all(message in question_exchange for message in receipt_messages[call["id"]]):
                                raise BindingError("question exchange differs from its durable receipt")
                        continue
                    if task.id == exclude_task_id and turn_id == exclude_turn_id:
                        terminal = [
                            call
                            for call in calls
                            if (
                                self.run.metadata.get("decision_cycle_policy") == "decision-cycle-1"
                                and call["name"] == "review_step"
                                and isinstance(call["args"], dict)
                                and call["args"].get("action") == "complete_review"
                                and isinstance(call["args"].get("payload"), dict)
                                # Match ReviewCompletion's declared default when auditing raw calls.
                                and call["args"]["payload"].get("finish", True) is True
                            )
                            or (scope_id == plan.integration_scope_id and call["name"] == "finish_integration")
                            or (
                                scope_id != plan.integration_scope_id
                                and call["name"] == "submit_unit_decision"
                                and isinstance(call["args"], dict)
                                and call["args"].get("finish") is True
                            )
                        ]
                        if (
                            len(terminal) == 1
                            and all(isinstance(ident, str) and ident for ident in ids)
                            and len(set(ids)) == len(ids)
                            and not failed_receipts
                            and all(call["id"] in receipts for call in calls if call is not terminal[0])
                        ):
                            continue
                except (OSError, ValueError, TypeError, KeyError) as exc:
                    entry["reason"] = "turn audit unavailable or invalid: " + str(exc)
                result.append(entry)
        return result

    def abort_fenced_read_batch(self, task_id: str, turn_id: str) -> dict[str, Any]:
        """Controller records a fenced, exclusively read-only batch as not adopted.

        This is not a tool receipt or a source-delivery permit. The replacement
        reviewer must obtain its own source bodies in a successful model turn.
        """
        if self.run.policy != "v42-units-6" or self.run.metadata.get("shared_investigation") != "shared-1":
            raise BindingError("fenced read recovery requires shared-1")
        if not isinstance(task_id, str) or not task_id or not isinstance(turn_id, str) or not turn_id:
            raise ValueError("task and turn IDs are required")
        # Do not include run_check: executing tests can have external effects.
        readonly = {
            "read_source",
            "read_diff",
            "search_code",
            "find_references",
            "read_knowledge",
            "search_knowledge",
            "read_evidence",
            "query_shared",
            "read_search_section",
            "list_navigation",
            "get_shared_investigation",
            "query_shared_investigations",
            "query_shared_cases",
        }
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload FROM objects WHERE kind='Task' AND id=?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(task_id)
            task = Task(**json.loads(row[0]))
            if task.run_id != self.run.id or task.kind != "review":
                raise BindingError("batch task is not a review in this run")
            scope = db.execute(
                "SELECT revision FROM objects WHERE kind='ReviewScope' AND id=?", (task.object_id,)
            ).fetchone()
            if scope is None or scope[0] == task.input_revision:
                raise BindingError("task input has not been fenced")
            plan = self._unit_plan_db(db)
            if task.object_id not in {*plan.primary_scope_ids, plan.integration_scope_id}:
                raise BindingError("batch scope is not in this run's plan")
            if not db.execute(
                "SELECT 1 FROM unit_turn_owners WHERE task_id=? AND turn_id=?", (task_id, turn_id)
            ).fetchone():
                raise BindingError("turn lacks its original lease identity")
            phases = dict(
                db.execute(
                    "SELECT phase,content_hash FROM turn_audit WHERE task_id=? AND turn_id=?", (task_id, turn_id)
                ).fetchall()
            )
            if "request" not in phases or "response" not in phases or "exchange" in phases:
                raise BindingError("batch does not have an incomplete audited response")

            def load(phase: str) -> list[dict[str, Any]]:
                raw = (self.audit_dir / phases[phase]).read_bytes()
                if hashlib.sha256(raw).hexdigest() != phases[phase]:
                    raise BindingError("turn audit blob hash mismatch")
                messages = json.loads(raw)
                if not isinstance(messages, list) or any(not isinstance(message, dict) for message in messages):
                    raise BindingError("invalid turn audit")
                return messages

            for phase in phases:
                if phase not in {"request", "response", "native"} and not phase.startswith("tool:"):
                    raise BindingError("unknown turn audit phase")
                load(phase)
            response = load("response")
            if len(response) != 1 or response[0].get("type") not in {"ai", "AIMessage", "ai_chunk", "AIMessageChunk"}:
                raise BindingError("response is not one native assistant message")
            body = response[0].get("data")
            if isinstance(body, dict) and body.get("invalid_tool_calls"):
                raise BindingError("response contains invalid native calls")
            calls = body.get("tool_calls") if isinstance(body, dict) else None
            if not isinstance(calls, list) or not calls or any(not isinstance(call, dict) for call in calls):
                raise BindingError("response has no complete native read calls")
            ids = [call.get("id") for call in calls]
            if any(not isinstance(ident, str) or not ident.strip() for ident in ids) or len(ids) != len(set(ids)):
                raise BindingError("ambiguous native call IDs")
            native = load("native") if "native" in phases else []
            raw_calls = None
            if "native" in phases:
                if (
                    len(native) != 1
                    or set(native[0]) != {"raw_tool_calls", "invalid_tool_calls"}
                    or native[0].get("invalid_tool_calls")
                    or not isinstance(native[0].get("invalid_tool_calls"), list)
                ):
                    raise BindingError("invalid native calls")
                raw_calls = native[0].get("raw_tool_calls")
                if raw_calls is not None and (not isinstance(raw_calls, list) or len(raw_calls) != len(calls)):
                    raise BindingError("native call count differs")
            from reviewforge.engine.native_submission import strict_tool_call

            for call in calls:
                if call.get("name") not in readonly or not isinstance(call.get("args"), dict):
                    raise BindingError("batch includes a potentially mutating or unknown call")
                if "review_state" in call["args"]:
                    raise BindingError("read call also carried a mutable review state")
                json.dumps(call["args"], allow_nan=False)
                raw = None
                if raw_calls is not None:
                    matches = [item for item in raw_calls if isinstance(item, dict) and item.get("id") == call["id"]]
                    if len(matches) != 1:
                        raise BindingError("native call identity is ambiguous")
                    raw = matches[0]
                strict_tool_call(call, raw)
                failure = (
                    "Tool unavailable: "
                    + call["name"]
                    + " "
                    + json.dumps(call["args"], sort_keys=True, ensure_ascii=False)
                )
                for (payload,) in db.execute("SELECT payload FROM objects WHERE kind='Question'"):
                    question = Question(**json.loads(payload))
                    if (
                        question.scope_id == task.object_id
                        and question.owner == "tool_controller"
                        and question.statement == failure
                    ):
                        raise BindingError("read may have changed a tool-failure question")
            receipt_ids = set()
            receipt_indices = set()
            for phase in phases:
                if not phase.startswith("tool:"):
                    continue
                parts = phase.split(":", 2)
                if (
                    len(parts) != 3
                    or not parts[1].isdigit()
                    or parts[2] not in ids
                    or parts[2] in receipt_ids
                    or int(parts[1]) in receipt_indices
                    or int(parts[1]) >= len(ids)
                    or ids[int(parts[1])] != parts[2]
                ):
                    raise BindingError("invalid or duplicated read receipt")
                messages = load(phase)
                if (
                    len(messages) != 1
                    or messages[0].get("type") != "tool"
                    or not isinstance(messages[0].get("data"), dict)
                    or messages[0]["data"].get("tool_call_id") != parts[2]
                ):
                    raise BindingError("read receipt does not match the response")
                content = messages[0]["data"].get("content")
                if not isinstance(content, str) or not isinstance(json.loads(content), dict):
                    raise BindingError("read receipt is incomplete")
                receipt_ids.add(parts[2])
                receipt_indices.add(int(parts[1]))
            if receipt_indices != set(range(len(receipt_indices))):
                raise BindingError("read receipt sequence is incomplete")
            marker = {
                "task_id": task_id,
                "turn_id": turn_id,
                "scope_id": task.object_id,
                "input_revision": task.input_revision,
                "fenced_revision": scope[0],
                "phases": phases,
                "calls": [
                    {
                        "id": call["id"],
                        "name": call["name"],
                        "receipt": "recorded" if call["id"] in receipt_ids else "not_adopted",
                    }
                    for call in calls
                ],
                "reason": "fenced read batch; results not adopted",
            }
            row = db.execute(
                "SELECT payload FROM unit_aborted_read_batches WHERE task_id=? AND turn_id=?", (task_id, turn_id)
            ).fetchone()
            if row:
                existing = json.loads(row[0])
                if any(
                    existing.get(key) != marker[key]
                    for key in ("task_id", "turn_id", "scope_id", "input_revision", "phases")
                ):
                    raise RevisionConflict("aborted batch identity changed")
                return existing
            db.execute(
                "INSERT INTO unit_aborted_read_batches VALUES (?,?,?,?)",
                (task_id, turn_id, task.object_id, _json(marker)),
            )
            self._event(db, "Task", task_id, "fenced_read_batch_aborted", marker)
            db.commit()
            return marker

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

    def _unit_refs(
        self, db: sqlite3.Connection, ids: list[str], *, blobs: dict[str, bytes] | None = None
    ) -> list[EvidenceRef]:
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
            if blobs is not None:
                blobs[ref_id] = raw
            refs.append(ref)
        return refs

    @staticmethod
    def _task_frontier_refs(db: sqlite3.Connection, task_id: str) -> set[str]:
        refs = set()
        for (payload,) in db.execute("SELECT payload FROM frontier_entries WHERE task_id=?", (task_id,)):
            refs.update(FrontierEntry(**json.loads(payload)).aliases)
        return refs

    def _source_union_delivered(
        self, db: sqlite3.Connection, task_id: str, shown: dict[str, Any]
    ) -> list[tuple[EvidenceRef, int, int]]:
        """Verify each visible intersection against its immutable task-bound source."""
        if self.run.metadata.get("frontier_projection_policy") != "source-line-union-1":
            raise BindingError("source union projection is not enabled")
        task = self._frontier_task(db, task_id)
        if task.kind == "review":
            scope_id = task.object_id
        else:
            qrow = db.execute(
                "SELECT payload FROM objects WHERE kind='Question' AND id=?", (task.object_id,)
            ).fetchone()
            if qrow is None:
                raise BindingError("source union question scope is missing")
            scope_id = Question(**json.loads(qrow[0])).scope_id
        start, end, text = shown.get("start_line"), shown.get("end_line"), shown.get("content")
        mappings = shown.get("mappings")
        try:
            content_digest = hashlib.sha256(text.encode("utf-8")).hexdigest() if isinstance(text, str) else None
        except UnicodeEncodeError as exc:
            raise BindingError("source union content is not UTF-8") from exc
        if (
            type(start) is not int
            or type(end) is not int
            or start < 1
            or end < start
            or not isinstance(text, str)
            or not isinstance(mappings, list)
            or not mappings
            or not isinstance(shown.get("content_hash"), str)
            or content_digest != shown["content_hash"]
            or len(source_lines(self.run, text, keepends=True)) != end - start + 1
            or shown.get("repo") != self.run.repo
            or not isinstance(shown.get("side"), str)
            or shown["side"] not in {"base", "head"}
            or shown.get("source_revision") != getattr(self.run, shown.get("side", ""), None)
            or not isinstance(shown.get("path"), str)
            or not shown["path"]
            or not isinstance(shown.get("environment"), str)
            or not isinstance(shown.get("workspace_digest"), str)
            or not shown["workspace_digest"]
            or shown["workspace_digest"] != self.run.metadata.get("workspace_digest")
        ):
            raise BindingError("source union segment is invalid")
        result: list[tuple[EvidenceRef, int, int]] = []
        seen = set()
        for mapping in mappings:
            if not isinstance(mapping, dict):
                raise BindingError("source union mapping is invalid")
            entry_id, ref_id = mapping.get("entry_id"), mapping.get("ref_id")
            a, b = mapping.get("start_line"), mapping.get("end_line")
            if (
                not isinstance(entry_id, str)
                or not entry_id
                or not isinstance(ref_id, str)
                or not ref_id
                or type(a) is not int
                or type(b) is not int
                or a < start
                or b > end
                or b < a
                or (entry_id, ref_id, a, b) in seen
            ):
                raise BindingError("source union mapping is invalid")
            seen.add((entry_id, ref_id, a, b))
            row = db.execute(
                "SELECT payload FROM frontier_entries WHERE task_id=? AND entry_id=?", (task_id, entry_id)
            ).fetchone()
            if row is None:
                raise BindingError("source union entry is not owned by this task")
            try:
                entry = FrontierEntry(**json.loads(row[0]))
                ref = self._unit_refs(db, [ref_id])[0]
                self._validate_question_original(ref)
                if (
                    entry.id != entry_id
                    or entry.run_id != self.run.id
                    or entry.task_id != task_id
                    or entry.scope_id != scope_id
                    or ref_id not in entry.aliases
                    or ref.kind != "source"
                    or entry.kind != "source"
                    or entry.repo != ref.repo
                    or entry.side != ref.side
                    or entry.source_revision != ref.revision
                    or entry.path != ref.path
                    or entry.environment != ref.environment
                    or any(
                        shown[key] != value
                        for key, value in (
                            ("repo", ref.repo),
                            ("side", ref.side),
                            ("source_revision", ref.revision),
                            ("path", ref.path),
                            ("environment", ref.environment),
                            ("workspace_digest", ref.metadata.get("workspace_digest")),
                        )
                    )
                ):
                    raise BindingError("source union identity does not match original source")
                raw = (self.blob_dir / ref.content_hash).read_bytes()
                if hashlib.sha256(raw).hexdigest() != ref.content_hash:
                    raise BindingError("source union original blob hash mismatch")
                if (ref.start_line is not None and (type(ref.start_line) is not int or ref.start_line < 1)) or (
                    ref.end_line is not None and (type(ref.end_line) is not int or ref.end_line < (ref.start_line or 1))
                ):
                    raise BindingError("source union original range is invalid")
                raw_text = raw.decode("utf-8")
                raw_last = (ref.start_line or 1) + len(source_lines(self.run, raw_text, keepends=True)) - 1
                if ref.end_line is not None and ref.end_line != raw_last:
                    raise BindingError("source union original range does not match its content")
                page = self._frontier_content(
                    ref,
                    raw,
                    entry.start_line if entry.is_page else None,
                    entry.end_line if entry.is_page else None,
                    run=self.run,
                )
                if hashlib.sha256(page).hexdigest() != entry.content_hash:
                    raise BindingError("source union original page hash mismatch")
                page_text = page.decode("utf-8")
                first = entry.start_line if entry.is_page else (ref.start_line or 1)
                last = first + len(source_lines(self.run, page_text, keepends=True)) - 1
                if (entry.is_page and entry.end_line != last) or (
                    not entry.is_page
                    and (
                        entry.start_line != ref.start_line
                        or entry.end_line != ref.end_line
                        or (ref.end_line is not None and ref.end_line != last)
                    )
                ):
                    raise BindingError("source union original page range mismatch")
                if (a, b) != (max(start, first), min(end, last)):
                    raise BindingError("source union mapping is not the actual intersection")
                actual = source_slice(self.run, page_text, a - first + 1, b - first + 1)
                visible = source_slice(self.run, text, a - start + 1, b - start + 1)
                if actual.encode("utf-8") != visible.encode("utf-8"):
                    raise BindingError("source union content differs from its original")
                result.append((ref, a, b))
            except (KeyError, OSError, TypeError, ValueError, UnicodeError) as exc:
                raise BindingError("source union source is unavailable or invalid") from exc
        next_line = start
        for _, a, b in sorted(result, key=lambda item: (item[1], item[2])):
            if a > next_line:
                break
            next_line = max(next_line, b + 1)
        if next_line <= end:
            raise BindingError("source union contains uncovered lines")
        return result

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
            last = end if end is not None else first + len(source_lines(self.run, content.decode("utf-8"))) - 1
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
            if (
                message["type"] == "tool"
                and self.run.metadata.get("decision_cycle_policy") == "decision-cycle-1"
                and isinstance(obj.get("step_id"), str)
                and isinstance(obj.get("results"), list)
            ):
                step_row = db.execute(
                    "SELECT task_id,turn_id,payload FROM decision_steps WHERE id=?", (obj["step_id"],)
                ).fetchone()
                if step_row is None or step_row[0] != task_id:
                    continue
                origin_owner = db.execute(
                    "SELECT lease_hash FROM unit_turn_owners WHERE task_id=? AND turn_id=?",
                    (task_id, step_row[1]),
                ).fetchone()
                if origin_owner is None or step_row[1] == turn_id:
                    continue
                seen_operations = set()
                for wrapped in obj["results"]:
                    if not isinstance(wrapped, dict) or set(wrapped) != {"operation_id", "result"}:
                        continue
                    operation_id = wrapped["operation_id"]
                    if not isinstance(operation_id, str) or operation_id in seen_operations:
                        continue
                    seen_operations.add(operation_id)
                    row = db.execute(
                        "SELECT payload FROM decision_operations WHERE id=? AND step_id=?",
                        (operation_id, obj["step_id"]),
                    ).fetchone()
                    if row is None:
                        continue
                    operation = json.loads(row[0])
                    result = wrapped["result"]
                    if operation.get("status") != "completed" or operation.get("result") != result:
                        continue
                    if not isinstance(result, dict) or not isinstance(result.get("evidence_ref"), str):
                        continue
                    ref_id = result["evidence_ref"]
                    if ref_id not in ref_ids or not isinstance(result.get("content"), str):
                        continue
                    ref = self._unit_refs(db, [ref_id])[0]
                    raw = (self.blob_dir / ref.content_hash).read_bytes()
                    start, end = result.get("start_line"), result.get("end_line")
                    try:
                        actual = self._frontier_content(ref, raw, start, end, run=self.run).decode("utf-8")
                    except (ValueError, UnicodeError):
                        continue
                    if result["content"] == actual:
                        record(ref, actual.encode("utf-8"), start, end)
            elif message["type"] == "tool" and isinstance(obj.get("evidence_ref"), str):
                ref_id = obj["evidence_ref"]
                if ref_id not in ref_ids or not isinstance(obj.get("content"), str):
                    continue
                ref = self._unit_refs(db, [ref_id])[0]
                raw = (self.blob_dir / ref.content_hash).read_bytes()
                start, end = obj.get("start_line"), obj.get("end_line")
                try:
                    actual = self._frontier_content(ref, raw, start, end, run=self.run).decode("utf-8")
                except (ValueError, UnicodeError):
                    continue
                if obj["content"] == actual:
                    record(ref, actual.encode("utf-8"), start, end)
            elif message["type"] == "human" and "frontier_revision" in obj and isinstance(obj.get("entries"), list):
                for shown in obj["entries"]:
                    if (
                        self.run.metadata.get("frontier_projection_policy") == "source-line-union-1"
                        and isinstance(shown, dict)
                        and shown.get("kind") == "source_union"
                    ):
                        for ref, first, last in self._source_union_delivered(db, task_id, shown):
                            if ref.id in ref_ids:
                                delivered.setdefault(ref.id, []).append((first, last))
                        continue
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
                                run=self.run,
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

    def _pending_shared_investigations(self, db: sqlite3.Connection, scope_id: str) -> list[str]:
        """Return unclosed shared work affecting a unit, or all work for integration."""
        if self.run.policy != "v42-units-6" or self.run.metadata.get("shared_investigation") != "shared-1":
            return []
        if (
            db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='shared_investigations'").fetchone()
            is None
        ):
            return ["shared-investigation-store-unavailable"]
        plan = self._unit_plan_db(db)
        pending = []
        for investigation_id, payload in db.execute("SELECT id,payload FROM shared_investigations"):
            try:
                item = json.loads(payload)
                scopes = item["scope_ids"]
                if (
                    item.get("run_id") != self.run.id
                    or item.get("id") != investigation_id
                    or not isinstance(scopes, list)
                    or not scopes
                    or not all(isinstance(sid, str) for sid in scopes)
                    or len(set(scopes)) != len(scopes)
                    or not set(scopes) <= {*plan.primary_scope_ids, plan.integration_scope_id}
                ):
                    raise ValueError("invalid shared investigation binding")
                status = item["status"]
                if status not in {"open", "partial", "unavailable", "resolved", "retracted"}:
                    raise ValueError("invalid shared investigation status")
                if status in {"open", "partial", "unavailable"} and (
                    scope_id == plan.integration_scope_id or scope_id in scopes
                ):
                    pending.append(investigation_id)
            except (KeyError, TypeError, ValueError):
                pending.append(investigation_id)
        return sorted(pending)

    def _invalidate_scopes_for_shared(
        self,
        db: sqlite3.Connection,
        scope_ids: list[str],
        investigation_id: str,
        reason: str,
        *,
        current_task_id: str | None = None,
    ) -> None:
        """Fence affected responsibilities within the investigation's transaction.

        The caller must validate the creating task's lease in this same transaction.
        Only the investigation's explicitly associated region checks are cleared.
        Unrelated case verdicts remain unchanged.
        """
        if self.run.policy != "v42-units-6" or self.run.metadata.get("shared_investigation") != "shared-1":
            raise BindingError("shared investigation requires the shared-1 opt-in")
        plan = self._unit_plan_db(db)
        if (
            not isinstance(scope_ids, list)
            or not scope_ids
            or len(set(scope_ids)) != len(scope_ids)
            or not set(scope_ids) <= {*plan.primary_scope_ids, plan.integration_scope_id}
            or not isinstance(investigation_id, str)
            or not investigation_id
            or not isinstance(reason, str)
            or not reason.strip()
        ):
            raise BindingError("invalid shared investigation responsibilities")
        row = db.execute("SELECT payload FROM shared_investigations WHERE id=?", (investigation_id,)).fetchone()
        if row is None:
            raise BindingError("shared investigation is missing")
        item = json.loads(row[0])
        region_ids = item.get("region_ids")
        if (
            item.get("run_id") != self.run.id
            or item.get("id") != investigation_id
            or item.get("scope_ids") != sorted(scope_ids)
            or not isinstance(region_ids, list)
            or not region_ids
            or not all(isinstance(rid, str) and rid for rid in region_ids)
            or len(set(region_ids)) != len(region_ids)
        ):
            raise BindingError("invalid shared investigation region binding")
        affected = set(scope_ids) | {plan.integration_scope_id}
        current = None
        if current_task_id is not None:
            row = db.execute("SELECT payload FROM objects WHERE kind='Task' AND id=?", (current_task_id,)).fetchone()
            if row is None:
                raise LeaseError("creating review task does not exist")
            current = Task(**json.loads(row[0]))
            if current.kind != "review" or current.status != "leased" or current.run_id != self.run.id:
                raise LeaseError("creating review task must retain its valid lease")
        for sid in sorted(affected):
            row = db.execute("SELECT payload FROM objects WHERE kind='ReviewScope' AND id=?", (sid,)).fetchone()
            if row is None:
                raise BindingError("shared investigation scope does not exist")
            scope = ReviewScope(**json.loads(row[0]))
            if scope.run_id != self.run.id or scope.status == "superseded":
                raise BindingError("shared investigation scope is not active")
            creator = current is not None and current.object_id == sid
            if creator and current.input_revision != scope.revision:
                raise RevisionConflict("creating task scope revision is stale")
            updated = replace(
                scope,
                status="pending",
                checked_regions=[r for r in scope.checked_regions if r.get("id") not in region_ids],
                revision=scope.revision + 1,
            )
            db.execute(
                "UPDATE objects SET revision=?,payload=? WHERE kind='ReviewScope' AND id=?",
                (updated.revision, _json(asdict(updated)), sid),
            )
            if sid in plan.primary_scope_ids:
                db.execute("UPDATE unit_epochs SET revision=revision+1 WHERE scope_id=?", (sid,))
            if creator:
                current = replace(current, input_revision=updated.revision, revision=current.revision + 1)
                db.execute(
                    "UPDATE objects SET revision=?,payload=? WHERE kind='Task' AND id=?",
                    (current.revision, _json(asdict(current)), current.id),
                )
            self._event(
                db,
                "ReviewScope",
                sid,
                "shared_investigation_changed",
                {"investigation_id": investigation_id, "reason": reason},
            )

    def _apply_shared_resolution(
        self, db: sqlite3.Connection, investigation_id: str, *, current_task_id: str | None = None
    ) -> None:
        """Bind resolved raw sources to the affected regions before closing the work."""
        if self.run.policy != "v42-units-6" or self.run.metadata.get("shared_investigation") != "shared-1":
            raise BindingError("shared investigation requires the shared-1 opt-in")
        row = db.execute("SELECT payload FROM shared_investigations WHERE id=?", (investigation_id,)).fetchone()
        if row is None:
            raise BindingError("shared investigation is missing")
        item = json.loads(row[0])
        if item.get("status") not in {"resolved", "retracted"}:
            raise BindingError("only a closed investigation can be applied")
        refs = []
        if item["status"] == "resolved":
            observations = item.get("observations")
            if not isinstance(observations, list) or not observations:
                raise BindingError("resolved investigation requires source observations")
            identities = set()
            for observation in observations:
                if not isinstance(observation, dict) or not observation.get("evidence_refs"):
                    raise BindingError("resolved investigation requires source observations")
                if observation.get("delivered_task_id") != current_task_id or not observation.get("delivered_turn_id"):
                    raise BindingError("resolved source must be delivered to the closing task")
                source_refs = self._unit_refs(db, observation["evidence_refs"])
                delivered = self._require_delivered(
                    db, current_task_id or "", None, observation["delivered_turn_id"], {ref.id for ref in source_refs}
                )
                found_source = False
                for ref in source_refs:
                    if (
                        ref.kind.casefold()
                        in {
                            "search",
                            "search_index",
                            "search_section",
                            "diff",
                            "query",
                            "navigation",
                            "frontier",
                            "shared_investigation",
                        }
                        or ref.metadata.get("navigation_only") is True
                        or ref.metadata.get("complete") is False
                        or ref.metadata.get("status", "available") not in {"available", "success", "ok", "executed"}
                    ):
                        raise BindingError("resolved investigation cannot cite navigation or unavailable material")
                    if ref.kind != "source":
                        continue
                    if (
                        ref.side not in {"head", "base"}
                        or not ref.path
                        or ref.metadata.get("tool_name") not in (None, "read_source")
                    ):
                        raise BindingError("resolved investigation requires original available source")
                    found_source = True
                    try:
                        lines = source_lines(self.run, (self.blob_dir / ref.content_hash).read_bytes().decode("utf-8"))
                    except UnicodeDecodeError as exc:
                        raise BindingError("resolved source is not readable") from exc
                    if (
                        not lines
                        or type(ref.start_line) is not int
                        or type(ref.end_line) is not int
                        or (ref.end_line - ref.start_line + 1 != len(lines))
                    ):
                        raise BindingError("resolved source requires an exact original range")
                    cursor = ref.start_line
                    for first, last in sorted(delivered.get(ref.id, [])):
                        if first <= cursor <= last:
                            cursor = last + 1
                    if cursor <= ref.end_line:
                        raise BindingError("resolved source was not fully delivered")
                    identity = _unit_source_identity(ref)
                    if identity not in identities:
                        identities.add(identity)
                        refs.append(ref.id)
                if not found_source:
                    raise BindingError("each resolved observation requires original source")
        self._invalidate_scopes_for_shared(
            db,
            item["scope_ids"],
            investigation_id,
            "shared investigation " + item["status"],
            current_task_id=current_task_id,
        )
        for sid in item["scope_ids"]:
            row = db.execute("SELECT payload FROM objects WHERE kind='ReviewScope' AND id=?", (sid,)).fetchone()
            scope = ReviewScope(**json.loads(row[0]))
            for region in scope.regions:
                if region.get("id") in item["region_ids"]:
                    for ref_id in refs:
                        db.execute(
                            "INSERT OR IGNORE INTO unit_pending_sources VALUES (?,?,?)", (sid, region["id"], ref_id)
                        )

    def _settle_scope_decision(
        self,
        db: sqlite3.Connection,
        task_id: str,
        turn_id: str,
        call_id: str,
        *,
        result_status: str,
        expected: dict[str, Any] | None = None,
    ) -> None:
        row = db.execute(
            "SELECT id,payload FROM decision_steps WHERE task_id=? AND turn_id=? AND call_id=?",
            (task_id, turn_id, call_id),
        ).fetchone()
        if row is None:
            raise BindingError("completion decision has not been staged")
        step = json.loads(row[1])
        args = step.get("args", {})
        payload = args.get("payload") if isinstance(args, dict) else None
        if step.get("action") != "complete_review" or not isinstance(payload, dict):
            raise BindingError("completion decision action mismatch")
        if expected is not None:
            for key, value in expected.items():
                default = True if key == "finish" else []
                if payload.get(key, default) != value:
                    raise BindingError("completion decision differs from its native payload")
        step.update(status="settled", disposition=result_status)
        db.execute("UPDATE decision_steps SET payload=? WHERE id=?", (_json(step), row[0]))
        self._event(db, "DecisionStep", row[0], "scope_disposition", step)

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
        _db: sqlite3.Connection | None = None,
        decision_call_id: str | None = None,
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
        with self._connect() if _db is None else nullcontext(_db) as db:
            if _db is None:
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
            if finish and self.pending_shared_batches(
                task.object_id, db=db, exclude_task_id=task_id, exclude_turn_id=turn_id
            ):
                raise BindingError("unit has an unfinished native tool batch")
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
                    lines = source_lines(self.run, raw.decode("utf-8"))
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
            decision_items_open = self.run.metadata.get("decision_cycle_policy") == "decision-cycle-1" and any(
                json.loads(row[0]).get("status") != "answered"
                for row in db.execute("SELECT payload FROM decision_items WHERE scope_id=?", (scope.id,))
            )
            blocked = (
                decision_items_open
                or current_unresolved
                or self._pending_shared_investigations(db, scope.id)
                or self.pending_candidate_drafts(scope.id, db=db)
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
            if self.run.metadata.get("decision_cycle_policy") == "decision-cycle-1" and decision_call_id:
                self._settle_scope_decision(
                    db,
                    task_id,
                    turn_id,
                    decision_call_id,
                    result_status=status,
                    expected={
                        "checked_region_ids": checked_region_ids,
                        "observations": observations,
                        "unresolved_refs": unresolved,
                        "finish": finish,
                    },
                )
            elif self.run.metadata.get("decision_cycle_policy") == "decision-cycle-1" and finish:
                raise BindingError("completion requires a staged native decision")
            if _db is None:
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
        _db: sqlite3.Connection | None = None,
        **changes: Any,
    ) -> Any:
        if cls not in (Question, Case) or {"id", "run_id", "scope_id", "revision"} & changes.keys():
            raise ValueError("invalid unit object update")
        if changes.keys() - {f.name for f in fields(cls)}:
            raise ValueError("unknown unit object fields")
        with self._connect() if _db is None else nullcontext(_db) as db:
            if _db is None:
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
            if _db is None:
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

    def register_case_counterevidence(
        self,
        task_id: str,
        token: str,
        case_id: str,
        evidence_refs: list[str],
        *,
        turn_id: str,
        reason: str,
        expected_revision: int | None = None,
        now: float | None = None,
    ) -> Case:
        """Record a possible conflict and invalidate only its explicitly named case.

        The submitting reviewer's description is a lead, not a semantic verdict.
        Every original source must have been delivered to this lease in full.
        """
        if self.run.policy != "v42-units-6" or self.run.metadata.get("shared_investigation") != "shared-1":
            raise BindingError("cross-unit counterevidence requires the shared-1 opt-in")
        if not isinstance(case_id, str) or not case_id or not isinstance(reason, str) or not reason.strip():
            raise ValueError("a case and a reason for the possible conflict are required")
        if (
            not isinstance(evidence_refs, list)
            or not evidence_refs
            or any(not isinstance(ref, str) or not ref for ref in evidence_refs)
        ):
            raise ValueError("original evidence references are required")
        if expected_revision is not None and (type(expected_revision) is not int or expected_revision < 0):
            raise ValueError("expected revision must be a nonnegative integer")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            plan = self._unit_plan_db(db)
            task_row = db.execute("SELECT payload FROM objects WHERE kind='Task' AND id=?", (task_id,)).fetchone()
            if task_row is None:
                raise KeyError(task_id)
            task = Task(**json.loads(task_row[0]))
            if task.object_id not in {*plan.primary_scope_ids, plan.integration_scope_id}:
                raise BindingError("review task is outside the current unit plan")
            self._guard_review_task(db, task_id, token, task.object_id, now)
            row = db.execute("SELECT payload FROM objects WHERE kind='Case' AND id=?", (case_id,)).fetchone()
            if row is None:
                raise KeyError(case_id)
            case = Case(**json.loads(row[0]))
            if case.run_id != self.run.id or case.scope_id not in {*plan.primary_scope_ids, plan.integration_scope_id}:
                raise BindingError("case is outside the current unit plan")
            if case.scope_id == task.object_id:
                raise BindingError("case must belong to another responsibility")
            row = db.execute(
                "SELECT payload FROM objects WHERE kind='ReviewScope' AND id=?", (case.scope_id,)
            ).fetchone()
            if row is None:
                raise BindingError("case responsibility is missing")
            scope = ReviewScope(**json.loads(row[0]))
            if scope.run_id != self.run.id or scope.status == "superseded":
                raise BindingError("case responsibility is not active")
            refs = self._unit_refs(db, list(dict.fromkeys(evidence_refs)))
            for ref in refs:
                if (
                    ref.kind != "source"
                    or ref.side not in {"base", "head"}
                    or not ref.path
                    or ref.metadata.get("navigation_only") is True
                    or ref.metadata.get("complete") is False
                    or ref.metadata.get("status", "available") not in {"available", "success", "ok"}
                    or ref.metadata.get("tool_name") not in (None, "read_source")
                ):
                    raise BindingError("possible counterevidence requires original fixed source")
                try:
                    lines = source_lines(self.run, (self.blob_dir / ref.content_hash).read_bytes().decode("utf-8"))
                except UnicodeDecodeError as exc:
                    raise BindingError("possible counterevidence must be readable") from exc
                if (
                    not lines
                    or type(ref.start_line) is not int
                    or type(ref.end_line) is not int
                    or (ref.end_line - ref.start_line + 1 != len(lines))
                ):
                    raise BindingError("possible counterevidence requires a complete original source range")
            if not {ref.id for ref in refs} <= self._task_frontier_refs(db, task_id):
                raise BindingError("possible counterevidence requires explicit reads by the reporting task")
            delivered = self._require_delivered(db, task_id, token, turn_id, {ref.id for ref in refs})
            for ref in refs:
                cursor = ref.start_line
                for first, last in sorted(delivered.get(ref.id, [])):
                    if first <= cursor <= last:
                        cursor = last + 1
                if cursor <= ref.end_line:
                    raise BindingError("possible counterevidence was not fully delivered")
            existing = {_unit_source_identity(ref) for ref in self._unit_refs(db, case.counter_evidence_refs)}
            new_refs = []
            for ref in refs:
                identity = _unit_source_identity(ref)
                if identity not in existing:
                    existing.add(identity)
                    new_refs.append(ref.id)
            if new_refs and expected_revision is not None and case.revision != expected_revision:
                raise RevisionConflict("case revision changed before counterevidence registration")
            for ref in refs:
                identity = hashlib.sha256(_json(_unit_source_identity(ref)).encode()).hexdigest()
                payload = {
                    "case_id": case.id,
                    "source_ref": ref.id,
                    "source_identity": identity,
                    "origin_task_id": task_id,
                    "origin_turn_id": turn_id,
                    "reason": reason,
                }
                cursor = db.execute(
                    "INSERT OR IGNORE INTO case_counterevidence_links VALUES (?,?,?)",
                    (case.id, identity, _json(payload)),
                )
                if cursor.rowcount:
                    self._event(db, "Case", case.id, "possible_counterevidence_registered", payload)
            if not new_refs:
                db.commit()
                return case
            updated = replace(
                case, counter_evidence_refs=[*case.counter_evidence_refs, *new_refs], revision=case.revision + 1
            )
            self._validate_refs(db, updated)
            db.execute(
                "UPDATE objects SET revision=?,payload=? WHERE kind='Case' AND id=?",
                (updated.revision, _json(asdict(updated)), case.id),
            )
            self._event(
                db, "Case", case.id, "counterevidence_changed", {"before": asdict(case), "after": asdict(updated)}
            )
            if case.scope_id in plan.primary_scope_ids:
                for region in scope.regions:
                    for ref_id in new_refs:
                        db.execute(
                            "INSERT OR IGNORE INTO unit_pending_sources VALUES (?,?,?)",
                            (scope.id, region["id"], ref_id),
                        )
                db.execute(
                    "INSERT INTO unit_pending_cases VALUES (?,?,?) ON CONFLICT(case_id) DO UPDATE SET "
                    "scope_id=excluded.scope_id,obligation_id=excluded.obligation_id",
                    (case.id, scope.id, ""),
                )
                scope = replace(scope, status="pending", checked_regions=[], revision=scope.revision + 1)
                db.execute(
                    "UPDATE objects SET revision=?,payload=? WHERE kind='ReviewScope' AND id=?",
                    (scope.revision, _json(asdict(scope)), scope.id),
                )
                db.execute("UPDATE unit_epochs SET revision=revision+1 WHERE scope_id=?", (scope.id,))
                self._reopen_integration(db, plan)
                self._event(
                    db, "ReviewScope", scope.id, "reopen_for_counterevidence", {"case_id": case.id, "refs": new_refs}
                )
            else:
                self._reopen_integration(db, plan)
                oid = (
                    "ob_"
                    + hashlib.sha256(
                        _json([self.run.id, "case_counterevidence", case.id, updated.revision]).encode()
                    ).hexdigest()
                )
                obligation = IntegrationObligation(
                    oid,
                    self.run.id,
                    plan.integration_scope_id,
                    "case_followup",
                    list(plan.primary_scope_ids),
                    "Investigate possible counterevidence for case " + case.id,
                    origin_evidence_refs=list(new_refs),
                    origin_task_id=task_id,
                    origin_turn_id=turn_id,
                )
                db.execute("INSERT INTO integration_obligations VALUES (?,?)", (oid, _json(asdict(obligation))))
                db.execute(
                    "INSERT INTO unit_pending_cases VALUES (?,?,?) ON CONFLICT(case_id) DO UPDATE SET "
                    "scope_id=excluded.scope_id,obligation_id=excluded.obligation_id",
                    (case.id, plan.integration_scope_id, oid),
                )
                self._event(db, "IntegrationObligation", oid, "case_counterevidence", asdict(obligation))
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
        *,
        turn_id: str | None = None,
        decision_call_id: str | None = None,
    ) -> ReviewScope:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            plan = self._unit_plan_db(db)
            self._guard_review_task(db, task_id, token, plan.integration_scope_id, now)
            if self.run.metadata.get("shared_investigation") == "shared-1":
                self._require_delivered(db, task_id, token, turn_id, set())
                if self.pending_shared_batches(
                    plan.integration_scope_id, db=db, exclude_task_id=task_id, exclude_turn_id=turn_id
                ):
                    raise BindingError("integration has an unfinished native tool batch")
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
                (
                    self.run.metadata.get("decision_cycle_policy") == "decision-cycle-1"
                    and any(
                        json.loads(row[0]).get("status") != "answered"
                        for row in db.execute("SELECT payload FROM decision_items WHERE scope_id=?", (scope.id,))
                    )
                )
                or scope.unresolved
                or self._pending_shared_investigations(db, scope.id)
                or self.pending_candidate_drafts(db=db)
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
            if self.run.metadata.get("decision_cycle_policy") == "decision-cycle-1":
                if not decision_call_id or not turn_id:
                    raise BindingError("integration completion requires a staged native decision")
                self._settle_scope_decision(
                    db, task_id, turn_id, decision_call_id, result_status="reviewed", expected={"finish": True}
                )
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
            "pending_batches": [],
            "reasons": reasons,
        }
        try:
            plan = self._unit_plan_db(db)
            result["plan"] = asdict(plan)
            result["manifest_hash"] = plan.manifest_hash
            result["integration_scope_id"] = plan.integration_scope_id
            result["primary_scope_ids"] = plan.primary_scope_ids
            for scope_id in [*plan.primary_scope_ids, plan.integration_scope_id]:
                result["pending_batches"].extend(self.pending_shared_batches(scope_id, db=db))
            if result["pending_batches"]:
                reasons.append("unfinished native tool batches")
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
                if self._pending_shared_investigations(db, sid):
                    reasons.append("primary unit has unresolved shared investigation: " + sid)
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
            if self._pending_shared_investigations(db, plan.integration_scope_id):
                reasons.append("integration has unresolved shared investigation")
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
            if self._candidate_draft_enabled():
                result["pending_candidate_drafts"] = self.pending_candidate_drafts(db=db)
                if result["pending_candidate_drafts"]:
                    reasons.append("candidate drafts require repair or explicit withdrawal")
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

    def _question_policy_enabled(self) -> bool:
        return (
            self.run.policy == "v42-units-6"
            and self.run.metadata.get("shared_investigation") == "shared-1"
            and self.run.metadata.get("question_assistant_policy") == "question-delivery-1"
        )

    def _candidate_draft_enabled(self) -> bool:
        return (
            self.run.policy == "v42-units-6"
            and self.run.metadata.get("shared_investigation") == "shared-1"
            and self.run.metadata.get("candidate_draft_policy") == "candidate-draft-1"
        )

    def list_candidate_drafts(
        self, scope_id: str | None = None, *, db: sqlite3.Connection | None = None
    ) -> list[dict[str, Any]]:
        if not self._candidate_draft_enabled():
            return []
        if db is None:
            with self._connect() as connection:
                connection.execute("BEGIN")
                return self.list_candidate_drafts(scope_id, db=connection)
        plan = self._unit_plan_db(db)
        valid_scopes = {*plan.primary_scope_ids, plan.integration_scope_id}
        if scope_id is not None and scope_id not in valid_scopes:
            raise BindingError("candidate draft scope is outside the unit plan")
        try:
            rows = db.execute("SELECT id,scope_id,revision,status,payload FROM candidate_drafts ORDER BY id").fetchall()
        except sqlite3.DatabaseError as exc:
            raise BindingError("candidate draft directory is unavailable") from exc
        drafts = []
        for ident, sid, revision, status, payload in rows:
            try:
                item = json.loads(payload)
                if (
                    not isinstance(item, dict)
                    or item.get("id") != ident
                    or item.get("run_id") != self.run.id
                    or item.get("scope_id") != sid
                    or sid not in valid_scopes
                    or type(revision) is not int
                    or item.get("revision") != revision
                    or item.get("status") != status
                    or status not in {"pending_validation", "repair_needed", "promoted", "retracted", "applied"}
                    or not isinstance(item.get("original_args"), dict)
                    or not isinstance(item.get("current_args"), dict)
                    or not isinstance(item.get("errors"), list)
                    or any(not isinstance(error, dict) for error in item["errors"])
                    or any(
                        not isinstance(item.get(key), str) or not item[key]
                        for key in ("origin_task_id", "origin_turn_id", "origin_call_id", "response_hash")
                    )
                    or re.fullmatch(r"[0-9a-f]{64}", item["response_hash"]) is None
                    or not isinstance(item.get("case_id"), str)
                    or not isinstance(item.get("reason"), str)
                    or type(item.get("requires_resolution")) is not bool
                    or item.get("target_resolution") not in {"new", "unknown"}
                    or (
                        status == "applied"
                        and (not isinstance(item.get("linked_draft_id"), str) or not item["linked_draft_id"])
                    )
                    or (status == "promoted" and (not item["case_id"] or type(item.get("case_revision")) is not int))
                    or (status == "retracted" and not item["reason"].strip())
                ):
                    raise ValueError("candidate draft binding is invalid")
            except (TypeError, ValueError) as exc:
                raise BindingError("candidate draft directory is invalid") from exc
            if scope_id is None or scope_id == sid:
                drafts.append(item)
        by_id = {item["id"]: item for item in drafts}
        for item in drafts:
            if item["status"] == "applied":
                target = by_id.get(item["linked_draft_id"])
                if target is None or target["scope_id"] != item["scope_id"] or target["id"] == item["id"]:
                    raise BindingError("candidate attempt link is invalid")
        return drafts

    def pending_candidate_drafts(
        self, scope_id: str | None = None, *, db: sqlite3.Connection | None = None
    ) -> list[dict[str, Any]]:
        if db is None:
            with self._connect() as connection:
                connection.execute("BEGIN")
                return self.pending_candidate_drafts(scope_id, db=connection)
        if scope_id is not None:
            self.list_candidate_drafts(scope_id, db=db)
        all_items = self.list_candidate_drafts(db=db)
        by_id = {item["id"]: item for item in all_items}
        pending = []
        for item in all_items:
            if item["status"] not in {"pending_validation", "repair_needed"} or (
                scope_id is not None and item["scope_id"] != scope_id
            ):
                continue
            args = item["original_args"]
            target_id = args.get("draft_id")
            if item.get("requires_resolution"):
                state = "unknown"
            elif target_id is None or target_id == "":
                state = (
                    "new"
                    if args.get("expected_revision") is None
                    and all(
                        args.get(key) is None
                        for key in ("candidate_resolution", "target_draft_id", "target_expected_revision")
                    )
                    else "unknown"
                )
            else:
                target = by_id.get(target_id) if isinstance(target_id, str) else None
                state = (
                    "known"
                    if (
                        target is not None
                        and target["scope_id"] == item["scope_id"]
                        and type(args.get("expected_revision")) is int
                        and target["revision"] == args["expected_revision"]
                        and target["status"] in {"pending_validation", "repair_needed"}
                        and not target.get("requires_resolution")
                    )
                    else "unknown"
                )
            pending.append({**item, "target_resolution": state})
        return pending

    def get_candidate_draft(self, draft_id: str, *, db: sqlite3.Connection | None = None) -> dict[str, Any]:
        for item in self.list_candidate_drafts(db=db):
            if item["id"] == draft_id:
                return item
        raise KeyError(draft_id)

    def _candidate_turn_calls(
        self, db: sqlite3.Connection, task_id: str, token: str, turn_id: str
    ) -> tuple[list[dict[str, Any]], str]:
        self._require_delivered(db, task_id, token, turn_id, set())
        phases = dict(
            db.execute("SELECT phase,content_hash FROM turn_audit WHERE task_id=? AND turn_id=?", (task_id, turn_id))
        )

        def audit(phase: str) -> Any:
            digest = phases[phase]
            raw = (self.audit_dir / digest).read_bytes()
            if hashlib.sha256(raw).hexdigest() != digest:
                raise BindingError("turn audit blob hash mismatch")
            return json.loads(raw)

        response = audit("response")
        if (
            not isinstance(response, list)
            or len(response) != 1
            or not isinstance(response[0], dict)
            or response[0].get("type") not in {"ai", "AIMessage", "ai_chunk", "AIMessageChunk"}
            or not isinstance(response[0].get("data"), dict)
        ):
            raise BindingError("candidate turn response is invalid")
        calls = response[0]["data"].get("tool_calls", [])
        if not isinstance(calls, list) or any(not isinstance(call, dict) for call in calls):
            raise BindingError("candidate native calls are invalid")
        ids = [call.get("id") for call in calls]
        if any(not isinstance(ident, str) or not ident.strip() for ident in ids) or len(ids) != len(set(ids)):
            raise BindingError("candidate native call IDs are ambiguous")
        raw_calls = None
        if "native" in phases:
            native = audit("native")
            if (
                not isinstance(native, list)
                or len(native) != 1
                or not isinstance(native[0], dict)
                or not isinstance(native[0].get("invalid_tool_calls"), list)
                or (
                    self.run.metadata.get("decision_cycle_policy") != "decision-cycle-1"
                    and (native[0]["invalid_tool_calls"] or native[0].get("protocol_errors"))
                )
            ):
                raise BindingError("candidate native calls are invalid")
            raw_calls = native[0].get("raw_tool_calls")
            if raw_calls is not None and (
                not isinstance(raw_calls, list)
                or (
                    self.run.metadata.get("decision_cycle_policy") != "decision-cycle-1"
                    and len(raw_calls) != len(calls)
                )
            ):
                raise BindingError("candidate native call count differs")
        from reviewforge.engine.native_submission import strict_tool_call

        valid_calls = []
        for call in calls:
            try:
                raw_call = None
                if raw_calls is not None:
                    matching = [item for item in raw_calls if isinstance(item, dict) and item.get("id") == call["id"]]
                    if len(matching) != 1:
                        raise BindingError("candidate raw call identity is ambiguous")
                    raw_call = matching[0]
                strict_tool_call(call, raw_call)
                valid_calls.append(call)
            except (BindingError, ValueError, TypeError):
                if self.run.metadata.get("decision_cycle_policy") != "decision-cycle-1":
                    raise
        calls = valid_calls
        if self.run.metadata.get("decision_cycle_policy") == "decision-cycle-1":
            adapted = []
            for call in calls:
                args = call.get("args")
                if (
                    call.get("name") == "review_step"
                    and isinstance(args, dict)
                    and args.get("action") == "offer_candidate"
                    and isinstance(args.get("payload"), dict)
                    and isinstance(args["payload"].get("candidate"), dict)
                ):
                    call = {**call, "name": "submit_case", "args": args["payload"]["candidate"]}
                elif (
                    call.get("name") == "review_step"
                    and isinstance(args, dict)
                    and args.get("action") == "offer_candidate"
                    and isinstance(args.get("payload"), dict)
                    and isinstance(args["payload"].get("retract_draft"), dict)
                ):
                    call = {**call, "name": "retract_candidate_draft", "args": args["payload"]["retract_draft"]}
                adapted.append(call)
            calls = adapted
        return calls, phases["response"]

    def stage_candidate_batch(
        self, task_id: str, token: str, turn_id: str, *, now: float | None = None
    ) -> list[dict[str, Any]]:
        """Persist original candidates from one valid native batch before actions run."""
        if not self._candidate_draft_enabled():
            raise BindingError("candidate drafts are not enabled")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload FROM objects WHERE kind='Task' AND id=?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(task_id)
            task = Task(**json.loads(row[0]))
            self._guard_review_task(db, task_id, token, task.object_id, now)
            calls, response_hash = self._candidate_turn_calls(db, task_id, token, turn_id)
            created, result = False, []
            for call in calls:
                args = call.get("args")
                if call.get("name") != "submit_case" or not isinstance(args, dict):
                    continue
                call_id = call["id"]
                ident = (
                    "draft_"
                    + hashlib.sha256(
                        _json([self.run.id, task.object_id, task_id, turn_id, call_id]).encode()
                    ).hexdigest()
                )
                original = dict(args)
                current = {
                    key: value
                    for key, value in args.items()
                    if key
                    not in {
                        "review_state",
                        "draft_id",
                        "expected_revision",
                        "candidate_resolution",
                        "target_draft_id",
                        "target_expected_revision",
                    }
                }
                item = {
                    "id": ident,
                    "run_id": self.run.id,
                    "scope_id": task.object_id,
                    "origin_task_id": task_id,
                    "origin_turn_id": turn_id,
                    "origin_call_id": call_id,
                    "response_hash": response_hash,
                    "original_args": original,
                    "current_args": current,
                    "revision": 0,
                    "status": "pending_validation",
                    "errors": [],
                    "case_id": "",
                    "reason": "",
                    "requires_resolution": False,
                    "target_resolution": "unknown" if args.get("draft_id") else "new",
                }
                row = db.execute("SELECT payload FROM candidate_drafts WHERE id=?", (ident,)).fetchone()
                if row:
                    existing = json.loads(row[0])
                    if any(
                        existing.get(k) != item[k]
                        for k in (
                            "run_id",
                            "scope_id",
                            "origin_task_id",
                            "origin_turn_id",
                            "origin_call_id",
                            "response_hash",
                            "original_args",
                        )
                    ):
                        raise RevisionConflict("candidate native identity already has different input")
                    item = existing
                else:
                    db.execute(
                        "INSERT INTO candidate_drafts VALUES (?,?,0,?,?)",
                        (ident, task.object_id, item["status"], _json(item)),
                    )
                    self._event(db, "CandidateDraft", ident, "stage", item)
                    created = True
                result.append(item)
            if created:
                scope_row = db.execute(
                    "SELECT payload FROM objects WHERE kind='ReviewScope' AND id=?", (task.object_id,)
                ).fetchone()
                if scope_row is None:
                    raise BindingError("candidate responsibility is missing")
                scope = ReviewScope(**json.loads(scope_row[0]))
                if scope.status == "reviewed":
                    scope = replace(scope, status="pending", revision=scope.revision + 1)
                    task = replace(task, input_revision=scope.revision, revision=task.revision + 1)
                    db.execute(
                        "UPDATE objects SET revision=?,payload=? WHERE kind='ReviewScope' AND id=?",
                        (scope.revision, _json(asdict(scope)), scope.id),
                    )
                    db.execute(
                        "UPDATE objects SET revision=?,payload=? WHERE kind='Task' AND id=?",
                        (task.revision, _json(asdict(task)), task.id),
                    )
                    plan = self._unit_plan_db(db)
                    if scope.id != plan.integration_scope_id:
                        self._reopen_integration(db, plan)
                    self._event(
                        db,
                        "ReviewScope",
                        scope.id,
                        "candidate_draft_reopened",
                        {"task_id": task_id, "turn_id": turn_id},
                    )
            self._guard_review_task(db, task_id, token, task.object_id, now)
            db.commit()
            return result

    def settle_candidate_preflight_failure(
        self,
        task_id: str,
        token: str,
        turn_id: str,
        call_id: str,
        *,
        phase: str,
        errors: list[dict[str, Any]],
        now: float | None = None,
    ) -> dict[str, Any]:
        """Record a known pre-execution rejection without inventing target relationships.

        The controller calls this only for explicitly classified input errors, not
        for ambiguous execution, source corruption, storage errors or lease loss.
        """
        if self.run.metadata.get("decision_cycle_policy") != "decision-cycle-1":
            raise BindingError("candidate preflight settlement requires decision-cycle-1")
        if phase not in {"schema_validation", "decision_validation"}:
            raise ValueError("invalid candidate preflight phase")
        if (
            not isinstance(errors, list)
            or not errors
            or any(
                not isinstance(error, dict) or not isinstance(error.get("reason"), str) or not error["reason"].strip()
                for error in errors
            )
        ):
            raise ValueError("candidate preflight requires structured errors")
        request = {"phase": phase, "errors": errors}
        encoded_request = _json(request)
        operation_id = hashlib.sha256(_json([self.run.id, task_id, turn_id, call_id]).encode()).hexdigest()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload FROM objects WHERE kind='Task' AND id=?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(task_id)
            task = Task(**json.loads(row[0]))
            self._guard_review_task(db, task_id, token, task.object_id, now)
            calls, response_hash = self._candidate_turn_calls(db, task_id, token, turn_id)
            matches = [call for call in calls if call.get("id") == call_id and call.get("name") == "submit_case"]
            if len(matches) != 1 or not isinstance(matches[0].get("args"), dict):
                raise BindingError("candidate preflight does not match a unique native call")
            attempt_row = db.execute(
                "SELECT payload FROM decision_attempts WHERE task_id=? AND turn_id=? AND call_id=?",
                (task_id, turn_id, call_id),
            ).fetchone()
            if attempt_row is None:
                raise BindingError("candidate preflight has no audited decision attempt")
            decision_attempt = json.loads(attempt_row[0])
            decision_args = decision_attempt.get("args")
            response = json.loads((self.audit_dir / response_hash).read_bytes())
            native_matches = [
                call
                for message in response
                if isinstance(message, dict) and isinstance(message.get("data"), dict)
                for call in message["data"].get("tool_calls", [])
                if isinstance(call, dict) and call.get("id") == call_id and call.get("name") == "review_step"
            ]
            if (
                len(native_matches) != 1
                or native_matches[0].get("args") != decision_args
                or decision_attempt.get("response_hash") != response_hash
                or not isinstance(decision_args, dict)
                or decision_args.get("action") != "offer_candidate"
                or not isinstance(decision_args.get("payload"), dict)
                or decision_args["payload"].get("candidate") != matches[0]["args"]
            ):
                raise BindingError("candidate preflight attempt differs from audited native call")
            ident = (
                "draft_"
                + hashlib.sha256(_json([self.run.id, task.object_id, task_id, turn_id, call_id]).encode()).hexdigest()
            )
            item = self.get_candidate_draft(ident, db=db)
            if (
                item["scope_id"] != task.object_id
                or item["origin_task_id"] != task_id
                or item["origin_turn_id"] != turn_id
                or item["origin_call_id"] != call_id
                or item["response_hash"] != response_hash
                or item["original_args"] != matches[0]["args"]
            ):
                raise BindingError("candidate preflight draft binding differs")
            previous = db.execute(
                "SELECT request,result FROM candidate_draft_operations WHERE id=?", (operation_id,)
            ).fetchone()
            if previous:
                if previous[0] != encoded_request:
                    raise RevisionConflict("candidate preflight identity already has different input")
                return json.loads(previous[1])
            if db.execute(
                "SELECT 1 FROM decision_steps WHERE task_id=? AND turn_id=? AND call_id=?",
                (task_id, turn_id, call_id),
            ).fetchone():
                raise BindingError("candidate decision already staged; preflight settlement is not allowed")
            if item["revision"] != 0 or item["status"] != "pending_validation":
                raise RevisionConflict("candidate attempt revision or status changed")
            target_keys = (
                "draft_id",
                "expected_revision",
                "candidate_resolution",
                "target_draft_id",
                "target_expected_revision",
            )
            target_intent = any(
                key in item["original_args"] and item["original_args"][key] not in (None, "") for key in target_keys
            )
            outer = decision_args["payload"]
            outer_target = any(key in outer and outer[key] not in (None, "") for key in target_keys)
            target_intent = target_intent or outer_target
            target_hint: dict[str, Any] = {"status": "unknown"}
            original = item["original_args"]
            target_id, target_rev = original.get("draft_id"), original.get("expected_revision")
            if (
                isinstance(target_id, str)
                and target_id
                and type(target_rev) is int
                and target_id != ident
                and not outer_target
                and all(original.get(key) in (None, "") for key in target_keys[2:])
            ):
                try:
                    target = self.get_candidate_draft(target_id, db=db)
                except KeyError:
                    target = None
                if (
                    target is not None
                    and target["scope_id"] == task.object_id
                    and target["revision"] == target_rev
                    and target["status"] in {"pending_validation", "repair_needed"}
                    and not target["requires_resolution"]
                ):
                    target_hint = {"status": "known", "draft_id": target_id, "revision": target_rev}
            item.update(
                status="repair_needed",
                revision=1,
                errors=errors,
                requires_resolution=target_intent,
                target_resolution="unknown" if target_intent else "new",
                preflight_phase=phase,
                preflight_target=target_hint,
                original_decision_args=decision_args,
            )
            db.execute(
                "UPDATE candidate_drafts SET revision=?,status=?,payload=? WHERE id=?",
                (item["revision"], item["status"], _json(item), ident),
            )
            db.execute(
                "INSERT INTO candidate_draft_operations VALUES (?,?,?)", (operation_id, encoded_request, _json(item))
            )
            self._event(
                db,
                "CandidateDraft",
                ident,
                "preflight_rejected",
                {
                    "task_id": task_id,
                    "turn_id": turn_id,
                    "call_id": call_id,
                    "phase": phase,
                    "result": item,
                },
            )
            self._guard_review_task(db, task_id, token, task.object_id, now)
            db.commit()
            return item

    def submit_candidate_draft(
        self,
        task_id: str,
        token: str,
        draft_id: str,
        expected_revision: int,
        *,
        turn_id: str,
        call_id: str,
        args: dict[str, Any],
        now: float | None = None,
    ) -> dict[str, Any]:
        if not self._candidate_draft_enabled():
            raise BindingError("candidate drafts are not enabled")
        if type(expected_revision) is not int or expected_revision < 0 or not isinstance(args, dict):
            raise ValueError("candidate revision and arguments are invalid")
        operation_id = hashlib.sha256(_json([self.run.id, task_id, turn_id, call_id]).encode()).hexdigest()
        request = {"draft_id": draft_id, "expected_revision": expected_revision, "args": args}
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload FROM objects WHERE kind='Task' AND id=?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(task_id)
            task = Task(**json.loads(row[0]))
            self._guard_review_task(db, task_id, token, task.object_id, now)
            calls, _ = self._candidate_turn_calls(db, task_id, token, turn_id)
            matches = [call for call in calls if call.get("id") == call_id and call.get("name") == "submit_case"]
            if len(matches) != 1 or not isinstance(matches[0].get("args"), dict):
                raise BindingError("candidate submission does not match its native call")
            native_args = matches[0]["args"]
            business = {
                key: value
                for key, value in native_args.items()
                if key
                not in {
                    "review_state",
                    "draft_id",
                    "expected_revision",
                    "candidate_resolution",
                    "target_draft_id",
                    "target_expected_revision",
                }
            }
            if business != args:
                raise BindingError("candidate arguments differ from the native call")
            attempt = self.get_candidate_draft(draft_id, db=db)
            if attempt["scope_id"] != task.object_id or (
                attempt["origin_task_id"],
                attempt["origin_turn_id"],
                attempt["origin_call_id"],
            ) != (task_id, turn_id, call_id):
                raise BindingError("candidate attempt does not match this native call")
            previous = db.execute(
                "SELECT request,result FROM candidate_draft_operations WHERE id=?", (operation_id,)
            ).fetchone()
            if previous:
                if previous[0] != _json(request):
                    raise RevisionConflict("candidate operation has different inputs")
                return json.loads(previous[1])
            if attempt["revision"] != expected_revision or attempt["status"] != "pending_validation":
                raise RevisionConflict("candidate attempt revision or status changed")
            item = attempt
            source = None
            resolution_error = None
            force_new = False
            requested_id = native_args.get("draft_id")
            requested_rev = native_args.get("expected_revision")
            if requested_id:
                try:
                    if not isinstance(requested_id, str) or type(requested_rev) is not int:
                        raise ValueError("invalid_target")
                    source = self.get_candidate_draft(requested_id, db=db)
                    if source["scope_id"] != task.object_id or source["id"] == attempt["id"]:
                        raise ValueError("unknown_or_wrong_scope")
                    if source["revision"] != requested_rev or source["status"] not in {
                        "pending_validation",
                        "repair_needed",
                    }:
                        raise ValueError("target_revision_or_status_changed")
                    item = source
                    if source.get("requires_resolution"):
                        resolution = native_args.get("candidate_resolution")
                        if resolution == "new_case":
                            if (
                                native_args.get("target_draft_id") is not None
                                or native_args.get("target_expected_revision") is not None
                            ):
                                raise ValueError("new_case_cannot_target_draft")
                            force_new = True
                        elif resolution == "apply_to_draft":
                            target_id = native_args.get("target_draft_id")
                            target_rev = native_args.get("target_expected_revision")
                            if (
                                not isinstance(target_id, str)
                                or type(target_rev) is not int
                                or target_id == source["id"]
                            ):
                                raise ValueError("invalid_explicit_target")
                            item = self.get_candidate_draft(target_id, db=db)
                            if (
                                item["scope_id"] != task.object_id
                                or item["revision"] != target_rev
                                or item["status"] not in {"pending_validation", "repair_needed"}
                            ):
                                raise ValueError("explicit_target_unavailable_or_changed")
                            if item.get("requires_resolution"):
                                raise ValueError("explicit_target_requires_resolution")
                        else:
                            raise ValueError("explicit_resolution_required")
                    elif any(
                        native_args.get(key) is not None
                        for key in ("candidate_resolution", "target_draft_id", "target_expected_revision")
                    ):
                        raise ValueError("unexpected_resolution")
                except (KeyError, ValueError) as exc:
                    resolution_error = str(exc).strip("'") or "unknown_target"
                    source = None
                    item = attempt
            elif requested_id is not None and requested_id != "" or requested_rev is not None:
                resolution_error = "invalid_target"
            elif any(
                native_args.get(key) is not None
                for key in ("candidate_resolution", "target_draft_id", "target_expected_revision")
            ):
                resolution_error = "unexpected_resolution"
            errors = []
            if resolution_error:
                attempt["requires_resolution"] = True
                attempt["target_resolution"] = "unknown"
                errors.append({"field": "draft_id", "reason": resolution_error})
            if "review_state" in native_args:
                try:
                    scope_row = db.execute(
                        "SELECT payload FROM objects WHERE kind='ReviewScope' AND id=?", (task.object_id,)
                    ).fetchone()
                    scope = ReviewScope(**json.loads(scope_row[0]))
                    state = native_args["review_state"]
                    self._validate_refs(db, replace(scope, working_state=state))
                    updates = [("question", text, []) for text in state["open_questions"]]
                    for observation in state["observations"]:
                        if not observation["evidence_refs"]:
                            raise BindingError("review_state observation has no source")
                        updates.append(("observation", observation["statement"], observation["evidence_refs"]))
                    for kind, value, refs in updates:
                        fingerprint = self._navigation_fingerprint(scope.id, kind, value, refs)
                        entry = db.execute(
                            "SELECT o.payload FROM navigation_aliases n JOIN objects o "
                            "ON o.kind='NavigationItem' AND o.id=n.item_id WHERE n.scope_id=? AND n.fingerprint=?",
                            (scope.id, fingerprint),
                        ).fetchone()
                        if entry is None:
                            raise BindingError("review_state has not been merged")
                        nav = NavigationItem(**json.loads(entry[0]))
                        if self._navigation_fingerprint(scope.id, nav.kind, nav.text, nav.evidence_refs) != fingerprint:
                            raise BindingError("review_state navigation was corrected")
                except (BindingError, KeyError, TypeError, ValueError) as exc:
                    errors.append({"field": "review_state", "reason": str(exc) or "invalid_review_state"})
            allowed = {"trigger", "behavior", "impact", "evidence_refs", "case_id", "open_questions"}
            if set(args) - allowed:
                errors.append({"field": "arguments", "reason": "unknown_fields", "fields": sorted(set(args) - allowed)})
            for field in ("trigger", "behavior", "impact"):
                if not isinstance(args.get(field), str) or not args[field].strip():
                    errors.append({"field": field, "reason": "nonempty_string_required"})
            submitted_refs = args.get("evidence_refs")
            if not isinstance(submitted_refs, list) or not submitted_refs:
                errors.append({"field": "evidence_refs", "reason": "nonempty_list_required"})
                submitted_refs = []
            case_id = args.get("case_id", "")
            if not isinstance(case_id, str):
                errors.append({"field": "case_id", "reason": "invalid_case_id"})
                case_id = ""
            target = None
            if force_new and case_id:
                errors.append({"field": "case_id", "reason": "new_case_cannot_target_existing_case"})
            if case_id and not resolution_error and not force_new:
                case_row = db.execute("SELECT payload FROM objects WHERE kind='Case' AND id=?", (case_id,)).fetchone()
                if case_row is None:
                    errors.append({"field": "case_id", "value": case_id, "reason": "unknown_case"})
                else:
                    target = Case(**json.loads(case_row[0]))
                    if target.run_id != self.run.id or target.scope_id != task.object_id:
                        errors.append({"field": "case_id", "value": case_id, "reason": "wrong_scope"})
                        target = None
            elif (
                not force_new
                and not resolution_error
                and isinstance(args.get("trigger"), str)
                and isinstance(args.get("behavior"), str)
            ):
                candidates = [
                    Case(**json.loads(row[0])) for row in db.execute("SELECT payload FROM objects WHERE kind='Case'")
                ]
                candidates = [
                    case
                    for case in candidates
                    if case.run_id == self.run.id
                    and case.scope_id == task.object_id
                    and case.trigger == args["trigger"].strip()
                    and case.mechanism == args["behavior"].strip()
                ]
                if len(candidates) > 1:
                    errors.append({"field": "case_id", "reason": "ambiguous_existing_case"})
                elif candidates:
                    target = candidates[0]
            if target and isinstance(args.get("trigger"), str) and target.trigger != args["trigger"].strip():
                errors.append({"field": "trigger", "reason": "existing_trigger_immutable"})
            question_ids = args.get("open_questions", [])
            if not isinstance(question_ids, list):
                errors.append({"field": "open_questions", "reason": "list_required"})
                question_ids = []
            for qid in question_ids:
                qrow = (
                    db.execute("SELECT payload FROM objects WHERE kind='Question' AND id=?", (qid,)).fetchone()
                    if isinstance(qid, str)
                    else None
                )
                if qrow is None or Question(**json.loads(qrow[0])).scope_id != task.object_id:
                    errors.append({"field": "open_questions", "value": qid, "reason": "unknown_or_wrong_scope"})
            all_refs = list(submitted_refs)
            if target:
                all_refs.extend(target.evidence_refs + target.counter_evidence_refs)
            decision_cycle = self.run.metadata.get("decision_cycle_policy") == "decision-cycle-1"
            verified_refs: dict[str, EvidenceRef] = {}
            verified_content: dict[str, str] = {}
            delivered_refs: dict[str, list[tuple[int, int]]] = {}
            if decision_cycle:
                # Validate every existing reference before reporting any model-input
                # error; unknown IDs must not mask a corrupt or foreign sibling.
                existing_ids = sorted(
                    {
                        ref_id
                        for ref_id in all_refs
                        if isinstance(ref_id, str)
                        and ref_id
                        and db.execute("SELECT 1 FROM objects WHERE kind='EvidenceRef' AND id=?", (ref_id,)).fetchone()
                        is not None
                    }
                )
                for ref in self._unit_refs(db, existing_ids):
                    identity = asdict(ref)
                    ref_id = identity.pop("id")
                    if ref_id != "ev_" + hashlib.sha256(_json(identity).encode()).hexdigest():
                        raise BindingError("candidate evidence identity mismatch")
                    verified_refs[ref_id] = ref
                    verified_content[ref_id] = (self.blob_dir / ref.content_hash).read_bytes().decode("utf-8")
                    if ref.kind in {"source", "documentation", "issue", "test"} and (
                        ref.metadata.get("navigation_only") is not True
                    ):
                        self._validate_question_original(ref)
                delivered_refs = self._require_delivered(
                    db, task_id, token, turn_id, set(existing_ids), required_ref_ids=set()
                )
            seen = set()
            frontier = self._task_frontier_refs(db, task_id)
            for ref_id in all_refs:
                if not isinstance(ref_id, str) or not ref_id:
                    errors.append({"field": "evidence_refs", "ref": ref_id, "reason": "invalid_id"})
                    continue
                if ref_id in seen:
                    continue
                seen.add(ref_id)
                if db.execute("SELECT 1 FROM objects WHERE kind='EvidenceRef' AND id=?", (ref_id,)).fetchone() is None:
                    errors.append({"field": "evidence_refs", "ref": ref_id, "reason": "unknown_id"})
                    continue
                if decision_cycle:
                    ref = verified_refs[ref_id]
                    path = (
                        f"payload.candidate.evidence_refs[{submitted_refs.index(ref_id)}]"
                        if ref_id in submitted_refs
                        else "existing_case_evidence"
                    )
                    reason = ""
                    if ref.kind not in {"source", "documentation", "issue", "test"} or (
                        ref.metadata.get("navigation_only") is True
                    ):
                        reason = "navigation_only"
                    elif ref_id not in frontier:
                        reason = "not_read_by_task"
                    elif ref_id not in delivered_refs:
                        reason = "not_delivered"
                    else:
                        content = verified_content[ref_id]
                        first = ref.start_line or 1
                        last = first + len(source_lines(self.run, content)) - 1
                        next_line = first
                        for start, end in sorted(delivered_refs[ref_id]):
                            if start <= next_line <= end:
                                next_line = end + 1
                        if next_line <= last:
                            reason = "not_fully_delivered"
                    if reason:
                        errors.append(
                            {
                                "field": "evidence_refs",
                                "ref": ref_id,
                                "reason": reason,
                                "kind": ref.kind,
                                "path": path,
                            }
                        )
                    continue
                try:
                    ref = self._unit_refs(db, [ref_id])[0]
                    self._validate_question_original(ref)
                    if ref_id not in frontier:
                        raise BindingError("source was not read by this task")
                    spans = self._require_delivered(db, task_id, token, turn_id, {ref_id})[ref_id]
                    content = (self.blob_dir / ref.content_hash).read_bytes().decode("utf-8")
                    first = ref.start_line or 1
                    last = first + len(source_lines(self.run, content)) - 1
                    next_line = first
                    for start, end in sorted(spans):
                        if start <= next_line <= end:
                            next_line = end + 1
                    if next_line <= last:
                        raise BindingError("source was not fully delivered")
                except (BindingError, KeyError, OSError, UnicodeError, ValueError) as exc:
                    errors.append({"field": "evidence_refs", "ref": ref_id, "reason": str(exc) or "invalid_source"})
            if errors:
                item.update(current_args=args, errors=errors, status="repair_needed", revision=item["revision"] + 1)
            else:
                trigger, behavior, impact = (args[field].strip() for field in ("trigger", "behavior", "impact"))
                claim_id = "claim_" + hashlib.sha256(_json([item["id"], behavior, impact]).encode()).hexdigest()
                if target:
                    claims = list(target.claims)
                    if not any(
                        claim.get("statement") == behavior and claim.get("impact") == impact for claim in claims
                    ):
                        claims.append({"id": claim_id, "statement": behavior, "impact": impact})
                    refs = list(dict.fromkeys(target.evidence_refs + submitted_refs))
                    questions = list(dict.fromkeys(target.question_ids + question_ids))
                    if claims == target.claims and refs == target.evidence_refs and questions == target.question_ids:
                        updated = target
                    else:
                        updated = self.update_unit_object(
                            task_id,
                            token,
                            Case,
                            target.id,
                            target.revision,
                            turn_id=turn_id,
                            now=now,
                            _db=db,
                            claims=claims,
                            evidence_refs=refs,
                            question_ids=questions,
                            status="candidate",
                        )
                else:
                    ident = "case_" + hashlib.sha256(_json([self.run.id, item["id"]]).encode()).hexdigest()
                    new_case = Case(
                        ident,
                        self.run.id,
                        scope_id=task.object_id,
                        trigger=trigger,
                        mechanism=behavior,
                        claims=[{"id": claim_id, "statement": behavior, "impact": impact}],
                        evidence_refs=submitted_refs,
                        question_ids=question_ids,
                    )
                    self.commit_unit_decision(
                        task_id,
                        token,
                        task.input_revision,
                        "decision_" + operation_id,
                        checked_region_ids=[],
                        observations=[],
                        questions=[],
                        cases=[new_case],
                        unresolved=[],
                        turn_id=turn_id,
                        now=now,
                        _db=db,
                    )
                    updated = new_case
                item.update(
                    current_args=args,
                    errors=[],
                    status="promoted",
                    revision=item["revision"] + 1,
                    case_id=updated.id,
                    case_revision=updated.revision,
                )
            db.execute(
                "UPDATE candidate_drafts SET revision=?,status=?,payload=? WHERE id=?",
                (item["revision"], item["status"], _json(item), item["id"]),
            )
            if source is not None and source["id"] != item["id"]:
                source.update(status="applied", revision=source["revision"] + 1, linked_draft_id=item["id"])
                db.execute(
                    "UPDATE candidate_drafts SET revision=?,status=?,payload=? WHERE id=?",
                    (source["revision"], source["status"], _json(source), source["id"]),
                )
                self._event(db, "CandidateDraft", source["id"], "applied", {"target": item["id"], "result": source})
            if attempt["id"] != item["id"]:
                attempt.update(status="applied", revision=attempt["revision"] + 1, linked_draft_id=item["id"])
                db.execute(
                    "UPDATE candidate_drafts SET revision=?,status=?,payload=? WHERE id=?",
                    (attempt["revision"], attempt["status"], _json(attempt), attempt["id"]),
                )
                self._event(db, "CandidateDraft", attempt["id"], "applied", {"target": item["id"], "result": attempt})
            db.execute(
                "INSERT INTO candidate_draft_operations VALUES (?,?,?)", (operation_id, _json(request), _json(item))
            )
            self._event(
                db,
                "CandidateDraft",
                draft_id,
                "submit",
                {"task_id": task_id, "turn_id": turn_id, "call_id": call_id, "result": item},
            )
            if self.run.metadata.get("decision_cycle_policy") == "decision-cycle-1":
                decision_row = db.execute(
                    "SELECT id,payload FROM decision_steps WHERE task_id=? AND turn_id=? AND call_id=?",
                    (task_id, turn_id, call_id),
                ).fetchone()
                if decision_row is None:
                    raise BindingError("candidate decision must be staged before promotion")
                step = json.loads(decision_row[1])
                if step.get("action") != "offer_candidate":
                    raise BindingError("candidate decision action mismatch")
                step.update(status="settled", disposition=item["status"], candidate_draft_id=item["id"])
                if item["status"] == "promoted":
                    step["case_id"] = item["case_id"]
                    db.execute(
                        "INSERT INTO decision_verification_queue VALUES (?,?,?) "
                        "ON CONFLICT(case_id) DO UPDATE SET step_id=excluded.step_id,"
                        "case_revision=excluded.case_revision",
                        (item["case_id"], decision_row[0], item["case_revision"]),
                    )
                db.execute("UPDATE decision_steps SET payload=? WHERE id=?", (_json(step), decision_row[0]))
                self._event(db, "DecisionStep", decision_row[0], "candidate_disposition", step)
            self._guard_review_task(db, task_id, token, task.object_id, now)
            db.commit()
            return item

    def retract_candidate_draft(
        self,
        task_id: str,
        token: str,
        draft_id: str,
        expected_revision: int,
        *,
        turn_id: str,
        call_id: str,
        reason: str,
        now: float | None = None,
    ) -> dict[str, Any]:
        if not self._candidate_draft_enabled():
            raise BindingError("candidate drafts are not enabled")
        if (
            type(expected_revision) is not int
            or expected_revision < 0
            or not isinstance(reason, str)
            or not reason.strip()
        ):
            raise ValueError("candidate withdrawal requires revision and reason")
        operation_id = hashlib.sha256(_json([self.run.id, task_id, turn_id, call_id]).encode()).hexdigest()
        request = {"draft_id": draft_id, "expected_revision": expected_revision, "reason": reason}
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload FROM objects WHERE kind='Task' AND id=?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(task_id)
            task = Task(**json.loads(row[0]))
            self._guard_review_task(db, task_id, token, task.object_id, now)
            calls, _ = self._candidate_turn_calls(db, task_id, token, turn_id)
            matches = [
                call for call in calls if call.get("id") == call_id and call.get("name") == "retract_candidate_draft"
            ]
            if (
                len(matches) != 1
                or not isinstance(matches[0].get("args"), dict)
                or {key: value for key, value in matches[0]["args"].items() if key != "review_state"} != request
            ):
                raise BindingError("candidate withdrawal does not match its native call")
            item = self.get_candidate_draft(draft_id, db=db)
            if item["scope_id"] != task.object_id:
                raise BindingError("candidate draft belongs to another scope")
            previous = db.execute(
                "SELECT request,result FROM candidate_draft_operations WHERE id=?", (operation_id,)
            ).fetchone()
            if previous:
                if previous[0] != _json(request):
                    raise RevisionConflict("candidate operation has different inputs")
                return json.loads(previous[1])
            if item["revision"] != expected_revision or item["status"] not in {"pending_validation", "repair_needed"}:
                raise RevisionConflict("candidate draft revision or status changed")
            item.update(status="retracted", reason=reason, revision=item["revision"] + 1)
            db.execute(
                "UPDATE candidate_drafts SET revision=?,status=?,payload=? WHERE id=?",
                (item["revision"], item["status"], _json(item), draft_id),
            )
            db.execute(
                "INSERT INTO candidate_draft_operations VALUES (?,?,?)", (operation_id, _json(request), _json(item))
            )
            self._event(
                db,
                "CandidateDraft",
                draft_id,
                "retract",
                {"task_id": task_id, "turn_id": turn_id, "call_id": call_id, "reason": reason},
            )
            if self.run.metadata.get("decision_cycle_policy") == "decision-cycle-1":
                decision_row = db.execute(
                    "SELECT id,payload FROM decision_steps WHERE task_id=? AND turn_id=? AND call_id=?",
                    (task_id, turn_id, call_id),
                ).fetchone()
                if decision_row is None:
                    raise BindingError("candidate withdrawal decision must be staged")
                step = json.loads(decision_row[1])
                if step.get("action") != "offer_candidate":
                    raise BindingError("candidate withdrawal action mismatch")
                step.update(status="settled", disposition="retracted", candidate_draft_id=draft_id)
                db.execute("UPDATE decision_steps SET payload=? WHERE id=?", (_json(step), decision_row[0]))
                self._event(db, "DecisionStep", decision_row[0], "candidate_withdrawal", step)
            self._guard_review_task(db, task_id, token, task.object_id, now)
            db.commit()
            return item

    def _question_input_digest(self, db: sqlite3.Connection, question: Question) -> str:
        """Fingerprint substantive delegated inputs, excluding routine scope/lease updates."""
        plan = self._unit_plan_db(db)
        if question.scope_id not in {*plan.primary_scope_ids, plan.integration_scope_id}:
            raise BindingError("question scope is outside the fixed unit plan")
        row = db.execute(
            "SELECT payload FROM objects WHERE kind='ReviewScope' AND id=?", (question.scope_id,)
        ).fetchone()
        if row is None:
            raise BindingError("question responsibility is missing")
        scope = ReviewScope(**json.loads(row[0]))
        if scope.run_id != self.run.id or scope.status == "superseded":
            raise BindingError("question responsibility is not active")

        def identities(ids: list[str]) -> list[Any]:
            return sorted({_json(_unit_source_identity(ref)) for ref in self._unit_refs(db, ids)})

        cases = []
        for (payload,) in db.execute("SELECT payload FROM objects WHERE kind='Case'"):
            case = Case(**json.loads(payload))
            if question.id not in case.question_ids and case.id != question.case_id:
                continue
            if case.run_id != self.run.id or case.scope_id not in {*plan.primary_scope_ids, plan.integration_scope_id}:
                raise BindingError("question case binding is invalid")
            cases.append(
                {
                    "id": case.id,
                    "scope_id": case.scope_id,
                    "trigger": case.trigger,
                    "mechanism": case.mechanism,
                    "claims": case.claims,
                    "status": case.status,
                    "question_ids": sorted(case.question_ids),
                    "evidence": identities(case.evidence_refs),
                    "counter_evidence": identities(case.counter_evidence_refs),
                }
            )
        if question.case_id and not any(case["id"] == question.case_id for case in cases):
            raise BindingError("question's linked case is missing")
        pending = []
        for sid, rid, ref_id in db.execute("SELECT scope_id,region_id,ref_id FROM unit_pending_sources"):
            if sid == question.scope_id or question.scope_id == plan.integration_scope_id:
                pending.append([sid, rid, *identities([ref_id])])
        pending = sorted({_json(item) for item in pending})
        if (
            db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='shared_investigations'").fetchone()
            is None
        ):
            raise BindingError("shared investigation directory is unavailable")
        investigations = []
        for (payload,) in db.execute("SELECT payload FROM shared_investigations"):
            item = json.loads(payload)
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("scope_ids"), list)
                or not item["scope_ids"]
                or any(
                    not isinstance(sid, str) or sid not in {*plan.primary_scope_ids, plan.integration_scope_id}
                    for sid in item["scope_ids"]
                )
            ):
                raise BindingError("shared investigation association is unknown")
            if question.scope_id != plan.integration_scope_id and question.scope_id not in item["scope_ids"]:
                continue
            status = item.get("status")
            if status not in {"open", "partial", "unavailable", "resolved", "retracted"}:
                raise BindingError("shared investigation status is unknown")
            observations = item.get("observations")
            regions = item.get("region_ids")
            if (
                not isinstance(item.get("id"), str)
                or not isinstance(regions, list)
                or not regions
                or any(not isinstance(region, str) or not region for region in regions)
                or not isinstance(observations, list)
                or any(
                    not isinstance(observation, dict) or not isinstance(observation.get("evidence_refs"), list)
                    for observation in observations
                )
            ):
                raise BindingError("shared investigation dependencies are unknown")
            permitted_regions = set()
            for sid in item["scope_ids"]:
                scope_row = db.execute(
                    "SELECT payload FROM objects WHERE kind='ReviewScope' AND id=?", (sid,)
                ).fetchone()
                if scope_row is None:
                    raise BindingError("shared investigation responsibility is missing")
                related_scope = ReviewScope(**json.loads(scope_row[0]))
                if related_scope.run_id != self.run.id or not isinstance(related_scope.regions, list):
                    raise BindingError("shared investigation responsibility is invalid")
                for region in related_scope.regions:
                    if not isinstance(region, dict) or not isinstance(region.get("id"), str):
                        raise BindingError("shared investigation region is invalid")
                    permitted_regions.add(region["id"])
            if not set(regions) <= permitted_regions:
                raise BindingError("shared investigation region is outside its responsibilities")
            refs = [ref for observation in observations for ref in observation["evidence_refs"]]
            investigations.append(
                {
                    "id": item["id"],
                    "scope_ids": sorted(item["scope_ids"]),
                    "region_ids": sorted(regions),
                    "status": status,
                    "evidence": identities(refs),
                }
            )
        obligations = []
        for (payload,) in db.execute("SELECT payload FROM integration_obligations"):
            item = json.loads(payload)
            unit_ids = item.get("unit_ids") if isinstance(item, dict) else None
            if not isinstance(unit_ids, list) or any(
                not isinstance(sid, str) or sid not in plan.primary_scope_ids for sid in unit_ids
            ):
                raise BindingError("integration obligation association is unknown")
            if question.scope_id != plan.integration_scope_id and unit_ids and question.scope_id not in unit_ids:
                continue
            if (
                not isinstance(item.get("id"), str)
                or item.get("disposition") not in {"open", "checked", "unresolved"}
                or not isinstance(item.get("evidence_refs"), list)
                or not isinstance(item.get("origin_evidence_refs"), list)
            ):
                raise BindingError("integration obligation state is unknown")
            obligations.append(
                {
                    "id": item["id"],
                    "kind": item.get("kind"),
                    "unit_ids": sorted(unit_ids),
                    "disposition": item["disposition"],
                    "evidence": identities(item["evidence_refs"]),
                    "origin_evidence": identities(item["origin_evidence_refs"]),
                }
            )
        binding = {
            "run": [self.run.id, self.run.repo, self.run.base, self.run.head],
            "plan": asdict(plan),
            "scope": {"id": scope.id, "regions": scope.regions, "dependencies": scope.dependencies},
            "question": asdict(question),
            "cases": sorted(cases, key=lambda item: item["id"]),
            "pending_sources": pending,
            "investigations": sorted(investigations, key=lambda item: item["id"]),
            "obligations": sorted(obligations, key=lambda item: item["id"]),
        }
        return hashlib.sha256(_json(binding).encode("utf-8")).hexdigest()

    def _guard_question_task(
        self, db: sqlite3.Connection, task_id: str, token: str, now: float | None
    ) -> tuple[Task, Question, str]:
        if not self._question_policy_enabled():
            raise BindingError("audited question delegation is not enabled")
        now = time.time() if now is None else now
        if not math.isfinite(now):
            raise ValueError("time must be finite")
        row = db.execute("SELECT payload FROM objects WHERE kind='Task' AND id=?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(task_id)
        task = Task(**json.loads(row[0]))
        if (
            task.run_id != self.run.id
            or task.kind != "question"
            or task.status != "leased"
            or not token
            or task.lease_token != token
            or task.lease_expires <= now
        ):
            raise LeaseError("a current delegated question lease is required")
        row = db.execute(
            "SELECT scope_id,scope_revision FROM question_delegations WHERE task_id=?", (task_id,)
        ).fetchone()
        if row is None:
            raise BindingError("question task has no audited delegation")
        scope_id, scope_revision = row
        qrow = db.execute("SELECT payload FROM objects WHERE kind='Question' AND id=?", (task.object_id,)).fetchone()
        srow = db.execute("SELECT revision FROM objects WHERE kind='ReviewScope' AND id=?", (scope_id,)).fetchone()
        if qrow is None or srow is None:
            raise BindingError("question delegation target is missing")
        question = Question(**json.loads(qrow[0]))
        if question.run_id != self.run.id or question.scope_id != scope_id:
            raise BindingError("question delegation scope or run changed")
        if question.revision != task.input_revision:
            raise RevisionConflict("question delegation inputs are stale")
        if self.run.metadata.get("question_fencing_policy") == "question-input-1":
            expected = db.execute("SELECT digest FROM question_input_bindings WHERE task_id=?", (task_id,)).fetchone()
            try:
                actual = self._question_input_digest(db, question)
            except (BindingError, KeyError, OSError, ValueError, TypeError) as exc:
                raise RevisionConflict("question delegation dependencies are unavailable") from exc
            if expected is None or expected[0] != actual:
                raise RevisionConflict("question delegation inputs are stale")
        elif srow[0] != scope_revision:
            raise RevisionConflict("question delegation inputs are stale")
        return task, question, scope_id

    def _guard_frontier_or_audit_task(self, db: sqlite3.Connection, task: Task, token: str, now: float | None) -> str:
        if task.kind == "question":
            return self._guard_question_task(db, task.id, token, now)[2]
        self._guard_review_task(db, task.id, token, task.object_id, now)
        return task.object_id

    def assert_question_task_current(self, task_id: str, token: str, *, now: float | None = None) -> Question:
        with self._connect() as db:
            return self._guard_question_task(db, task_id, token, now)[1]

    def settle_question_task_stale(
        self, task_id: str, token: str, result: dict[str, Any], *, now: float | None = None
    ) -> Task:
        if not self._question_policy_enabled():
            raise BindingError("audited question delegation is not enabled")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload FROM objects WHERE kind='Task' AND id=?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(task_id)
            task = Task(**json.loads(row[0]))
            clock = time.time() if now is None else now
            if (
                not math.isfinite(clock)
                or task.kind != "question"
                or task.status != "leased"
                or not token
                or task.lease_token != token
                or task.lease_expires <= clock
            ):
                raise LeaseError("a current question lease is required")
            try:
                self._guard_question_task(db, task_id, token, now)
            except RevisionConflict:
                pass
            else:
                raise RevisionConflict("question task is not stale")
            task = replace(task, status="superseded", result=result, revision=task.revision + 1)
            db.execute(
                "UPDATE objects SET revision=?,payload=? WHERE kind='Task' AND id=?",
                (task.revision, _json(asdict(task)), task.id),
            )
            self._event(db, "Task", task.id, "supersede", result)
            db.commit()
            return task

    def supersede_stale_question_task(self, task_id: str, *, now: float | None = None) -> Task:
        """Settle an unowned stale delegation; its unfinished audit remains visible."""
        if not self._question_policy_enabled():
            raise BindingError("audited question delegation is not enabled")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload FROM objects WHERE kind='Task' AND id=?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(task_id)
            task = Task(**json.loads(row[0]))
            clock = time.time() if now is None else now
            if not math.isfinite(clock) or task.kind != "question" or task.run_id != self.run.id:
                raise BindingError("invalid question task")
            if task.status == "leased" and task.lease_expires > clock:
                raise LeaseError("question task is still leased")
            if task.status not in {"pending", "leased"}:
                raise LeaseError("question task is already settled")
            delegation = db.execute(
                "SELECT scope_id,scope_revision FROM question_delegations WHERE task_id=?", (task_id,)
            ).fetchone()
            qrow = db.execute(
                "SELECT revision FROM objects WHERE kind='Question' AND id=?", (task.object_id,)
            ).fetchone()
            if delegation is None or qrow is None:
                raise BindingError("question delegation binding is missing")
            scope = db.execute(
                "SELECT revision FROM objects WHERE kind='ReviewScope' AND id=?", (delegation[0],)
            ).fetchone()
            if scope is None:
                raise BindingError("question scope is missing")
            inputs_current = qrow[0] == task.input_revision and scope[0] == delegation[1]
            if self.run.metadata.get("question_fencing_policy") == "question-input-1":
                payload = db.execute(
                    "SELECT payload FROM objects WHERE kind='Question' AND id=?", (task.object_id,)
                ).fetchone()
                expected = db.execute(
                    "SELECT digest FROM question_input_bindings WHERE task_id=?", (task.id,)
                ).fetchone()
                inputs_current = (
                    qrow[0] == task.input_revision
                    and expected is not None
                    and expected[0] == self._question_input_digest(db, Question(**json.loads(payload[0])))
                )
            if inputs_current:
                raise RevisionConflict("question task is not stale")
            task = replace(
                task,
                status="superseded",
                lease_token="",
                lease_expires=0,
                revision=task.revision + 1,
                result={"reason": "delegation inputs changed"},
            )
            db.execute(
                "UPDATE objects SET revision=?,payload=? WHERE kind='Task' AND id=?",
                (task.revision, _json(asdict(task)), task.id),
            )
            self._event(db, "Task", task.id, "supersede", task.result)
            db.commit()
            return task

    def create_question_task(
        self,
        review_task_id: str,
        token: str,
        question_id: str,
        expected_revision: int,
        *,
        turn_id: str,
        tool_call_id: str,
        instruction: str,
        now: float | None = None,
    ) -> Task:
        """Create exactly one task for a controller-identified native delegation call."""
        if not self._question_policy_enabled():
            raise BindingError("audited question delegation is not enabled")
        if not all(isinstance(x, str) and x.strip() for x in (question_id, turn_id, tool_call_id, instruction)):
            raise ValueError("question, turn, call and instruction are required")
        request = {"question_id": question_id, "reason": instruction}
        task_id = (
            "question_"
            + hashlib.sha256(_json([self.run.id, review_task_id, turn_id, tool_call_id]).encode()).hexdigest()
        )
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload FROM objects WHERE kind='Question' AND id=?", (question_id,)).fetchone()
            if row is None:
                raise KeyError(question_id)
            question = Question(**json.loads(row[0]))
            self._guard_review_task(db, review_task_id, token, question.scope_id, now)
            self._require_delivered(db, review_task_id, token, turn_id, set())
            digest = db.execute(
                "SELECT content_hash FROM turn_audit WHERE task_id=? AND turn_id=? AND phase='response'",
                (review_task_id, turn_id),
            ).fetchone()[0]
            raw = (self.audit_dir / digest).read_bytes()
            if hashlib.sha256(raw).hexdigest() != digest:
                raise BindingError("turn audit blob hash mismatch")
            response = json.loads(raw)
            calls = [
                call
                for message in response
                if isinstance(message, dict) and isinstance(message.get("data"), dict)
                for call in message["data"].get("tool_calls", [])
                if isinstance(call, dict)
            ]
            ids = [call.get("id") for call in calls]
            matching = [
                call for call in calls if call.get("id") == tool_call_id and call.get("name") == "delegate_question"
            ]
            if (
                any(not isinstance(i, str) or not i.strip() for i in ids)
                or len(ids) != len(set(ids))
                or len(matching) != 1
                or not isinstance(matching[0].get("args"), dict)
                or {k: v for k, v in matching[0]["args"].items() if k != "review_state"} != request
            ):
                raise BindingError("delegation must match an unambiguous native call")
            if "review_state" in matching[0]["args"]:
                state = matching[0]["args"]["review_state"]
                scope_row = db.execute(
                    "SELECT payload FROM objects WHERE kind='ReviewScope' AND id=?", (question.scope_id,)
                ).fetchone()
                scope = ReviewScope(**json.loads(scope_row[0]))
                self._validate_refs(db, replace(scope, working_state=state))
                updates = [("question", text, []) for text in state["open_questions"]]
                for observation in state["observations"]:
                    if not observation["evidence_refs"]:
                        raise BindingError("unmerged review_state observation")
                    updates.append(("observation", observation["statement"], observation["evidence_refs"]))
                for kind, value, refs in updates:
                    fingerprint = self._navigation_fingerprint(scope.id, kind, value, refs)
                    item = db.execute(
                        "SELECT o.payload FROM navigation_aliases n JOIN objects o "
                        "ON o.kind='NavigationItem' AND o.id=n.item_id WHERE n.scope_id=? AND n.fingerprint=?",
                        (scope.id, fingerprint),
                    ).fetchone()
                    if item is None:
                        raise BindingError("review_state has not been merged")
                    navigation = NavigationItem(**json.loads(item[0]))
                    if (
                        self._navigation_fingerprint(
                            scope.id, navigation.kind, navigation.text, navigation.evidence_refs
                        )
                        != fingerprint
                    ):
                        raise BindingError("review_state navigation was corrected")
            native_row = db.execute(
                "SELECT content_hash FROM turn_audit WHERE task_id=? AND turn_id=? AND phase='native'",
                (review_task_id, turn_id),
            ).fetchone()
            raw_calls = None
            if native_row:
                native_raw = (self.audit_dir / native_row[0]).read_bytes()
                if hashlib.sha256(native_raw).hexdigest() != native_row[0]:
                    raise BindingError("turn audit blob hash mismatch")
                native = json.loads(native_raw)
                if (
                    not isinstance(native, list)
                    or len(native) != 1
                    or not isinstance(native[0], dict)
                    or not isinstance(native[0].get("invalid_tool_calls"), list)
                    or native[0]["invalid_tool_calls"]
                    or native[0].get("protocol_errors")
                ):
                    raise BindingError("invalid native calls")
                raw_calls = native[0].get("raw_tool_calls")
                if raw_calls is not None and (not isinstance(raw_calls, list) or len(raw_calls) != len(calls)):
                    raise BindingError("native call count differs")
            from reviewforge.engine.native_submission import strict_tool_call

            for call in calls:
                raw_call = None
                if raw_calls is not None:
                    matches = [item for item in raw_calls if isinstance(item, dict) and item.get("id") == call["id"]]
                    if len(matches) != 1:
                        raise BindingError("ambiguous native call identities")
                    raw_call = matches[0]
                strict_tool_call(call, raw_call)
            existing = db.execute("SELECT request FROM question_delegations WHERE task_id=?", (task_id,)).fetchone()
            if existing:
                if existing[0] != _json({**request, "expected_revision": expected_revision}):
                    raise RevisionConflict("delegation call already has different inputs")
                return Task(
                    **json.loads(
                        db.execute("SELECT payload FROM objects WHERE kind='Task' AND id=?", (task_id,)).fetchone()[0]
                    )
                )
            if question.run_id != self.run.id or question.status != "open" or question.revision != expected_revision:
                raise RevisionConflict("delegated question is not open at the expected revision")
            scope_revision = db.execute(
                "SELECT revision FROM objects WHERE kind='ReviewScope' AND id=?", (question.scope_id,)
            ).fetchone()[0]
            other = db.execute(
                "SELECT o.payload FROM question_delegations d JOIN objects o "
                "ON o.kind='Task' AND o.id=d.task_id WHERE o.id<>? AND d.scope_id=?",
                (task_id, question.scope_id),
            ).fetchall()
            if any(
                (t := Task(**json.loads(row[0]))).object_id == question_id
                and t.input_revision == expected_revision
                and t.status not in {"completed", "superseded"}
                for row in other
            ):
                raise RevisionConflict("question already has an active delegation")
            task = Task(task_id, self.run.id, "question", question_id, expected_revision, instruction=instruction)
            db.execute("INSERT INTO objects VALUES ('Task',?,0,?)", (task.id, _json(asdict(task))))
            db.execute(
                "INSERT INTO question_delegations VALUES (?,?,?,?,?,?,?)",
                (
                    task.id,
                    question.scope_id,
                    scope_revision,
                    review_task_id,
                    turn_id,
                    tool_call_id,
                    _json({**request, "expected_revision": expected_revision}),
                ),
            )
            if self.run.metadata.get("question_fencing_policy") == "question-input-1":
                db.execute(
                    "INSERT INTO question_input_bindings VALUES (?,?)",
                    (task.id, self._question_input_digest(db, question)),
                )
            self._event(
                db,
                "Task",
                task.id,
                "delegate",
                {
                    "question_id": question_id,
                    "scope_id": question.scope_id,
                    "turn_id": turn_id,
                    "tool_call_id": tool_call_id,
                },
            )
            self._guard_review_task(db, review_task_id, token, question.scope_id, now)
            db.commit()
            return task

    def _validate_question_original(self, ref: EvidenceRef, *, content: str | None = None) -> None:
        """Accept only provenance shapes emitted by the fixed-source gateway."""
        metadata = ref.metadata
        expected_tools = {
            "source": "read_source",
            "documentation": "read_knowledge",
            "issue": "read_knowledge",
            "test": "run_check",
        }
        if (
            ref.kind not in expected_tools
            or metadata.get("tool_name") != expected_tools[ref.kind]
            or ("navigation_only" in metadata and metadata["navigation_only"] is not False)
            or metadata.get("complete", True) is not True
            or metadata.get("status", "available")
            not in (
                {"available", "success", "ok", "executed"} if ref.kind == "test" else {"available", "success", "ok"}
            )
        ):
            raise BindingError("answers require available original sources")
        args = metadata.get("tool_args")
        if not isinstance(args, dict) or args.get("path") != ref.path or not ref.path:
            raise BindingError("original source tool and path do not match")
        if ref.kind != "issue" and (
            ref.path.startswith("/")
            or "\\" in ref.path
            or ":" in ref.path
            or any(part in {"", ".", ".."} for part in ref.path.split("/"))
        ):
            raise BindingError("invalid fixed source path")
        if ref.kind == "source":
            if ref.side not in {"base", "head"} or args.get("side", "head") != ref.side:
                raise BindingError("source must bind its selected fixed snapshot")
            return
        receipt = metadata.get("original_receipt")
        if not isinstance(receipt, dict) or receipt.get("path") != ref.path or receipt.get("head_sha") != self.run.head:
            raise BindingError("original source receipt binding is missing")
        if ref.kind == "documentation":
            if (
                ref.side != "head"
                or receipt.get("status") != "success"
                or receipt.get("revision") != self.run.head
                or receipt.get("content_sha256") != ref.content_hash
            ):
                raise BindingError("documentation receipt does not match fixed original content")
        elif ref.kind == "issue":
            source_url = metadata.get("source_url")
            captured_at = metadata.get("captured_at")
            if not isinstance(source_url, str) or not isinstance(captured_at, str):
                raise BindingError("external issue provenance is invalid")
            try:
                parsed = urlsplit(source_url)
                captured = datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
            except (AttributeError, TypeError, ValueError) as exc:
                raise BindingError("external issue provenance is invalid") from exc
            if (
                ref.side != "external"
                or not ref.revision
                or ref.path != "issue:" + source_url
                or receipt.get("revision") != ref.revision
                or receipt.get("status") != "success"
                or receipt.get("content_sha256") != ref.content_hash
                or parsed.scheme not in {"http", "https"}
                or not parsed.netloc
                or parsed.username
                or parsed.password
                or captured.tzinfo is None
                or captured.utcoffset() is None
                or metadata.get("historical_head_time") is not False
                or metadata.get("external_snapshot") is not True
            ):
                raise BindingError("external issue receipt is not independently bound")
        else:
            if ref.side != "head":
                raise BindingError("test receipt must use the fixed head")
            try:
                raw = json.loads(content if content is not None else (self.blob_dir / ref.content_hash).read_bytes())
            except (OSError, ValueError, UnicodeError) as exc:
                raise BindingError("test receipt is unavailable") from exc
            if (
                not isinstance(raw, dict)
                or raw.get("status") not in {"success", "executed"}
                or raw.get("head_sha") != self.run.head
                or raw.get("path") != ref.path
                or raw.get("runtime") != args.get("runtime")
                or raw.get("runtime") not in {"python-unittest", "node-test"}
                or type(raw.get("exit_code")) is not int
                or receipt.get("exit_code") != raw.get("exit_code")
                or receipt.get("runtime") != raw.get("runtime")
                or not isinstance(raw.get("source_sha256"), str)
                or re.fullmatch(r"[0-9a-f]{64}", raw["source_sha256"]) is None
                or receipt.get("source_sha256") != raw["source_sha256"]
                or not isinstance(raw.get("image_id"), str)
                or re.fullmatch(r"sha256:[0-9a-f]{64}", raw["image_id"]) is None
                or receipt.get("image_id") != raw["image_id"]
                or not isinstance(raw.get("started_at"), str)
                or not raw["started_at"].strip()
                or not isinstance(raw.get("finished_at"), str)
                or not raw["finished_at"].strip()
            ):
                raise BindingError("test receipt does not prove a fixed-snapshot execution")

    def commit_question_answer(
        self,
        task_id: str,
        token: str,
        expected_revision: int,
        *,
        turn_id: str,
        status: str,
        answer: str,
        evidence_refs: list[str],
        now: float | None = None,
    ) -> Question:
        """Commit an untrusted answer only after this lease received its original sources."""
        if status not in {"answered", "unresolved"} or not isinstance(answer, str):
            raise ValueError("invalid question answer")
        if not answer.strip():
            raise ValueError("question answer or unresolved reason is required")
        if (
            not isinstance(evidence_refs, list)
            or any(not isinstance(ref, str) or not ref for ref in evidence_refs)
            or len(set(evidence_refs)) != len(evidence_refs)
        ):
            raise ValueError("invalid evidence references")
        if status == "answered" and (not answer.strip() or not evidence_refs):
            raise ValueError("answered questions need an answer and original sources")
        request = {
            "expected_revision": expected_revision,
            "status": status,
            "answer": answer,
            "evidence_refs": evidence_refs,
        }
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            task, old, scope_id = self._guard_question_task(db, task_id, token, now)
            previous = db.execute(
                "SELECT request,result FROM question_decisions WHERE task_id=? AND turn_id=?", (task_id, turn_id)
            ).fetchone()
            if previous:
                if previous[0] != _json(request):
                    raise RevisionConflict("question decision already has different inputs")
                self._require_delivered(db, task_id, token, turn_id, set())
                return Question(**json.loads(previous[1]))
            if old.revision != expected_revision:
                raise RevisionConflict("question revision changed")
            refs = self._unit_refs(db, evidence_refs)
            for ref in refs:
                self._validate_question_original(ref)
            delivered = self._require_delivered(db, task_id, token, turn_id, set(evidence_refs))
            if not set(evidence_refs) <= self._task_frontier_refs(db, task_id):
                raise BindingError("sources require explicit reads by this question task")
            lease_hash = hashlib.sha256(token.encode()).hexdigest()
            pinned = {
                row[0]
                for row in db.execute(
                    "SELECT ref_id FROM question_lease_refs WHERE task_id=? AND lease_hash=?", (task_id, lease_hash)
                )
            }
            if not set(evidence_refs) <= pinned:
                raise BindingError("sources were not pinned under this question lease")
            for ref in refs:
                raw = (self.blob_dir / ref.content_hash).read_bytes()
                try:
                    count = len(source_lines(self.run, raw.decode("utf-8")))
                except UnicodeError as exc:
                    raise BindingError("source is not UTF-8") from exc
                first = ref.start_line or 1
                if not any(start <= first and end >= first + count - 1 for start, end in delivered[ref.id]):
                    raise BindingError("entire evidence reference must be delivered")
            phases = dict(
                db.execute(
                    "SELECT phase,content_hash FROM turn_audit WHERE task_id=? AND turn_id=?", (task_id, turn_id)
                )
            )

            def audit(phase: str) -> Any:
                digest = phases[phase]
                raw = (self.audit_dir / digest).read_bytes()
                if hashlib.sha256(raw).hexdigest() != digest:
                    raise BindingError("turn audit blob hash mismatch")
                return json.loads(raw)

            response = audit("response")
            if not isinstance(response, list) or len(response) != 1 or not isinstance(response[0].get("data"), dict):
                raise BindingError("invalid native question response")
            calls = response[0]["data"].get("tool_calls", [])
            if not isinstance(calls, list) or not calls or any(not isinstance(call, dict) for call in calls):
                raise BindingError("invalid native question calls")
            ids = [call.get("id") for call in calls]
            if any(not isinstance(i, str) or not i.strip() for i in ids) or len(ids) != len(set(ids)):
                raise BindingError("ambiguous native call identities")
            if sum(call.get("name") == "answer_question" for call in calls) != 1:
                raise BindingError("exactly one question answer is required")
            calls = sorted(calls, key=lambda call: call.get("name") == "answer_question")
            terminal = calls[-1]
            if terminal.get("name") != "answer_question" or terminal.get("args") != {
                "status": status,
                "answer": answer,
                "evidence_refs": evidence_refs,
            }:
                raise BindingError("question answer must be the final matching native call")
            raw_calls = None
            if "native" in phases:
                native = audit("native")
                if (
                    not isinstance(native, list)
                    or len(native) != 1
                    or not isinstance(native[0], dict)
                    or not isinstance(native[0].get("invalid_tool_calls"), list)
                    or native[0]["invalid_tool_calls"]
                    or native[0].get("protocol_errors")
                ):
                    raise BindingError("invalid native calls")
                raw_calls = native[0].get("raw_tool_calls")
                if raw_calls is not None and (not isinstance(raw_calls, list) or len(raw_calls) != len(calls)):
                    raise BindingError("native call count differs")
            from reviewforge.engine.native_submission import strict_tool_call

            for call in calls:
                raw = None
                if raw_calls is not None:
                    matches = [item for item in raw_calls if isinstance(item, dict) and item.get("id") == call["id"]]
                    if len(matches) != 1:
                        raise BindingError("ambiguous native call identities")
                    raw = matches[0]
                strict_tool_call(call, raw)
            if any(
                batch["task_id"] == task_id and batch["turn_id"] != turn_id
                for batch in self.pending_shared_batches(scope_id, db=db)
            ):
                raise BindingError("previous question tool batch remains unfinished")
            for index, call in enumerate(calls[:-1]):
                phase = f"tool:{index}:{call['id']}"
                if phase not in phases:
                    raise BindingError("preceding call has no durable receipt")
                messages = audit(phase)
                matched = [
                    m.get("data")
                    for m in messages
                    if isinstance(m, dict)
                    and m.get("type") == "tool"
                    and isinstance(m.get("data"), dict)
                    and m["data"].get("tool_call_id") == call["id"]
                ]
                if not matched:
                    raise BindingError("preceding receipt identity mismatch")
                for data in matched:
                    try:
                        content = (
                            json.loads(data.get("content"))
                            if isinstance(data.get("content"), str)
                            else data.get("content")
                        )
                    except ValueError:
                        content = None
                    if (
                        data.get("status") == "error"
                        or not isinstance(content, dict)
                        or "error" in content
                        or content.get("status") in {"error", "failed", "unavailable"}
                    ):
                        raise BindingError("preceding call failed")
            updated = replace(old, status=status, answer=answer, evidence_refs=evidence_refs, revision=old.revision + 1)
            self._validate_refs(db, updated)
            db.execute(
                "UPDATE objects SET revision=?,payload=? WHERE kind='Question' AND id=?",
                (updated.revision, _json(asdict(updated)), old.id),
            )
            plan = self._unit_plan_db(db)
            old_source_identities = {_unit_source_identity(ref) for ref in self._unit_refs(db, old.evidence_refs)}
            pending_refs = {ref.id for ref in refs if _unit_source_identity(ref) not in old_source_identities}
            affected_case_ids = []
            scope = ReviewScope(
                **json.loads(
                    db.execute("SELECT payload FROM objects WHERE kind='ReviewScope' AND id=?", (scope_id,)).fetchone()[
                        0
                    ]
                )
            )
            scope = replace(scope, status="pending", checked_regions=[], revision=scope.revision + 1)
            db.execute(
                "UPDATE objects SET revision=?,payload=? WHERE kind='ReviewScope' AND id=?",
                (scope.revision, _json(asdict(scope)), scope_id),
            )
            db.execute("UPDATE question_delegations SET scope_revision=? WHERE task_id=?", (scope.revision, task_id))
            task = replace(task, input_revision=updated.revision, revision=task.revision + 1)
            db.execute(
                "UPDATE objects SET revision=?,payload=? WHERE kind='Task' AND id=?",
                (task.revision, _json(asdict(task)), task_id),
            )
            if scope_id in plan.primary_scope_ids:
                db.execute("UPDATE unit_epochs SET revision=revision+1 WHERE scope_id=?", (scope_id,))
                self._reopen_integration(db, plan)
            for case_id, payload in db.execute("SELECT id,payload FROM objects WHERE kind='Case'").fetchall():
                case = Case(**json.loads(payload))
                if old.id in case.question_ids or case_id == old.case_id:
                    known = {_unit_source_identity(ref) for ref in self._unit_refs(db, case.evidence_refs)}
                    additions = []
                    for ref in refs:
                        identity = _unit_source_identity(ref)
                        if identity not in known:
                            additions.append(ref.id)
                            known.add(identity)
                    if additions:
                        pending_refs.update(additions)
                        affected_case_ids.append(case_id)
                        case = replace(case, evidence_refs=case.evidence_refs + additions, revision=case.revision + 1)
                        db.execute(
                            "UPDATE objects SET revision=?,payload=? WHERE kind='Case' AND id=?",
                            (case.revision, _json(asdict(case)), case_id),
                        )
                        db.execute(
                            "INSERT INTO unit_pending_cases VALUES (?,?,?) ON CONFLICT(case_id) "
                            "DO UPDATE SET scope_id=excluded.scope_id,obligation_id=excluded.obligation_id",
                            (case_id, scope_id, ""),
                        )
                        self._event(
                            db, "Case", case_id, "question_sources", {"question_id": old.id, "evidence_refs": additions}
                        )
            if scope_id in plan.primary_scope_ids:
                for region in scope.regions:
                    for ref_id in pending_refs:
                        db.execute(
                            "INSERT OR IGNORE INTO unit_pending_sources VALUES (?,?,?)",
                            (scope_id, region["id"], ref_id),
                        )
            elif pending_refs:
                obligation_id = (
                    "ob_"
                    + hashlib.sha256(
                        _json([self.run.id, "question_followup", old.id, updated.revision]).encode()
                    ).hexdigest()
                )
                obligation = IntegrationObligation(
                    obligation_id,
                    self.run.id,
                    plan.integration_scope_id,
                    "case_followup",
                    list(plan.primary_scope_ids),
                    "Review new original sources for question " + old.id,
                    origin_evidence_refs=sorted(pending_refs),
                    origin_task_id=task_id,
                    origin_turn_id=turn_id,
                )
                db.execute(
                    "INSERT INTO integration_obligations VALUES (?,?)", (obligation.id, _json(asdict(obligation)))
                )
                for case_id in affected_case_ids:
                    db.execute(
                        "UPDATE unit_pending_cases SET obligation_id=? WHERE case_id=?", (obligation_id, case_id)
                    )
                self._event(db, "IntegrationObligation", obligation.id, "question_followup", asdict(obligation))
            db.execute(
                "INSERT INTO question_decisions VALUES (?,?,?,?)",
                (task_id, turn_id, _json(request), _json(asdict(updated))),
            )
            if self.run.metadata.get("question_fencing_policy") == "question-input-1":
                db.execute(
                    "UPDATE question_input_bindings SET digest=? WHERE task_id=?",
                    (self._question_input_digest(db, updated), task_id),
                )
            self._event(
                db,
                "Question",
                old.id,
                "delegated_answer",
                {"before": asdict(old), "after": asdict(updated), "task_id": task_id, "turn_id": turn_id},
            )
            self._guard_question_task(db, task_id, token, now)
            db.commit()
            return updated

    def add_for_task(self, task_id: str, token: str, obj: T, *, now: float | None = None) -> T:
        if self.run.policy == "v42-units-6" and isinstance(obj, (Question, Case)):
            raise BindingError("unit decisions must use audited commit_unit_decision")
        if isinstance(obj, Task):
            if self._question_policy_enabled():
                raise BindingError("question tasks require audited create_question_task")
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
                _install_verified_blob(temp, blob, digest, label="evidence")
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
        if task.run_id != self.run.id or not (
            task.kind == "review"
            or (
                task.kind == "question"
                and self._question_policy_enabled()
                and db.execute("SELECT 1 FROM question_delegations WHERE task_id=?", (task.id,)).fetchone()
            )
        ):
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
    def _frontier_content(
        ref: EvidenceRef, raw: bytes, start: int | None, end: int | None, *, run: Run | None = None
    ) -> bytes:
        if (start is None) != (end is None):
            raise ValueError("page requires actual start and end lines")
        if start is None:
            return raw
        if isinstance(start, bool) or isinstance(end, bool) or not isinstance(start, int) or not isinstance(end, int):
            raise ValueError("page lines must be integers")
        try:
            text = raw.decode("utf-8")
            lines = source_lines(run, text, keepends=True) if run is not None else text.splitlines(keepends=True)
        except UnicodeDecodeError as exc:
            raise BindingError("evidence page is not UTF-8") from exc
        first = ref.start_line or 1
        last = first + len(lines) - 1
        if start < first or end < start or end > last:
            raise ValueError("page must be within the actual evidence range")
        selected = (
            source_slice(run, text, start - first + 1, end - first + 1)
            if run is not None
            else "".join(lines[start - first : end - first + 1])
        )
        return selected.encode("utf-8")

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
            scope_id = self._guard_frontier_or_audit_task(db, task, token, now)
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
            content = self._frontier_content(ref, raw, start_line, end_line, run=self.run)
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
                "scope_id": scope_id,
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
            if task.kind == "question":
                db.execute(
                    "INSERT OR IGNORE INTO question_lease_refs VALUES (?,?,?)",
                    (task_id, hashlib.sha256(token.encode()).hexdigest(), ref.id),
                )
                self._guard_question_task(db, task_id, token, now)
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
            ref,
            raw,
            entry.start_line if entry.is_page else None,
            entry.end_line if entry.is_page else None,
            run=self.run,
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
            text = raw.decode("utf-8")
            lines = source_lines(self.run, text, keepends=True)
        except UnicodeDecodeError as exc:
            raise BindingError("frontier page is not UTF-8") from exc
        first = entry.start_line or 1
        last = first + len(lines) - 1
        if start_line < first or start_line > last:
            raise ValueError("page starts outside the pinned body")
        actual_end = min(end_line, last)
        return {
            "content": source_slice(self.run, text, start_line - first + 1, actual_end - first + 1).encode("utf-8"),
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
            self._guard_frontier_or_audit_task(db, task, token, now)
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
        requested_now = now
        now = time.time() if now is None else now
        if not math.isfinite(now):
            raise ValueError("time must be finite")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload FROM objects WHERE kind='Task' AND id=?", (task_id,)).fetchone()
            if not row:
                raise KeyError(task_id)
            task = Task(**json.loads(row[0]))
            if task.kind == "question" and self._question_policy_enabled() and task.status != "completed":
                _, question, _ = self._guard_question_task(db, task_id, token, requested_now)
                decisions = db.execute("SELECT result FROM question_decisions WHERE task_id=?", (task.id,)).fetchall()
                if not any(json.loads(row[0]) == asdict(question) for row in decisions):
                    raise BindingError("question task has no committed answer")
            if self.run.policy == "v42-units-6" and (
                task.kind == "review" or (task.kind == "question" and self._question_policy_enabled())
            ):
                scope_id = (
                    task.object_id
                    if task.kind == "review"
                    else db.execute("SELECT scope_id FROM question_delegations WHERE task_id=?", (task.id,)).fetchone()[
                        0
                    ]
                )
                pending = self.pending_shared_batches(scope_id, db=db)
                if task.kind == "question":
                    pending = [batch for batch in pending if batch["task_id"] == task.id]
                if pending:
                    raise BindingError("task has an unfinished native tool batch")
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
            if task.kind == "question" and self._question_policy_enabled():
                # Recheck the lease after lock-held validation and before commit.
                current = time.time() if requested_now is None else requested_now
                if task.lease_expires <= current:
                    raise LeaseError("question lease expired during completion")
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
        if self.run.metadata.get("verification_delivery_policy") == _VERIFICATION_DELIVERY_POLICY:
            raise BindingError("verification decisions require a claim delivery transaction")
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

    def _load_verification_sources(
        self, db: sqlite3.Connection, sources: _VerificationSources, ids: list[str]
    ) -> list[str]:
        if not isinstance(ids, list) or any(not isinstance(ref_id, str) or not ref_id for ref_id in ids):
            raise ValueError("verification evidence references must be IDs")
        ordered = sorted(set(ids))
        missing = [ref_id for ref_id in ordered if ref_id not in sources.refs]
        for ref_id, ref in zip(missing, self._unit_refs(db, missing, blobs=sources.blobs)):
            identity = asdict(ref)
            identity.pop("id")
            if ref.id != ref_id or ref_id != "ev_" + hashlib.sha256(_json(identity).encode()).hexdigest():
                raise BindingError("verification evidence identity mismatch")
            sources.refs[ref_id] = ref
        return ordered

    def is_verification_delivery_source(self, ref: EvidenceRef, content: str) -> bool:
        """Recognize an original eligible for the claim delivery profile."""
        from .verifier import is_verification_source

        if not is_verification_source(self, ref, content):
            return False
        try:
            self._validate_question_original(ref, content=content)
        except BindingError:
            return False
        if ref.kind != "source":
            return True
        if "\x00" in content:
            return False
        if not content:
            return ref.start_line is None and ref.end_line is None
        return (
            type(ref.start_line) is int
            and type(ref.end_line) is int
            and ref.start_line > 0
            and ref.end_line - ref.start_line + 1 == len(source_lines(self.run, content))
        )

    def _eligible_verification_sources(self, sources: _VerificationSources, ids: list[str]) -> list[str]:
        eligible = []
        for ref_id in ids:
            try:
                content = sources.read_evidence(ref_id).decode("utf-8")
            except UnicodeDecodeError:
                continue
            if self.is_verification_delivery_source(sources.refs[ref_id], content):
                eligible.append(ref_id)
        return eligible

    @staticmethod
    def _verification_result(
        db: sqlite3.Connection,
        sources: _VerificationSources,
        case: Case,
        claim: dict[str, Any],
        result: dict[str, Any],
        observed: list[str],
    ) -> dict[str, Any]:
        from .verifier import validate_result, verification_gap_id

        identity = {"case_trigger": case.trigger, "claim_id": claim["id"], "claim_statement": claim["statement"]}
        if not isinstance(result, dict) or any(result.get(key) != value for key, value in identity.items()):
            raise BindingError("original trigger or claim identity mismatch")
        checked = {**validate_result(sources.for_claim(observed), result, set(observed)), **identity}
        if set(result) - checked.keys() or result.get("cited_refs", checked["cited_refs"]) != checked["cited_refs"]:
            raise BindingError("verdict citations or result fields do not match validated proof")
        if checked["verdict"] in {"accept", "reject"} and set(case.counter_evidence_refs) - set(observed):
            raise BindingError("known counter-evidence was not delivered to this verifier claim")
        for gap in checked["gaps"]:
            qid = verification_gap_id(case.id, claim["id"], gap)
            row = db.execute("SELECT payload FROM objects WHERE kind='Question' AND id=?", (qid,)).fetchone()
            if qid not in case.question_ids or row is None:
                raise BindingError("uncertain verification requires a durable Case gap")
            question = Question(**json.loads(row[0]))
            if (
                question.id,
                question.run_id,
                question.case_id,
                question.scope_id,
                question.statement,
                question.owner,
            ) != (
                qid,
                case.run_id,
                case.id,
                case.scope_id,
                gap,
                "verifier",
            ) or question.status not in {"open", "unresolved"}:
                raise BindingError("uncertain verification gap does not match its Case question")
        return checked

    def _verification_decision(
        self,
        case: Case,
        claim_id: str,
        policy: str,
        result: dict[str, Any],
        case_refs: list[str],
        observed: list[str],
        sources: _VerificationSources,
    ) -> tuple[str, dict[str, Any]]:
        evidence_refs = sorted(set(case_refs) | set(observed))
        delivery = {
            "policy": _VERIFICATION_DELIVERY_POLICY,
            "case_digest": hashlib.sha256(_json(asdict(case)).encode()).hexdigest(),
            "case_evidence_refs": case_refs,
            "observed_evidence_refs": observed,
        }
        digest = hashlib.sha256(
            _json([delivery, [asdict(sources.refs[ref_id]) for ref_id in evidence_refs]]).encode()
        ).hexdigest()
        key = hashlib.sha256(_json([case.id, claim_id, case.revision, digest, policy]).encode()).hexdigest()
        return key, {
            "run_id": self.run.id,
            "repo": self.run.repo,
            "base": self.run.base,
            "head": self.run.head,
            "case_id": case.id,
            "claim_id": claim_id,
            "case_revision": case.revision,
            "source_digest": digest,
            "evidence_refs": evidence_refs,
            "delivery": delivery,
            "policy": policy,
            "verdict": result["verdict"],
            "result": result,
        }

    def _read_verification_delivery(
        self,
        db: sqlite3.Connection,
        sources: _VerificationSources,
        case: Case,
        claim: dict[str, Any],
        policy: str,
        case_refs: list[str],
        row: tuple[str, str],
    ) -> dict[str, Any]:
        stored_key, payload = row
        decision = json.loads(payload)
        delivery = decision.get("delivery")
        if not isinstance(delivery, dict) or delivery.get("policy") != _VERIFICATION_DELIVERY_POLICY:
            raise BindingError("verdict does not bind a claim delivery")
        observed = self._load_verification_sources(db, sources, delivery.get("observed_evidence_refs"))
        if self._eligible_verification_sources(sources, observed) != observed:
            raise BindingError("verdict observed evidence is no longer eligible")
        result = self._verification_result(db, sources, case, claim, decision.get("result"), observed)
        key, expected = self._verification_decision(case, claim["id"], policy, result, case_refs, observed, sources)
        if stored_key != key or decision != expected:
            raise BindingError("verdict source or Case binding no longer matches")
        return decision

    def save_verification_delivery(
        self,
        case_id: str,
        expected_revision: int,
        policy: str,
        deliveries: list[dict[str, Any]],
        *,
        task_id: str,
        lease_token: str,
        now: float | None = None,
    ) -> list[dict[str, Any]]:
        """Freeze each claim with its own observed proof, without changing the Case input."""
        if self.run.metadata.get("verification_delivery_policy") != _VERIFICATION_DELIVERY_POLICY:
            raise BindingError("claim delivery requires the verification delivery profile")
        if not isinstance(policy, str) or not policy or not isinstance(deliveries, list):
            raise ValueError("verification delivery requires a policy and a list of claims")
        incoming = {}
        for delivery in deliveries:
            if (
                not isinstance(delivery, dict)
                or set(delivery) != {"claim_id", "result", "observed_refs"}
                or not isinstance(delivery["claim_id"], str)
                or not delivery["claim_id"]
                or delivery["claim_id"] in incoming
            ):
                raise ValueError("each verification delivery requires a unique claim id, result and observed refs")
            incoming[delivery["claim_id"]] = delivery
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = time.time() if now is None else now
            if not math.isfinite(current):
                raise ValueError("time must be finite")
            row = db.execute("SELECT revision,payload FROM objects WHERE kind='Case' AND id=?", (case_id,)).fetchone()
            if row is None:
                raise KeyError(case_id)
            case = Case(**json.loads(row[1]))
            if row[0] != expected_revision or case.revision != expected_revision:
                raise RevisionConflict("case revision changed during verification")
            if case.id != case_id or case.run_id != self.run.id:
                raise BindingError("verification Case does not belong to this run")
            row = db.execute("SELECT payload FROM objects WHERE kind='Task' AND id=?", (task_id,)).fetchone()
            task = Task(**json.loads(row[0])) if row is not None else None
            if (
                task is None
                or task.id != task_id
                or task.run_id != self.run.id
                or task.kind not in {"verify", "verification"}
                or task.object_id != case_id
                or task.input_revision != expected_revision
                or task.status != "leased"
                or not lease_token
                or task.lease_token != lease_token
                or not math.isfinite(task.lease_expires)
                or task.lease_expires <= current
            ):
                raise LeaseError("a current verification lease for this case revision is required")
            if (
                not case.trigger.strip()
                or not case.mechanism.strip()
                or not case.claims
                or any(
                    not all(
                        isinstance(claim.get(key), str) and claim[key].strip() for key in ("id", "statement", "impact")
                    )
                    for claim in case.claims
                )
            ):
                raise ValueError("verification delivery must bind the Case's original trigger and concrete claims")
            claims = {claim["id"]: claim for claim in case.claims}
            if len(claims) != len(case.claims) or incoming.keys() - claims.keys():
                raise ValueError("verification delivery must bind the Case's concrete claims")
            sources = _VerificationSources(self.run)
            case_refs = self._load_verification_sources(db, sources, case.evidence_refs + case.counter_evidence_refs)
            decisions = []
            for claim_id, claim in claims.items():
                rows = db.execute(
                    "SELECT id,payload FROM verdicts WHERE case_id=? AND claim_id=? AND revision=? AND policy=?",
                    (case_id, claim_id, expected_revision, policy),
                ).fetchall()
                if len(rows) > 1:
                    raise BindingError("ambiguous verdicts for current input")
                existing = (
                    self._read_verification_delivery(db, sources, case, claim, policy, case_refs, rows[0])
                    if rows
                    else None
                )
                delivery = incoming.get(claim_id)
                if delivery is None:
                    if existing is None:
                        raise BindingError("verification delivery does not include every claim")
                    decisions.append(existing)
                    continue
                observed = self._load_verification_sources(db, sources, delivery["observed_refs"])
                eligible = self._eligible_verification_sources(sources, observed)
                result = self._verification_result(db, sources, case, claim, delivery["result"], eligible)
                key, decision = self._verification_decision(
                    case, claim_id, policy, result, case_refs, eligible, sources
                )
                if existing is not None:
                    if existing != decision:
                        raise RevisionConflict("a different verdict is already frozen for this input")
                else:
                    db.execute(
                        "INSERT INTO verdicts VALUES (?,?,?,?,?,?)",
                        (key, case_id, claim_id, expected_revision, policy, _json(decision)),
                    )
                    self._event(db, "Case", case_id, "verdict", decision)
                    audit_refs = sorted(set(observed) - set(eligible))
                    if audit_refs:
                        self._event(
                            db,
                            "Case",
                            case_id,
                            "verification_navigation",
                            {
                                "task_id": task_id,
                                "input_revision": expected_revision,
                                "claim_id": claim_id,
                                "evidence_refs": audit_refs,
                            },
                        )
                decisions.append(decision)
            if task.lease_expires <= (time.time() if now is None else now):
                raise LeaseError("verification lease expired during delivery")
            db.commit()
        return decisions

    def get_verdict(self, case_id: str, claim_id: str, policy: str) -> dict[str, Any] | None:
        """Check case, decision and sources against a single database snapshot."""
        with self._connect() as db:
            db.execute("BEGIN")
            row = db.execute("SELECT revision,payload FROM objects WHERE kind='Case' AND id=?", (case_id,)).fetchone()
            if row is None:
                raise KeyError(case_id)
            case = Case(**json.loads(row[1]))
            claim_delivery = self.run.metadata.get("verification_delivery_policy") == _VERIFICATION_DELIVERY_POLICY
            if claim_delivery and (case.id != case_id or case.run_id != self.run.id or row[0] != case.revision):
                raise BindingError("verdict does not match current Case input")
            rows = db.execute(
                "SELECT id,payload FROM verdicts WHERE case_id=? AND claim_id=? AND revision=? AND policy=?",
                (case_id, claim_id, case.revision, policy),
            ).fetchall()
            if not rows:
                return None
            if len(rows) != 1:
                raise BindingError("ambiguous verdicts for current input")
            if claim_delivery:
                claims = [claim for claim in case.claims if claim["id"] == claim_id]
                if len(claims) != 1:
                    raise BindingError("verdict does not match current Case input")
                sources = _VerificationSources(self.run)
                case_refs = self._load_verification_sources(
                    db, sources, case.evidence_refs + case.counter_evidence_refs
                )
                return self._read_verification_delivery(db, sources, case, claims[0], policy, case_refs, rows[0])
            decision = json.loads(rows[0][1])
            if "delivery" in decision:
                raise BindingError("claim delivery requires the verification delivery profile")
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
        requested_now = now
        now = time.time() if now is None else now
        if not math.isfinite(now):
            raise ValueError("time must be finite")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload FROM objects WHERE kind='Task' AND id=?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(task_id)
            task = Task(**json.loads(row[0]))
            if task.kind == "question" and self._question_policy_enabled():
                self._guard_question_task(db, task_id, token, requested_now)
                db.execute(
                    "INSERT INTO question_session_owners VALUES (?,?) "
                    "ON CONFLICT(task_id) DO UPDATE SET lease_hash=excluded.lease_hash",
                    (task_id, hashlib.sha256(token.encode()).hexdigest()),
                )
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
            if task.kind == "question" and self._question_policy_enabled():
                self._guard_question_task(db, task_id, token, requested_now)
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
            if task.kind == "question" and self._question_policy_enabled():
                owner = db.execute(
                    "SELECT lease_hash FROM question_session_owners WHERE task_id=?", (task_id,)
                ).fetchone()
                if (
                    owner is None
                    or not task.lease_token
                    or owner[0] != hashlib.sha256(task.lease_token.encode()).hexdigest()
                ):
                    return []
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
            decision_cycle = self.run.metadata.get("decision_cycle_policy") == "decision-cycle-1"
            if decision_cycle and (task.run_id != self.run.id or case.run_id != self.run.id):
                raise BindingError("verification task or case belongs to another run")
            if (
                self.run.metadata.get("verification_delivery_policy") == _VERIFICATION_DELIVERY_POLICY
                and evidence_refs != case.evidence_refs
            ):
                raise BindingError("verification observations cannot change Case evidence under claim delivery")

            def validate_decision_refs(ids: list[str]) -> list[EvidenceRef]:
                refs = self._unit_refs(db, ids)
                for ref in refs:
                    identity = asdict(ref)
                    ref_id = identity.pop("id")
                    if ref_id != "ev_" + hashlib.sha256(_json(identity).encode()).hexdigest():
                        raise BindingError("verification evidence identity mismatch")
                return refs

            question_ids = list(case.question_ids)
            for question in questions:
                if question.run_id != case.run_id or question.case_id != case.id or question.scope_id != case.scope_id:
                    raise BindingError("verification question does not belong to this case and scope")
                if question.revision != 0:
                    raise ValueError("new questions start at revision 0")
                self._validate_refs(db, question)
                if decision_cycle:
                    question_refs = list(question.evidence_refs)
                    for interpretation in question.interpretations:
                        question_refs.extend(interpretation.get("evidence_refs", []))
                    validate_decision_refs(question_refs)
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
                # Validate every incoming ref before treating any of it as navigation.
                # Filtering first would silently hide missing, foreign or corrupt blobs.
                incoming = (
                    validate_decision_refs(evidence_refs)
                    if decision_cycle
                    else self._unit_refs(db, [r for r in evidence_refs if r not in case.evidence_refs])
                )
                if decision_cycle:
                    navigation = [
                        ref.id
                        for ref in incoming
                        if ref.id not in case.evidence_refs
                        and (
                            ref.kind.casefold() == "search"
                            or (
                                ref.kind.casefold() in {"diff", "query"} and ref.metadata.get("navigation_only") is True
                            )
                        )
                    ]
                    if navigation:
                        self._event(
                            db,
                            "Case",
                            case.id,
                            "verification_navigation",
                            {
                                "task_id": task_id,
                                "input_revision": expected_revision,
                                "evidence_refs": navigation,
                            },
                        )
                    navigation_ids = set(navigation)
                    incoming = [ref for ref in incoming if ref.id not in navigation_ids]
                old_sources = self._unit_refs(db, case.evidence_refs)
                identities = {_unit_source_identity(ref) for ref in old_sources}
                genuinely_new = []
                for ref in incoming:
                    if ref.id in case.evidence_refs:
                        continue
                    identity = _unit_source_identity(ref)
                    if identity not in identities:
                        genuinely_new.append(ref.id)
                        identities.add(identity)
                evidence_refs = [*case.evidence_refs, *genuinely_new]
            updated = replace(case, evidence_refs=list(evidence_refs), question_ids=question_ids)
            self._validate_refs(db, updated)
            if updated == case:
                if decision_cycle:
                    db.commit()
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
            self._guard_frontier_or_audit_task(db, task, token, now)
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
                    _install_verified_blob(temp, blob, digest, label="turn audit")
                finally:
                    temp.unlink(missing_ok=True)
            if row:
                return {"seq": row[0], "task_id": task_id, "turn_id": turn_id, "phase": phase, "content_hash": digest}
            cursor = db.execute(
                "INSERT INTO turn_audit (task_id,turn_id,phase,content_hash) VALUES (?,?,?,?)",
                (task_id, turn_id, phase, digest),
            )
            if task.kind == "question":
                self._guard_question_task(db, task_id, token, now)
            result = {
                "seq": cursor.lastrowid,
                "task_id": task_id,
                "turn_id": turn_id,
                "phase": phase,
                "content_hash": digest,
            }
            db.commit()
            return result

    def record_fenced_question_response(
        self,
        task_id: str,
        token: str,
        turn_id: str,
        response: list[dict[str, Any]],
        native: list[dict[str, Any]] | None = None,
        *,
        now: float | None = None,
    ) -> None:
        """Retain a response received after a question lease or its input was fenced.

        This records no tool receipt, execution or evidence-delivery permission.
        """
        if not self._question_policy_enabled() or not isinstance(turn_id, str) or not turn_id or not token:
            raise BindingError("fenced question response is not enabled or bound")
        if not isinstance(response, list) or len(response) != 1 or not isinstance(response[0], dict):
            raise ValueError("a single visible native response is required")
        if response[0].get("type") not in {"ai", "AIMessage", "ai_chunk", "AIMessageChunk"}:
            raise ValueError("response must be an assistant message")
        if native is not None and (not isinstance(native, list) or any(not isinstance(item, dict) for item in native)):
            raise ValueError("native protocol metadata must be a list of objects")
        data = response[0].get("data")
        if not isinstance(data, dict):
            raise ValueError("response data must be visible")
        private = {
            "reasoning",
            "reasoning_content",
            "reasoning_details",
            "thinking",
            "thinking_blocks",
            "redacted_thinking",
        }
        for container in (response[0], data, data.get("additional_kwargs"), data.get("response_metadata")):
            if isinstance(container, dict) and any(isinstance(k, str) and k.lower() in private for k in container):
                raise ValueError("provider reasoning is not a visible audit message")
        content = data.get("content")
        for block in content if isinstance(content, list) else [content]:
            if isinstance(block, dict) and str(block.get("type", "")).lower() in private:
                raise ValueError("provider reasoning is not a visible audit message")
        phases = {"response": response}
        if native is not None:
            phases["native"] = native
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            task_row = db.execute("SELECT payload FROM objects WHERE kind='Task' AND id=?", (task_id,)).fetchone()
            binding = db.execute(
                "SELECT scope_id,scope_revision FROM question_delegations WHERE task_id=?", (task_id,)
            ).fetchone()
            owner = db.execute(
                "SELECT lease_hash FROM unit_turn_owners WHERE task_id=? AND turn_id=?", (task_id, turn_id)
            ).fetchone()
            request = db.execute(
                "SELECT content_hash FROM turn_audit WHERE task_id=? AND turn_id=? AND phase='request'",
                (task_id, turn_id),
            ).fetchone()
            if (
                not task_row
                or not binding
                or not owner
                or not request
                or owner[0] != hashlib.sha256(token.encode()).hexdigest()
            ):
                raise BindingError("fenced response lacks its original task, turn or lease binding")
            task = Task(**json.loads(task_row[0]))
            qrow = db.execute(
                "SELECT payload FROM objects WHERE kind='Question' AND id=?", (task.object_id,)
            ).fetchone()
            scope = db.execute(
                "SELECT revision FROM objects WHERE kind='ReviewScope' AND id=?", (binding[0],)
            ).fetchone()
            if task.run_id != self.run.id or task.kind != "question" or not qrow or not scope:
                raise BindingError("fenced response has no valid question binding")
            question = Question(**json.loads(qrow[0]))
            if question.run_id != self.run.id or question.scope_id != binding[0]:
                raise BindingError("fenced response belongs to a different scope")
            clock = time.time() if now is None else now
            if not math.isfinite(clock):
                raise ValueError("time must be finite")
            current_inputs = task.input_revision == question.revision and scope[0] == binding[1]
            if self.run.metadata.get("question_fencing_policy") == "question-input-1":
                expected = db.execute(
                    "SELECT digest FROM question_input_bindings WHERE task_id=?", (task_id,)
                ).fetchone()
                try:
                    actual = self._question_input_digest(db, question)
                except (BindingError, KeyError, OSError, ValueError, TypeError):
                    actual = None
                current_inputs = (
                    task.input_revision == question.revision and expected is not None and expected[0] == actual
                )
            if task.status == "leased" and task.lease_token == token and task.lease_expires > clock and current_inputs:
                raise RevisionConflict("question turn is not fenced")
            request_blob = (self.audit_dir / request[0]).read_bytes()
            if hashlib.sha256(request_blob).hexdigest() != request[0]:
                raise BindingError("turn audit blob hash mismatch")
            changed = False
            for phase, messages in phases.items():
                payload = _json(messages).encode("utf-8")
                digest = hashlib.sha256(payload).hexdigest()
                existing = db.execute(
                    "SELECT content_hash FROM turn_audit WHERE task_id=? AND turn_id=? AND phase=?",
                    (task_id, turn_id, phase),
                ).fetchone()
                if existing and existing[0] != digest:
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
                        _install_verified_blob(temp, blob, digest, label="turn audit")
                    finally:
                        temp.unlink(missing_ok=True)
                if not existing:
                    changed = True
                    db.execute(
                        "INSERT INTO turn_audit (task_id,turn_id,phase,content_hash) VALUES (?,?,?,?)",
                        (task_id, turn_id, phase, digest),
                    )
            if changed:
                self._event(
                    db, "Task", task_id, "fenced_question_response", {"scope_id": binding[0], "turn_id": turn_id}
                )
            db.commit()

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
