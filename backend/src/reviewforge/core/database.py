"""Database — async SQLite persistence for review history and metrics.

Uses aiosqlite for zero-dependency async SQLite access.
All review runs, findings, and metrics are persisted here
so the dashboard can query historical data.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite

logger = logging.getLogger(__name__)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS review_runs (
    run_id       TEXT PRIMARY KEY,
    repo         TEXT NOT NULL,
    pr_number    INTEGER NOT NULL,
    head_sha     TEXT NOT NULL DEFAULT '',
    base_sha     TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'running',
    started_at   TEXT NOT NULL,
    completed_at TEXT DEFAULT NULL,
    summary_json TEXT DEFAULT '{}',
    task_checkpoint_version INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS review_findings (
    id           TEXT PRIMARY KEY,
    run_id       TEXT NOT NULL,
    file         TEXT NOT NULL,
    line         INTEGER NOT NULL DEFAULT 0,
    severity     TEXT NOT NULL DEFAULT 'info',
    category     TEXT NOT NULL DEFAULT '',
    message      TEXT NOT NULL DEFAULT '',
    suggestion   TEXT NOT NULL DEFAULT '',
    confidence   REAL NOT NULL DEFAULT 0.5,
    reviewer     TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'candidate',
    verified_by  TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (run_id) REFERENCES review_runs(run_id)
);

CREATE TABLE IF NOT EXISTS reviewer_metrics (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        TEXT NOT NULL,
    reviewer_name TEXT NOT NULL,
    findings_count INTEGER NOT NULL DEFAULT 0,
    duration_ms   INTEGER NOT NULL DEFAULT 0,
    status        TEXT NOT NULL DEFAULT 'completed',
    error         TEXT NOT NULL DEFAULT '',
    prompt_tokens     INTEGER DEFAULT 0,
    completion_tokens INTEGER DEFAULT 0,
    total_tokens      INTEGER DEFAULT 0,
    FOREIGN KEY (run_id) REFERENCES review_runs(run_id)
);

-- Durable task identity/checkpoint history.  reviewer_metrics remains a
-- dashboard/usage table; it is deliberately not a recovery source because a
-- reviewer can own several file chunks and some failures never emit metrics.
CREATE TABLE IF NOT EXISTS review_task_checkpoints (
    run_id        TEXT NOT NULL,
    task_id       TEXT NOT NULL,
    attempt       INTEGER NOT NULL DEFAULT 1,
    round_id      TEXT NOT NULL DEFAULT '',
    reviewer_name TEXT NOT NULL,
    files_json    TEXT NOT NULL DEFAULT '[]',
    task_signature TEXT NOT NULL,
    task_kind     TEXT NOT NULL DEFAULT 'reviewer',
    rationale     TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL DEFAULT 'pending',
    error         TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    PRIMARY KEY (run_id, task_id, attempt),
    FOREIGN KEY (run_id) REFERENCES review_runs(run_id)
);

-- Planner proposals are a round-level decision.  The sealed envelope proves
-- that every task in the proposal was checkpointed in the same transaction.
CREATE TABLE IF NOT EXISTS review_task_rounds (
    run_id        TEXT NOT NULL,
    round_id      TEXT NOT NULL,
    task_count    INTEGER NOT NULL,
    task_hash     TEXT NOT NULL,
    sealed        INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL,
    sealed_at     TEXT DEFAULT NULL,
    PRIMARY KEY (run_id, round_id),
    FOREIGN KEY (run_id) REFERENCES review_runs(run_id)
);

-- Append-only hypothesis history.  Resume selects the latest revision for
-- each identity; prior revisions remain available for audit.
CREATE TABLE IF NOT EXISTS hypotheses (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        TEXT NOT NULL,
    identity      TEXT NOT NULL,
    hypothesis_json TEXT NOT NULL,
    head_sha      TEXT NOT NULL DEFAULT '',
    workspace_digest TEXT NOT NULL DEFAULT '',
    updated_at    TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES review_runs(run_id)
);

CREATE TABLE IF NOT EXISTS observations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT NOT NULL,
    hypothesis_id   TEXT NOT NULL,
    observation_id  TEXT NOT NULL,
    observation_json TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES review_runs(run_id)
);

-- Token usage tracking per agent per run
CREATE TABLE IF NOT EXISTS token_usage (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id           TEXT NOT NULL,
    agent_name       TEXT NOT NULL,
    prompt_tokens    INTEGER DEFAULT 0,
    completion_tokens INTEGER DEFAULT 0,
    total_tokens     INTEGER DEFAULT 0,
    model            TEXT DEFAULT '',
    created_at       TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES review_runs(run_id)
);

-- Code symbols: functions/classes defined in reviewed code
CREATE TABLE IF NOT EXISTS code_symbols (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path       TEXT NOT NULL,
    symbol_name     TEXT NOT NULL,
    symbol_type     TEXT NOT NULL,
    risk_level      TEXT DEFAULT 'safe',
    risk_categories TEXT DEFAULT '[]',
    defined_in_run  TEXT NOT NULL,
    pr_number       INTEGER DEFAULT 0,
    language        TEXT DEFAULT '',
    UNIQUE(file_path, symbol_name)
);

-- Code relations: import and call relationships
CREATE TABLE IF NOT EXISTS code_relations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        TEXT NOT NULL,
    source_file   TEXT NOT NULL,
    source_symbol TEXT NOT NULL DEFAULT '',
    target_file   TEXT NOT NULL DEFAULT '',
    target_symbol TEXT NOT NULL DEFAULT '',
    relation_type TEXT NOT NULL,
    UNIQUE(run_id, source_file, source_symbol, target_file, target_symbol, relation_type)
);

-- File risk summary cache
CREATE TABLE IF NOT EXISTS file_risk_summary (
    file_path       TEXT PRIMARY KEY,
    max_risk        TEXT NOT NULL DEFAULT 'safe',
    risk_categories TEXT DEFAULT '[]',
    findings_count  INTEGER DEFAULT 0,
    last_run_id     TEXT,
    last_updated    TEXT
);

-- Source-grounded repository wiki pages.  Pages are compact, deterministic
-- code facts rather than model-authored prose, so every statement remains
-- traceable to an immutable repository revision and line range.
CREATE TABLE IF NOT EXISTS wiki_pages (
    repo          TEXT NOT NULL,
    page_key      TEXT NOT NULL,
    kind          TEXT NOT NULL,
    title         TEXT NOT NULL,
    content_json  TEXT NOT NULL DEFAULT '{}',
    search_terms  TEXT NOT NULL DEFAULT '',
    source_path   TEXT NOT NULL,
    source_sha    TEXT NOT NULL DEFAULT '',
    source_start  INTEGER NOT NULL DEFAULT 0,
    source_end    INTEGER NOT NULL DEFAULT 0,
    content_hash  TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    PRIMARY KEY (repo, page_key, source_sha)
);

CREATE INDEX IF NOT EXISTS idx_findings_run ON review_findings(run_id);
CREATE INDEX IF NOT EXISTS idx_findings_file ON review_findings(file);
CREATE INDEX IF NOT EXISTS idx_findings_category ON review_findings(category);
CREATE INDEX IF NOT EXISTS idx_metrics_run ON reviewer_metrics(run_id);
CREATE INDEX IF NOT EXISTS idx_task_checkpoints_run ON review_task_checkpoints(run_id, task_id, attempt);
CREATE INDEX IF NOT EXISTS idx_task_checkpoints_round ON review_task_checkpoints(run_id, round_id);
CREATE INDEX IF NOT EXISTS idx_hypotheses_run_identity ON hypotheses(run_id, identity, id);
CREATE INDEX IF NOT EXISTS idx_observations_run_hypothesis ON observations(run_id, hypothesis_id, id);
CREATE INDEX IF NOT EXISTS idx_runs_repo ON review_runs(repo);
CREATE INDEX IF NOT EXISTS idx_symbols_file ON code_symbols(file_path);
CREATE INDEX IF NOT EXISTS idx_symbols_risk ON code_symbols(risk_level);
CREATE INDEX IF NOT EXISTS idx_symbols_name ON code_symbols(symbol_name);
CREATE INDEX IF NOT EXISTS idx_relations_source ON code_relations(source_file);
CREATE INDEX IF NOT EXISTS idx_relations_target ON code_relations(target_file, target_symbol);
CREATE INDEX IF NOT EXISTS idx_risk_max ON file_risk_summary(max_risk);
CREATE INDEX IF NOT EXISTS idx_token_run ON token_usage(run_id);
CREATE INDEX IF NOT EXISTS idx_wiki_repo_path ON wiki_pages(repo, source_path);
CREATE INDEX IF NOT EXISTS idx_wiki_repo_title ON wiki_pages(repo, title);
"""


class Database:
    """Async SQLite database for review persistence."""

    def __init__(self, db_path: str | Path = ".reviewforge/reviewforge.db") -> None:
        self._db_path = Path(db_path)
        self._db: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        """Open connection and initialize schema."""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(str(self._db_path))
        self._db.row_factory = aiosqlite.Row
        # B11: 启用外键、WAL 模式、busy_timeout
        await self._db.execute("PRAGMA foreign_keys=ON")
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA busy_timeout=5000")
        await self._db.executescript(SCHEMA_SQL)
        await self._migrate_schema()
        await self._db.commit()
        logger.info(f"Database connected: {self._db_path}")

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    async def _migrate_schema(self) -> None:
        """Apply additive schema migrations for existing SQLite databases."""

        cursor = await self._db.execute("PRAGMA table_info(review_runs)")
        run_columns = {row["name"] for row in await cursor.fetchall()}
        if "task_checkpoint_version" not in run_columns:
            # Existing runs get version 0 and are intentionally treated as
            # untrusted for resume.  Metrics cannot reconstruct task chunks.
            await self._db.execute(
                "ALTER TABLE review_runs ADD COLUMN task_checkpoint_version INTEGER NOT NULL DEFAULT 0"
            )

        cursor = await self._db.execute("PRAGMA table_info(review_task_checkpoints)")
        checkpoint_columns = {row["name"] for row in await cursor.fetchall()}
        if "round_id" not in checkpoint_columns:
            await self._db.execute("ALTER TABLE review_task_checkpoints ADD COLUMN round_id TEXT NOT NULL DEFAULT ''")

        cursor = await self._db.execute("PRAGMA table_info(code_relations)")
        columns = {row["name"] for row in await cursor.fetchall()}
        if "source_symbol" not in columns:
            await self._db.execute("ALTER TABLE code_relations RENAME TO code_relations_old")
            await self._db.execute(
                """
                CREATE TABLE code_relations (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id        TEXT NOT NULL,
                    source_file   TEXT NOT NULL,
                    source_symbol TEXT NOT NULL DEFAULT '',
                    target_file   TEXT NOT NULL DEFAULT '',
                    target_symbol TEXT NOT NULL DEFAULT '',
                    relation_type TEXT NOT NULL,
                    UNIQUE(run_id, source_file, source_symbol, target_file, target_symbol, relation_type)
                )
                """
            )
            await self._db.execute(
                """
                INSERT OR IGNORE INTO code_relations
                    (run_id, source_file, source_symbol, target_file, target_symbol, relation_type)
                SELECT run_id, source_file, '', target_file, target_symbol, relation_type
                FROM code_relations_old
                """
            )
            await self._db.execute("DROP TABLE code_relations_old")
        await self._db.execute("CREATE INDEX IF NOT EXISTS idx_relations_source ON code_relations(source_file)")
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_relations_target ON code_relations(target_file, target_symbol)"
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_relations_source_symbol ON code_relations(source_file, source_symbol)"
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_task_checkpoints_run ON review_task_checkpoints(run_id, task_id, attempt)"
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_task_checkpoints_round ON review_task_checkpoints(run_id, round_id)"
        )

    # ── Review Runs ──────────────────────────────────────────────

    async def create_run(
        self,
        run_id: str,
        repo: str,
        pr_number: int,
        head_sha: str = "",
        base_sha: str = "",
    ) -> None:
        now = datetime.now(UTC).isoformat()
        await self._db.execute(
            "INSERT INTO review_runs "
            "(run_id, repo, pr_number, head_sha, base_sha, status, started_at, task_checkpoint_version) "
            "VALUES (?, ?, ?, ?, ?, 'running', ?, 2)",
            (run_id, repo, pr_number, head_sha, base_sha, now),
        )
        await self._db.commit()

    async def complete_run(self, run_id: str, summary: dict[str, Any]) -> None:
        now = datetime.now(UTC).isoformat()
        try:
            cursor = await self._db.execute(
                "UPDATE review_runs SET status='completed', completed_at=?, summary_json=? WHERE run_id=?",
                (now, json.dumps(summary, ensure_ascii=False), run_id),
            )
            if (cursor.rowcount or 0) != 1:
                raise LookupError(f"review run not found: {run_id}")
            await self._db.commit()
        except Exception:
            await self._db.rollback()
            raise

    async def fail_run(self, run_id: str, error: str, summary: dict[str, Any] | None = None) -> None:
        now = datetime.now(UTC).isoformat()
        payload = dict(summary or {})
        payload["error"] = error
        try:
            cursor = await self._db.execute(
                "UPDATE review_runs SET status='failed', completed_at=?, summary_json=? WHERE run_id=?",
                (now, json.dumps(payload, ensure_ascii=False), run_id),
            )
            if (cursor.rowcount or 0) != 1:
                raise LookupError(f"review run not found: {run_id}")
            await self._db.commit()
        except Exception:
            await self._db.rollback()
            raise

    async def restart_run(self, run_id: str) -> bool:
        """Atomically claim a failed/stale run for retry.

        Returning ``False`` means another worker already claimed or completed
        the run, so the caller must not execute it concurrently.
        """

        now = datetime.now(UTC).isoformat()
        cursor = await self._db.execute(
            "UPDATE review_runs SET status='running', started_at=?, completed_at=NULL, summary_json='{}' "
            "WHERE run_id=? AND "
            "(status='failed' OR "
            "(status='running' AND julianday(started_at) <= julianday('now', '-15 minutes')))",
            (now, run_id),
        )
        await self._db.commit()
        return (cursor.rowcount or 0) == 1

    async def fail_running_runs(self, error: str) -> int:
        """Mark orphaned running runs as failed, returning the affected count."""
        now = datetime.now(UTC).isoformat()
        cursor = await self._db.execute(
            "UPDATE review_runs SET status='failed', completed_at=?, summary_json=? WHERE status='running'",
            (now, json.dumps({"error": error}, ensure_ascii=False)),
        )
        await self._db.commit()
        return cursor.rowcount or 0

    async def get_runs(
        self,
        repo: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """List runs, collapsing duplicate runs for the same commit.

        A single PR commit can accumulate several runs — a redelivered webhook,
        an ``opened``+``synchronize`` pair, or a resumed/failed retry. Those all
        share the same (repo, pr_number, head_sha), so we keep only the most
        recent run per commit here; distinct commits still each get a row.
        """
        where = "WHERE repo=?" if repo else ""
        params: list[Any] = [repo] if repo else []
        cursor = await self._db.execute(
            f"""
            SELECT run_id, repo, pr_number, head_sha, base_sha, status,
                   started_at, completed_at, summary_json
            FROM (
                SELECT review_runs.*, rowid AS _sequence, ROW_NUMBER() OVER (
                    PARTITION BY repo, pr_number, head_sha
                    ORDER BY started_at DESC, rowid DESC
                ) AS _rn
                FROM review_runs
                {where}
            )
            WHERE _rn = 1
            ORDER BY started_at DESC, _sequence DESC
            LIMIT ? OFFSET ?
            """,
            (*params, limit, offset),
        )
        rows = await cursor.fetchall()
        return [self._row_to_dict(r) for r in rows]

    async def get_run(self, run_id: str) -> dict[str, Any] | None:
        cursor = await self._db.execute(
            "SELECT * FROM review_runs WHERE run_id=?",
            (run_id,),
        )
        row = await cursor.fetchone()
        return self._row_to_dict(row) if row else None

    async def get_resumable_run(self, repo: str, pr_number: int, head_sha: str) -> dict[str, Any] | None:
        """Most recent crashed/stale run for this exact PR head — used to resume.

        Only resumes a 'failed' run, or a 'running' run older than 15 minutes (i.e.
        crashed/orphaned). A run that is still actively running (e.g. a concurrent
        webhook for the same head) is NOT resumed, to avoid two runs colliding on one id.
        """
        cursor = await self._db.execute(
            "SELECT * FROM review_runs WHERE repo=? AND pr_number=? AND head_sha=? "
            "AND (status = 'failed' OR "
            "(status = 'running' AND julianday(started_at) <= julianday('now', '-15 minutes'))) "
            "ORDER BY started_at DESC LIMIT 1",
            (repo, pr_number, head_sha),
        )
        row = await cursor.fetchone()
        return self._row_to_dict(row) if row else None

    async def has_active_run_for_head(self, repo: str, pr_number: int, head_sha: str) -> bool:
        """True if this exact commit was already reviewed or is being reviewed now.

        Complements get_resumable_run: a 'completed' run (already reviewed) or a
        freshly 'running' one (in-flight, <15 min) means a redelivered webhook
        should be ignored instead of spawning a duplicate review. A 'failed' or
        stale 'running' run is NOT active — those are left for get_resumable_run
        to resume.
        """
        cursor = await self._db.execute(
            "SELECT 1 FROM review_runs WHERE repo=? AND pr_number=? AND head_sha=? "
            "AND (status = 'completed' OR "
            "(status = 'running' AND julianday(started_at) > julianday('now', '-15 minutes'))) "
            "LIMIT 1",
            (repo, pr_number, head_sha),
        )
        return await cursor.fetchone() is not None

    # ── Findings ─────────────────────────────────────────────────

    async def insert_finding(self, run_id: str, finding: dict[str, Any]) -> None:
        await self._db.execute(
            "INSERT OR REPLACE INTO review_findings "
            "(id, run_id, file, line, severity, category, message, suggestion, confidence, reviewer, status, verified_by) "  # noqa: E501
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                finding["id"],
                run_id,
                finding["file"],
                finding["line"],
                finding["severity"],
                finding["category"],
                finding["message"],
                finding.get("suggestion", ""),
                finding["confidence"],
                finding.get("reviewer", ""),
                finding.get("status", "candidate"),
                finding.get("verified_by", ""),
            ),
        )
        await self._db.commit()

    async def update_finding_status(
        self,
        finding_id: str,
        status: str,
        verified_by: str = "",
    ) -> None:
        await self._db.execute(
            "UPDATE review_findings SET status=?, verified_by=? WHERE id=?",
            (status, verified_by, finding_id),
        )
        await self._db.commit()

    async def get_findings(
        self,
        run_id: str | None = None,
        status: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        conditions, params = [], []
        if run_id:
            conditions.append("run_id=?")
            params.append(run_id)
        if status:
            conditions.append("status=?")
            params.append(status)
        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        cursor = await self._db.execute(
            f"SELECT * FROM review_findings{where} ORDER BY severity DESC, confidence DESC LIMIT ?",
            (*params, limit),
        )
        rows = await cursor.fetchall()
        return [self._row_to_dict(r) for r in rows]

    async def get_findings_counts(self, run_ids: list[str]) -> dict[str, dict[str, int]]:
        """Finding totals per run in a single query (avoids N+1 when listing runs)."""
        if not run_ids:
            return {}
        placeholders = ",".join("?" * len(run_ids))
        cursor = await self._db.execute(
            f"""
            SELECT run_id,
                   COUNT(*) AS total,
                   SUM(CASE WHEN status IN ('confirmed', 'reported') THEN 1 ELSE 0 END) AS confirmed,
                   SUM(CASE WHEN status = 'false_positive' THEN 1 ELSE 0 END) AS false_positives
            FROM review_findings
            WHERE run_id IN ({placeholders})
            GROUP BY run_id
            """,
            tuple(run_ids),
        )
        out: dict[str, dict[str, int]] = {}
        for r in await cursor.fetchall():
            d = self._row_to_dict(r)
            out[d["run_id"]] = {
                "total": d["total"] or 0,
                "confirmed": d["confirmed"] or 0,
                "false_positives": d["false_positives"] or 0,
            }
        return out

    # ── Reviewer Metrics ─────────────────────────────────────────

    async def insert_metric(
        self,
        run_id: str,
        reviewer_name: str,
        findings_count: int = 0,
        duration_ms: int = 0,
        status: str = "completed",
        error: str = "",
    ) -> None:
        await self._db.execute(
            "INSERT INTO reviewer_metrics (run_id, reviewer_name, findings_count, duration_ms, status, error) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (run_id, reviewer_name, findings_count, duration_ms, status, error),
        )
        await self._db.commit()

    async def get_metrics(self, run_id: str | None = None) -> list[dict[str, Any]]:
        if run_id:
            cursor = await self._db.execute(
                "SELECT * FROM reviewer_metrics WHERE run_id=?",
                (run_id,),
            )
        else:
            cursor = await self._db.execute("SELECT * FROM reviewer_metrics ORDER BY id DESC LIMIT 500")
        rows = await cursor.fetchall()
        return [self._row_to_dict(r) for r in rows]

    # ── Review Task Checkpoints ──────────────────────────────────

    @staticmethod
    def task_signature(reviewer_name: str, files: list[str]) -> str:
        """Return a collision-resistant signature for reviewer + file-set identity.

        File ordering and duplicates do not create a distinct review workload;
        the original ordered list is still retained separately in ``files_json``
        for faithful execution/presentation.
        """

        canonical = json.dumps(
            {"reviewer": reviewer_name, "files": sorted(set(files))},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @classmethod
    def task_round_hash(cls, tasks: list[dict[str, Any]]) -> str:
        """Hash a complete planner proposal independently of insertion order."""

        identities = [
            {
                "task_id": str(task["task_id"]),
                "reviewer_name": str(task["reviewer_name"]),
                "task_signature": cls.task_signature(
                    str(task["reviewer_name"]),
                    list(task["files"]),
                ),
                "task_kind": str(task.get("task_kind", "reviewer")),
                "rationale": str(task.get("rationale", "")),
            }
            for task in tasks
        ]
        identities.sort(key=lambda item: item["task_id"])
        canonical = json.dumps(
            identities,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    async def checkpoint_task_round(
        self,
        *,
        run_id: str,
        round_id: str,
        tasks: list[dict[str, Any]],
    ) -> str:
        """Atomically checkpoint and seal one complete Planner proposal."""

        if not run_id or not round_id:
            raise ValueError("run_id and round_id are required")
        task_ids: set[str] = set()
        normalized: list[dict[str, Any]] = []
        for task in tasks:
            task_id = str(task.get("task_id", ""))
            reviewer_name = str(task.get("reviewer_name", ""))
            files = task.get("files")
            if not task_id or not reviewer_name:
                raise ValueError("planner round tasks require task_id and reviewer_name")
            if task_id in task_ids:
                raise ValueError(f"duplicate task id in planner round: {task_id}")
            if not isinstance(files, list) or not all(isinstance(path, str) for path in files):
                raise ValueError("planner round task files must be a list of strings")
            task_ids.add(task_id)
            normalized.append(
                {
                    "task_id": task_id,
                    "reviewer_name": reviewer_name,
                    "files": list(files),
                    "task_kind": str(task.get("task_kind", "reviewer")),
                    "rationale": str(task.get("rationale", "")),
                }
            )

        task_hash = self.task_round_hash(normalized)
        now = datetime.now(UTC).isoformat()
        try:
            await self._db.execute("BEGIN IMMEDIATE")
            await self._db.execute(
                "INSERT INTO review_task_rounds "
                "(run_id, round_id, task_count, task_hash, sealed, created_at) "
                "VALUES (?, ?, ?, ?, 0, ?)",
                (run_id, round_id, len(normalized), task_hash, now),
            )
            for task in normalized:
                files_json = json.dumps(task["files"], ensure_ascii=False, separators=(",", ":"))
                signature = self.task_signature(task["reviewer_name"], task["files"])
                await self._db.execute(
                    "INSERT INTO review_task_checkpoints "
                    "(run_id, task_id, attempt, round_id, reviewer_name, files_json, task_signature, "
                    "task_kind, rationale, status, error, created_at, updated_at) "
                    "VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, 'pending', '', ?, ?)",
                    (
                        run_id,
                        task["task_id"],
                        round_id,
                        task["reviewer_name"],
                        files_json,
                        signature,
                        task["task_kind"],
                        task["rationale"],
                        now,
                        now,
                    ),
                )
            cursor = await self._db.execute(
                "UPDATE review_task_rounds SET sealed=1, sealed_at=? WHERE run_id=? AND round_id=? AND sealed=0",
                (now, run_id, round_id),
            )
            if (cursor.rowcount or 0) != 1:
                raise RuntimeError(f"could not seal planner task round {run_id}/{round_id}")
            await self._db.commit()
            return task_hash
        except Exception:
            await self._db.rollback()
            raise

    async def upsert_task_checkpoint(
        self,
        *,
        run_id: str,
        task_id: str,
        reviewer_name: str,
        files: list[str],
        status: str,
        error: str = "",
        rationale: str = "",
        task_kind: str = "reviewer",
        round_id: str = "",
    ) -> int:
        """Persist one task transition and return its current attempt number.

        A retry preserves ``task_id`` and opens a new attempt only when a
        previously failed or orphaned claimed attempt is claimed again.  Other
        transitions update the latest attempt in place.  Identity drift for an
        existing ``(run_id, task_id)`` is rejected instead of silently turning
        one checkpoint into a different task.
        """

        allowed_statuses = {"pending", "claimed", "completed", "failed"}
        if status not in allowed_statuses:
            raise ValueError(f"invalid task checkpoint status: {status}")
        if not run_id or not task_id or not reviewer_name:
            raise ValueError("run_id, task_id and reviewer_name are required")
        if not isinstance(files, list) or not all(isinstance(path, str) for path in files):
            raise ValueError("task checkpoint files must be a list of strings")

        files_json = json.dumps(files, ensure_ascii=False, separators=(",", ":"))
        signature = self.task_signature(reviewer_name, files)
        now = datetime.now(UTC).isoformat()
        try:
            cursor = await self._db.execute(
                "SELECT * FROM review_task_checkpoints WHERE run_id=? AND task_id=? ORDER BY attempt DESC LIMIT 1",
                (run_id, task_id),
            )
            row = await cursor.fetchone()
            if row is None:
                attempt = 1
                await self._db.execute(
                    "INSERT INTO review_task_checkpoints "
                    "(run_id, task_id, attempt, round_id, reviewer_name, files_json, task_signature, "
                    "task_kind, rationale, status, error, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        run_id,
                        task_id,
                        attempt,
                        round_id,
                        reviewer_name,
                        files_json,
                        signature,
                        task_kind,
                        rationale,
                        status,
                        error,
                        now,
                        now,
                    ),
                )
            else:
                previous = self._row_to_dict(row)
                if (
                    previous["reviewer_name"] != reviewer_name
                    or previous["task_signature"] != signature
                    or previous["task_kind"] != task_kind
                    or previous["rationale"] != rationale
                    or (round_id and previous["round_id"] != round_id)
                ):
                    raise ValueError(f"task checkpoint identity drift for {run_id}/{task_id}")
                if previous["status"] == "completed" and status != "completed":
                    raise ValueError(f"completed task checkpoint is immutable for {run_id}/{task_id}")
                attempt = int(previous["attempt"])
                if status == "claimed" and previous["status"] in {"failed", "claimed"}:
                    attempt += 1
                    await self._db.execute(
                        "INSERT INTO review_task_checkpoints "
                        "(run_id, task_id, attempt, round_id, reviewer_name, files_json, task_signature, "
                        "task_kind, rationale, status, error, created_at, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            run_id,
                            task_id,
                            attempt,
                            previous["round_id"],
                            reviewer_name,
                            files_json,
                            signature,
                            task_kind,
                            rationale,
                            status,
                            error,
                            now,
                            now,
                        ),
                    )
                else:
                    await self._db.execute(
                        "UPDATE review_task_checkpoints SET status=?, error=?, updated_at=? "
                        "WHERE run_id=? AND task_id=? AND attempt=?",
                        (status, error, now, run_id, task_id, attempt),
                    )
            await self._db.commit()
            return attempt
        except Exception:
            await self._db.rollback()
            raise

    async def get_task_checkpoints(
        self,
        run_id: str,
        *,
        latest_only: bool = True,
    ) -> list[dict[str, Any]]:
        """Return decoded task checkpoints, latest attempt per task by default."""

        if latest_only:
            await self._validate_sealed_task_rounds(run_id)
            sql = """
                SELECT checkpoint.*
                FROM review_task_checkpoints AS checkpoint
                JOIN (
                    SELECT run_id, task_id, MAX(attempt) AS attempt
                    FROM review_task_checkpoints
                    WHERE run_id=?
                    GROUP BY run_id, task_id
                ) AS latest
                 ON checkpoint.run_id=latest.run_id
                 AND checkpoint.task_id=latest.task_id
                 AND checkpoint.attempt=latest.attempt
                WHERE (checkpoint.round_id='' AND checkpoint.task_kind!='reviewer') OR EXISTS (
                    SELECT 1 FROM review_task_rounds AS task_round
                    WHERE task_round.run_id=checkpoint.run_id
                      AND task_round.round_id=checkpoint.round_id
                      AND task_round.sealed=1
                )
                ORDER BY checkpoint.created_at, checkpoint.task_id
            """
        else:
            sql = "SELECT * FROM review_task_checkpoints WHERE run_id=? ORDER BY task_id, attempt"
        cursor = await self._db.execute(sql, (run_id,))
        checkpoints: list[dict[str, Any]] = []
        for row in await cursor.fetchall():
            checkpoint = self._row_to_dict(row)
            try:
                files = json.loads(checkpoint.pop("files_json"))
            except (TypeError, json.JSONDecodeError) as exc:
                raise ValueError(f"invalid task checkpoint files for {run_id}/{checkpoint.get('task_id', '')}") from exc
            if not isinstance(files, list) or not all(isinstance(path, str) for path in files):
                raise ValueError(f"invalid task checkpoint files for {run_id}/{checkpoint.get('task_id', '')}")
            expected_signature = self.task_signature(str(checkpoint["reviewer_name"]), files)
            if checkpoint["task_signature"] != expected_signature:
                raise ValueError(f"invalid task checkpoint signature for {run_id}/{checkpoint['task_id']}")
            checkpoint["files"] = files
            checkpoints.append(checkpoint)
        return checkpoints

    async def get_task_rounds(self, run_id: str) -> list[dict[str, Any]]:
        cursor = await self._db.execute(
            "SELECT * FROM review_task_rounds WHERE run_id=? ORDER BY created_at, round_id",
            (run_id,),
        )
        return [self._row_to_dict(row) for row in await cursor.fetchall()]

    async def _validate_sealed_task_rounds(self, run_id: str) -> None:
        cursor = await self._db.execute(
            "SELECT * FROM review_task_rounds WHERE run_id=? AND sealed=1",
            (run_id,),
        )
        for row in await cursor.fetchall():
            envelope = self._row_to_dict(row)
            task_cursor = await self._db.execute(
                "SELECT * FROM review_task_checkpoints WHERE run_id=? AND round_id=? AND attempt=1 ORDER BY task_id",
                (run_id, envelope["round_id"]),
            )
            tasks: list[dict[str, Any]] = []
            for task_row in await task_cursor.fetchall():
                task = self._row_to_dict(task_row)
                try:
                    files = json.loads(task["files_json"])
                except (TypeError, json.JSONDecodeError) as exc:
                    raise ValueError(f"invalid sealed task round files for {run_id}/{envelope['round_id']}") from exc
                if not isinstance(files, list) or not all(isinstance(path, str) for path in files):
                    raise ValueError(f"invalid sealed task round files for {run_id}/{envelope['round_id']}")
                tasks.append(
                    {
                        "task_id": task["task_id"],
                        "reviewer_name": task["reviewer_name"],
                        "files": files,
                        "task_kind": task["task_kind"],
                        "rationale": task["rationale"],
                    }
                )
            if len(tasks) != int(envelope["task_count"]):
                raise ValueError(f"sealed task round count mismatch for {run_id}/{envelope['round_id']}")
            if self.task_round_hash(tasks) != envelope["task_hash"]:
                raise ValueError(f"sealed task round hash mismatch for {run_id}/{envelope['round_id']}")

    # ── Aggregates (for dashboard) ───────────────────────────────

    async def get_summary_stats(self, repo: str | None = None) -> dict[str, Any]:
        """Global summary: total runs, total findings, confirmation rate."""
        repo_filter = "WHERE r.repo=?" if repo else ""
        params = (repo,) if repo else ()

        cursor = await self._db.execute(
            f"""
            SELECT
                COUNT(DISTINCT r.run_id) as total_runs,
                COUNT(f.id) as total_findings,
                SUM(CASE WHEN f.status IN ('confirmed', 'reported') THEN 1 ELSE 0 END) as confirmed,
                SUM(CASE WHEN f.status='false_positive' THEN 1 ELSE 0 END) as false_positives,
                AVG(CASE WHEN f.status IN ('confirmed', 'reported') THEN f.confidence END) as avg_confidence
            FROM review_runs r
            LEFT JOIN review_findings f ON f.run_id = r.run_id
            {repo_filter}
        """,
            params,
        )
        row = await cursor.fetchone()
        return self._row_to_dict(row) if row else {}

    async def get_category_distribution(self, repo: str | None = None) -> list[dict[str, Any]]:
        """Finding count by category."""
        repo_join = "JOIN review_runs r ON f.run_id=r.run_id WHERE r.repo=?" if repo else ""
        params = (repo,) if repo else ()
        cursor = await self._db.execute(
            f"""
            SELECT category, COUNT(*) as count
            FROM review_findings f
            {repo_join}
            GROUP BY category ORDER BY count DESC
        """,
            params,
        )
        return [self._row_to_dict(r) for r in await cursor.fetchall()]

    async def get_weekly_trends(self, repo: str | None = None, weeks: int = 12) -> list[dict[str, Any]]:
        """Finding count by week."""
        repo_filter = "AND r.repo=?" if repo else ""
        interval = f"-{int(weeks) * 7} days"
        params = (interval, repo) if repo else (interval,)
        async with self._db.execute(
            f"""
            SELECT
                strftime('%Y-W%W', r.started_at) as week,
                COUNT(f.id) as total,
                SUM(CASE WHEN f.status IN ('confirmed', 'reported') THEN 1 ELSE 0 END) as confirmed
            FROM review_runs r
            LEFT JOIN review_findings f ON f.run_id = r.run_id
            WHERE r.started_at > datetime('now', ?)
            {repo_filter}
            GROUP BY week ORDER BY week
        """,
            params,
        ) as cursor:
            return [self._row_to_dict(r) for r in await cursor.fetchall()]

    async def get_hotspot_files(self, repo: str | None = None, limit: int = 10) -> list[dict[str, Any]]:
        """Files with most findings."""
        repo_join = "JOIN review_runs r ON f.run_id=r.run_id WHERE r.repo=?" if repo else ""
        params = (repo,) if repo else ()
        cursor = await self._db.execute(
            f"""
            SELECT file, COUNT(*) as count,
                   SUM(CASE WHEN f.status IN ('confirmed', 'reported') THEN 1 ELSE 0 END) as confirmed
            FROM review_findings f
            {repo_join}
            GROUP BY file ORDER BY count DESC LIMIT ?
        """,
            (*params, limit),
        )
        return [self._row_to_dict(r) for r in await cursor.fetchall()]

    async def get_reviewer_stats(self, repo: str | None = None) -> list[dict[str, Any]]:
        """Per-reviewer statistics."""
        repo_join = "JOIN review_runs r ON m.run_id=r.run_id WHERE r.repo=?" if repo else ""
        params = (repo,) if repo else ()
        cursor = await self._db.execute(
            f"""
            SELECT
                m.reviewer_name,
                COUNT(*) as total_runs,
                SUM(m.findings_count) as total_findings,
                AVG(m.duration_ms) as avg_duration_ms,
                SUM(CASE WHEN m.status='completed' THEN 1 ELSE 0 END) as success_count
            FROM reviewer_metrics m
            {repo_join}
            GROUP BY m.reviewer_name ORDER BY total_findings DESC
        """,
            params,
        )
        return [self._row_to_dict(r) for r in await cursor.fetchall()]

    async def get_recurring_issues(self, repo: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        """Same file + same category appearing in multiple runs."""
        repo_join = "JOIN review_runs r ON f.run_id=r.run_id WHERE r.repo=?" if repo else ""
        params = (repo,) if repo else ()
        cursor = await self._db.execute(
            f"""
            SELECT file, category, COUNT(DISTINCT run_id) as run_count, COUNT(*) as total_count
            FROM review_findings f
            {repo_join}
            GROUP BY file, category
            HAVING run_count > 1
            ORDER BY run_count DESC, total_count DESC
            LIMIT ?
        """,
            (*params, limit),
        )
        return [self._row_to_dict(r) for r in await cursor.fetchall()]

    # ── Repository Wiki ──────────────────────────────────────────

    async def upsert_wiki_page(
        self,
        *,
        repo: str,
        page_key: str,
        kind: str,
        title: str,
        content: dict[str, Any],
        search_terms: list[str],
        source_path: str,
        source_sha: str,
        source_start: int,
        source_end: int,
        content_hash: str,
    ) -> None:
        """Store one revision-anchored wiki page."""

        now = datetime.now(UTC).isoformat()
        await self._db.execute(
            "INSERT INTO wiki_pages "
            "(repo, page_key, kind, title, content_json, search_terms, source_path, source_sha, "
            "source_start, source_end, content_hash, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(repo, page_key, source_sha) DO UPDATE SET "
            "kind=excluded.kind, title=excluded.title, content_json=excluded.content_json, "
            "search_terms=excluded.search_terms, source_path=excluded.source_path, "
            "source_sha=excluded.source_sha, source_start=excluded.source_start, "
            "source_end=excluded.source_end, content_hash=excluded.content_hash, "
            "updated_at=excluded.updated_at",
            (
                repo,
                page_key,
                kind,
                title,
                json.dumps(content, ensure_ascii=False),
                " ".join(dict.fromkeys(term.lower() for term in search_terms if term)),
                source_path,
                source_sha,
                source_start,
                source_end,
                content_hash,
                now,
            ),
        )
        await self._db.commit()

    async def search_wiki_pages(
        self,
        repo: str,
        terms: list[str],
        *,
        limit: int = 12,
        source_sha: str = "",
    ) -> list[dict[str, Any]]:
        """Retrieve wiki pages using bounded exact/lexical hybrid scoring."""

        needles = list(dict.fromkeys(term.strip().lower() for term in terms if len(term.strip()) >= 3))[:12]
        if not repo or not needles:
            return []
        clauses = " OR ".join("lower(title) LIKE ? OR search_terms LIKE ?" for _ in needles)
        params: list[Any] = [repo]
        for needle in needles:
            pattern = f"%{needle}%"
            params.extend((pattern, pattern))
        params.append(max(limit * 4, limit))
        cursor = await self._db.execute(
            f"SELECT * FROM wiki_pages WHERE repo=? AND ({clauses}) ORDER BY updated_at DESC LIMIT ?",
            tuple(params),
        )
        rows = [self._row_to_dict(row) for row in await cursor.fetchall()]
        for row in rows:
            title = str(row.get("title", "")).lower()
            haystack = f"{title} {row.get('search_terms', '')}"
            row["retrieval_score"] = (8 if source_sha and row.get("source_sha") == source_sha else 0) + sum(
                4 if needle == title else 2 if needle in title else 1 if needle in haystack else 0 for needle in needles
            )
            try:
                row["content"] = json.loads(str(row.pop("content_json", "{}")))
            except json.JSONDecodeError:
                row["content"] = {}
        rows.sort(key=lambda row: (-int(row.get("retrieval_score", 0)), str(row.get("title", ""))))
        deduplicated: list[dict[str, Any]] = []
        seen_pages: set[str] = set()
        for row in rows:
            page_key = str(row.get("page_key", ""))
            if page_key in seen_pages:
                continue
            seen_pages.add(page_key)
            deduplicated.append(row)
            if len(deduplicated) >= limit:
                break
        return deduplicated

    # ── Helpers ──────────────────────────────────────────────────

    @staticmethod
    def _row_to_dict(row: Any) -> dict[str, Any]:
        if hasattr(row, "keys"):
            return {k: row[k] for k in row.keys()}
        return dict(row) if row else {}

    # ── Hypothesis ledger ────────────────────────────────────────

    async def append_hypothesis(
        self,
        run_id: str,
        hypothesis: Any,
        *,
        head_sha: str = "",
        workspace_digest: str = "",
    ) -> None:
        """Append one immutable hypothesis revision and its observations."""

        payload = hypothesis.to_dict() if hasattr(hypothesis, "to_dict") else dict(hypothesis)
        now = datetime.now(UTC).isoformat()
        await self._db.execute(
            "INSERT INTO hypotheses "
            "(run_id, identity, hypothesis_json, head_sha, workspace_digest, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                run_id,
                str(payload["identity"]),
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                head_sha,
                workspace_digest,
                now,
            ),
        )
        for observation in payload.get("observations", []):
            await self._db.execute(
                "INSERT INTO observations "
                "(run_id, hypothesis_id, observation_id, observation_json, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    run_id,
                    str(payload["id"]),
                    str(observation["id"]),
                    json.dumps(observation, ensure_ascii=False, sort_keys=True),
                    now,
                ),
            )
        await self._db.commit()

    async def load_hypothesis_ledger(self, run_id: str):
        """Rebuild a ledger from the latest append-only revision per identity."""

        from reviewforge.engine.hypothesis import Hypothesis, HypothesisLedger

        cursor = await self._db.execute(
            "SELECT h.hypothesis_json, h.head_sha, h.workspace_digest "
            "FROM hypotheses h JOIN ("
            " SELECT identity, MAX(id) AS max_id FROM hypotheses WHERE run_id = ? GROUP BY identity"
            ") latest ON latest.max_id = h.id ORDER BY h.identity",
            (run_id,),
        )
        rows = await cursor.fetchall()
        if not rows:
            return None
        ledger = HypothesisLedger(
            run_id=run_id,
            head_sha=str(rows[0]["head_sha"]),
            workspace_digest=str(rows[0]["workspace_digest"]),
        )
        for row in rows:
            hypothesis = Hypothesis.from_dict(json.loads(row["hypothesis_json"]))
            ledger.items[hypothesis.identity] = hypothesis
        return ledger

    # ── Token Usage ──────────────────────────────────────────────

    async def record_token_usage(
        self,
        run_id: str,
        agent_name: str,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        total_tokens: int = 0,
        model: str = "",
    ) -> None:
        now = datetime.now(UTC).isoformat()
        await self._db.execute(
            "INSERT INTO token_usage (run_id, agent_name, prompt_tokens, completion_tokens, total_tokens, model, created_at) "  # noqa: E501
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (run_id, agent_name, prompt_tokens, completion_tokens, total_tokens, model, now),
        )
        await self._db.commit()

    async def get_token_usage(self, run_id: str | None = None) -> list[dict[str, Any]]:
        if run_id:
            cursor = await self._db.execute(
                "SELECT * FROM token_usage WHERE run_id=? ORDER BY id",
                (run_id,),
            )
        else:
            cursor = await self._db.execute(
                "SELECT * FROM token_usage ORDER BY created_at DESC LIMIT 500",
            )
        return [self._row_to_dict(r) for r in await cursor.fetchall()]

    async def get_token_totals(self, run_ids: list[str]) -> dict[str, int]:
        """Total tokens per run in a single query (avoids N+1 when listing runs)."""
        if not run_ids:
            return {}
        placeholders = ",".join("?" * len(run_ids))
        cursor = await self._db.execute(
            f"SELECT run_id, SUM(total_tokens) AS total FROM token_usage "
            f"WHERE run_id IN ({placeholders}) GROUP BY run_id",
            tuple(run_ids),
        )
        out: dict[str, int] = {}
        for r in await cursor.fetchall():
            d = self._row_to_dict(r)
            out[d["run_id"]] = d["total"] or 0
        return out

    async def get_token_summary(self, repo: str | None = None) -> dict[str, Any]:
        repo_join = "JOIN review_runs r ON t.run_id=r.run_id WHERE r.repo=?" if repo else ""
        params = (repo,) if repo else ()
        cursor = await self._db.execute(
            f"""
            SELECT
                SUM(t.prompt_tokens) as total_prompt,
                SUM(t.completion_tokens) as total_completion,
                SUM(t.total_tokens) as total_tokens,
                COUNT(DISTINCT t.run_id) as run_count
            FROM token_usage t
            {repo_join}
        """,
            params,
        )
        row = await cursor.fetchone()
        return self._row_to_dict(row) if row else {}

    async def get_token_by_agent(self, repo: str | None = None) -> list[dict[str, Any]]:
        repo_join = "JOIN review_runs r ON t.run_id=r.run_id WHERE r.repo=?" if repo else ""
        params = (repo,) if repo else ()
        cursor = await self._db.execute(
            f"""
            SELECT
                t.agent_name,
                SUM(t.total_tokens) as total_tokens,
                COUNT(*) as call_count,
                AVG(t.total_tokens) as avg_tokens
            FROM token_usage t
            {repo_join}
            GROUP BY t.agent_name ORDER BY total_tokens DESC
        """,
            params,
        )
        return [self._row_to_dict(r) for r in await cursor.fetchall()]

    # ── Code Graph (Symbols & Relations) ─────────────────────────

    async def upsert_symbol(
        self,
        file_path: str,
        symbol_name: str,
        symbol_type: str,
        run_id: str,
        pr_number: int = 0,
        language: str = "",
        risk_level: str = "safe",
        risk_categories: list[str] | None = None,
    ) -> None:
        cats = json.dumps(risk_categories or [], ensure_ascii=False)
        await self._db.execute(
            "INSERT INTO code_symbols (file_path, symbol_name, symbol_type, risk_level, risk_categories, defined_in_run, pr_number, language) "  # noqa: E501
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(file_path, symbol_name) DO UPDATE SET "
            "risk_level=excluded.risk_level, risk_categories=excluded.risk_categories, "
            "defined_in_run=excluded.defined_in_run, pr_number=excluded.pr_number",
            (file_path, symbol_name, symbol_type, risk_level, cats, run_id, pr_number, language),
        )
        await self._db.commit()

    async def get_symbol(self, file_path: str, symbol_name: str) -> dict[str, Any] | None:
        cursor = await self._db.execute(
            "SELECT * FROM code_symbols WHERE file_path=? AND symbol_name=?",
            (file_path, symbol_name),
        )
        row = await cursor.fetchone()
        return self._row_to_dict(row) if row else None

    async def get_risky_symbols(self, file_path: str | None = None) -> list[dict[str, Any]]:
        if file_path:
            cursor = await self._db.execute(
                "SELECT * FROM code_symbols WHERE file_path=? AND risk_level != 'safe'",
                (file_path,),
            )
        else:
            cursor = await self._db.execute(
                "SELECT * FROM code_symbols WHERE risk_level != 'safe' ORDER BY pr_number DESC LIMIT 500",
            )
        return [self._row_to_dict(r) for r in await cursor.fetchall()]

    async def find_risky_symbols_by_name(self, symbol_name: str) -> list[dict[str, Any]]:
        cursor = await self._db.execute(
            "SELECT * FROM code_symbols WHERE symbol_name=? AND risk_level != 'safe' ORDER BY pr_number DESC LIMIT 50",
            (symbol_name,),
        )
        return [self._row_to_dict(r) for r in await cursor.fetchall()]

    async def find_symbols_by_name(self, symbol_name: str) -> list[dict[str, Any]]:
        """Return recent definitions regardless of risk classification."""
        cursor = await self._db.execute(
            "SELECT * FROM code_symbols WHERE symbol_name=? ORDER BY pr_number DESC LIMIT 50",
            (symbol_name,),
        )
        return [self._row_to_dict(r) for r in await cursor.fetchall()]

    async def upsert_relation(
        self,
        run_id: str,
        source_file: str,
        target_file: str,
        target_symbol: str,
        relation_type: str,
        source_symbol: str = "",
    ) -> None:
        await self._db.execute(
            "INSERT OR REPLACE INTO code_relations "
            "(run_id, source_file, source_symbol, target_file, target_symbol, relation_type) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (run_id, source_file, source_symbol, target_file, target_symbol, relation_type),
        )
        await self._db.commit()

    async def get_relations_from(self, source_file: str) -> list[dict[str, Any]]:
        cursor = await self._db.execute(
            "SELECT * FROM code_relations WHERE source_file=?",
            (source_file,),
        )
        return [self._row_to_dict(r) for r in await cursor.fetchall()]

    async def get_relations_from_symbol(self, source_file: str, source_symbol: str) -> list[dict[str, Any]]:
        cursor = await self._db.execute(
            "SELECT * FROM code_relations WHERE source_file=? AND source_symbol=?",
            (source_file, source_symbol),
        )
        return [self._row_to_dict(r) for r in await cursor.fetchall()]

    async def get_relations_to(self, target_file: str) -> list[dict[str, Any]]:
        cursor = await self._db.execute(
            "SELECT * FROM code_relations WHERE target_file=?",
            (target_file,),
        )
        return [self._row_to_dict(r) for r in await cursor.fetchall()]

    async def find_relations_to_symbol(self, target_symbol: str) -> list[dict[str, Any]]:
        """Return recent graph edges targeting one symbol."""
        cursor = await self._db.execute(
            "SELECT * FROM code_relations WHERE target_symbol=? ORDER BY id DESC LIMIT 50",
            (target_symbol,),
        )
        return [self._row_to_dict(r) for r in await cursor.fetchall()]

    async def upsert_file_risk(
        self,
        file_path: str,
        max_risk: str,
        risk_categories: list[str] | None = None,
        findings_count: int = 0,
        run_id: str = "",
    ) -> None:
        now = datetime.now(UTC).isoformat()
        cats = json.dumps(risk_categories or [], ensure_ascii=False)
        await self._db.execute(
            "INSERT INTO file_risk_summary (file_path, max_risk, risk_categories, findings_count, last_run_id, last_updated) "  # noqa: E501
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(file_path) DO UPDATE SET "
            "max_risk=excluded.max_risk, risk_categories=excluded.risk_categories, "
            "findings_count=excluded.findings_count, last_run_id=excluded.last_run_id, last_updated=excluded.last_updated",  # noqa: E501
            (file_path, max_risk, cats, findings_count, run_id, now),
        )
        await self._db.commit()

    async def get_file_risk(self, file_path: str) -> dict[str, Any] | None:
        cursor = await self._db.execute(
            "SELECT * FROM file_risk_summary WHERE file_path=?",
            (file_path,),
        )
        row = await cursor.fetchone()
        return self._row_to_dict(row) if row else None

    async def find_risky_files_for_import(self, import_source: str) -> list[dict[str, Any]]:
        """Find files matching an import path that have known risks."""
        # Match by suffix: import 'utils/data' should match 'backend/src/utils/data.py'
        pattern = f"%{import_source.replace('.', '/')}%"
        cursor = await self._db.execute(
            "SELECT * FROM file_risk_summary WHERE file_path LIKE ? AND max_risk != 'safe'",
            (pattern,),
        )
        return [self._row_to_dict(r) for r in await cursor.fetchall()]
