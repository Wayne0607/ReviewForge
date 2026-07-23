"""Orchestrator — the main review loop.

Coordinates Planner → Reviewers → Dynamic Calibration → Commenter.
Persists all results to the database for dashboard consumption.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from langchain_openai import ChatOpenAI

from reviewforge.core.database import Database
from reviewforge.core.events import EventBus
from reviewforge.core.loop_detector import LoopDetector
from reviewforge.core.scheduler import Scheduler
from reviewforge.core.specs import SpecRegistry
from reviewforge.core.state import Finding, Note, ReviewTask, StateStore
from reviewforge.engine.calibrator import DynamicCalibrator, apply_actionability_gate, apply_code_evidence_gate
from reviewforge.engine.context_engine import ContextEngine, render_impact_manifest
from reviewforge.engine.coverage_gap import build_evidence_cards, filter_gap_findings
from reviewforge.engine.coverage_ledger import (
    CoverageCell,
    CoverageDimension,
    CoverageLedger,
    CoverageStatus,
)
from reviewforge.engine.cross_pr_analyzer import CrossPRAnalyzer
from reviewforge.engine.detectors.unified_diff import iter_right_lines
from reviewforge.engine.escalation import EscalationReviewer, PublicationGateReviewer
from reviewforge.engine.evidence_verifier import (
    EvidenceItem,
    EvidenceVerdict,
    EvidenceVerifier,
)
from reviewforge.engine.finding_anchors import (
    reanchor_accessibility_findings,
    reanchor_quality_detector_duplicates,
    reanchor_security_detector_duplicates,
    unsupported_python_open_redirect_findings,
)
from reviewforge.engine.model_router import ModelRouter
from reviewforge.engine.phase0 import finding_identity, scan_changed_files
from reviewforge.engine.planner import Planner
from reviewforge.engine.reviewers import REVIEWER_MAP, BaseReviewer
from reviewforge.engine.security_categories import is_security_category
from reviewforge.engine.semantic_diff import SemanticUnit, compile_semantic_changeset
from reviewforge.engine.token_tracker import RunContext, TrackedChatLLM
from reviewforge.engine.verifier import Verifier
from reviewforge.tools.gateway import ToolGateway
from reviewforge.tools.github_api import MAX_REVIEW_COMMENTS_PER_REQUEST

logger = logging.getLogger(__name__)

# ── Correctness-task slicing budget ──────────────────────────────────────
# The reviewer prompt has a 36k-char diff ceiling (_REVIEWER_MAX_DIFF_CHARS
# in prompt.py).  System preamble, skill body, impact manifest, and
# per-file "### path" headers all consume that budget, so a 24k-char
# chunk target leaves ~12k for non-diff content and avoids the blanket
# shallow truncation that defeats deep correctness review on large PRs.
_SLICE_MAX_FILES = 8
_SLICE_MAX_DIFF_CHARS = 24_000


def _split_oversized_correctness_tasks(
    tasks: list[ReviewTask],
    file_diffs: dict[str, str],
) -> list[ReviewTask]:
    """Split oversized ``correctness_reviewer`` tasks into bounded chunks.

    Only tasks whose ``reviewer`` is exactly ``correctness_reviewer`` are
    candidates for splitting.  Non-correctness tasks and correctness tasks
    already within both budgets are returned unchanged (same object / id).

    Chunking uses deterministic sequential greedy packing in original file
    order.  A single file whose rendered cost exceeds the character budget
    becomes a one-file chunk and is never dropped or duplicated.

    Args:
        tasks: Planner-proposed tasks (may be empty).
        file_diffs: Per-file patch cache from ``state.file_diffs``.

    Returns:
        A flat list of tasks ready for ``state.add_task``.
    """
    if not tasks:
        return tasks

    result: list[ReviewTask] = []
    for task in tasks:
        # Non-correctness tasks pass through regardless of size.
        if task.reviewer != "correctness_reviewer":
            result.append(task)
            continue

        files = task.files
        # A single file (or empty list) cannot be split further — return unchanged.
        if len(files) <= 1:
            result.append(task)
            continue

        # Check whether splitting is needed: file-count *or* char budget.
        needs_split = len(files) > _SLICE_MAX_FILES
        if not needs_split:
            total_cost = sum(len(f"### {fp}\n{file_diffs.get(fp, '')}\n\n") for fp in files)
            needs_split = total_cost > _SLICE_MAX_DIFF_CHARS

        if not needs_split:
            result.append(task)
            continue

        # Sequential greedy packing — preserve original file order.
        chunks: list[list[str]] = []
        current: list[str] = []
        current_cost = 0

        for fp in files:
            patch = str(file_diffs.get(fp, ""))
            cost = len(f"### {fp}\n{patch}\n\n")

            if current:
                # Would adding this file exceed either budget?
                if len(current) + 1 > _SLICE_MAX_FILES or current_cost + cost > _SLICE_MAX_DIFF_CHARS:
                    chunks.append(current)
                    current = []
                    current_cost = 0

            current.append(fp)
            current_cost += cost

        if current:
            chunks.append(current)

        # Emit fresh ReviewTask instances with distinct ids, preserving
        # the original reviewer and rationale.
        for chunk_files in chunks:
            result.append(
                ReviewTask(
                    reviewer=task.reviewer,
                    files=chunk_files,
                    rationale=task.rationale,
                )
            )

    return result


@dataclass(frozen=True)
class CommentDeliveryResult:
    """Per-finding delivery outcome used to decide whether a run is resumable."""

    reported: int = 0
    permanent_rejections: int = 0
    transient_failures: int = 0
    errors: tuple[str, ...] = ()

    @property
    def retryable(self) -> bool:
        return self.transient_failures > 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "reported": self.reported,
            "comments_posted": self.reported,
            "permanent_rejections": self.permanent_rejections,
            "transient_failures": self.transient_failures,
            "retryable": self.retryable,
            "errors": list(self.errors),
        }


def _should_escalate_finding(
    finding: Finding,
    confidence_min: float,
    confidence_max: float,
    high_confidence_security_threshold: float = 0.75,
) -> bool:
    """Route only ambiguous findings into the expensive agentic verifier."""
    if is_security_category(finding.category) and finding.confidence >= high_confidence_security_threshold:
        return False
    return EscalationReviewer.should_escalate(finding, confidence_min, confidence_max)


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
        model_router: ModelRouter | None = None,
        agentic_reviewers: list[str] | None = None,
        agentic_default: bool = False,
        escalation_enabled: bool = True,
        escalation_confidence_min: float = 0.4,
        escalation_confidence_max: float = 0.7,
        escalation_max_steps: int = 3,
        escalation_max_tokens: int = 5000,
        publication_gate_enabled: bool = False,
        publication_gate_max_steps: int = 4,
        publication_gate_max_tokens: int = 6000,
        publication_gate_concurrency: int = 4,
        coverage_gap_enabled: bool = False,
        coverage_gap_min_risk_score: int = 4,
        coverage_gap_max_cards: int = 3,
        coverage_gap_min_confidence: float = 0.65,
        skills_dir: str | Path | None = None,
        # V3 coverage-driven pipeline (all default off)
        v3_enabled: bool = False,
        v3_coverage_min_risk_score: float = 0.15,
        v3_coverage_max_cells_per_round: int = 24,
        v3_coverage_max_attempts: int = 2,
        v3_evidence_mode: str = "shadow",
        v3_evidence_max_candidates: int = 20,
    ) -> None:
        self._registry = registry
        self._gateway = gateway
        self._events = event_bus
        self._db = db
        self._model_router = model_router  # D6: 多模型路由
        self._agentic_reviewers = set(agentic_reviewers or [])  # W1: agentic 显式 allowlist
        self._agentic_default = agentic_default  # #1: 无 allowlist 时所有 reviewer 默认走工具循环

        # Escalation config
        self._escalation_enabled = escalation_enabled
        self._escalation_confidence_min = escalation_confidence_min
        self._escalation_confidence_max = escalation_confidence_max
        self._escalation_max_steps = escalation_max_steps
        self._escalation_max_tokens = escalation_max_tokens
        self._publication_gate_enabled = publication_gate_enabled
        self._publication_gate_max_steps = max(1, publication_gate_max_steps)
        self._publication_gate_max_tokens = max(1, publication_gate_max_tokens)
        self._publication_gate_concurrency = max(1, publication_gate_concurrency)
        self._publication_gate_reviewer: PublicationGateReviewer | None = None
        self._coverage_gap_enabled = coverage_gap_enabled
        self._coverage_gap_min_risk_score = max(0, coverage_gap_min_risk_score)
        self._coverage_gap_max_cards = max(0, coverage_gap_max_cards)
        self._coverage_gap_min_confidence = min(1.0, max(0.0, coverage_gap_min_confidence))

        # V3 coverage-driven pipeline config
        self._v3_enabled = v3_enabled
        self._v3_coverage_min_risk_score = min(1.0, max(0.0, v3_coverage_min_risk_score))
        self._v3_coverage_max_cells_per_round = max(1, v3_coverage_max_cells_per_round)
        self._v3_coverage_max_attempts = max(1, v3_coverage_max_attempts)
        normalized_evidence_mode = str(v3_evidence_mode).strip().lower()
        self._v3_evidence_mode = (
            normalized_evidence_mode if normalized_evidence_mode in {"off", "shadow", "enforce"} else "off"
        )
        self._v3_evidence_max_candidates = max(1, v3_evidence_max_candidates)

        # V3 runtime tracking (populated per-run)
        self._v3_change_set = None
        self._v3_ledger: CoverageLedger | None = None
        self._v3_task_dimensions: dict[str, list[str]] = {}  # task_id → [dimension, ...]
        self._v3_closure_task_ids: set[str] = set()
        self._v3_closure_finding_ids: set[str] = set()

        # V3 evidence verifier (lazy-init, only when v3_enabled and mode != off)
        self._v3_evidence_verifier: EvidenceVerifier | None = None
        self._calibrator_llm_raw = calibrator_llm  # store for lazy evidence LLM init
        self._v3_evidence_summary: dict[str, Any] | None = None

        # Token tracking context — updated per-run
        self._token_ctx = RunContext()
        self._escalation_llm_raw = reviewer_llm  # store for lazy init

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
        self._context_engine = ContextEngine(gateway, db)
        self._verifier = Verifier()  # #5: 纯逻辑去重/合并阶段（在 LLM 校准之前）
        self._escalation_reviewer: EscalationReviewer | None = None
        # B4: LoopDetector 每 run 新建，避免跨 run 状态污染
        # Plugin-loaded reviewers (merged at init time)
        self._extra_reviewers: dict[str, type[BaseReviewer]] = {}

        # 渐进式 Skill 加载（Level 1）：发现 skills，建立 reviewer_type -> [SkillMeta] 1:N 映射
        from reviewforge.skills.loader import SkillLoader

        self._skill_loader = SkillLoader(skills_dir or Path(__file__).resolve().parent.parent / "skills")
        self._skills_by_type: dict[str, list[Any]] = {}
        try:
            for meta in self._skill_loader.discover():
                if meta.reviewer_type:
                    self._skills_by_type.setdefault(meta.reviewer_type, []).append(meta)
        except Exception as e:
            logger.warning(f"Skill discovery failed: {e}")

    def register_plugin_reviewers(self, plugins: dict[str, type[BaseReviewer]]) -> None:
        """Merge plugin-loaded reviewers into the reviewer map."""
        self._extra_reviewers.update(plugins)

    @property
    def skills_dir(self):
        """Directory the SkillLoader scans (for console-driven skill CRUD)."""
        return self._skill_loader._skills_dir

    def reload_skills(self) -> int:
        """Re-scan the skills directory and rebuild the reviewer_type → [SkillMeta] map.

        Skill *bodies* are already read fresh per run; this picks up new skills and
        changed frontmatter (the type mapping) without a restart. Returns skill count.
        """
        self._skills_by_type = {}
        metas = self._skill_loader.discover()
        for meta in metas:
            if meta.reviewer_type:
                self._skills_by_type.setdefault(meta.reviewer_type, []).append(meta)
        return len(metas)

    def register_config_agent(
        self,
        *,
        reviewer_type: str,
        description: str,
        allowed_tools: list[str],
        model_profile: str = "default",
        max_steps: int = 6,
        instructions: str = "",
    ) -> str:
        """Register a config-type reviewer into the live registry + reviewer map (no restart).

        Returns the reviewer name (``<reviewer_type>_reviewer``).
        """
        from reviewforge.core.specs import AgentSpec
        from reviewforge.engine.generic_reviewer import make_config_reviewer

        name = f"{reviewer_type}_reviewer"
        self._registry.register_agent(
            AgentSpec(
                name=name,
                role="executor",
                description=description,
                allowed_tools=list(allowed_tools),
                model_profile=model_profile,
                max_steps=max_steps,
            )
        )
        self._extra_reviewers[name] = make_config_reviewer(
            name=name, reviewer_type=reviewer_type, instructions=instructions, max_steps=max_steps
        )
        return name

    def unregister_config_agent(self, reviewer_type: str) -> bool:
        """Remove a config-type reviewer from the live registry + reviewer map."""
        name = f"{reviewer_type}_reviewer"
        removed = self._extra_reviewers.pop(name, None) is not None
        self._registry.unregister_agent(name)
        return removed

    def _resolve_skill(self, metas: list[Any], language: str | None = None, framework: str | None = None) -> Any | None:
        """从同 reviewer_type 的多个 skill 中按语言/框架选出最佳匹配。

        优先级:
          1. (lang, fw)   精确匹配 — 例如 Vue TS 文件走 vue_patterns
          2. (lang, none)  语言匹配且无框架限制 — 例如 Go 文件走 go_best_practices
          3. (*, fw)      框架匹配（语言不限）— 例如 Vue JS 文件也走 vue_patterns
          4. (*, *)       通用 skill（无语言无框架限制）— 例如 security_rules, code_quality
          5. 兜底: 同类型任意一个
        """
        if not metas:
            return None

        # 1. (language, framework) exact
        if language and framework:
            for m in metas:
                if language in m.languages and framework in m.frameworks:
                    return m

        # 2. (language, no framework restriction) — best when we know the language
        if language:
            for m in metas:
                if language in m.languages and not m.frameworks:
                    return m

        # 3. (*, framework) — framework match with any (or no) language
        if framework:
            for m in metas:
                langs = m.languages
                has_lang_match = (not langs) or (language and language in langs)
                if framework in m.frameworks and has_lang_match:
                    return m

        # 4. (language, *) only when framework IS known but no exact (lang,fw) match
        if language and framework:
            for m in metas:
                if language in m.languages:
                    return m

        # 5. Universal — no language or framework constraints
        for m in metas:
            if not m.languages and not m.frameworks:
                return m

        # 6. Any skill of this type
        for m in metas:
            return m

        return None

    def _detect_task_language(self, task: Any) -> str | None:
        """从 task 文件列表检测主导语言（多数投票）。"""
        from collections import Counter

        from reviewforge.engine.symbol_extractor import detect_language

        if not task.files:
            return None
        langs = [detect_language(f) for f in task.files]
        known = [lang for lang in langs if lang and lang != "unknown"]
        if not known:
            return None
        return Counter(known).most_common(1)[0][0]

    def _detect_task_framework(self, task: Any) -> str | None:
        """从前端文件中检测使用的框架。

        仅处理 JS/TS 文件，通过特征导入或文件扩展名判断框架。
        """
        if not task.files:
            return None

        # 扩展名特征（最强信号）
        ext_fw = {".vue": "vue", ".svelte": "svelte"}
        for f in task.files:
            for ext, fw in ext_fw.items():
                if f.endswith(ext):
                    return fw

        # JSX/TSX 默认 React（后续可通过 import 内容精判）
        has_jsx = any(f.endswith((".jsx", ".tsx")) for f in task.files)
        if has_jsx:
            return "react"

        return None

    async def run(self, state: StateStore) -> dict[str, Any]:
        """Execute the full review pipeline. Returns summary."""
        loop_detector = LoopDetector()  # B4: per-run instance
        self._v3_evidence_summary = None

        # Resume (可恢复): if a prior run for this exact (repo, pr, head_sha) didn't
        # complete, reuse its run_id and rehydrate findings + completed reviewers from
        # the DB so already-done work is skipped instead of redone.
        resumed = await self._db.get_resumable_run(state.repo, state.pr_number, state.head_sha) if self._db else None
        if resumed:
            run_id = resumed["run_id"]
            if not await self._db.restart_run(run_id):
                logger.info(
                    "Review retry for %s/%s@%s was already claimed", state.repo, state.pr_number, state.head_sha
                )
                return {
                    "status": "duplicate_skipped",
                    "total_findings": 0,
                    "confirmed": 0,
                    "false_positives": 0,
                    "tasks_completed": 0,
                    "tasks_failed": 0,
                }
            await self._rehydrate(state, run_id)
            self._events.set_run_id(run_id)
            self._events.emit(
                "review.resumed",
                {
                    "repo": state.repo,
                    "pr": state.pr_number,
                    "run_id": run_id,
                    "prior_findings": len(state.findings),
                    "done_reviewers": len(state.list_tasks()),
                },
            )
        else:
            if self._db and await self._db.has_active_run_for_head(state.repo, state.pr_number, state.head_sha):
                logger.info(
                    "Review for %s/%s@%s is already active/completed", state.repo, state.pr_number, state.head_sha
                )
                return {
                    "status": "duplicate_skipped",
                    "total_findings": 0,
                    "confirmed": 0,
                    "false_positives": 0,
                    "tasks_completed": 0,
                    "tasks_failed": 0,
                }
            run_id = uuid.uuid4().hex[:12]
            self._events.set_run_id(run_id)
            self._events.emit("review.started", {"repo": state.repo, "pr": state.pr_number, "run_id": run_id})
            if self._db:
                await self._db.create_run(
                    run_id=run_id,
                    repo=state.repo,
                    pr_number=state.pr_number,
                    head_sha=state.head_sha,
                    base_sha=state.base_sha,
                )

        # Set token tracking context for this run
        if self._db:
            self._token_ctx.set(run_id, self._db)

        planner_errors: list[str] = []
        try:
            # Build repository-aware context before planning. Failure is
            # non-fatal: the original diff-only pipeline remains available.
            self._events.emit("context_engine.started", {"file_count": len(state.files_changed)})
            try:
                manifest = await self._context_engine.build(state)
                self._events.emit(
                    "context_engine.completed",
                    {
                        "indexed_files": manifest.get("coverage", {}).get("indexed_files", 0),
                        "references": sum(len(item.get("paths", [])) for item in manifest.get("references", [])),
                        "wiki_pages": len(manifest.get("wiki_pages", [])),
                        "risk_signals": len(manifest.get("risk_signals", [])),
                    },
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Context Engine failed; continuing with diff-only review: %s", exc, exc_info=True)
                state.impact_manifest = {}
                self._events.emit("context_engine.failed", {"error": str(exc)})

            # Phase 0: deterministic coverage is independent of Planner routing
            # and Reviewer health. Keep its keys so later reviewer overlap can be
            # merged at ingestion instead of becoming duplicate findings.
            self._events.emit("deterministic_scan.started", {"file_count": len(state.files_changed)})
            scan_result = await scan_changed_files(self._gateway, state)
            phase0_keys = {finding_identity(finding) for finding in scan_result.findings}
            existing_keys = {finding_identity(finding) for finding in state.list_findings()}
            added_count = 0
            for finding in scan_result.findings:
                key = finding_identity(finding)
                if key in existing_keys:
                    continue
                state.add_finding(finding)
                existing_keys.add(key)
                added_count += 1
            self._events.emit(
                "deterministic_scan.completed",
                {
                    "files_scanned": scan_result.files_scanned,
                    "files_failed": len(scan_result.file_errors),
                    "scanners_failed": len(scan_result.scanner_errors),
                    "findings_count": added_count,
                },
            )

            # V3: compile SemanticChangeSet and build CoverageLedger
            if self._v3_enabled:
                await self._v3_compile_and_build_ledger(state)

            # Phase 1+2: iterative plan → schedule → execute (re-planning loop).
            # #4 Scheduler dispatches reviewers by priority with bounded concurrency.
            # #2/#3 bounded rounds; loop-detector rescue→stall guards repeats and writes
            # a Note that the Planner consumes when re-planning the next round.
            scheduler = Scheduler(concurrency=4)
            max_rounds = 3

            async def _run_one(task: ReviewTask) -> None:
                self._events.emit("reviewer.started", {"reviewer": task.reviewer, "files": task.files})
                t_start = time.monotonic()
                try:
                    reviewer = self._create_reviewer(task.reviewer)
                    if not reviewer:
                        state.update_task(task.id, status="failed", error=f"unknown reviewer: {task.reviewer}")
                        if self._db:
                            await self._db.insert_metric(
                                run_id, task.reviewer, status="failed", error=f"unknown reviewer: {task.reviewer}"
                            )
                        return
                    if reviewer._agentic and not self._has_agentic_context(task, state):
                        reviewer._agentic = False
                        self._events.emit(
                            "reviewer.agentic_skipped",
                            {
                                "reviewer": task.reviewer,
                                "reason": "impact manifest has no cross-file or historical graph evidence",
                            },
                        )
                    # 按 task 文件检测语言/框架，注入匹配的 skill
                    lang = self._detect_task_language(task)
                    fw = self._detect_task_framework(task)
                    self._attach_skill(reviewer, lang, fw)
                    reviewer._target_language = lang or ""
                    reviewer._target_framework = fw or ""
                    findings = await reviewer.execute(task, state)
                    accepted_task_findings: list[Finding] = []
                    for f in findings:
                        if finding_identity(f) in phase0_keys:
                            continue
                        state.add_finding(f)
                        accepted_task_findings.append(f)
                    state.update_task(task.id, status="completed")
                    if self._v3_enabled:
                        self._track_broad_pass_coverage(
                            task_id=task.id,
                            reviewer=task.reviewer,
                            task_files=task.files,
                            findings=accepted_task_findings,
                        )
                    self._events.emit(
                        "reviewer.completed",
                        {"reviewer": task.reviewer, "findings_count": len(accepted_task_findings)},
                    )
                    if self._db:
                        await self._db.insert_metric(
                            run_id,
                            task.reviewer,
                            findings_count=len(accepted_task_findings),
                            duration_ms=int((time.monotonic() - t_start) * 1000),
                        )
                except Exception as e:
                    state.update_task(task.id, status="failed", error=str(e))
                    self._events.emit("reviewer.failed", {"reviewer": task.reviewer, "error": str(e)})
                    if self._db:
                        await self._db.insert_metric(
                            run_id,
                            task.reviewer,
                            duration_ms=int((time.monotonic() - t_start) * 1000),
                            status="failed",
                            error=str(e),
                        )

            for round_no in range(max_rounds):
                self._events.emit("planner.started", {"round": round_no})
                notes = state.consume_notes()  # #3: feed prior-round hints to the planner
                planner_succeeded = True
                try:
                    proposed = await self._planner.plan(state, notes=notes)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    # Phase 0 findings remain actionable when the planner model or
                    # provider is unavailable. Continue through verification and
                    # commenting instead of failing the whole review.
                    logger.error("Planner failed in round %d: %s", round_no, e, exc_info=True)
                    self._events.emit("planner.failed", {"round": round_no, "error": str(e)})
                    planner_errors.append(f"round {round_no}: {e}")
                    planner_succeeded = False
                    proposed = []
                # Slice oversized correctness tasks before they enter
                # StateStore/Scheduler so each chunk fits the reviewer
                # prompt's 36k-char diff budget.
                proposed = _split_oversized_correctness_tasks(proposed, state.file_diffs or {})
                for task in proposed:
                    state.add_task(task)
                if planner_succeeded:
                    self._events.emit("planner.completed", {"round": round_no, "task_count": len(proposed)})

                pending = state.list_tasks(status="pending")
                if not pending:
                    break  # planner proposed nothing new → converged

                # Loop detection (rescue → stall) before dispatch
                runnable = []
                for task in pending:
                    sig = LoopDetector.make_signature(task.reviewer, task.files)
                    loop_result = loop_detector.check(sig)
                    if loop_result == "stall":
                        state.update_task(task.id, status="failed", error="loop_stalled")
                        self._events.emit("reviewer.stalled", {"task_id": task.id, "signature": sig})
                    elif loop_result == "rescue":
                        state.update_task(task.id, status="failed", error="rescue_drain")
                        self._events.emit("reviewer.rescued", {"task_id": task.id})
                        # #3: hint the Planner that this reviewer/file combo is looping
                        state.add_note(
                            Note(
                                from_agent="loop_detector",
                                type="rescue_hint",
                                content=f"Reviewer {task.reviewer} 在相同文件上重复，已排空；请改派其他维度或停止。",
                            )
                        )
                    else:
                        state.update_task(task.id, status="claimed")
                        runnable.append(task)

                if runnable:
                    await scheduler.dispatch(runnable, _run_one)  # #4: priority + concurrency

                if loop_detector.is_stalled:
                    self._events.emit("planner.stalled", {"round": round_no})
                    break
                # Re-plan only when fresh hints (notes) exist; otherwise converged.
                if not state.notes:
                    break

            # Phase 2.5: spend one bounded call only on high-risk changed
            # symbols that the broad first pass did not cover. Its output still
            # passes through every normal verifier/calibration gate below.
            if self._coverage_gap_enabled:
                await self._run_coverage_gap_pass(state, run_id, phase0_keys)

            # V3: mark unresolved broad-pass cells as ABSTAINED, then run targeted closure
            if self._v3_enabled:
                self._v3_mark_unresolved_cells()
                await self._v3_run_targeted_closure(state, run_id, phase0_keys)

            # Phase 3: Verifier (#5, pure-logic de-dupe/merge) → Dynamic Calibration.
            raw_candidates = state.list_findings(status="candidate")
            unsupported_redirects = unsupported_python_open_redirect_findings(raw_candidates, state.diff_summary)
            unsupported_redirect_ids = {finding.id for finding in unsupported_redirects}
            for finding in unsupported_redirects:
                state.update_finding(
                    finding.id,
                    status="false_positive",
                    verified_by="diff-evidence",
                    verify_reason="The enclosing Python function contains no redirect response API.",
                )
            raw_candidates = [finding for finding in raw_candidates if finding.id not in unsupported_redirect_ids]
            reanchored = [
                *reanchor_accessibility_findings(raw_candidates, state.diff_summary),
                *reanchor_security_detector_duplicates(raw_candidates, state.diff_summary),
                *reanchor_quality_detector_duplicates(raw_candidates, state.diff_summary),
            ]
            for finding in reanchored:
                state.update_finding(finding.id, line=finding.line, category=finding.category)
            if reanchored:
                self._events.emit("anchors.repaired", {"count": len(reanchored)})
            if unsupported_redirects:
                self._events.emit("anchors.rejected", {"count": len(unsupported_redirects)})
            candidates, dropped_ids = self._verifier.verify(raw_candidates)
            for fid in dropped_ids:
                state.update_finding(
                    fid, status="false_positive", verified_by="verifier", verify_reason="重复/低置信，已合并"
                )
            if dropped_ids:
                self._events.emit("verifier.completed", {"kept": len(candidates), "merged": len(dropped_ids)})

            # Remove generic "missing tests/docs" advice before the escalation
            # split.  This deterministic evidence gate avoids spending an
            # agentic tool loop on findings that cannot become actionable.
            candidates, actionability_rejected = apply_actionability_gate(candidates, state.diff_summary)
            for finding in actionability_rejected:
                state.update_finding(
                    finding.id,
                    status="false_positive",
                    verified_by=finding.verified_by,
                    verify_reason=finding.verify_reason,
                )
            if actionability_rejected:
                self._events.emit(
                    "actionability.completed",
                    {"kept": len(candidates), "filtered": len(actionability_rejected)},
                )

            # Apply zero-token static proofs before fuzzy findings reach escalation.
            # Escalation and calibration are exclusive paths, so leaving this gate
            # inside the calibrator lets provably-safe findings bypass it.
            candidates, code_evidence_rejected = apply_code_evidence_gate(
                candidates,
                state.diff_summary,
            )
            for finding in code_evidence_rejected:
                state.update_finding(
                    finding.id,
                    status="false_positive",
                    verified_by=finding.verified_by,
                    verify_reason=finding.verify_reason,
                )
            if code_evidence_rejected:
                self._events.emit(
                    "code_evidence.completed",
                    {"kept": len(candidates), "filtered": len(code_evidence_rejected)},
                )

            # V3: Evidence verification (after deterministic gates, before escalation/calibration)
            if self._v3_enabled and self._v3_evidence_mode != "off":
                candidates = await self._run_v3_evidence_verification(candidates, state, run_id)

            # Phase 3.5/4: split candidates — trace/uncertain findings → Escalation
            # (agentic verify, verdict is FINAL); the rest → Dynamic Calibration. Mutually
            # exclusive, so a finding is judged once and escalation verdicts are never
            # overwritten by the calibrator's security auto-confirm.
            esc_set: list[Finding] = []
            calib_set: list[Finding] = []
            if candidates and self._escalation_enabled:
                if self._escalation_reviewer is None:
                    esc_llm = self._escalation_llm_raw
                    if self._db:
                        esc_llm = TrackedChatLLM(inner=esc_llm, ctx=self._token_ctx, agent_name="escalation")
                    self._escalation_reviewer = EscalationReviewer(
                        llm=esc_llm,
                        gateway=self._gateway,
                        max_steps=self._escalation_max_steps,
                        max_tokens=self._escalation_max_tokens,
                        confidence_min=self._escalation_confidence_min,
                        confidence_max=self._escalation_confidence_max,
                        event_bus=self._events,
                    )
                for f in candidates:
                    bucket = (
                        esc_set
                        if _should_escalate_finding(f, self._escalation_confidence_min, self._escalation_confidence_max)
                        else calib_set
                    )
                    bucket.append(f)
            else:
                calib_set = list(candidates)

            if esc_set:
                self._events.emit("escalation.started", {"candidate_count": len(esc_set)})
                escalated = await self._escalation_reviewer.escalate_batch(esc_set, state)
                for f in escalated:
                    if f.status in ("confirmed", "false_positive"):
                        state.update_finding(
                            f.id, status=f.status, verified_by=f.verified_by, verify_reason=f.verify_reason
                        )
                self._events.emit(
                    "escalation.completed",
                    {
                        "confirmed": len([f for f in escalated if f.status == "confirmed"]),
                        "filtered": len([f for f in escalated if f.status == "false_positive"]),
                    },
                )

            if calib_set:
                self._events.emit("calibration.started", {"candidate_count": len(calib_set)})
                wiki_pages = state.impact_manifest.get("wiki_pages", [])
                calibration_context = (
                    render_impact_manifest(
                        {
                            "version": state.impact_manifest.get("version", 1),
                            "files": [],
                            "references": [],
                            "historical_graph": [],
                            "risk_signals": [],
                            "wiki_pages": wiki_pages,
                            "coverage_gap": state.impact_manifest.get("coverage_gap", {}),
                        },
                        max_chars=3_000,
                    )
                    if wiki_pages
                    else ""
                )
                calibrated = await self._calibrator.calibrate(
                    calib_set,
                    state.diff_summary,
                    context_evidence=calibration_context,
                )
                for f in calibrated:
                    if f.status == "confirmed":
                        state.update_finding(
                            f.id, status="confirmed", verified_by=f.verified_by, verify_reason=f.verify_reason
                        )
                    elif f.status == "false_positive":
                        state.update_finding(
                            f.id, status="false_positive", verified_by=f.verified_by, verify_reason=f.verify_reason
                        )
                confirmed_count = len([f for f in calibrated if f.status == "confirmed"])
                filtered_count = len([f for f in calibrated if f.status == "false_positive"])
                self._events.emit(
                    "calibration.completed",
                    {"confirmed": confirmed_count, "filtered": filtered_count},
                )

            # Phase 3.5: Cross-PR Analysis
            if self._cross_pr:
                # A retry rehydrates already-delivered Phase-0 findings as
                # ``reported``. They still need to seed graph provenance under
                # the run that is about to complete, but must not be commented
                # a second time.
                confirmed_findings = [
                    *state.list_findings(status="confirmed"),
                    *state.list_findings(status="reported"),
                ]
                self._events.emit("cross_pr.started")
                try:
                    cross_findings = await self._cross_pr.analyze(
                        run_id=run_id,
                        state=state,
                        existing_findings=confirmed_findings,
                    )
                    existing_cross_keys = {finding_identity(finding) for finding in state.list_findings()}
                    accepted_cross_findings = []
                    for f in cross_findings:
                        key = finding_identity(f)
                        if key in existing_cross_keys:
                            continue
                        state.add_finding(f)
                        existing_cross_keys.add(key)
                        accepted_cross_findings.append(f)
                    if accepted_cross_findings:
                        self._events.emit(
                            "cross_pr.completed",
                            {
                                "cross_pr_findings": len(accepted_cross_findings),
                            },
                        )
                        logger.info(f"Cross-PR: found {len(accepted_cross_findings)} cross-PR issues")
                    else:
                        self._events.emit("cross_pr.completed", {"cross_pr_findings": 0})
                except Exception as e:
                    logger.error(f"Cross-PR analysis failed: {e}")
                    self._events.emit("cross_pr.failed", {"error": str(e)})

            if self._publication_gate_enabled:
                await self._run_publication_gate(state)

            # Persist all findings to DB
            if self._db:
                for f in state.findings.values():
                    await self._db.insert_finding(run_id, f.to_dict())

            # Phase 4: Comment
            confirmed = state.list_findings(status="confirmed")
            comment_result = CommentDeliveryResult()
            if confirmed:
                self._events.emit("commenter.started", {"finding_count": len(confirmed)})
                comment_result = await self._post_comments(confirmed, state)
                self._events.emit("commenter.completed", comment_result.to_dict())

            summary = {
                "total_findings": len(state.findings),
                "confirmed": len(state.list_findings(status="confirmed")) + len(state.list_findings(status="reported")),
                "false_positives": len(state.list_findings(status="false_positive")),
                "tasks_completed": len(state.list_tasks(status="completed")),
                "tasks_failed": len(state.list_tasks(status="failed")),
                "comment_delivery": comment_result.to_dict(),
            }

            # V3: add coverage summary
            if self._v3_enabled:
                v3_cov = self._build_v3_coverage_summary()
                if v3_cov:
                    summary["v3_coverage"] = v3_cov
                if self._v3_evidence_summary:
                    summary["v3_evidence"] = self._v3_evidence_summary
            retryable_errors = list(planner_errors)
            if comment_result.retryable:
                retryable_errors.extend(comment_result.errors or ("Transient comment delivery failure",))
            if retryable_errors:
                summary.update({"status": "partial", "retryable": True})
                self._events.emit(
                    "review.partial",
                    {**summary, "errors": retryable_errors},
                )
            self._events.emit("review.completed", summary)

            # A Planner/provider or transient comment-delivery outage must not
            # permanently de-duplicate this head as reviewed. Keep the same run
            # resumable; already-reported findings will not be posted twice.
            if self._db:
                if retryable_errors:
                    await self._db.fail_run(
                        run_id,
                        "Review incomplete and retryable: " + "; ".join(retryable_errors),
                        summary=summary,
                    )
                else:
                    await self._db.complete_run(run_id, summary)

            return summary

        except asyncio.CancelledError:
            if self._db:
                await self._db.fail_run(run_id, "review task cancelled")
            raise
        except Exception as e:
            if self._db:
                await self._db.fail_run(run_id, str(e))
            raise

    async def _run_publication_gate(self, state: StateStore) -> None:
        """Independently verify every not-yet-reported finding before publication."""

        candidates = state.list_findings(status="confirmed")
        if not candidates:
            self._events.emit(
                "publication_gate.completed",
                {"attempted": 0, "confirmed": 0, "filtered": 0, "inconclusive": 0},
            )
            return

        if self._publication_gate_reviewer is None:
            gate_llm = self._escalation_llm_raw
            if self._db:
                gate_llm = TrackedChatLLM(
                    inner=gate_llm,
                    ctx=self._token_ctx,
                    agent_name="publication_gate",
                )
            self._publication_gate_reviewer = PublicationGateReviewer(
                llm=gate_llm,
                gateway=self._gateway,
                max_steps=self._publication_gate_max_steps,
                max_tokens=self._publication_gate_max_tokens,
                event_bus=self._events,
            )

        self._events.emit(
            "publication_gate.started",
            {"candidate_count": len(candidates)},
        )
        verdicts = await self._publication_gate_reviewer.escalate_batch(
            candidates,
            state,
            concurrency=self._publication_gate_concurrency,
        )
        counts = {"confirmed": 0, "filtered": 0, "inconclusive": 0}
        for finding in verdicts:
            if finding.status == "confirmed":
                counts["confirmed"] += 1
            elif finding.status == "false_positive":
                counts["filtered"] += 1
            else:
                counts["inconclusive"] += 1
            state.update_finding(
                finding.id,
                status=finding.status,
                verified_by=finding.verified_by,
                verify_reason=finding.verify_reason,
                confidence=finding.confidence,
            )
        self._events.emit(
            "publication_gate.completed",
            {"attempted": len(candidates), **counts},
        )

    async def _run_coverage_gap_pass(
        self,
        state: StateStore,
        run_id: str,
        phase0_keys: set[tuple[Any, ...]],
    ) -> None:
        """Run one evidence-constrained correctness pass when coverage warrants it."""

        if any(task.reviewer == "coverage_gap_reviewer" and task.status == "completed" for task in state.list_tasks()):
            self._events.emit("coverage_gap.skipped", {"reason": "already completed in resumed run"})
            return

        cards = build_evidence_cards(
            state.impact_manifest,
            state.list_findings(),
            min_risk_score=self._coverage_gap_min_risk_score,
            max_cards=self._coverage_gap_max_cards,
        )
        state.impact_manifest["coverage_gap"] = {
            "version": 1,
            "selected": len(cards),
            "cards": [card.to_dict() for card in cards],
        }
        self._events.emit(
            "coverage_gap.analyzed",
            {
                "selected": len(cards),
                "min_risk_score": self._coverage_gap_min_risk_score,
                "max_cards": self._coverage_gap_max_cards,
            },
        )
        if not cards:
            return

        files = list(dict.fromkeys(card.file for card in cards))
        task = ReviewTask(
            reviewer="coverage_gap_reviewer",
            files=files,
            rationale="selective high-risk uncovered-symbol correctness pass",
            status="claimed",
        )
        state.add_task(task)
        reviewer = self._create_reviewer(
            "correctness_reviewer",
            model_agent_name="coverage_gap_reviewer",
            force_agentic=False,
        )
        if reviewer is None:
            state.update_task(task.id, status="failed", error="correctness reviewer unavailable")
            self._events.emit("coverage_gap.failed", {"error": "correctness reviewer unavailable"})
            return

        lang = self._detect_task_language(task)
        fw = self._detect_task_framework(task)
        self._attach_skill(reviewer, lang, fw)
        reviewer._target_language = lang or ""
        reviewer._target_framework = fw or ""
        reviewer._review_focus = (
            "This is a selective coverage-gap pass. Inspect only the symbols in "
            "coverage_gap.cards. A coverage gap is not itself a defect. Report only a "
            "concrete, observable correctness or security failure supported by the diff "
            "and Evidence Card. Anchor every finding to one of the card's added_lines. "
            "Do not report missing tests, documentation, style, speculative hardening, "
            "or generic advice. Return an empty findings array when evidence is insufficient."
        )

        started = time.monotonic()
        self._events.emit("coverage_gap.started", {"cards": len(cards), "files": files})
        try:
            findings = await reviewer.execute(task, state)
            accepted, rejected = filter_gap_findings(
                findings,
                cards,
                min_confidence=self._coverage_gap_min_confidence,
            )
            existing_keys = {finding_identity(finding) for finding in state.list_findings()}
            added = 0
            for finding in accepted:
                key = finding_identity(finding)
                if key in phase0_keys or key in existing_keys:
                    continue
                state.add_finding(finding)
                existing_keys.add(key)
                added += 1
            state.update_task(task.id, status="completed")
            duration_ms = int((time.monotonic() - started) * 1000)
            self._events.emit(
                "coverage_gap.completed",
                {
                    "cards": len(cards),
                    "accepted": added,
                    "rejected": len(rejected) + len(accepted) - added,
                    "duration_ms": duration_ms,
                },
            )
            if self._db:
                await self._db.insert_metric(
                    run_id,
                    "coverage_gap_reviewer",
                    findings_count=added,
                    duration_ms=duration_ms,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            state.update_task(task.id, status="failed", error=str(exc))
            self._events.emit("coverage_gap.failed", {"error": str(exc)})
            if self._db:
                await self._db.insert_metric(
                    run_id,
                    "coverage_gap_reviewer",
                    duration_ms=int((time.monotonic() - started) * 1000),
                    status="failed",
                    error=str(exc),
                )

    # ── V3 integration helpers ─────────────────────────────────────────────

    async def _v3_compile_and_build_ledger(self, state: StateStore) -> None:
        """Compile SemanticChangeSet and build CoverageLedger from state."""
        self._v3_task_dimensions.clear()
        self._v3_closure_task_ids.clear()
        self._v3_closure_finding_ids.clear()
        try:
            cs = compile_semantic_changeset(state)
            self._v3_change_set = cs

            cs_dict = cs.to_dict()
            self._v3_ledger = CoverageLedger.from_change_set(cs_dict)

            # Store bounded summaries in impact_manifest (avoid exploding prompts)
            unit_summaries = [
                {
                    "id": u.id,
                    "path": u.path,
                    "symbol": u.symbol,
                    "start_line": u.start_line,
                    "end_line": u.end_line,
                    "risk_score": u.risk_score,
                    "risk_reasons": list(u.risk_reasons),
                }
                for u in cs.units
            ]
            state.impact_manifest["v3"] = {
                "semantic": {
                    "repo": cs.repo,
                    "pr_number": cs.pr_number,
                    "head_sha": cs.head_sha,
                    "unit_count": len(cs.units),
                    "unresolved_count": len(cs.unresolved_files),
                    "units": unit_summaries,
                },
                "coverage_summary": self._v3_ledger.completion_summary(),
            }

            self._events.emit(
                "v3.semantic.compiled",
                {
                    "repo": cs.repo,
                    "pr_number": cs.pr_number,
                    "unit_count": len(cs.units),
                    "unresolved_files": len(cs.unresolved_files),
                },
            )
            self._events.emit(
                "v3.coverage.created",
                {
                    "cell_count": len(self._v3_ledger.cells),
                    "mandatory_total": self._v3_ledger.completion_summary()["mandatory_total"],
                },
            )
        except Exception as exc:
            logger.warning("V3 semantic compilation failed: %s", exc, exc_info=True)
            self._v3_change_set = None
            self._v3_ledger = None
            self._events.emit("v3.semantic.failed", {"error": str(exc)})

    def _track_broad_pass_coverage(
        self,
        task_id: str,
        reviewer: str,
        task_files: list[str],
        findings: list[Finding],
    ) -> None:
        """Track which coverage cells were addressed by a broad-pass reviewer task."""
        if not self._v3_ledger or not self._v3_change_set:
            return

        dimensions = self._reviewer_dimensions(reviewer)
        self._v3_task_dimensions[task_id] = dimensions

        task_files_set = set(task_files)

        for dim_name in dimensions:
            try:
                dim = CoverageDimension(dim_name)
            except ValueError:
                continue
            cells = self._v3_ledger.cells_by_dimension(dim)
            for cell in cells:
                if cell.status != CoverageStatus.PENDING:
                    continue
                if cell.path not in task_files_set:
                    continue
                unit = self._find_unit_by_id(cell.unit_id)
                # Check if any finding is unit-specific
                has_unit_specific = False
                for f in findings:
                    if self._finding_matches_unit(f, unit):
                        has_unit_specific = True
                        break
                if has_unit_specific:
                    # Find the matching finding to record
                    for f in findings:
                        if self._finding_matches_unit(f, unit):
                            try:
                                cell.transition(CoverageStatus.ASSIGNED, task_id=task_id)
                                cell.transition(CoverageStatus.COVERED, terminal_reason=f"finding:{f.id}")
                                cell.add_finding(f.id)
                            except (ValueError, KeyError):
                                pass
                            break
                else:
                    # No unit-specific finding — mark as ABSTAINED
                    try:
                        cell.transition(CoverageStatus.ASSIGNED, task_id=task_id)
                        cell.transition(
                            CoverageStatus.ABSTAINED,
                            terminal_reason="broad pass produced no unit-specific finding for this cell",
                        )
                    except (ValueError, KeyError):
                        pass

    def _v3_mark_unresolved_cells(self) -> None:
        """Mark broad-pass cells that were not resolved as ABSTAINED."""
        if not self._v3_ledger:
            return
        for cell in self._v3_ledger.cells:
            if cell.status == CoverageStatus.PENDING:
                if cell.assigned_task_ids:
                    try:
                        cell.transition(CoverageStatus.ASSIGNED, task_id=cell.assigned_task_ids[-1])
                        cell.transition(
                            CoverageStatus.ABSTAINED,
                            terminal_reason="broad pass produced no unit-specific finding for this cell",
                        )
                    except (ValueError, KeyError):
                        pass

    async def _v3_run_targeted_closure(
        self,
        state: StateStore,
        run_id: str,
        phase0_keys: set[tuple[Any, ...]],
    ) -> None:
        """Run bounded targeted coverage closure for unresolved mandatory cells."""
        if not self._v3_ledger or not self._v3_change_set:
            return

        self._events.emit("v3.coverage.started", {"cell_count": len(self._v3_ledger.cells)})

        # Select cells: unresolved mandatory, prioritized by risk
        selected = self._select_closure_cells(
            state,
            state.list_findings(),
            self._v3_ledger,
        )
        if not selected:
            self._events.emit(
                "v3.coverage.completed",
                {"total_cells": len(self._v3_ledger.cells), "selected": 0, "closure_findings": 0},
            )
            return

        existing_keys = {finding_identity(f) for f in state.list_findings()}
        scheduler = Scheduler(concurrency=4)

        # Run one attempt per eligible cell in each wave. Reviewer calls are
        # independent and expensive, so they may overlap; all shared-state
        # integration stays below, in the stable ``selected`` order.
        for _wave in range(self._v3_coverage_max_attempts):
            wave_cells = [
                cell
                for cell in selected
                if cell.status in (CoverageStatus.PENDING, CoverageStatus.ABSTAINED, CoverageStatus.FAILED)
                and len(cell.assigned_task_ids) < self._v3_coverage_max_attempts
            ]
            if not wave_cells:
                break

            wave_items: list[tuple[CoverageCell, Any, ReviewTask, Any]] = []
            item_by_task_id: dict[str, tuple[CoverageCell, Any, ReviewTask, Any]] = {}
            execution_results: dict[str, tuple[list[Finding] | None, Exception | None, int]] = {}

            for cell in wave_cells:
                unit = self._find_unit_by_id(cell.unit_id)
                dim = cell.dimension.value
                reviewer = self._dimension_reviewer(dim)
                focus = self._build_review_focus(
                    path=cell.path,
                    symbol=getattr(unit, "symbol", "") if unit else "",
                    start_line=getattr(unit, "start_line", 0) if unit else 0,
                    end_line=getattr(unit, "end_line", 0) if unit else 0,
                    dimension=dim,
                    risk_reasons=getattr(unit, "risk_reasons", []) if unit else [],
                    is_retry=bool(cell.assigned_task_ids),
                )

                task = ReviewTask(
                    reviewer=reviewer,
                    files=[cell.path],
                    rationale=f"v3 targeted closure: {dim} for {cell.unit_id}",
                    status="claimed",
                )
                state.add_task(task)
                self._v3_closure_task_ids.add(task.id)

                try:
                    cell.transition(CoverageStatus.ASSIGNED, task_id=task.id)
                except ValueError:
                    self._events.emit(
                        "v3.coverage.cell_failed",
                        {"unit_id": cell.unit_id, "dimension": dim, "error": "invalid cell state"},
                    )
                    continue

                reviewer_obj = self._create_reviewer(reviewer)
                if not reviewer_obj:
                    error = f"unknown reviewer: {reviewer}"
                    state.update_task(task.id, status="failed", error=error)
                    try:
                        cell.transition(CoverageStatus.FAILED, terminal_reason=error)
                    except ValueError:
                        pass
                    self._events.emit(
                        "v3.coverage.cell_failed",
                        {"unit_id": cell.unit_id, "dimension": dim, "error": error},
                    )
                    continue

                lang = self._detect_task_language(task)
                fw = self._detect_task_framework(task)
                self._attach_skill(reviewer_obj, lang, fw)
                reviewer_obj._target_language = lang or ""
                reviewer_obj._target_framework = fw or ""
                reviewer_obj._review_focus = focus

                item = (cell, unit, task, reviewer_obj)
                wave_items.append(item)
                item_by_task_id[task.id] = item

            async def _execute_closure_task(task: ReviewTask) -> None:
                reviewer_obj = item_by_task_id[task.id][3]
                started = time.monotonic()
                try:
                    findings = await reviewer_obj.execute(task, state)
                    execution_results[task.id] = (
                        findings,
                        None,
                        int((time.monotonic() - started) * 1000),
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    execution_results[task.id] = (
                        None,
                        exc,
                        int((time.monotonic() - started) * 1000),
                    )

            await scheduler.dispatch([item[2] for item in wave_items], _execute_closure_task)

            # Deterministic commit barrier: execution completion order cannot
            # affect finding de-duplication, state insertion, or emitted events.
            for cell, unit, task, _reviewer_obj in wave_items:
                findings, error, duration_ms = execution_results.get(
                    task.id,
                    (None, RuntimeError("closure worker produced no result"), 0),
                )
                dim = cell.dimension.value
                reviewer = task.reviewer
                if error is not None:
                    state.update_task(task.id, status="failed", error=str(error))
                    try:
                        cell.transition(CoverageStatus.FAILED, terminal_reason=str(error)[:200])
                    except ValueError:
                        pass
                    self._events.emit(
                        "v3.coverage.cell_failed",
                        {"unit_id": cell.unit_id, "dimension": dim, "error": str(error)},
                    )
                    if self._db:
                        await self._db.insert_metric(
                            run_id,
                            f"v3_closure_{reviewer}",
                            duration_ms=duration_ms,
                            status="failed",
                            error=str(error)[:200],
                        )
                    continue

                accepted_task_findings: list[Finding] = []
                for finding in findings or []:
                    key = finding_identity(finding)
                    if key in phase0_keys or key in existing_keys:
                        continue
                    state.add_finding(finding)
                    existing_keys.add(key)
                    accepted_task_findings.append(finding)
                    self._v3_closure_finding_ids.add(finding.id)
                state.update_task(task.id, status="completed")

                matching_findings = [
                    finding for finding in accepted_task_findings if self._finding_matches_unit(finding, unit)
                ]
                if matching_findings:
                    finding = matching_findings[0]
                    cell.transition(
                        CoverageStatus.COVERED,
                        terminal_reason=f"closure finding:{finding.id}",
                    )
                    cell.add_finding(finding.id)
                else:
                    cell.transition(
                        CoverageStatus.ABSTAINED,
                        terminal_reason="targeted closure produced no unit-specific finding",
                    )

                self._events.emit(
                    "v3.coverage.cell_reviewed",
                    {
                        "unit_id": cell.unit_id,
                        "dimension": dim,
                        "attempt": len(cell.assigned_task_ids),
                        "findings": len(accepted_task_findings),
                        "unit_specific_findings": len(matching_findings),
                        "duration_ms": duration_ms,
                    },
                )
                if self._db:
                    await self._db.insert_metric(
                        run_id,
                        f"v3_closure_{reviewer}",
                        findings_count=len(accepted_task_findings),
                        duration_ms=duration_ms,
                    )

        self._events.emit(
            "v3.coverage.completed",
            {
                "total_cells": len(self._v3_ledger.cells),
                "selected": len(selected),
                "closure_findings": len(self._v3_closure_finding_ids),
            },
        )

    def _select_closure_cells(
        self,
        state: StateStore,
        findings: list[Finding],
        ledger: CoverageLedger,
    ) -> list[CoverageCell]:
        """Select unresolved cells for targeted closure, respecting risk and attempt limits."""
        # Use assigned_task_ids count (number of dispatches) rather than
        # raw attempts (which increments on both ASSIGNED and FAILED).
        unresolved = [
            c
            for c in ledger.cells
            if c.status == CoverageStatus.PENDING
            or (
                c.status in (CoverageStatus.ABSTAINED, CoverageStatus.FAILED)
                and len(c.assigned_task_ids) < self._v3_coverage_max_attempts
            )
        ]
        unresolved.sort(key=lambda c: (not c.mandatory, -c.risk, c.path, c.line, c.dimension.value))

        # Normal selection: above risk threshold (mandatory status does not
        # bypass the risk gate — only the zero-candidate fallback does).
        selected = [c for c in unresolved if c.risk >= self._v3_coverage_min_risk_score][
            : self._v3_coverage_max_cells_per_round
        ]

        # Zero-candidate fallback: if the PR has zero candidate findings,
        # include correctness cells regardless of risk.
        if not findings and not selected:
            correctness_unresolved = [c for c in unresolved if c.dimension == CoverageDimension.CORRECTNESS]
            selected = correctness_unresolved[: self._v3_coverage_max_cells_per_round]

        return selected

    def _build_v3_coverage_summary(self) -> dict[str, Any]:
        """Build bounded v3_coverage summary for the run result."""
        if not self._v3_ledger:
            return {}
        cs = self._v3_ledger.completion_summary()
        return {
            "units": len(self._v3_change_set.units) if self._v3_change_set else 0,
            "cells": cs["total"],
            "mandatory_total": cs["mandatory_total"],
            "mandatory_success": cs["mandatory_success"],
            "abstained": cs["by_status"].get("abstained", 0),
            "failed": cs["by_status"].get("failed", 0),
            "attempts": sum(len(c.assigned_task_ids) for c in self._v3_ledger.cells),
            "selected": len(
                [
                    c
                    for c in self._v3_ledger.cells
                    if any(tid in self._v3_closure_task_ids for tid in c.assigned_task_ids)
                ]
            ),
            "closure_findings": len(self._v3_closure_finding_ids),
        }

    # ── V3 Evidence Verifier ──────────────────────────────────────────────────

    def _init_v3_evidence_verifier(self) -> EvidenceVerifier | None:
        """Lazy-init the EvidenceVerifier with three separate LLM wrappers.

        Prover, refuter, and arbiter are separately attributable, derived from
        calibrator_llm by default. When DB is available, each is wrapped with
        a distinct agent_name for token tracking.
        """
        if self._v3_evidence_verifier is not None:
            return self._v3_evidence_verifier

        if not self._v3_enabled or self._v3_evidence_mode == "off":
            return None

        base = self._calibrator_llm_raw
        if self._db:
            prover_llm = TrackedChatLLM(inner=base, ctx=self._token_ctx, agent_name="evidence_prover")
            refuter_llm = TrackedChatLLM(inner=base, ctx=self._token_ctx, agent_name="evidence_refuter")
            arbiter_llm = TrackedChatLLM(inner=base, ctx=self._token_ctx, agent_name="evidence_arbiter")
        else:
            prover_llm = base
            refuter_llm = base
            arbiter_llm = base

        self._v3_evidence_verifier = EvidenceVerifier(
            prover_model=prover_llm,
            refuter_model=refuter_llm,
            arbiter_model=arbiter_llm,
            max_candidates=self._v3_evidence_max_candidates,
        )
        return self._v3_evidence_verifier

    def _build_evidence_items(
        self,
        finding: Finding,
        state: StateStore,
    ) -> list[EvidenceItem]:
        """Build EvidenceItems from exact RIGHT-side diff lines for a finding.

        Only lines parsed by iter_right_lines where path matches finding.file,
        sha equals state.head_sha, and line equals finding.line are used.
        trigger and violated_contract are left empty to avoid activating the
        deterministic-confirm shortcut (an assertion is not proof).
        """
        patch = (state.file_diffs or {}).get(finding.file)
        if not patch:
            return []

        right_lines = iter_right_lines(patch)
        evidence: list[EvidenceItem] = []
        for line_no, content in right_lines:
            if line_no == finding.line:
                evidence.append(
                    EvidenceItem(
                        kind="supporting",
                        path=finding.file,
                        sha=state.head_sha,
                        line=line_no,
                        snippet=content[:500],
                        trigger="",
                        violated_contract="",
                    )
                )
        return evidence

    async def _run_v3_evidence_verification(
        self,
        candidates: list[Finding],
        state: StateStore,
        run_id: str,
    ) -> list[Finding]:
        """Run v3 evidence verification on candidates.

        Returns the list of candidates that should continue through the
        existing escalation/calibration path (i.e., not resolved by enforce mode).

        In shadow mode: records events/summary but does not mutate any finding.
        In enforce mode: CONFIRMED → confirmed, REJECTED → false_positive,
                         ABSTAIN/failure → continue through existing path.
        Respects v3_evidence_max_candidates cap.
        """
        verifier = self._init_v3_evidence_verifier()
        if verifier is None:
            return candidates

        mode = self._v3_evidence_mode
        if mode == "off":
            return candidates

        cap = self._v3_evidence_max_candidates
        capped = candidates[:cap]
        skipped = candidates[cap:]

        self._events.emit(
            "v3_evidence.started",
            {
                "mode": mode,
                "candidate_count": len(candidates),
                "capped": len(capped),
                "skipped": len(skipped),
            },
        )

        # Build evidence items and diff for each candidate
        evidence_map: dict[str, list[EvidenceItem]] = {}
        for f in capped:
            evidence_map[f.id] = self._build_evidence_items(f, state)

        # Use the full PR diff (existing truncation handled by verifier)
        full_diff = "\n\n".join(f"### {path}\n{patch}" for path, patch in (state.file_diffs or {}).items())

        # A batch-level operational failure must never suppress candidates or
        # fail the surrounding review.
        try:
            capsules = await verifier.verify_batch(
                capped,
                evidence_map,
                full_diff,
                expected_head_sha=state.head_sha,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("V3 evidence batch failed: %s", exc, exc_info=True)
            summary = {
                "mode": mode,
                "attempted": 0,
                "confirmed": 0,
                "rejected": 0,
                "abstained": 0,
                "failed": len(capped),
                "capped": len(skipped),
                "skipped": len(skipped),
            }
            self._events.emit("v3_evidence.failed", {**summary, "error": str(exc)[:200]})
            self._events.emit("v3_evidence.completed", summary)
            self._v3_evidence_summary = summary
            return list(candidates)

        # Track summary
        attempted = len(capsules)
        confirmed = 0
        rejected = 0
        abstained = 0
        failed = 0

        resolved_ids: set[str] = set()

        for finding, capsule in zip(capped, capsules):
            verdict = capsule.final_verdict

            operational_failure = capsule.has_failure or bool(capsule.retry_metadata)
            if operational_failure:
                failed += 1
            elif verdict == EvidenceVerdict.CONFIRMED:
                confirmed += 1
            elif verdict == EvidenceVerdict.REJECTED:
                rejected += 1
            else:
                abstained += 1

            # Emit per-capsule event (bounded, no full prompts/diffs)
            self._events.emit(
                "v3_evidence.capsule",
                {
                    "finding_id": finding.id,
                    "verdict": verdict.value,
                    "confidence": round(capsule.confidence, 3),
                    "has_failure": operational_failure,
                    "evidence_count": len(capsule.evidence),
                },
            )

            if mode == "shadow":
                # Shadow: never mutate, all candidates pass through
                continue
            elif mode == "enforce":
                if verdict == EvidenceVerdict.CONFIRMED and not operational_failure:
                    state.update_finding(
                        finding.id,
                        status="confirmed",
                        verified_by="v3-evidence",
                        verify_reason=capsule.rationale[:500],
                        confidence=capsule.confidence,
                    )
                    resolved_ids.add(finding.id)
                    # Do NOT add to passthrough — removed from escalation/calibration
                elif verdict == EvidenceVerdict.REJECTED and not operational_failure:
                    state.update_finding(
                        finding.id,
                        status="false_positive",
                        verified_by="v3-evidence",
                        verify_reason=capsule.rationale[:500],
                        confidence=max(0.0, 1.0 - capsule.confidence),
                    )
                    resolved_ids.add(finding.id)
                    # Do NOT add to passthrough — removed from escalation/calibration
                else:
                    # ABSTAIN or failure → continue through existing path
                    pass

        # Emit summary event
        summary = {
            "mode": mode,
            "attempted": attempted,
            "confirmed": confirmed,
            "rejected": rejected,
            "abstained": abstained,
            "failed": failed,
            "capped": len(skipped),
            "skipped": len(skipped),
        }
        self._events.emit("v3_evidence.completed", summary)

        # Store summary for run result
        self._v3_evidence_summary = summary

        return [finding for finding in candidates if finding.id not in resolved_ids]

    @staticmethod
    def _reviewer_dimensions(reviewer: str) -> list[str]:
        """Map a reviewer name to the coverage dimensions it addresses."""
        _reviewer_dim_map = {
            "security_reviewer": ["security"],
            "testing_reviewer": ["testing"],
            "localization_reviewer": ["localization"],
            "performance_reviewer": ["performance"],
            "correctness_reviewer": ["correctness"],
        }
        return _reviewer_dim_map.get(reviewer, ["correctness"])

    @staticmethod
    def _dimension_reviewer(dimension: str) -> str:
        """Map a coverage dimension to its reviewer."""
        _dim_reviewer_map = {
            "security": "security_reviewer",
            "testing": "testing_reviewer",
            "localization": "localization_reviewer",
            "performance": "performance_reviewer",
        }
        return _dim_reviewer_map.get(dimension, "correctness_reviewer")

    @staticmethod
    def _finding_matches_unit(finding: Finding, unit: SemanticUnit | None) -> bool:
        """Check if a finding is unit-specific (same file, line within range)."""
        if unit is None:
            return False
        if finding.file != unit.path:
            return False
        if finding.line <= 0:
            return False
        if unit.start_line > 0 and unit.end_line > 0:
            return unit.start_line <= finding.line <= unit.end_line
        return True

    def _find_unit_by_id(self, unit_id: str) -> SemanticUnit | None:
        """Look up a SemanticUnit by ID from the current change set."""
        if not self._v3_change_set:
            return None
        for u in self._v3_change_set.units:
            if u.id == unit_id:
                return u
        return None

    @staticmethod
    def _build_review_focus(
        *,
        path: str,
        symbol: str,
        start_line: int,
        end_line: int,
        dimension: str,
        risk_reasons: list[str],
        is_retry: bool,
    ) -> str:
        """Build concise review_focus text for a targeted closure task."""
        line_range = f"{start_line}-{end_line}" if start_line > 0 and end_line > 0 else "unknown"
        focus = (
            f"Targeted coverage closure. Focus on: "
            f"path={path}, symbol={symbol or '<file>'}, "
            f"lines={line_range}, dimension={dimension}."
        )
        if risk_reasons:
            focus += f" Risk reasons: {', '.join(risk_reasons)}."
        if is_retry:
            focus += (
                " This is an adversarial retry — the first attempt found no issue. Look harder for subtle problems."
            )
        return focus

    def _create_reviewer(
        self,
        name: str,
        *,
        model_agent_name: str | None = None,
        force_agentic: bool | None = None,
    ) -> BaseReviewer | None:
        """D6+W2: 按 reviewer 名字解析 LLM + agentic 标志。"""
        cls = REVIEWER_MAP.get(name) or self._extra_reviewers.get(name)
        if cls:
            # D6: 如果有 ModelRouter，按 agent 名字取对应 LLM
            route_name = model_agent_name or name
            if self._model_router:
                llm = self._model_router.get_llm(route_name)
                if self._db:
                    llm = TrackedChatLLM(inner=llm, ctx=self._token_ctx, agent_name=route_name)
            else:
                llm = self._reviewer_llm
            # W2/#1: 有显式 allowlist 时按成员判定，否则用默认（默认全部 reviewer 走工具循环）
            agentic = name in self._agentic_reviewers if self._agentic_reviewers else self._agentic_default
            if force_agentic is not None:
                agentic = force_agentic
            # Construct with the base (llm, registry, gateway) signature so custom plugins
            # (which only accept those three) work too; set per-run flags as attributes.
            reviewer = cls(llm, self._registry, self._gateway)
            reviewer._agentic = agentic
            reviewer._events = self._events
            return reviewer
        return None

    @staticmethod
    def _has_agentic_context(task: ReviewTask, state: StateStore) -> bool:
        """Use the costly tool loop only when retrieval found evidence to inspect.

        Non-security reviewers retain their configured behavior. Security is the
        production allowlisted reviewer and needs cross-file/live-reference or
        historical graph evidence before an agentic investigation is useful.
        """
        if task.reviewer != "security_reviewer":
            return True
        manifest = state.impact_manifest or {}
        task_files = set(task.files or state.files_changed)
        sensitive_symbols = {
            (str(signal.get("file", "")), str(signal.get("symbol", "")))
            for signal in manifest.get("risk_signals", [])
            if signal.get("type") == "security-sensitive-symbol"
        }
        for signal in manifest.get("risk_signals", []):
            if signal.get("type") != "blast-radius":
                continue
            key = (str(signal.get("file", "")), str(signal.get("symbol", "")))
            if (
                int(signal.get("reference_count", 0)) >= 2
                and key in sensitive_symbols
                and (not key[0] or key[0] in task_files)
            ):
                return True
        for row in manifest.get("historical_graph", []):
            paths = {row.get("file"), row.get("source_file"), row.get("target_file")}
            if task_files.intersection(path for path in paths if path):
                return True
        return False

    def _attach_skill(
        self, reviewer: BaseReviewer, target_language: str | None = None, target_framework: str | None = None
    ) -> None:
        """渐进式 Skill 加载（Level 2）：按语言/框架选出最佳 SKILL.md 注入 reviewer。

        对于通用 skill（如 security_rules），如果知道目标语言，还会将对应的
        语言特定参考文件（如 rust_patterns.md）内联注入，确保 single-shot 模式
        下也能获得语言特定的漏洞模式。
        """
        # Config-type agents carry inline instructions as their skill body — don't clobber.
        if getattr(reviewer, "_skill_body", ""):
            return
        metas = self._skills_by_type.get(reviewer.reviewer_type, [])
        if not metas:
            return
        meta = self._resolve_skill(metas, target_language, target_framework)
        if not meta:
            return
        try:
            content = self._skill_loader.load(meta.name)
            body = content.body

            # For universal skills with language-specific references, inline the
            # matching reference so single-shot reviewers see language patterns.
            if target_language and meta.references:
                lang_ref_map = {
                    "python": "patterns.md",
                    "go": "go_patterns.md",
                    "java": "java_patterns.md",
                    "rust": "rust_patterns.md",
                    "ruby": "patterns.md",
                    "javascript": "frontend_patterns.md",
                    "typescript": "frontend_patterns.md",
                }
                ref_name = lang_ref_map.get(target_language)
                if ref_name and ref_name in meta.references:
                    try:
                        ref_body = self._skill_loader.read_ref(meta.name, ref_name)
                        body += (
                            f"\n\n## 语言特定安全规则 ({target_language})\n\n"
                            f"以下是针对 {target_language} 的详细安全检测模式：\n\n{ref_body}"
                        )
                        logger.debug(f"Inlined {ref_name} for {reviewer.name} ({target_language})")
                    except Exception:
                        pass

            reviewer._skill_body = body
            reviewer._skill_name = meta.name
            reviewer._skill_refs = list(meta.references or [])
            reviewer._skill_loader = self._skill_loader
        except Exception as e:
            logger.warning(f"Skill load failed for {meta.name}: {e}")

    async def _rehydrate(self, state: StateStore, run_id: str) -> None:
        """Resume: load a prior run's persisted findings + completed reviewers into state,
        so the re-planning loop skips finished reviewers and keeps their findings."""
        for fd in await self._db.get_findings(run_id=run_id, limit=10000):
            try:
                state.add_finding(
                    Finding(
                        id=fd["id"],
                        file=fd["file"],
                        line=fd["line"],
                        severity=fd["severity"],
                        category=fd["category"],
                        message=fd["message"],
                        suggestion=fd.get("suggestion", ""),
                        confidence=fd["confidence"],
                        reviewer=fd.get("reviewer", ""),
                        status=fd.get("status", "candidate"),
                        verified_by=fd.get("verified_by", ""),
                    )
                )
            except Exception as e:
                logger.warning(f"resume: skip finding {fd.get('id')}: {e}")
        for m in await self._db.get_metrics(run_id=run_id):
            if m.get("status") == "completed":
                try:
                    state.add_task(
                        ReviewTask(reviewer=m["reviewer_name"], files=state.files_changed, status="completed")
                    )
                except Exception:
                    pass

    async def _post_comments(self, findings: list[Finding], state: StateStore) -> CommentDeliveryResult:
        """Post confirmed findings in serialized, bounded GitHub reviews.

        Coordinates are checked against the visible RIGHT side of the PR patch
        before making a write request. GitHub validation failures are then
        isolated by splitting the affected batch, so one rejected coordinate
        cannot discard otherwise valid comments. Permanent coordinate failures
        are retired as false positives; operational failures remain confirmed
        and make the containing run retryable.
        """

        if not findings:
            return CommentDeliveryResult()

        findings_by_id: dict[str, Finding] = {}
        for finding in findings:
            if finding.id not in findings_by_id and finding.status != "reported":
                findings_by_id[finding.id] = finding
        if not findings_by_id:
            return CommentDeliveryResult()

        reported_ids: set[str] = set()
        transient_ids: set[str] = set()
        permanent: dict[str, tuple[str, str]] = {}
        errors: list[str] = []

        def add_error(message: str) -> None:
            message = message[:1000]
            if message not in errors:
                errors.append(message)

        def mark_reported(finding: Finding) -> None:
            reported_ids.add(finding.id)
            transient_ids.discard(finding.id)
            permanent.pop(finding.id, None)

        def mark_transient(batch: list[tuple[Finding, dict[str, Any]]], message: str) -> None:
            add_error(message)
            for finding, _comment in batch:
                if finding.id not in reported_ids and finding.id not in permanent:
                    transient_ids.add(finding.id)

        def mark_permanent(finding: Finding, reason: str, verified_by: str) -> None:
            if finding.id in reported_ids:
                return
            transient_ids.discard(finding.id)
            permanent[finding.id] = (verified_by, reason[:500])

        async def finalize() -> CommentDeliveryResult:
            for finding_id in reported_ids:
                finding = findings_by_id[finding_id]
                state.update_finding(finding_id, status="reported")
                if self._db:
                    try:
                        await self._db.update_finding_status(finding_id, "reported", finding.verified_by)
                    except Exception as exc:
                        logger.error(
                            "Comment delivered but DB status update failed for %s: %s",
                            finding_id,
                            exc,
                        )

            for finding_id, (verified_by, reason) in permanent.items():
                state.update_finding(
                    finding_id,
                    status="false_positive",
                    verified_by=verified_by,
                    verify_reason=reason,
                )
                if self._db:
                    try:
                        await self._db.update_finding_status(finding_id, "false_positive", verified_by)
                    except Exception as exc:
                        logger.error(
                            "Permanent comment rejection recorded in memory but DB update failed for %s: %s",
                            finding_id,
                            exc,
                        )

            return CommentDeliveryResult(
                reported=len(reported_ids),
                permanent_rejections=len(permanent),
                transient_failures=len(transient_ids),
                errors=tuple(errors),
            )

        try:
            await self._gateway.ensure_file_diffs(state)
        except Exception as exc:
            message = f"Unable to load PR patches for comment prevalidation: {exc}"
            logger.error(message)
            mark_transient(
                [(finding, {}) for finding in findings_by_id.values()],
                message,
            )
            return await finalize()

        right_lines_by_file: dict[str, set[int]] = {}
        patch_errors: dict[str, str] = {}
        unique_files = {finding.file for finding in findings if finding.line > 0}
        for file_path in unique_files:
            patch = (state.file_diffs or {}).get(file_path)
            if patch is None:
                try:
                    patch = await self._gateway.invoke(
                        "read_diff",
                        {"file_path": file_path},
                        state,
                        agent_name="orchestrator",
                    )
                except Exception as exc:
                    patch_errors[file_path] = str(exc)
                    logger.warning(
                        "Comment prevalidation could not read %s: %s",
                        file_path,
                        exc,
                    )
                    continue
            if not (patch or "").strip():
                patch_errors[file_path] = "GitHub returned an empty patch"
                continue
            mapped_right_lines = iter_right_lines(patch)
            if not mapped_right_lines and "@@" not in patch:
                patch_errors[file_path] = "GitHub returned an unanchored or truncated patch"
                continue
            right_lines_by_file[file_path] = {line for line, _content in mapped_right_lines}

        pending: list[tuple[Finding, dict[str, Any]]] = []
        for finding in findings_by_id.values():
            if finding.line <= 0:
                logger.warning(
                    "Comment prevalidation rejected %s: invalid RIGHT line %d",
                    finding.id,
                    finding.line,
                )
                mark_permanent(
                    finding,
                    f"Invalid GitHub RIGHT-side line coordinate: {finding.line}",
                    "comment-coordinate-validator",
                )
                continue
            if finding.file in patch_errors:
                message = (
                    f"Unable to load patch for {finding.file} while delivering "
                    f"{finding.id}: {patch_errors[finding.file]}"
                )
                mark_transient([(finding, {})], message)
                continue
            if finding.line not in right_lines_by_file.get(finding.file, set()):
                logger.warning(
                    "Comment prevalidation rejected %s: %s:%d is not a visible RIGHT-side diff line",
                    finding.id,
                    finding.file,
                    finding.line,
                )
                mark_permanent(
                    finding,
                    f"{finding.file}:{finding.line} is not a visible RIGHT-side diff coordinate",
                    "comment-coordinate-validator",
                )
                continue
            pending.append(
                (
                    finding,
                    {
                        "file_path": finding.file,
                        "line": finding.line,
                        "body": self._format_comment(finding),
                    },
                )
            )

        async def deliver_batch(batch: list[tuple[Finding, dict[str, Any]]]) -> None:
            if not batch:
                return
            try:
                result = await self._gateway.invoke(
                    "post_review",
                    {"comments": [comment for _finding, comment in batch]},
                    state,
                    agent_name="orchestrator",
                )
            except Exception as exc:
                kind = str(getattr(exc, "kind", "unknown"))
                status_code = int(getattr(exc, "status_code", 0) or 0)
                response_body = str(getattr(exc, "response_body", ""))
                retryable = bool(getattr(exc, "retryable", False))
                error_text = str(exc)
                lowered = error_text.lower()
                if status_code == 0 and "422" in lowered:
                    status_code = 422
                if kind == "unknown":
                    if "spam" in lowered or "secondary rate limit" in lowered:
                        kind = "spam"
                        retryable = True
                    elif "rate limit" in lowered:
                        kind = "rate_limit"
                        retryable = True
                    elif status_code == 422:
                        kind = "validation"
                    elif any(marker in lowered for marker in ("network", "timeout", "connection")):
                        kind = "network"
                logger.error(
                    "GitHub review delivery failed (status=%d, kind=%s, comments=%d): %s; body=%s",
                    status_code,
                    kind,
                    len(batch),
                    exc,
                    response_body[:1000],
                )
                # A batch-level coordinate validation response does not identify
                # the offending entry. Bisect serially to salvage valid comments.
                is_validation = kind == "validation" or (
                    status_code == 422 and not retryable and kind not in {"spam", "rate_limit"}
                )
                if is_validation:
                    if len(batch) > 1:
                        midpoint = len(batch) // 2
                        await deliver_batch(batch[:midpoint])
                        await deliver_batch(batch[midpoint:])
                    else:
                        finding = batch[0][0]
                        mark_permanent(
                            finding,
                            f"GitHub permanently rejected the inline coordinate: {response_body or error_text}",
                            "github-comment-validation",
                        )
                else:
                    mark_transient(
                        batch,
                        f"GitHub comment delivery transient failure (status={status_code}, kind={kind}): {error_text}",
                    )
                return

            if not isinstance(result, dict):
                logger.error("GitHub review delivery returned an invalid result: %r", result)
                mark_transient(
                    batch,
                    f"GitHub review delivery returned an invalid result: {result!r}",
                )
                return

            raw_indexes = result.get("delivered_indexes", [])
            if not isinstance(raw_indexes, (list, tuple, set)):
                raw_indexes = []
            delivered_indexes = {
                index
                for index in raw_indexes
                if isinstance(index, int) and not isinstance(index, bool) and 0 <= index < len(batch)
            }
            for index in sorted(delivered_indexes):
                mark_reported(batch[index][0])

            failed_indexes: set[int] = set()
            raw_failures = result.get("failures", [])
            if not isinstance(raw_failures, list):
                raw_failures = []
            for failure in raw_failures:
                if not isinstance(failure, dict):
                    continue
                index = failure.get("index")
                if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < len(batch):
                    add_error(f"GitHub comment delivery returned an invalid failure index: {index!r}")
                    continue
                failed_indexes.add(index)
                finding = batch[index][0]
                finding_id = finding.id
                kind = str(failure.get("kind", "unknown"))
                status_code = int(failure.get("status_code", 0) or 0)
                retryable = bool(failure.get("retryable", False))
                error_text = str(failure.get("error", "unknown error"))
                response_body = str(failure.get("response_body", ""))
                logger.error(
                    "GitHub comment delivery failed for %s (status=%s, kind=%s): %s; body=%s",
                    finding_id,
                    status_code,
                    kind,
                    error_text,
                    response_body[:1000],
                )
                is_validation = kind == "validation" or (
                    status_code == 422 and not retryable and kind not in {"spam", "rate_limit"}
                )
                if is_validation:
                    mark_permanent(
                        finding,
                        f"GitHub permanently rejected the inline coordinate: {response_body or error_text}",
                        "github-comment-validation",
                    )
                else:
                    mark_transient(
                        [batch[index]],
                        f"GitHub comment delivery transient failure (status={status_code}, kind={kind}): {error_text}",
                    )

            unresolved = set(range(len(batch))) - delivered_indexes - failed_indexes
            if unresolved:
                unresolved_batch = [batch[index] for index in sorted(unresolved)]
                mark_transient(
                    unresolved_batch,
                    f"GitHub review result omitted delivery outcomes for {len(unresolved_batch)} comments",
                )

        for offset in range(0, len(pending), MAX_REVIEW_COMMENTS_PER_REQUEST):
            await deliver_batch(pending[offset : offset + MAX_REVIEW_COMMENTS_PER_REQUEST])

        return await finalize()

    @staticmethod
    def _format_comment(finding: Finding) -> str:
        severity_emoji = {"error": "🔴", "warning": "🟡", "info": "🔵"}.get(finding.severity, "⚪")
        return (
            f"{severity_emoji} **[{finding.category}]** (置信度: {finding.confidence:.0%})\n\n"
            f"{finding.message}\n\n"
            f"**建议:** {finding.suggestion}\n\n"
            f"<sub>ReviewForge • {finding.reviewer}</sub>"
        )
