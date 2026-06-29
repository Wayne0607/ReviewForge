"""Orchestrator — the main review loop.

Coordinates Planner → Reviewers → Dynamic Calibration → Commenter.
Persists all results to the database for dashboard consumption.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

from langchain_openai import ChatOpenAI

from reviewforge.core.database import Database
from reviewforge.core.events import EventBus
from reviewforge.core.loop_detector import LoopDetector
from reviewforge.core.specs import SpecRegistry
from reviewforge.core.state import Finding, ReviewTask, StateStore
from reviewforge.engine.calibrator import DynamicCalibrator
from reviewforge.engine.cross_pr_analyzer import CrossPRAnalyzer
from reviewforge.engine.planner import Planner
from reviewforge.engine.reviewers import REVIEWER_MAP, BaseReviewer
from reviewforge.engine.token_tracker import RunContext, TrackedChatLLM
from reviewforge.tools.gateway import ToolGateway

logger = logging.getLogger(__name__)


class Orchestrator:
    """Main review loop: Planner → Reviewers → Dynamic Calibration → Commenter."""

    def __init__(
        self,
        registry: SpecRegistry,
        gateway: ToolGateway,
        event_bus: EventBus,
        planner_llm: ChatOpenAI,
        reviewer_llm: ChatOpenAI,
        calibrator_llm: ChatOpenAI,
        db: Database | None = None,
        cross_pr_llm: ChatOpenAI | None = None,
        github_client: Any = None,
    ) -> None:
        self._registry = registry
        self._gateway = gateway
        self._events = event_bus
        self._db = db

        # Token tracking context — updated per-run
        self._token_ctx = RunContext()

        # Wrap LLMs with token tracking if DB available
        if db:
            tracked_planner = TrackedChatLLM(inner=planner_llm, ctx=self._token_ctx, agent_name="planner")
            tracked_calibrator = TrackedChatLLM(inner=calibrator_llm, ctx=self._token_ctx, agent_name="calibrator")
            self._planner = Planner(tracked_planner, registry)
            self._calibrator = DynamicCalibrator(tracked_calibrator, registry)
            self._reviewer_llm = TrackedChatLLM(inner=reviewer_llm, ctx=self._token_ctx, agent_name="reviewer")
            if cross_pr_llm:
                cross_pr_llm = TrackedChatLLM(inner=cross_pr_llm, ctx=self._token_ctx, agent_name="cross_pr_analyzer")
        else:
            self._planner = Planner(planner_llm, registry)
            self._calibrator = DynamicCalibrator(calibrator_llm, registry)
            self._reviewer_llm = reviewer_llm

        self._cross_pr = CrossPRAnalyzer(db, cross_pr_llm, github_client) if db else None
        # B4: LoopDetector 每 run 新建，避免跨 run 状态污染
        # Plugin-loaded reviewers (merged at init time)
        self._extra_reviewers: dict[str, type[BaseReviewer]] = {}

    def register_plugin_reviewers(self, plugins: dict[str, type[BaseReviewer]]) -> None:
        """Merge plugin-loaded reviewers into the reviewer map."""
        self._extra_reviewers.update(plugins)

    async def run(self, state: StateStore) -> dict[str, Any]:
        """Execute the full review pipeline. Returns summary."""
        run_id = uuid.uuid4().hex[:12]
        loop_detector = LoopDetector()  # B4: per-run instance
        self._events.set_run_id(run_id)
        self._events.emit("review.started", {"repo": state.repo, "pr": state.pr_number, "run_id": run_id})

        # Set token tracking context for this run
        if self._db:
            self._token_ctx.set(run_id, self._db)

        # Persist run start
        if self._db:
            await self._db.create_run(
                run_id=run_id, repo=state.repo,
                pr_number=state.pr_number,
                head_sha=state.head_sha, base_sha=state.base_sha,
            )

        try:
            # Phase 1: Plan
            self._events.emit("planner.started")
            tasks = await self._planner.plan(state)
            for task in tasks:
                state.add_task(task)
            self._events.emit("planner.completed", {"task_count": len(tasks)})

            # Phase 2: Execute reviewers
            for task in state.list_tasks(status="pending"):
                sig = LoopDetector.make_signature(task.reviewer, task.files)
                loop_result = loop_detector.check(sig)

                if loop_result == "stall":
                    state.update_task(task.id, status="failed", error="loop_stalled")
                    self._events.emit("reviewer.stalled", {"task_id": task.id, "signature": sig})
                    continue
                if loop_result == "rescue":
                    state.update_task(task.id, status="failed", error="rescue_drain")
                    self._events.emit("reviewer.rescued", {"task_id": task.id})
                    continue

                state.update_task(task.id, status="claimed")
                self._events.emit("reviewer.started", {"reviewer": task.reviewer, "files": task.files})

                t_start = time.monotonic()
                try:
                    reviewer = self._create_reviewer(task.reviewer)
                    if reviewer:
                        findings = await reviewer.execute(task, state)
                        for f in findings:
                            state.add_finding(f)
                        state.update_task(task.id, status="completed")
                        self._events.emit("reviewer.completed", {
                            "reviewer": task.reviewer,
                            "findings_count": len(findings),
                        })
                        # Persist metric
                        if self._db:
                            duration_ms = int((time.monotonic() - t_start) * 1000)
                            await self._db.insert_metric(
                                run_id, task.reviewer,
                                findings_count=len(findings),
                                duration_ms=duration_ms,
                            )
                    else:
                        state.update_task(task.id, status="failed", error=f"unknown reviewer: {task.reviewer}")
                        if self._db:
                            await self._db.insert_metric(
                                run_id, task.reviewer,
                                status="failed", error=f"unknown reviewer: {task.reviewer}",
                            )
                except Exception as e:
                    state.update_task(task.id, status="failed", error=str(e))
                    self._events.emit("reviewer.failed", {"reviewer": task.reviewer, "error": str(e)})
                    if self._db:
                        duration_ms = int((time.monotonic() - t_start) * 1000)
                        await self._db.insert_metric(
                            run_id, task.reviewer,
                            duration_ms=duration_ms, status="failed", error=str(e),
                        )

            # Phase 3: Dynamic Calibration (adversarial verify + conditional judge)
            candidates = state.list_findings(status="candidate")
            if candidates:
                self._events.emit("calibration.started", {"candidate_count": len(candidates)})

                diff_context = state.diff_summary
                calibrated = await self._calibrator.calibrate(candidates, diff_context)

                for f in calibrated:
                    if f.status == "confirmed":
                        state.update_finding(f.id, status="confirmed", verified_by=f.verified_by,
                                             verify_reason=f.verify_reason)
                    elif f.status == "false_positive":
                        state.update_finding(f.id, status="false_positive", verified_by=f.verified_by,
                                             verify_reason=f.verify_reason)

                confirmed_count = len([f for f in calibrated if f.status == "confirmed"])
                filtered_count = len([f for f in calibrated if f.status == "false_positive"])
                self._events.emit("calibration.completed", {
                    "confirmed": confirmed_count,
                    "filtered": filtered_count,
                })

            # Phase 3.5: Cross-PR Analysis
            if self._cross_pr:
                confirmed_findings = state.list_findings(status="confirmed")
                self._events.emit("cross_pr.started")
                try:
                    cross_findings = await self._cross_pr.analyze(
                        run_id=run_id,
                        state=state,
                        existing_findings=confirmed_findings,
                    )
                    for f in cross_findings:
                        state.add_finding(f)
                    if cross_findings:
                        self._events.emit("cross_pr.completed", {
                            "cross_pr_findings": len(cross_findings),
                        })
                        logger.info(f"Cross-PR: found {len(cross_findings)} cross-PR issues")
                    else:
                        self._events.emit("cross_pr.completed", {"cross_pr_findings": 0})
                except Exception as e:
                    logger.error(f"Cross-PR analysis failed: {e}")
                    self._events.emit("cross_pr.failed", {"error": str(e)})

            # Persist all findings to DB
            if self._db:
                for f in state.findings.values():
                    await self._db.insert_finding(run_id, f.to_dict())

            # Phase 4: Comment
            confirmed = state.list_findings(status="confirmed")
            if confirmed:
                self._events.emit("commenter.started", {"finding_count": len(confirmed)})
                comment_count = await self._post_comments(confirmed, state)
                self._events.emit("commenter.completed", {"comments_posted": comment_count})

            summary = {
                "total_findings": len(state.findings),
                "confirmed": len(state.list_findings(status="confirmed")),
                "false_positives": len(state.list_findings(status="false_positive")),
                "tasks_completed": len(state.list_tasks(status="completed")),
                "tasks_failed": len(state.list_tasks(status="failed")),
            }
            self._events.emit("review.completed", summary)

            # Persist run completion
            if self._db:
                await self._db.complete_run(run_id, summary)

            return summary

        except Exception as e:
            if self._db:
                await self._db.fail_run(run_id, str(e))
            raise

    def _create_reviewer(self, name: str) -> BaseReviewer | None:
        # Check built-in reviewers first, then plugins
        cls = REVIEWER_MAP.get(name) or self._extra_reviewers.get(name)
        if cls:
            return cls(self._reviewer_llm, self._registry, self._gateway)
        return None

    async def _post_comments(self, findings: list[Finding], state: StateStore) -> int:
        """Post review comments via the tool gateway."""
        count = 0
        for finding in findings:
            # Validate line number — GitHub rejects line=0 or invalid lines
            if finding.line <= 0:
                logger.warning(f"Skipping comment for {finding.id}: invalid line {finding.line}")
                continue
            try:
                await self._gateway.invoke("post_comment", {
                    "file_path": finding.file,
                    "line": finding.line,
                    "body": self._format_comment(finding),
                    "severity": finding.severity,
                }, state)
                state.update_finding(finding.id, status="reported")
                count += 1
            except Exception as e:
                error_msg = str(e)
                if "422" in error_msg:
                    logger.warning(f"Skipping comment for {finding.id}: line {finding.line} not in diff")
                else:
                    logger.error(f"Failed to post comment for {finding.id}: {e}")
        return count

    @staticmethod
    def _format_comment(finding: Finding) -> str:
        severity_emoji = {"error": "🔴", "warning": "🟡", "info": "🔵"}.get(finding.severity, "⚪")
        return (
            f"{severity_emoji} **[{finding.category}]** (置信度: {finding.confidence:.0%})\n\n"
            f"{finding.message}\n\n"
            f"**建议:** {finding.suggestion}\n\n"
            f"<sub>ReviewForge • {finding.reviewer}</sub>"
        )
