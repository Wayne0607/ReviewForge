"""Dashboard API — endpoints for the frontend dashboard.

Provides review history, metrics, trends, and system info
for the React dashboard to consume. All counts are computed
from actual findings in the database, not from summary_json.
"""

from __future__ import annotations

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
    """B10: 用 SQL 聚合计算全局统计。"""
    db = request.app.state.db
    stats = await db.get_summary_stats(repo=repo)
    return {
        "total_runs": stats.get("total_runs") or 0,
        "total_findings": stats.get("total_findings") or 0,
        "confirmed": stats.get("confirmed") or 0,
        "false_positives": stats.get("false_positives") or 0,
        "avg_confidence": stats.get("avg_confidence") or 0,
    }


@router.get("/metrics/categories")
async def metrics_categories(request: Request, repo: str | None = None):
    """Finding distribution by category."""
    db = request.app.state.db
    return await db.get_category_distribution(repo=repo)


@router.get("/metrics/trends")
async def metrics_trends(request: Request, repo: str | None = None, weeks: int = Query(default=12, ge=1, le=52)):
    """Weekly finding trends."""
    db = request.app.state.db
    return await db.get_weekly_trends(repo=repo, weeks=weeks)


@router.get("/metrics/hotspots")
async def metrics_hotspots(request: Request, repo: str | None = None, limit: int = Query(default=10, ge=1, le=100)):
    """Files with the most findings."""
    db = request.app.state.db
    return await db.get_hotspot_files(repo=repo, limit=limit)


@router.get("/metrics/reviewers")
async def metrics_reviewers(request: Request, repo: str | None = None):
    """Per-reviewer statistics."""
    db = request.app.state.db
    return await db.get_reviewer_stats(repo=repo)


@router.get("/metrics/recurring")
async def metrics_recurring(request: Request, repo: str | None = None, limit: int = Query(default=20, ge=1, le=100)):
    """Recurring issues across PRs."""
    db = request.app.state.db
    return await db.get_recurring_issues(repo=repo, limit=limit)


# ── Token Usage ──────────────────────────────────────────────


@router.get("/tokens/summary")
async def token_summary(request: Request, repo: str | None = None):
    """Global token usage summary."""
    db = request.app.state.db
    return await db.get_token_summary(repo=repo)


@router.get("/tokens/by-agent")
async def token_by_agent(request: Request, repo: str | None = None):
    """Token usage breakdown by agent."""
    db = request.app.state.db
    return await db.get_token_by_agent(repo=repo)


@router.get("/tokens/{run_id}")
async def token_by_run(request: Request, run_id: str):
    """Token usage for a specific review run."""
    db = request.app.state.db
    usage = await db.get_token_usage(run_id=run_id)
    total = sum(u.get("total_tokens", 0) for u in usage)
    return {"run_id": run_id, "agents": usage, "total_tokens": total}
