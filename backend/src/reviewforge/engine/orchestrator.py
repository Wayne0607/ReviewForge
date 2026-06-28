"""Orchestrator — the main review loop.

Coordinates Planner → Scheduler → Reviewers → Verifier → Commenter.
"""

from __future__ import annotations

import logging
from typing import Any

from langchain_openai import ChatOpenAI

from reviewforge.core.events import EventBus
from reviewforge.core.loop_detector import LoopDetector
from reviewforge.core.specs import SpecRegistry
from reviewforge.core.state import Finding, ReviewTask, StateStore
from reviewforge.engine.planner import Planner
from reviewforge.engine.reviewers import REVIEWER_MAP, BaseReviewer
from reviewforge.engine.verifier import Verifier
from reviewforge.tools.gateway import ToolGateway

logger = logging.getLogger(__name__)


class Orchestrator:
    """Main review loop: Planner → Reviewers → Verifier → Commenter."""

    def __init__(
        self,
        registry: SpecRegistry,
        gateway: ToolGateway,
        event_bus: EventBus,
        planner_llm: ChatOpenAI,
        reviewer_llm: ChatOpenAI,
        verifier_llm: ChatOpenAI,
    ) -> None:
        self._registry = registry
        self._gateway = gateway
        self._events = event_bus
        self._planner = Planner(planner_llm, registry)
        self._verifier = Verifier(verifier_llm, registry)
        self._reviewer_llm = reviewer_llm
        self._loop_detector = LoopDetector()

    async def run(self, state: StateStore) -> dict[str, Any]:
        """Execute the full review pipeline. Returns summary."""
        self._events.emit("review.started", {"repo": state.repo, "pr": state.pr_number})

        # Phase 1: Plan
        self._events.emit("planner.started")
        tasks = await self._planner.plan(state)
        for task in tasks:
            state.add_task(task)
        self._events.emit("planner.completed", {"task_count": len(tasks)})

        # Phase 2: Execute reviewers
        for task in state.list_tasks(status="pending"):
            sig = LoopDetector.make_signature(task.reviewer, task.files)
            loop_result = self._loop_detector.check(sig)

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
                else:
                    state.update_task(task.id, status="failed", error=f"unknown reviewer: {task.reviewer}")
            except Exception as e:
                state.update_task(task.id, status="failed", error=str(e))
                self._events.emit("reviewer.failed", {"reviewer": task.reviewer, "error": str(e)})

        # Phase 3: Verify
        candidates = state.list_findings(status="candidate")
        if candidates:
            self._events.emit("verifier.started", {"candidate_count": len(candidates)})
            confirmed = await self._verifier.verify(state)
            for f in confirmed:
                state.update_finding(f.id, status="confirmed", verified_by="verifier")
            self._events.emit("verifier.completed", {
                "confirmed": len(confirmed),
                "filtered": len(candidates) - len(confirmed),
            })

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
        return summary

    def _create_reviewer(self, name: str) -> BaseReviewer | None:
        cls = REVIEWER_MAP.get(name)
        if cls:
            return cls(self._reviewer_llm, self._registry, self._gateway)
        return None

    async def _post_comments(self, findings: list[Finding], state: StateStore) -> int:
        """Post review comments via the tool gateway."""
        count = 0
        for finding in findings:
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
