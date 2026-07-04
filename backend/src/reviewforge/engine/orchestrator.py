"""Orchestrator — the main review loop.

Coordinates Planner → Reviewers → Dynamic Calibration → Commenter.
Persists all results to the database for dashboard consumption.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from pathlib import Path
from typing import Any

from langchain_openai import ChatOpenAI

from reviewforge.core.database import Database
from reviewforge.core.events import EventBus
from reviewforge.core.loop_detector import LoopDetector
from reviewforge.core.scheduler import Scheduler
from reviewforge.core.specs import SpecRegistry
from reviewforge.core.state import Finding, Note, ReviewTask, StateStore
from reviewforge.engine.calibrator import DynamicCalibrator
from reviewforge.engine.cross_pr_analyzer import CrossPRAnalyzer
from reviewforge.engine.escalation import EscalationReviewer
from reviewforge.engine.model_router import ModelRouter
from reviewforge.engine.planner import Planner
from reviewforge.engine.reviewers import REVIEWER_MAP, BaseReviewer
from reviewforge.engine.token_tracker import RunContext, TrackedChatLLM
from reviewforge.engine.verifier import Verifier
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
        model_router: ModelRouter | None = None,
        agentic_reviewers: list[str] | None = None,
        agentic_default: bool = False,
        escalation_enabled: bool = True,
        escalation_confidence_min: float = 0.4,
        escalation_confidence_max: float = 0.7,
        escalation_max_steps: int = 3,
        escalation_max_tokens: int = 5000,
        skills_dir: str | Path | None = None,
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

        # Resume (可恢复): if a prior run for this exact (repo, pr, head_sha) didn't
        # complete, reuse its run_id and rehydrate findings + completed reviewers from
        # the DB so already-done work is skipped instead of redone.
        resumed = await self._db.get_resumable_run(state.repo, state.pr_number, state.head_sha) if self._db else None
        if resumed:
            run_id = resumed["run_id"]
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

        try:
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
                    # 按 task 文件检测语言/框架，注入匹配的 skill
                    lang = self._detect_task_language(task)
                    fw = self._detect_task_framework(task)
                    self._attach_skill(reviewer, lang, fw)
                    reviewer._target_language = lang or ""
                    reviewer._target_framework = fw or ""
                    findings = await reviewer.execute(task, state)
                    for f in findings:
                        state.add_finding(f)
                    state.update_task(task.id, status="completed")
                    self._events.emit(
                        "reviewer.completed",
                        {"reviewer": task.reviewer, "findings_count": len(findings)},
                    )
                    if self._db:
                        await self._db.insert_metric(
                            run_id,
                            task.reviewer,
                            findings_count=len(findings),
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
                proposed = await self._planner.plan(state, notes=notes)
                for task in proposed:
                    state.add_task(task)
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

            # Phase 3: Verifier (#5, pure-logic de-dupe/merge) → Dynamic Calibration.
            raw_candidates = state.list_findings(status="candidate")
            candidates, dropped_ids = self._verifier.verify(raw_candidates)
            for fid in dropped_ids:
                state.update_finding(
                    fid, status="false_positive", verified_by="verifier", verify_reason="重复/低置信，已合并"
                )
            if dropped_ids:
                self._events.emit("verifier.completed", {"kept": len(candidates), "merged": len(dropped_ids)})

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
                        if EscalationReviewer.should_escalate(
                            f, self._escalation_confidence_min, self._escalation_confidence_max
                        )
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
                calibrated = await self._calibrator.calibrate(calib_set, state.diff_summary)
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
                        self._events.emit(
                            "cross_pr.completed",
                            {
                                "cross_pr_findings": len(cross_findings),
                            },
                        )
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

        except asyncio.CancelledError:
            if self._db:
                await self._db.fail_run(run_id, "review task cancelled")
            raise
        except Exception as e:
            if self._db:
                await self._db.fail_run(run_id, str(e))
            raise

    def _create_reviewer(self, name: str) -> BaseReviewer | None:
        """D6+W2: 按 reviewer 名字解析 LLM + agentic 标志。"""
        cls = REVIEWER_MAP.get(name) or self._extra_reviewers.get(name)
        if cls:
            # D6: 如果有 ModelRouter，按 agent 名字取对应 LLM
            if self._model_router:
                llm = self._model_router.get_llm(name)
                if self._db:
                    llm = TrackedChatLLM(inner=llm, ctx=self._token_ctx, agent_name=name)
            else:
                llm = self._reviewer_llm
            # W2/#1: 有显式 allowlist 时按成员判定，否则用默认（默认全部 reviewer 走工具循环）
            agentic = name in self._agentic_reviewers if self._agentic_reviewers else self._agentic_default
            # Construct with the base (llm, registry, gateway) signature so custom plugins
            # (which only accept those three) work too; set per-run flags as attributes.
            reviewer = cls(llm, self._registry, self._gateway)
            reviewer._agentic = agentic
            reviewer._events = self._events
            return reviewer
        return None

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

    async def _post_comments(self, findings: list[Finding], state: StateStore) -> int:
        """Post review comments via the tool gateway."""
        count = 0
        for finding in findings:
            # Validate line number — GitHub rejects line=0 or invalid lines
            if finding.line <= 0:
                logger.warning(f"Skipping comment for {finding.id}: invalid line {finding.line}")
                continue
            try:
                await self._gateway.invoke(
                    "post_comment",
                    {
                        "file_path": finding.file,
                        "line": finding.line,
                        "body": self._format_comment(finding),
                        "severity": finding.severity,
                    },
                    state,
                    agent_name="orchestrator",
                )
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
