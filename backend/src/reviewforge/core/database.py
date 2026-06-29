"""Database — async SQLite persistence for review history and metrics.

Uses aiosqlite for zero-dependency async SQLite access.
All review runs, findings, and metrics are persisted here
so the dashboard can query historical data.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
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
    summary_json TEXT DEFAULT '{}'
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
    target_file   TEXT NOT NULL DEFAULT '',
    target_symbol TEXT NOT NULL DEFAULT '',
    relation_type TEXT NOT NULL,
    UNIQUE(run_id, source_file, target_file, target_symbol)
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

CREATE INDEX IF NOT EXISTS idx_findings_run ON review_findings(run_id);
CREATE INDEX IF NOT EXISTS idx_findings_file ON review_findings(file);
CREATE INDEX IF NOT EXISTS idx_findings_category ON review_findings(category);
CREATE INDEX IF NOT EXISTS idx_metrics_run ON reviewer_metrics(run_id);
CREATE INDEX IF NOT EXISTS idx_runs_repo ON review_runs(repo);
CREATE INDEX IF NOT EXISTS idx_symbols_file ON code_symbols(file_path);
CREATE INDEX IF NOT EXISTS idx_symbols_risk ON code_symbols(risk_level);
CREATE INDEX IF NOT EXISTS idx_symbols_name ON code_symbols(symbol_name);
CREATE INDEX IF NOT EXISTS idx_relations_source ON code_relations(source_file);
CREATE INDEX IF NOT EXISTS idx_relations_target ON code_relations(target_file, target_symbol);
CREATE INDEX IF NOT EXISTS idx_risk_max ON file_risk_summary(max_risk);
CREATE INDEX IF NOT EXISTS idx_token_run ON token_usage(run_id);
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
        await self._db.commit()
        logger.info(f"Database connected: {self._db_path}")

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    # ── Review Runs ──────────────────────────────────────────────

    async def create_run(
        self, run_id: str, repo: str, pr_number: int,
        head_sha: str = "", base_sha: str = "",
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        await self._db.execute(
            "INSERT INTO review_runs (run_id, repo, pr_number, head_sha, base_sha, status, started_at) "
            "VALUES (?, ?, ?, ?, ?, 'running', ?)",
            (run_id, repo, pr_number, head_sha, base_sha, now),
        )
        await self._db.commit()

    async def complete_run(self, run_id: str, summary: dict[str, Any]) -> None:
        now = datetime.now(timezone.utc).isoformat()
        await self._db.execute(
            "UPDATE review_runs SET status='completed', completed_at=?, summary_json=? WHERE run_id=?",
            (now, json.dumps(summary, ensure_ascii=False), run_id),
        )
        await self._db.commit()

    async def fail_run(self, run_id: str, error: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        await self._db.execute(
            "UPDATE review_runs SET status='failed', completed_at=?, summary_json=? WHERE run_id=?",
            (now, json.dumps({"error": error}), run_id),
        )
        await self._db.commit()

    async def get_runs(
        self, repo: str | None = None, limit: int = 50, offset: int = 0,
    ) -> list[dict[str, Any]]:
        if repo:
            cursor = await self._db.execute(
                "SELECT * FROM review_runs WHERE repo=? ORDER BY started_at DESC LIMIT ? OFFSET ?",
                (repo, limit, offset),
            )
        else:
            cursor = await self._db.execute(
                "SELECT * FROM review_runs ORDER BY started_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            )
        rows = await cursor.fetchall()
        return [self._row_to_dict(r) for r in rows]

    async def get_run(self, run_id: str) -> dict[str, Any] | None:
        cursor = await self._db.execute(
            "SELECT * FROM review_runs WHERE run_id=?", (run_id,),
        )
        row = await cursor.fetchone()
        return self._row_to_dict(row) if row else None

    # ── Findings ─────────────────────────────────────────────────

    async def insert_finding(self, run_id: str, finding: dict[str, Any]) -> None:
        await self._db.execute(
            "INSERT OR REPLACE INTO review_findings "
            "(id, run_id, file, line, severity, category, message, suggestion, confidence, reviewer, status, verified_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                finding["id"], run_id, finding["file"], finding["line"],
                finding["severity"], finding["category"], finding["message"],
                finding.get("suggestion", ""), finding["confidence"],
                finding.get("reviewer", ""), finding.get("status", "candidate"),
                finding.get("verified_by", ""),
            ),
        )
        await self._db.commit()

    async def update_finding_status(
        self, finding_id: str, status: str, verified_by: str = "",
    ) -> None:
        await self._db.execute(
            "UPDATE review_findings SET status=?, verified_by=? WHERE id=?",
            (status, verified_by, finding_id),
        )
        await self._db.commit()

    async def get_findings(
        self, run_id: str | None = None, status: str | None = None,
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

    # ── Reviewer Metrics ─────────────────────────────────────────

    async def insert_metric(
        self, run_id: str, reviewer_name: str,
        findings_count: int = 0, duration_ms: int = 0,
        status: str = "completed", error: str = "",
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
                "SELECT * FROM reviewer_metrics WHERE run_id=?", (run_id,),
            )
        else:
            cursor = await self._db.execute("SELECT * FROM reviewer_metrics ORDER BY id DESC LIMIT 500")
        rows = await cursor.fetchall()
        return [self._row_to_dict(r) for r in rows]

    # ── Aggregates (for dashboard) ───────────────────────────────

    async def get_summary_stats(self, repo: str | None = None) -> dict[str, Any]:
        """Global summary: total runs, total findings, confirmation rate."""
        repo_filter = "WHERE r.repo=?" if repo else ""
        params = (repo,) if repo else ()

        cursor = await self._db.execute(f"""
            SELECT
                COUNT(DISTINCT r.run_id) as total_runs,
                COUNT(f.id) as total_findings,
                SUM(CASE WHEN f.status='confirmed' THEN 1 ELSE 0 END) as confirmed,
                SUM(CASE WHEN f.status='false_positive' THEN 1 ELSE 0 END) as false_positives,
                AVG(CASE WHEN f.status='confirmed' THEN f.confidence END) as avg_confidence
            FROM review_runs r
            LEFT JOIN review_findings f ON f.run_id = r.run_id
            {repo_filter}
        """, params)
        row = await cursor.fetchone()
        return self._row_to_dict(row) if row else {}

    async def get_category_distribution(self, repo: str | None = None) -> list[dict[str, Any]]:
        """Finding count by category."""
        repo_join = "JOIN review_runs r ON f.run_id=r.run_id WHERE r.repo=?" if repo else ""
        params = (repo,) if repo else ()
        cursor = await self._db.execute(f"""
            SELECT category, COUNT(*) as count
            FROM review_findings f
            {repo_join}
            GROUP BY category ORDER BY count DESC
        """, params)
        return [self._row_to_dict(r) for r in await cursor.fetchall()]

    async def get_weekly_trends(self, repo: str | None = None, weeks: int = 12) -> list[dict[str, Any]]:
        """Finding count by week."""
        repo_filter = "AND r.repo=?" if repo else ""
        interval = f"-{int(weeks) * 7} days"
        params = (interval, repo) if repo else (interval,)
        async with self._db.execute(f"""
            SELECT
                strftime('%Y-W%W', r.started_at) as week,
                COUNT(f.id) as total,
                SUM(CASE WHEN f.status='confirmed' THEN 1 ELSE 0 END) as confirmed
            FROM review_runs r
            LEFT JOIN review_findings f ON f.run_id = r.run_id
            WHERE r.started_at > datetime('now', ?)
            {repo_filter}
            GROUP BY week ORDER BY week
        """, params) as cursor:
            return [self._row_to_dict(r) for r in await cursor.fetchall()]

    async def get_hotspot_files(self, repo: str | None = None, limit: int = 10) -> list[dict[str, Any]]:
        """Files with most findings."""
        repo_join = "JOIN review_runs r ON f.run_id=r.run_id WHERE r.repo=?" if repo else ""
        params = (repo,) if repo else ()
        cursor = await self._db.execute(f"""
            SELECT file, COUNT(*) as count,
                   SUM(CASE WHEN f.status='confirmed' THEN 1 ELSE 0 END) as confirmed
            FROM review_findings f
            {repo_join}
            GROUP BY file ORDER BY count DESC LIMIT ?
        """, (*params, limit))
        return [self._row_to_dict(r) for r in await cursor.fetchall()]

    async def get_reviewer_stats(self, repo: str | None = None) -> list[dict[str, Any]]:
        """Per-reviewer statistics."""
        repo_join = "JOIN review_runs r ON m.run_id=r.run_id WHERE r.repo=?" if repo else ""
        params = (repo,) if repo else ()
        cursor = await self._db.execute(f"""
            SELECT
                m.reviewer_name,
                COUNT(*) as total_runs,
                SUM(m.findings_count) as total_findings,
                AVG(m.duration_ms) as avg_duration_ms,
                SUM(CASE WHEN m.status='completed' THEN 1 ELSE 0 END) as success_count
            FROM reviewer_metrics m
            {repo_join}
            GROUP BY m.reviewer_name ORDER BY total_findings DESC
        """, params)
        return [self._row_to_dict(r) for r in await cursor.fetchall()]

    async def get_recurring_issues(self, repo: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        """Same file + same category appearing in multiple runs."""
        repo_join = "JOIN review_runs r ON f.run_id=r.run_id WHERE r.repo=?" if repo else ""
        params = (repo,) if repo else ()
        cursor = await self._db.execute(f"""
            SELECT file, category, COUNT(DISTINCT run_id) as run_count, COUNT(*) as total_count
            FROM review_findings f
            {repo_join}
            GROUP BY file, category
            HAVING run_count > 1
            ORDER BY run_count DESC, total_count DESC
            LIMIT ?
        """, (*params, limit))
        return [self._row_to_dict(r) for r in await cursor.fetchall()]

    # ── Helpers ──────────────────────────────────────────────────

    @staticmethod
    def _row_to_dict(row: Any) -> dict[str, Any]:
        if hasattr(row, "keys"):
            return {k: row[k] for k in row.keys()}
        return dict(row) if row else {}

    # ── Token Usage ──────────────────────────────────────────────

    async def record_token_usage(
        self, run_id: str, agent_name: str,
        prompt_tokens: int = 0, completion_tokens: int = 0,
        total_tokens: int = 0, model: str = "",
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        await self._db.execute(
            "INSERT INTO token_usage (run_id, agent_name, prompt_tokens, completion_tokens, total_tokens, model, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (run_id, agent_name, prompt_tokens, completion_tokens, total_tokens, model, now),
        )
        await self._db.commit()

    async def get_token_usage(self, run_id: str | None = None) -> list[dict[str, Any]]:
        if run_id:
            cursor = await self._db.execute(
                "SELECT * FROM token_usage WHERE run_id=? ORDER BY id", (run_id,),
            )
        else:
            cursor = await self._db.execute(
                "SELECT * FROM token_usage ORDER BY created_at DESC LIMIT 500",
            )
        return [self._row_to_dict(r) for r in await cursor.fetchall()]

    async def get_token_summary(self, repo: str | None = None) -> dict[str, Any]:
        repo_join = "JOIN review_runs r ON t.run_id=r.run_id WHERE r.repo=?" if repo else ""
        params = (repo,) if repo else ()
        cursor = await self._db.execute(f"""
            SELECT
                SUM(t.prompt_tokens) as total_prompt,
                SUM(t.completion_tokens) as total_completion,
                SUM(t.total_tokens) as total_tokens,
                COUNT(DISTINCT t.run_id) as run_count
            FROM token_usage t
            {repo_join}
        """, params)
        row = await cursor.fetchone()
        return self._row_to_dict(row) if row else {}

    async def get_token_by_agent(self, repo: str | None = None) -> list[dict[str, Any]]:
        repo_join = "JOIN review_runs r ON t.run_id=r.run_id WHERE r.repo=?" if repo else ""
        params = (repo,) if repo else ()
        cursor = await self._db.execute(f"""
            SELECT
                t.agent_name,
                SUM(t.total_tokens) as total_tokens,
                COUNT(*) as call_count,
                AVG(t.total_tokens) as avg_tokens
            FROM token_usage t
            {repo_join}
            GROUP BY t.agent_name ORDER BY total_tokens DESC
        """, params)
        return [self._row_to_dict(r) for r in await cursor.fetchall()]

    # ── Code Graph (Symbols & Relations) ─────────────────────────

    async def upsert_symbol(
        self, file_path: str, symbol_name: str, symbol_type: str,
        run_id: str, pr_number: int = 0, language: str = "",
        risk_level: str = "safe", risk_categories: list[str] | None = None,
    ) -> None:
        cats = json.dumps(risk_categories or [], ensure_ascii=False)
        await self._db.execute(
            "INSERT INTO code_symbols (file_path, symbol_name, symbol_type, risk_level, risk_categories, defined_in_run, pr_number, language) "
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

    async def upsert_relation(
        self, run_id: str, source_file: str,
        target_file: str, target_symbol: str, relation_type: str,
    ) -> None:
        await self._db.execute(
            "INSERT INTO code_relations (run_id, source_file, target_file, target_symbol, relation_type) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(run_id, source_file, target_file, target_symbol) DO UPDATE SET relation_type=excluded.relation_type",
            (run_id, source_file, target_file, target_symbol, relation_type),
        )
        await self._db.commit()

    async def get_relations_from(self, source_file: str) -> list[dict[str, Any]]:
        cursor = await self._db.execute(
            "SELECT * FROM code_relations WHERE source_file=?", (source_file,),
        )
        return [self._row_to_dict(r) for r in await cursor.fetchall()]

    async def get_relations_to(self, target_file: str) -> list[dict[str, Any]]:
        cursor = await self._db.execute(
            "SELECT * FROM code_relations WHERE target_file=?", (target_file,),
        )
        return [self._row_to_dict(r) for r in await cursor.fetchall()]

    async def upsert_file_risk(
        self, file_path: str, max_risk: str,
        risk_categories: list[str] | None = None,
        findings_count: int = 0, run_id: str = "",
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        cats = json.dumps(risk_categories or [], ensure_ascii=False)
        await self._db.execute(
            "INSERT INTO file_risk_summary (file_path, max_risk, risk_categories, findings_count, last_run_id, last_updated) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(file_path) DO UPDATE SET "
            "max_risk=excluded.max_risk, risk_categories=excluded.risk_categories, "
            "findings_count=excluded.findings_count, last_run_id=excluded.last_run_id, last_updated=excluded.last_updated",
            (file_path, max_risk, cats, findings_count, run_id, now),
        )
        await self._db.commit()

    async def get_file_risk(self, file_path: str) -> dict[str, Any] | None:
        cursor = await self._db.execute(
            "SELECT * FROM file_risk_summary WHERE file_path=?", (file_path,),
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
