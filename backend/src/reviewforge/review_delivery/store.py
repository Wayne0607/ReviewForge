from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .contracts import Binding
from .evidence import manifest_digest, validate_absence


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:16]}"


def _nonempty(value: Any, name: str) -> None:
    if not value:
        raise ValueError(f"{name} is required")


class DeliveryStore:
    """A run-bound durable ledger; navigation, evidence and semantic revisions stay separate."""

    def __init__(self, path: str | Path, binding: Binding, *, _must_exist: bool = False):
        # Detach nested configuration from the caller before freezing its identity.
        binding = Binding(**json.loads(_json(binding.as_dict())))
        self.binding = binding
        for key in (binding.run_id, binding.repo, binding.base_sha, binding.head_sha):
            _nonempty(key, "binding field")
        if binding.pr_number <= 0:
            raise ValueError("invalid PR number")
        if _must_exist:
            self.db = sqlite3.connect(
                Path(path).resolve().as_uri() + "?mode=rw", uri=True, timeout=30, isolation_level=None
            )
        else:
            self.db = sqlite3.connect(str(path), timeout=30, isolation_level=None)
        try:
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA foreign_keys=ON")
            if not _must_exist:
                existing = self.db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='binding'"
                ).fetchone()
                if existing:
                    row = self.db.execute("SELECT value FROM binding WHERE id=1").fetchone()
                    if row is not None and row[0] != _json(binding.as_dict()):
                        raise ValueError("run binding mismatch")
                self.db.executescript("""
                CREATE TABLE IF NOT EXISTS binding (id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, role TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS turns (id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL REFERENCES sessions(id),
                    payload TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    UNIQUE(session_id,seq));
                CREATE TABLE IF NOT EXISTS sources (id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    snapshot_side TEXT NOT NULL,
                    content BLOB NOT NULL,
                    sha256 TEXT NOT NULL,
                    metadata TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS deliveries (session_id TEXT NOT NULL REFERENCES sessions(id),
                    source_id TEXT NOT NULL REFERENCES sources(id),
                    PRIMARY KEY(session_id,source_id));
                CREATE TABLE IF NOT EXISTS research (id TEXT PRIMARY KEY,
                    question TEXT NOT NULL,
                    session_id TEXT NOT NULL REFERENCES sessions(id),
                    status TEXT NOT NULL,
                    result TEXT);
                CREATE TABLE IF NOT EXISTS notes (id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES research(id),
                    session_id TEXT NOT NULL REFERENCES sessions(id),
                    text TEXT NOT NULL,
                    source_ids TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS challenge_actions (
                    id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    result TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS note_holders (
                    session_id TEXT NOT NULL REFERENCES sessions(id),
                    task_id TEXT NOT NULL REFERENCES research(id),
                    last_seq INTEGER NOT NULL,
                    PRIMARY KEY(session_id,task_id));
                CREATE TABLE IF NOT EXISTS findings (id TEXT PRIMARY KEY,
                    revision INTEGER NOT NULL,
                    claim TEXT NOT NULL,
                    source_ids TEXT NOT NULL,
                    session_id TEXT NOT NULL REFERENCES sessions(id),
                    state TEXT NOT NULL,
                    duplicate_of TEXT REFERENCES findings(id));
                CREATE TABLE IF NOT EXISTS finding_history (finding_id TEXT NOT NULL REFERENCES findings(id),
                    revision INTEGER NOT NULL,
                    claim TEXT NOT NULL,
                    source_ids TEXT NOT NULL,
                    reason TEXT,
                    PRIMARY KEY(finding_id,revision));
                CREATE TABLE IF NOT EXISTS verifications (id TEXT PRIMARY KEY,
                    finding_id TEXT NOT NULL REFERENCES findings(id),
                    finding_revision INTEGER NOT NULL,
                    session_id TEXT NOT NULL REFERENCES sessions(id),
                    status TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS verdicts (id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL UNIQUE REFERENCES verifications(id),
                    finding_id TEXT NOT NULL REFERENCES findings(id),
                    finding_revision INTEGER NOT NULL,
                    verdict TEXT NOT NULL,
                    source_ids TEXT NOT NULL,
                    approved_payload TEXT,
                    missing_fact TEXT,
                    attempted TEXT,
                    recovery_condition TEXT,
                    reason TEXT);
                CREATE TABLE IF NOT EXISTS verdict_absence (verdict_id TEXT PRIMARY KEY REFERENCES verdicts(id),
                    premises TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS snapshot_manifests (side TEXT PRIMARY KEY, files TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS delivery_cooldowns (finding_id TEXT PRIMARY KEY REFERENCES findings(id),
                retry_not_before REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS outbox (finding_id TEXT PRIMARY KEY REFERENCES findings(id),
                    finding_revision INTEGER NOT NULL,
                    verdict_id TEXT NOT NULL REFERENCES verdicts(id),
                    approved_payload TEXT NOT NULL,
                    marker TEXT NOT NULL UNIQUE,
                    body TEXT,
                    status TEXT NOT NULL,
                    remote_id INTEGER,
                    last_delivered_body TEXT);
                """)
            with self._tx():
                value = _json(binding.as_dict())
                row = self.db.execute("SELECT value FROM binding WHERE id=1").fetchone()
                if row is None:
                    if _must_exist:
                        raise ValueError("existing run binding is missing")
                    self.db.execute("INSERT INTO binding VALUES (1,?)", (value,))
                elif row[0] != value:
                    raise ValueError("run binding mismatch")
        except BaseException:
            self.db.close()
            raise

    @classmethod
    def reopen(cls, path: str | Path) -> DeliveryStore:
        """Reopen only an existing run, using its persisted frozen binding."""
        database_path = Path(path)
        if not database_path.is_file():
            raise ValueError("existing delivery database is required")
        uri = database_path.resolve().as_uri() + "?mode=ro"
        try:
            db = sqlite3.connect(uri, uri=True)
            try:
                tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                required = {
                    "binding",
                    "sessions",
                    "turns",
                    "sources",
                    "deliveries",
                    "research",
                    "notes",
                    "note_holders",
                    "challenge_actions",
                    "findings",
                    "finding_history",
                    "verifications",
                    "verdicts",
                    "outbox",
                    "delivery_cooldowns",
                }
                if not required.issubset(tables):
                    raise ValueError("existing delivery database schema is incomplete")
                rows = db.execute("SELECT id,value FROM binding").fetchall()
                if len(rows) != 1 or rows[0][0] != 1:
                    raise ValueError("existing run binding is missing or invalid")
                value = json.loads(rows[0][1])
            finally:
                db.close()
        except (sqlite3.Error, json.JSONDecodeError, TypeError) as exc:
            raise ValueError("existing delivery database binding is invalid") from exc
        keys = {"run_id", "repo", "pr_number", "base_sha", "head_sha", "source_environment", "model_config"}
        if (
            not isinstance(value, dict)
            or set(value) != keys
            or any(
                not isinstance(value.get(key), str) or not value[key]
                for key in ("run_id", "repo", "base_sha", "head_sha")
            )
            or type(value.get("pr_number")) is not int
            or value["pr_number"] <= 0
            or not isinstance(value.get("source_environment"), (dict, str))
            or not isinstance(value.get("model_config"), (dict, str))
        ):
            raise ValueError("existing delivery database binding is invalid")
        return cls(database_path, Binding(**value), _must_exist=True)

    def close(self) -> None:
        self.db.close()

    @contextmanager
    def _tx(self) -> Iterator[None]:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.db.rollback()
            raise
        else:
            self.db.commit()

    def _get(self, table: str, item_id: str) -> dict[str, Any]:
        row = self.db.execute(f"SELECT * FROM {table} WHERE id=?", (item_id,)).fetchone()
        if row is None:
            raise ValueError(f"unknown {table} id: {item_id}")
        return self._decode(row)

    @staticmethod
    def _decode(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        for key in ("payload", "claim", "source_ids", "metadata", "result", "approved_payload", "attempted"):
            if key in result and result[key] is not None:
                result[key] = json.loads(result[key])
        return result

    def _list(self, table: str) -> list[dict[str, Any]]:
        return [self._decode(row) for row in self.db.execute(f"SELECT * FROM {table} ORDER BY rowid")]

    def create_session(self, role: str, session_id: str | None = None) -> dict[str, Any]:
        _nonempty(role, "role")
        session_id = session_id or _id("S")
        with self._tx():
            row = self.db.execute("SELECT role FROM sessions WHERE id=?", (session_id,)).fetchone()
            if row and row[0] != role:
                raise ValueError("session role mismatch")
            self.db.execute("INSERT OR IGNORE INTO sessions VALUES (?,?)", (session_id, role))
        return self.get_session(session_id)

    def get_session(self, session_id: str) -> dict[str, Any]:
        return self._get("sessions", session_id)

    def list_sessions(self) -> list[dict[str, Any]]:
        return self._list("sessions")

    def append_turn(self, session_id: str, payload: Any, turn_id: str | None = None) -> dict[str, Any]:
        turn_id = turn_id or _id("T")
        encoded = _json(payload)
        with self._tx():
            self.get_session(session_id)
            row = self.db.execute("SELECT * FROM turns WHERE id=?", (turn_id,)).fetchone()
            if row:
                if row["session_id"] != session_id or row["payload"] != encoded:
                    raise ValueError("turn id already used")
            else:
                seq = self.db.execute(
                    "SELECT COALESCE(MAX(seq),0)+1 FROM turns WHERE session_id=?", (session_id,)
                ).fetchone()[0]
                self.db.execute("INSERT INTO turns VALUES (?,?,?,?)", (turn_id, session_id, encoded, seq))
        return self._get("turns", turn_id)

    def list_turns(self, session_id: str) -> list[dict[str, Any]]:
        return [
            self._decode(row)
            for row in self.db.execute("SELECT * FROM turns WHERE session_id=? ORDER BY seq", (session_id,))
        ]

    def _validate_provenance(self, kind: str, snapshot_side: str, metadata: dict[str, Any]) -> None:
        if kind != "original":
            return
        raw_metadata = metadata.get("metadata")
        levels = [metadata, raw_metadata] if isinstance(raw_metadata, dict) else [metadata]
        for level in levels:
            if level.get("navigation_only") or level.get("status") in {"navigation", "navigation_only", "unavailable"}:
                raise ValueError("navigation is not original evidence")
        if metadata.get("kind") is not None and metadata["kind"] not in {"source", "documentation", "issue", "test"}:
            raise ValueError("navigation is not original evidence")
        if snapshot_side in {"base", "head"}:
            expected = self.binding.base_sha if snapshot_side == "base" else self.binding.head_sha
            if metadata.get("revision") != expected:
                raise ValueError("source revision does not match binding")
        else:
            environment = self.binding.source_environment
            external = environment.get("external", {}) if isinstance(environment, dict) else {}
            snapshot_id = metadata.get("path")
            provenance = metadata.get("provenance")
            if (
                not snapshot_id
                or snapshot_id not in external
                or external[snapshot_id] != provenance
                or not isinstance(provenance, dict)
                or not all(provenance.get(k) for k in ("source_url", "revision", "sha256", "captured_at"))
                or provenance.get("historical_head_time") is not False
            ):
                raise ValueError("external source provenance not frozen in binding")

    def put_source(
        self, kind: str, snapshot_side: str, content: bytes, metadata: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        if kind not in {"original", "navigation"} or snapshot_side not in {"base", "head", "external"}:
            raise ValueError("invalid source kind or snapshot side")
        if not isinstance(content, bytes):
            raise ValueError("source content must be bytes")
        self._validate_provenance(kind, snapshot_side, metadata or {})
        digest = hashlib.sha256(content).hexdigest()
        if kind == "original" and snapshot_side == "external" and metadata["provenance"]["sha256"] != digest:
            raise ValueError("external content does not match frozen snapshot")
        encoded = _json(metadata or {})
        # Content and provenance both participate: identical base/head bytes are distinct sources.
        identity = _json([self.binding.run_id, kind, snapshot_side, digest, json.loads(encoded)])
        source_id = "SRC-" + hashlib.sha256(identity.encode()).hexdigest()[:24]
        with self._tx():
            self.db.execute(
                "INSERT OR IGNORE INTO sources VALUES (?,?,?,?,?,?)",
                (source_id, kind, snapshot_side, content, digest, encoded),
            )
        return self.get_source(source_id)

    def get_source(self, source_id: str) -> dict[str, Any]:
        source = self._get("sources", source_id)
        self._validate_provenance(source["kind"], source["snapshot_side"], source["metadata"])
        if (
            source["kind"] == "original"
            and source["snapshot_side"] == "external"
            and source["metadata"]["provenance"]["sha256"] != source["sha256"]
        ):
            raise ValueError("external content does not match frozen snapshot")
        if hashlib.sha256(source["content"]).hexdigest() != source["sha256"]:
            raise ValueError("source hash mismatch")
        identity = _json(
            [self.binding.run_id, source["kind"], source["snapshot_side"], source["sha256"], source["metadata"]]
        )
        if source_id != "SRC-" + hashlib.sha256(identity.encode()).hexdigest()[:24]:
            raise ValueError("source provenance mismatch")
        return source

    def list_sources(self) -> list[dict[str, Any]]:
        return [self.get_source(row["id"]) for row in self.db.execute("SELECT id FROM sources ORDER BY rowid")]

    def record_delivery(self, session_id: str, source_ids: list[str] | tuple[str, ...]) -> None:
        with self._tx():
            self.get_session(session_id)
            for source_id in dict.fromkeys(source_ids):
                self.get_source(source_id)
                self.db.execute("INSERT OR IGNORE INTO deliveries VALUES (?,?)", (session_id, source_id))

    def delivered_sources(self, session_id: str) -> list[str]:
        self.get_session(session_id)
        return [
            row[0]
            for row in self.db.execute(
                "SELECT source_id FROM deliveries WHERE session_id=? ORDER BY rowid", (session_id,)
            )
        ]

    def create_research(self, question: str, session_id: str, task_id: str | None = None) -> dict[str, Any]:
        _nonempty(question, "question")
        task_id = task_id or _id("R")
        with self._tx():
            self.get_session(session_id)
            row = self.db.execute("SELECT * FROM research WHERE id=?", (task_id,)).fetchone()
            if row and (row["question"] != question or row["session_id"] != session_id):
                raise ValueError("research id already used")
            if row is None:
                self.db.execute("INSERT INTO research VALUES (?,?,?,'open',NULL)", (task_id, question, session_id))
        return self.get_research(task_id)

    def get_research(self, task_id: str) -> dict[str, Any]:
        return self._get("research", task_id)

    def list_research(self) -> list[dict[str, Any]]:
        return self._list("research")

    def complete_research(self, task_id: str, result: Any) -> dict[str, Any]:
        encoded = _json(result)
        with self._tx():
            task = self.get_research(task_id)
            if task["status"] == "complete" and _json(task["result"]) != encoded:
                raise ValueError("research already completed")
            self.db.execute("UPDATE research SET status='complete',result=? WHERE id=?", (encoded, task_id))
        return self.get_research(task_id)

    def add_note(
        self,
        task_id: str,
        session_id: str,
        text: str,
        source_ids: list[str] | tuple[str, ...] = (),
        note_id: str | None = None,
    ) -> dict[str, Any]:
        _nonempty(text, "text")
        note_id = note_id or _id("N")
        encoded_ids = _json(list(source_ids))
        with self._tx():
            existing = self.db.execute("SELECT * FROM notes WHERE id=?", (note_id,)).fetchone()
            if existing:
                if (existing["task_id"], existing["session_id"], existing["text"], existing["source_ids"]) != (
                    task_id,
                    session_id,
                    text,
                    encoded_ids,
                ):
                    raise ValueError("note id already used")
                return self._decode(existing)
            self.get_research(task_id)
            self.get_session(session_id)
            for source_id in source_ids:
                self.get_source(source_id)
            self.db.execute("INSERT INTO notes VALUES (?,?,?,?,?)", (note_id, task_id, session_id, text, encoded_ids))
        return self._get("notes", note_id)

    def list_notes(self, task_id: str | None = None) -> list[dict[str, Any]]:
        if task_id is None:
            rows = self.db.execute("SELECT rowid AS seq, * FROM notes ORDER BY rowid")
        else:
            rows = self.db.execute("SELECT rowid AS seq, * FROM notes WHERE task_id=? ORDER BY rowid", (task_id,))
        return [self._decode(row) for row in rows]

    def record_note_reads(self, session_id: str, task_watermarks: dict[str, int]) -> None:
        """Record only the note sequence numbers in the successful model request."""
        with self._tx():
            self.get_session(session_id)
            for task_id, seq in task_watermarks.items():
                self.get_research(task_id)
                if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
                    raise ValueError("invalid note watermark")
                if (
                    seq
                    and not self.db.execute(
                        "SELECT 1 FROM notes WHERE task_id=? AND rowid=?", (task_id, seq)
                    ).fetchone()
                ):
                    raise ValueError("unknown note watermark")
                self.db.execute(
                    "INSERT INTO note_holders VALUES (?,?,?) ON CONFLICT(session_id,task_id) "
                    "DO UPDATE SET last_seq=MAX(note_holders.last_seq,excluded.last_seq)",
                    (session_id, task_id, seq),
                )

    def changed_note_tasks(self, session_id: str) -> list[str]:
        self.get_session(session_id)
        return [
            row[0]
            for row in self.db.execute(
                "SELECT h.task_id FROM note_holders h WHERE h.session_id=? AND EXISTS "
                "(SELECT 1 FROM notes n WHERE n.task_id=h.task_id AND n.rowid>h.last_seq) ORDER BY h.rowid",
                (session_id,),
            )
        ]

    def _sources(self, source_ids: list[str] | tuple[str, ...]) -> list[str]:
        ids = list(dict.fromkeys(source_ids))
        for source_id in ids:
            if self.get_source(source_id)["kind"] != "original":
                raise ValueError("evidence must be original")
        return ids

    def create_finding(
        self, claim: dict[str, Any], source_ids: list[str], session_id: str, duplicate_of: str | None = None
    ) -> dict[str, Any]:
        _nonempty(claim, "claim")
        with self._tx():
            self.get_session(session_id)
            ids = self._sources(source_ids)
            if duplicate_of:
                return self.get_finding(duplicate_of)
            encoded_claim, encoded_ids = _json(claim), _json(ids)
            row = self.db.execute(
                "SELECT id FROM findings WHERE claim=? AND source_ids=? AND session_id=? AND duplicate_of IS NULL",
                (encoded_claim, encoded_ids, session_id),
            ).fetchone()
            if row:
                return self.get_finding(row[0])
            finding_id = _id("F")
            self.db.execute(
                "INSERT INTO findings VALUES (?,1,?,?,?,'open',NULL)",
                (finding_id, encoded_claim, encoded_ids, session_id),
            )
            self.db.execute(
                "INSERT INTO finding_history VALUES (?,1,?,?,NULL)", (finding_id, encoded_claim, encoded_ids)
            )
        return self.get_finding(finding_id)

    def get_finding(self, finding_id: str) -> dict[str, Any]:
        return self._get("findings", finding_id)

    def get_finding_revision(self, finding_id: str, revision: int) -> dict[str, Any]:
        row = self.db.execute(
            "SELECT * FROM finding_history WHERE finding_id=? AND revision=?", (finding_id, revision)
        ).fetchone()
        if row is None:
            raise ValueError("unknown finding revision")
        return self._decode(row)

    def list_findings(self) -> list[dict[str, Any]]:
        return self._list("findings")

    def update_finding(
        self, finding_id: str, claim: dict[str, Any], source_ids: list[str], same_defect_reason: str | None = None
    ) -> dict[str, Any]:
        _nonempty(claim, "claim")
        with self._tx():
            finding = self.get_finding(finding_id)
            old_outbox = self.get_outbox(finding_id)
            if old_outbox and old_outbox["status"] == "uncertain":
                raise ValueError("reconcile uncertain delivery before changing finding")
            ids = self._sources(source_ids)
            if finding["claim"] == claim:
                return finding
            _nonempty(same_defect_reason, "same_defect_reason")
            rev = finding["revision"] + 1
            self.db.execute(
                "UPDATE findings SET revision=?,claim=?,source_ids=?,state='open' WHERE id=?",
                (rev, _json(claim), _json(ids), finding_id),
            )
            self.db.execute(
                "INSERT INTO finding_history VALUES (?,?,?,?,?)",
                (finding_id, rev, _json(claim), _json(ids), same_defect_reason),
            )
        return self.get_finding(finding_id)

    def challenge_finding(
        self, finding_id: str, reason: str, source_ids: list[str] | None = None, challenge_id: str | None = None
    ) -> dict[str, Any]:
        _nonempty(reason, "reason")
        payload = _json({"finding_id": finding_id, "reason": reason, "source_ids": source_ids})
        with self._tx():
            if challenge_id is not None:
                _nonempty(challenge_id, "challenge_id")
                prior = self.db.execute("SELECT * FROM challenge_actions WHERE id=?", (challenge_id,)).fetchone()
                if prior:
                    if prior["payload"] != payload:
                        raise ValueError("challenge id already used")
                    return json.loads(prior["result"])
            finding = self.get_finding(finding_id)
            old_outbox = self.get_outbox(finding_id)
            if old_outbox and old_outbox["status"] == "uncertain":
                raise ValueError("reconcile uncertain delivery before changing finding")
            ids = self._sources(source_ids) if source_ids is not None else finding["source_ids"]
            rev = finding["revision"] + 1
            self.db.execute(
                "UPDATE findings SET revision=?,source_ids=?,state='open' WHERE id=?", (rev, _json(ids), finding_id)
            )
            self.db.execute(
                "INSERT INTO finding_history VALUES (?,?,?,?,?)",
                (finding_id, rev, _json(finding["claim"]), _json(ids), reason),
            )
            result = self.get_finding(finding_id)
            if challenge_id is not None:
                self.db.execute("INSERT INTO challenge_actions VALUES (?,?,?)", (challenge_id, payload, _json(result)))
        return result

    def create_verification(self, finding_id: str, session_id: str) -> dict[str, Any]:
        with self._tx():
            finding = self.get_finding(finding_id)
            session = self.get_session(session_id)
            if session_id == finding["session_id"] or session["role"] != "verifier":
                raise ValueError("verification requires independent verifier session")
            decided = self.db.execute(
                "SELECT id FROM verifications WHERE finding_id=? AND finding_revision=? "
                "AND status IN ('accept','reject') ORDER BY rowid LIMIT 1",
                (finding_id, finding["revision"]),
            ).fetchone()
            if decided:
                return self.get_verification(decided[0])
            row = self.db.execute(
                "SELECT id FROM verifications WHERE finding_id=? AND finding_revision=? "
                "AND session_id=? AND status!='needs_evidence' ORDER BY rowid DESC LIMIT 1",
                (finding_id, finding["revision"], session_id),
            ).fetchone()
            if row:
                return self.get_verification(row[0])
            task_id = _id("V")
            self.db.execute(
                "INSERT INTO verifications VALUES (?,?,?,?,'open')",
                (task_id, finding_id, finding["revision"], session_id),
            )
        return self.get_verification(task_id)

    def get_verification(self, task_id: str) -> dict[str, Any]:
        return self._get("verifications", task_id)

    def list_verifications(self) -> list[dict[str, Any]]:
        return self._list("verifications")

    def get_verdict(self, verdict_id: str) -> dict[str, Any]:
        verdict = self._get("verdicts", verdict_id)
        # Historical corrections can be recovered without upgrading their schema.
        if self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='verdict_absence'").fetchone():
            row = self.db.execute("SELECT premises FROM verdict_absence WHERE verdict_id=?", (verdict_id,)).fetchone()
            if row:
                verdict["absence_claims"] = json.loads(row[0])
        return verdict

    def list_verdicts(self) -> list[dict[str, Any]]:
        return [self.get_verdict(item["id"]) for item in self._list("verdicts")]

    def register_snapshot(self, side, files):
        """Persist controller-supplied membership only when it matches the frozen binding."""
        if side not in {"base", "head"} or manifest_digest(files) != self.binding.source_environment.get(side):
            raise ValueError("scope manifest differs from frozen snapshot binding")
        encoded = _json(files)
        with self._tx():
            old = self.db.execute("SELECT files FROM snapshot_manifests WHERE side=?", (side,)).fetchone()
            if old and old[0] != encoded:
                raise ValueError("scope manifest changed")
            self.db.execute("INSERT OR IGNORE INTO snapshot_manifests VALUES (?,?)", (side, encoded))

    def snapshot_manifest(self, side):
        row = self.db.execute("SELECT files FROM snapshot_manifests WHERE side=?", (side,)).fetchone()
        if row is None:
            raise ValueError("complete pinned scope manifest is unavailable")
        files = json.loads(row[0])
        if manifest_digest(files) != self.binding.source_environment.get(side):
            raise ValueError("scope manifest differs from frozen snapshot binding")
        return files

    def record_verdict(
        self,
        task_id: str,
        verdict: str,
        source_ids: list[str],
        approved_payload: dict[str, Any] | None = None,
        missing_fact: str | None = None,
        attempted: Any = None,
        recovery_condition: str | None = None,
        reason: str | None = None,
        absence_claims: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        if verdict not in {"accept", "reject", "needs_evidence"}:
            raise ValueError("invalid verdict")
        if verdict in {"accept", "reject"} and not reason:
            raise ValueError("verdict requires reason")
        if verdict == "needs_evidence" and not (missing_fact and attempted and recovery_condition):
            raise ValueError("needs_evidence requires missing_fact, attempted and recovery_condition")
        with self._tx():
            task = self.get_verification(task_id)
            finding = self.get_finding(task["finding_id"])
            if task["finding_revision"] != finding["revision"]:
                raise ValueError("stale verification")
            ids = self._sources(source_ids)
            delivered = set(self.delivered_sources(task["session_id"]))
            if not set(ids).issubset(delivered):
                raise ValueError("source not delivered to verifier")
            if verdict == "accept" and not ids:
                raise ValueError("accept requires delivered original evidence")
            if verdict == "accept" and all(
                self.get_source(sid)["metadata"].get("metadata", {}).get("check_type") == "closed-text-1" for sid in ids
            ):
                raise ValueError("accept also requires original triggering code; a check artifact has no code location")
            premises = absence_claims if absence_claims is not None else []
            validate_absence(premises, self.get_source, set(ids), delivered, self.snapshot_manifest)
            if approved_payload is not None and approved_payload != finding["claim"]:
                raise ValueError("approved payload differs from verified finding")
            payload = finding["claim"] if verdict == "accept" else None
            previous = self.db.execute("SELECT * FROM verdicts WHERE task_id=?", (task_id,)).fetchone()
            fields = (
                verdict,
                _json(ids),
                _json(payload) if payload is not None else None,
                missing_fact,
                _json(attempted) if attempted is not None else None,
                recovery_condition,
                reason,
            )
            if previous:
                prior_premises = self.get_verdict(previous["id"]).get("absence_claims", [])
                if prior_premises != premises:
                    raise ValueError("verification already decided with different absence premises")
                if (
                    tuple(
                        previous[k]
                        for k in (
                            "verdict",
                            "source_ids",
                            "approved_payload",
                            "missing_fact",
                            "attempted",
                            "recovery_condition",
                            "reason",
                        )
                    )
                    != fields
                ):
                    raise ValueError("verification already decided")
                return self._decode(previous)
            decisive = self.db.execute(
                "SELECT id FROM verdicts WHERE finding_id=? AND finding_revision=? AND verdict IN ('accept','reject')",
                (finding["id"], finding["revision"]),
            ).fetchone()
            if decisive:
                raise ValueError("finding revision already decided")
            old = self.get_outbox(finding["id"])
            if old and old["status"] == "uncertain":
                raise ValueError("reconcile uncertain prior delivery before new verdict")
            verdict_id = _id("D")
            self.db.execute(
                "INSERT INTO verdicts VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (verdict_id, task_id, finding["id"], finding["revision"], *fields),
            )
            self.db.execute("INSERT INTO verdict_absence VALUES (?,?)", (verdict_id, _json(premises)))
            self.db.execute("UPDATE verifications SET status=? WHERE id=?", (verdict, task_id))
            self.db.execute("UPDATE findings SET state=? WHERE id=?", (verdict, finding["id"]))
            if verdict == "accept":
                marker_hash = hashlib.sha256(
                    _json([self.binding.repo, self.binding.pr_number, self.binding.head_sha, finding["id"]]).encode()
                ).hexdigest()
                marker = f"<!-- reviewforge-finding:{marker_hash} -->"
                self.db.execute(
                    """INSERT INTO outbox VALUES (?,?,?,?,?,NULL,'intended',NULL,NULL)
                    ON CONFLICT(finding_id) DO UPDATE SET finding_revision=excluded.finding_revision,
                    verdict_id=excluded.verdict_id,approved_payload=excluded.approved_payload,
                    body=NULL,status='intended'""",
                    (finding["id"], finding["revision"], verdict_id, _json(payload), marker),
                )
            elif verdict == "reject":
                if old and old["remote_id"]:
                    self.db.execute(
                        "UPDATE outbox SET finding_revision=?,verdict_id=?,body=NULL,status='intended' "
                        "WHERE finding_id=?",
                        (finding["revision"], verdict_id, finding["id"]),
                    )
                elif old:
                    self.db.execute("DELETE FROM outbox WHERE finding_id=?", (finding["id"],))
            elif verdict == "needs_evidence" and old:
                if old["remote_id"] and old["last_delivered_body"]:
                    self.db.execute(
                        "UPDATE outbox SET finding_revision=?,verdict_id=?,approved_payload=?,body=NULL,"
                        "status='intended' WHERE finding_id=?",
                        (finding["revision"], verdict_id, _json(finding["claim"]), finding["id"]),
                    )
                elif not old["remote_id"]:
                    self.db.execute("DELETE FROM outbox WHERE finding_id=?", (finding["id"],))
        return self.get_verdict(verdict_id)

    def get_outbox(self, finding_id: str) -> dict[str, Any] | None:
        row = self.db.execute(
            "SELECT o.*,c.retry_not_before FROM outbox o LEFT JOIN delivery_cooldowns c USING(finding_id) "
            "WHERE o.finding_id=?",
            (finding_id,),
        ).fetchone()
        return self._decode(row) if row else None

    def list_outbox(self) -> list[dict[str, Any]]:
        return [
            self._decode(row)
            for row in self.db.execute(
                "SELECT o.*,c.retry_not_before FROM outbox o LEFT JOIN delivery_cooldowns c USING(finding_id) "
                "ORDER BY o.rowid"
            )
        ]

    def _current_outbox(self, finding_id: str, finding_revision: int) -> dict[str, Any]:
        item = self.get_outbox(finding_id)
        finding = self.get_finding(finding_id)
        if (
            item is None
            or item["finding_revision"] != finding_revision
            or finding["revision"] != finding_revision
            or not (
                finding["state"] == "accept" or (finding["state"] in {"reject", "needs_evidence"} and item["remote_id"])
            )
        ):
            raise ValueError("stale or absent accepted outbox")
        return item

    def freeze_outbox(self, finding_id: str, finding_revision: int, body: str) -> dict[str, Any]:
        _nonempty(body, "body")
        with self._tx():
            item = self._current_outbox(finding_id, finding_revision)
            if item["body"] is not None and item["body"] != body:
                raise ValueError("body already frozen")
            self.db.execute("UPDATE outbox SET body=? WHERE finding_id=?", (body, finding_id))
        return self.get_outbox(finding_id)  # type: ignore[return-value]

    def transition_outbox(
        self,
        finding_id: str,
        finding_revision: int,
        expected_status: str,
        status: str,
        remote_id: int | None = None,
        retry_not_before: float | None = None,
    ) -> dict[str, Any]:
        if status not in {"intended", "delivered", "uncertain", "head_changed"}:
            raise ValueError("invalid outbox status")
        if remote_id is not None and (not isinstance(remote_id, int) or isinstance(remote_id, bool) or remote_id <= 0):
            raise ValueError("invalid remote comment id")
        if retry_not_before is not None and (
            expected_status != "uncertain"
            or status != "intended"
            or isinstance(retry_not_before, bool)
            or not isinstance(retry_not_before, (int, float))
            or not math.isfinite(retry_not_before)
            or retry_not_before <= 0
        ):
            raise ValueError("invalid retry cooldown")
        with self._tx():
            item = self._current_outbox(finding_id, finding_revision)
            if item["status"] != expected_status:
                raise ValueError("outbox status changed")
            if expected_status == "intended" and status == "uncertain":
                if item["retry_not_before"] is not None and item["retry_not_before"] > time.time():
                    raise ValueError("delivery retry cooldown active")
                self.db.execute("DELETE FROM delivery_cooldowns WHERE finding_id=?", (finding_id,))
            if retry_not_before is not None:
                self.db.execute(
                    "INSERT INTO delivery_cooldowns VALUES (?,?) ON CONFLICT(finding_id) "
                    "DO UPDATE SET retry_not_before=MAX(retry_not_before,excluded.retry_not_before)",
                    (finding_id, retry_not_before),
                )
            if status == "delivered" and not (remote_id or item["remote_id"]):
                raise ValueError("delivered requires remote comment id")
            if status == "delivered" and not item["body"]:
                raise ValueError("delivered requires frozen body")
            if remote_id and item["remote_id"] and remote_id != item["remote_id"]:
                raise ValueError("remote comment id changed")
            self.db.execute(
                "UPDATE outbox SET status=?,remote_id=COALESCE(?,remote_id), "
                "last_delivered_body=CASE WHEN (?='delivered' OR (?='head_changed' AND ? IS NOT NULL)) "
                "AND COALESCE(?,remote_id) IS NOT NULL AND body IS NOT NULL "
                "THEN body ELSE last_delivered_body END WHERE finding_id=?",
                (status, remote_id, status, status, remote_id, remote_id, finding_id),
            )
        return self.get_outbox(finding_id)  # type: ignore[return-value]
