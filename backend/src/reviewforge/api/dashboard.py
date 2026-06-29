"""Dashboard API — endpoints for the frontend dashboard.

Provides review history, metrics, trends, and system info
for the React dashboard to consume. All counts are computed
from actual findings in the database, not from summary_json.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

router = APIRouter(prefix="/api/v1/dashboard")


async def _enrich_run(db, run: dict) -> dict:
    """Add accurate counts from actual findings instead of summary_json."""
    run_id = run["run_id"]
    findings = await db.get_findings(run_id=run_id)

    total = len(findings)
    confirmed = len([f for f in findings if f.get("status") == "confirmed"])
    false_pos = len([f for f in findings if f.get("status") == "false_positive"])

    # Override summary with actual DB counts
    run["summary"] = {
        "total_findings": total,
        "confirmed": confirmed,
        "false_positives": false_pos,
        "tasks_completed": 0,  # Not tracked in findings table
        "tasks_failed": 0,
    }
    return run


# ── Reviews ──────────────────────────────────────────────────

@router.get("/reviews")
async def list_reviews(
    request: Request,
    repo: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    """List review runs with pagination."""
    db = request.app.state.db
    runs = await db.get_runs(repo=repo, limit=limit, offset=offset)

    # Enrich each run with accurate counts
    enriched = []
    for r in runs:
        enriched.append(await _enrich_run(db, r))

    return {"runs": enriched, "limit": limit, "offset": offset}


@router.get("/reviews/{run_id}")
async def get_review_detail(request: Request, run_id: str):
    """Get detailed info for a single review run."""
    db = request.app.state.db
    run = await db.get_run(run_id)
    if not run:
        raise HTTPException(404, "Review run not found")

    run = await _enrich_run(db, run)
    findings = await db.get_findings(run_id=run_id)
    metrics = await db.get_metrics(run_id=run_id)

    return {"run": run, "findings": findings, "metrics": metrics}


# ── Metrics ──────────────────────────────────────────────────

@router.get("/metrics/summary")
async def metrics_summary(request: Request, repo: str | None = None):
    """Global summary statistics computed from actual findings."""
    db = request.app.state.db

    # Get all runs
    runs = await db.get_runs(repo=repo, limit=1000)
    total_runs = len(runs)

    # Get all findings
    findings = await db.get_findings(limit=10000)
    total = len(findings)
    confirmed = len([f for f in findings if f.get("status") == "confirmed"])
    false_pos = len([f for f in findings if f.get("status") == "false_positive"])
    avg_conf = sum(f.get("confidence", 0) for f in findings) / total if total else 0

    return {
        "total_runs": total_runs,
        "total_findings": total,
        "confirmed": confirmed,
        "false_positives": false_pos,
        "avg_confidence": avg_conf,
    }


@router.get("/metrics/categories")
async def metrics_categories(request: Request, repo: str | None = None):
    """Finding distribution by category."""
    db = request.app.state.db
    return await db.get_category_distribution(repo=repo)


@router.get("/metrics/trends")
async def metrics_trends(request: Request, repo: str | None = None, weeks: int = 12):
    """Weekly finding trends."""
    db = request.app.state.db
    return await db.get_weekly_trends(repo=repo, weeks=weeks)


@router.get("/metrics/hotspots")
async def metrics_hotspots(request: Request, repo: str | None = None, limit: int = 10):
    """Files with the most findings."""
    db = request.app.state.db
    return await db.get_hotspot_files(repo=repo, limit=limit)


@router.get("/metrics/reviewers")
async def metrics_reviewers(request: Request, repo: str | None = None):
    """Per-reviewer statistics."""
    db = request.app.state.db
    return await db.get_reviewer_stats(repo=repo)


@router.get("/metrics/recurring")
async def metrics_recurring(request: Request, repo: str | None = None, limit: int = 20):
    """Recurring issues across PRs."""
    db = request.app.state.db
    return await db.get_recurring_issues(repo=repo, limit=limit)
