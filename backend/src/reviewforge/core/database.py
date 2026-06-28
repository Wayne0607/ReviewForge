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
    FOREIGN KEY (run_id) REFERENCES review_runs(run_id)
);

CREATE INDEX IF NOT EXISTS idx_findings_run ON review_findings(run_id);
CREATE INDEX IF NOT EXISTS idx_findings_file ON review_findings(file);
CREATE INDEX IF NOT EXISTS idx_findings_category ON review_findings(category);
CREATE INDEX IF NOT EXISTS idx_metrics_run ON reviewer_metrics(run_id);
CREATE INDEX IF NOT EXISTS idx_runs_repo ON review_runs(repo);
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
        params = (repo,) if repo else ()
        cursor = await self._db.execute(f"""
            SELECT
                strftime('%Y-W%W', r.started_at) as week,
                COUNT(f.id) as total,
                SUM(CASE WHEN f.status='confirmed' THEN 1 ELSE 0 END) as confirmed
            FROM review_runs r
            LEFT JOIN review_findings f ON f.run_id = r.run_id
            WHERE r.started_at > datetime('now', '-{weeks * 7} days')
            {repo_filter}
            GROUP BY week ORDER BY week
        """, params)
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
