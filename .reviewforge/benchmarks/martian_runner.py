"""Run ReviewForge read-only against Martian Code Review Bench PR snapshots."""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import hashlib
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

import httpx

REPO_ROOT = Path(os.environ.get("REVIEWFORGE_REPO_ROOT", "/opt/reviewforge"))
BACKEND_ROOT = REPO_ROOT / "backend"
if str(BACKEND_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT / "src"))

import reviewforge.engine.orchestrator as orchestrator_module
from reviewforge.core.config import ReviewForgeConfig
from reviewforge.core.database import Database
from reviewforge.core.events import EventBus
from reviewforge.core.llm_settings import (
    EncryptedLLMSettingsStore,
    apply_override,
)
from reviewforge.core.scheduler import Scheduler
from reviewforge.core.specs import apply_reviewer_configs, build_registry
from reviewforge.core.state import StateStore
from reviewforge.engine.model_router import ModelRouter
from reviewforge.engine.orchestrator import Orchestrator
from reviewforge.engine.publication_policy import (
    PublicationPolicy,
    PublicationPolicyConfig,
)
from reviewforge.engine.reviewers import BaseReviewer
from reviewforge.tools.gateway import ToolGateway
from reviewforge.tools.github_api import GitHubClient

logger = logging.getLogger("martian_runner")
_SCHEDULER_CONCURRENCY = 4
_PUBLICATION_GATE_CONCURRENCY = 1
_SEARCH_INTERVAL_SECONDS = 7.0
_SEARCH_RATE_FILE = Path("/tmp/reviewforge-martian-search-rate")


class BenchmarkScheduler(Scheduler):
    """Allow benchmark sharding without raising aggregate reviewer concurrency."""

    def __init__(self, concurrency: int = 4) -> None:
        del concurrency
        super().__init__(concurrency=_SCHEDULER_CONCURRENCY)


class ReadOnlyGitHub:
    """Delegate reads to GitHub and turn all review writes into local receipts."""

    def __init__(self, client: GitHubClient) -> None:
        self._client = client

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)

    async def search_code(self, repo: str, pattern: str, file_glob: str = "") -> str:
        for attempt in range(5):
            await asyncio.to_thread(_wait_for_search_slot)
            try:
                return await self._client.search_code(repo, pattern, file_glob)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code not in {403, 429} or attempt == 4:
                    raise
                await asyncio.sleep(15 * (attempt + 1))
        raise RuntimeError("GitHub code search retries exhausted")

    async def post_review_comment(self, **kwargs: Any) -> dict[str, Any]:
        return {"id": 1, "benchmark_dry_run": True, **kwargs}

    async def post_review_comments(
        self, *, comments: list[dict[str, Any]], **kwargs: Any
    ) -> dict[str, Any]:
        return {
            "id": 1,
            "benchmark_dry_run": True,
            "comments": comments,
            **kwargs,
        }


def _wait_for_search_slot() -> None:
    _SEARCH_RATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with _SEARCH_RATE_FILE.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        handle.seek(0)
        raw = handle.read().strip()
        previous = float(raw) if raw else 0.0
        delay = _SEARCH_INTERVAL_SECONDS - (time.monotonic() - previous)
        if delay > 0:
            time.sleep(delay)
        handle.seek(0)
        handle.truncate()
        handle.write(str(time.monotonic()))
        handle.flush()
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _capture_invalid_reviewer_outputs(root: Path) -> None:
    original = BaseReviewer._extract_json
    output_dir = root / "invalid-outputs"
    output_dir.mkdir(parents=True, exist_ok=True)

    def capture(content: str) -> Any:
        parsed = original(content)
        if parsed is None:
            text = str(content or "")
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
            (output_dir / f"{digest}.txt").write_text(text, encoding="utf-8")
        return parsed

    BaseReviewer._extract_json = staticmethod(capture)


def _atomic_json(path: Path, payload: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(path)


def _review_comments(state: StateStore) -> list[dict[str, Any]]:
    comments: list[dict[str, Any]] = []
    for finding in state.list_findings():
        if finding.status not in {"confirmed", "reported"}:
            continue
        body = f"[{finding.severity}] {finding.message}".strip()
        if finding.suggestion:
            body += f"\n\nSuggested fix: {finding.suggestion}"
        comments.append(
            {
                "path": finding.file,
                "line": int(finding.line or 0),
                "body": body,
                "category": finding.category,
                "confidence": finding.confidence,
                "reviewer": finding.reviewer,
            }
        )
    return comments


async def _build_runtime(
    root: Path,
    model_override: str = "",
    output_language: str = "",
) -> tuple[Orchestrator, Database, GitHubClient]:
    if output_language:
        # The legacy prompt builder has no runtime-config argument yet; keep
        # this benchmark override visible to both config loading and that
        # compatibility boundary.  The process is short-lived, so the
        # per-run override cannot leak into another benchmark invocation.
        os.environ["REVIEWFORGE_OUTPUT_LANGUAGE"] = output_language
    cfg = ReviewForgeConfig.load(REPO_ROOT / "reviewforge.yaml")
    runtime_dir = Path(cfg.events_dir).parent
    cfg.llm = apply_override(cfg.llm, EncryptedLLMSettingsStore(runtime_dir).load())
    if model_override:
        cfg.llm.model = model_override
        for profile in cfg.llm.profiles.values():
            profile.model = model_override
    registry = apply_reviewer_configs(build_registry(), cfg.reviewers)
    errors = registry.validate()
    if errors:
        raise RuntimeError(f"Spec validation failed: {errors}")

    raw_github = GitHubClient(token=cfg.github.token)
    github = ReadOnlyGitHub(raw_github)
    router = ModelRouter(cfg.llm)
    policy_cfg = PublicationPolicyConfig(
        enabled=cfg.publication_policy.enabled,
        mode=cfg.publication_policy.mode,
        budget_enabled=cfg.publication_policy.budget_enabled,
        max_comments=cfg.publication_policy.max_comments,
        high_risk_overflow=cfg.publication_policy.high_risk_overflow,
        empty_review_rescue_enabled=cfg.publication_policy.empty_review_rescue_enabled,
    )
    # A benchmark process can be terminated deliberately by a production
    # deployment. A fixed DB would leave the interrupted run in "running" and
    # the immediate retry would be deduplicated as already in progress,
    # producing a false zero-comment completion. Each process attempt gets an
    # isolated DB; durable completion/skip state lives in result.json.
    db = Database(root / f"reviewforge-martian-{os.getpid()}.db")
    await db.connect()
    events = EventBus(log_dir=root / "events")
    orchestrator = Orchestrator(
        registry=registry,
        gateway=ToolGateway(registry, github),
        event_bus=events,
        planner_llm=router.get_llm("planner"),
        reviewer_llm=router.get_llm("reviewer"),
        calibrator_llm=router.get_llm("verifier"),
        db=db,
        cross_pr_llm=router.get_llm("verifier"),
        github_client=github,
        model_router=router,
        agentic_reviewers=cfg.agentic_reviewers,
        agentic_default=cfg.agentic_default,
        escalation_enabled=cfg.escalation_enabled,
        escalation_confidence_min=cfg.escalation_confidence_min,
        escalation_confidence_max=cfg.escalation_confidence_max,
        escalation_max_steps=cfg.escalation_max_steps,
        escalation_max_tokens=cfg.escalation_max_tokens,
        publication_gate_enabled=cfg.publication_gate_enabled,
        publication_gate_max_steps=cfg.publication_gate_max_steps,
        publication_gate_max_tokens=cfg.publication_gate_max_tokens,
        publication_gate_concurrency=_PUBLICATION_GATE_CONCURRENCY,
        publication_gate_dedup=cfg.publication_gate_dedup,
        root_cause_extended_families=cfg.root_cause_extended_families,
        publication_triage_enabled=cfg.publication_triage_enabled,
        publication_triage_batch_size=cfg.publication_triage_batch_size,
        publication_triage_concurrency=cfg.publication_triage_concurrency,
        publication_triage_max_candidates=cfg.publication_triage_max_candidates,
        publication_triage_context_lines=cfg.publication_triage_context_lines,
        publication_triage_max_tokens=cfg.publication_triage_max_tokens,
        publication_gate_llm=router.get_llm("publication_gate"),
        publication_policy=PublicationPolicy(policy_cfg),
        coverage_gap_enabled=cfg.coverage_gap_enabled,
        coverage_gap_min_risk_score=cfg.coverage_gap_min_risk_score,
        coverage_gap_max_cards=cfg.coverage_gap_max_cards,
        coverage_gap_min_confidence=cfg.coverage_gap_min_confidence,
        skills_dir=cfg.skills_dir,
        v3_enabled=cfg.v3.enabled,
        v3_coverage_min_risk_score=cfg.v3.coverage_min_risk_score,
        v3_coverage_max_cells_per_round=cfg.v3.coverage_max_cells_per_round,
        v3_coverage_max_attempts=cfg.v3.coverage_max_attempts,
        v3_evidence_mode=cfg.v3.evidence_mode,
        v3_evidence_max_candidates=cfg.v3.evidence_max_candidates,
        output_language=cfg.output_language,
    )
    return orchestrator, db, raw_github


async def _run_one(
    item: dict[str, Any],
    orchestrator: Orchestrator,
    db: Database,
    github: GitHubClient,
) -> dict[str, Any]:
    started = time.monotonic()
    repo = str(item["repo"])
    pr_number = int(item["pr_number"])
    pr = await github.get_pr_info(repo, pr_number)
    files = await github.get_pr_files(repo, pr_number)
    state = StateStore(
        pr_number=pr_number,
        repo=repo,
        head_sha=str(pr["head"]["sha"]),
        base_sha=str(pr["base"]["sha"]),
        files_changed=[str(file["filename"]) for file in files],
        diff_summary="\n".join(
            f"--- {file['filename']} (+{file.get('additions', 0)} -{file.get('deletions', 0)})\n"
            f"{file.get('patch') or ''}"
            for file in files
        ),
        file_diffs={
            str(file["filename"]): str(file.get("patch") or "") for file in files
        },
    )
    summary = await orchestrator.run(state)
    runs = await db.get_runs(repo=repo, limit=5)
    run = next((row for row in runs if row.get("head_sha") == state.head_sha), {})
    token_rows = (
        await db.get_token_usage(run_id=str(run.get("run_id") or "")) if run else []
    )
    return {
        **item,
        "head_sha": state.head_sha,
        "base_sha": state.base_sha,
        "changed_files": len(files),
        "review_comments": _review_comments(state),
        "summary": summary,
        "run_id": run.get("run_id", ""),
        "tokens": sum(int(row.get("total_tokens", 0) or 0) for row in token_rows),
        "tokens_by_agent": token_rows,
        "duration_seconds": round(time.monotonic() - started, 3),
        "status": "completed",
    }


async def main_async(args: argparse.Namespace) -> None:
    global _PUBLICATION_GATE_CONCURRENCY, _SCHEDULER_CONCURRENCY
    _SCHEDULER_CONCURRENCY = args.reviewer_concurrency
    _PUBLICATION_GATE_CONCURRENCY = args.publication_gate_concurrency
    orchestrator_module.Scheduler = BenchmarkScheduler
    root = Path(args.output).resolve().parent
    root.mkdir(parents=True, exist_ok=True)
    if args.capture_invalid_outputs:
        _capture_invalid_reviewer_outputs(root)
    workload = json.loads(Path(args.workload).read_text(encoding="utf-8"))
    if args.shard_count > 1:
        workload = [
            item
            for index, item in enumerate(workload)
            if index % args.shard_count == args.shard_index
        ]
    if args.limit:
        workload = workload[: args.limit]
    output_path = Path(args.output).resolve()
    existing = (
        json.loads(output_path.read_text(encoding="utf-8"))
        if output_path.exists()
        else []
    )
    results = {
        str(item["golden_url"]): item
        for item in existing
        if item.get("status") == "completed"
    }

    orchestrator, db, raw_github = await _build_runtime(root, args.model_override, args.output_language)
    try:
        for index, item in enumerate(workload, 1):
            key = str(item["golden_url"])
            if key in results:
                print(f"SKIP {index}/{len(workload)} {key}", flush=True)
                continue
            print(
                f"START {index}/{len(workload)} {key} -> {item['repo']}#{item['pr_number']}",
                flush=True,
            )
            try:
                result = await _run_one(item, orchestrator, db, raw_github)
                results[key] = result
                print(
                    f"DONE {index}/{len(workload)} comments={len(result['review_comments'])} "
                    f"tokens={result['tokens']} seconds={result['duration_seconds']}",
                    flush=True,
                )
            except Exception as exc:
                logger.exception("Benchmark PR failed: %s", key)
                results[key] = {
                    **item,
                    "status": "failed",
                    "error": f"{type(exc).__name__}: {exc}",
                }
                print(
                    f"FAIL {index}/{len(workload)} {type(exc).__name__}: {exc}",
                    flush=True,
                )
            _atomic_json(output_path, list(results.values()))
    finally:
        await db.close()
        await raw_github.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workload", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--reviewer-concurrency", type=int, default=4)
    parser.add_argument("--publication-gate-concurrency", type=int, default=1)
    parser.add_argument("--capture-invalid-outputs", action="store_true")
    parser.add_argument("--model-override", default="")
    parser.add_argument(
        "--output-language",
        choices=("auto", "en", "zh-CN"),
        default="",
        help="Override the review output language for this benchmark run",
    )
    parser.add_argument("--shard-count", type=int, default=1)
    parser.add_argument("--shard-index", type=int, default=0)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
