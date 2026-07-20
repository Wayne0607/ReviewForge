"""Reviewer Agents — stateless per-task executors.

Each reviewer focuses on one dimension (security/performance/style).
Supports two execution modes:
- Single-shot: one LLM call, parse findings (default, all reviewers)
- Agentic: model-driven tool loop with real-time tool calling (opt-in per reviewer)
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import StructuredTool
from langchain_openai import ChatOpenAI

from reviewforge.core.specs import SpecRegistry
from reviewforge.core.state import Finding, ReviewTask, StateStore
from reviewforge.engine.budget import MAX_TOOL_CALLS_PER_FILE, MAX_TOOL_OUTPUT_CHARS, TokenBudget
from reviewforge.engine.detectors import detect_dependency_findings, detect_security_findings
from reviewforge.engine.prompt import build_reviewer_prompt
from reviewforge.engine.security_categories import is_security_category, normalize_category
from reviewforge.tools.gateway import ToolGateway

logger = logging.getLogger(__name__)


def build_reviewer_tools(
    gateway: ToolGateway,
    state: StateStore,
    agent_name: str,
    skill_loader: Any = None,
    skill_name: str = "",
    skill_refs: list[str] | None = None,
) -> list[StructuredTool]:
    """Build read-only LangChain tools for agentic reviewers.

    Extracted from BaseReviewer so EscalationReviewer can reuse the same tool set.
    """
    gw = gateway

    async def read_file(file_path: str) -> str:
        """Read full file content at PR head commit."""
        return await gw.invoke("read_file", {"file_path": file_path}, state, agent_name=agent_name) or ""

    async def search_code(pattern: str, file_glob: str = "") -> str:
        """Search code in repo by pattern."""
        return (
            await gw.invoke("search_code", {"pattern": pattern, "file_glob": file_glob}, state, agent_name=agent_name)
            or ""
        )

    async def read_diff(file_path: str) -> str:
        """Read diff for a specific file in this PR."""
        return await gw.invoke("read_diff", {"file_path": file_path}, state, agent_name=agent_name) or ""

    async def get_change_context(file_path: str = "", symbol: str = "") -> str:
        """Read changed symbols, repository references and historical graph context."""
        params = {"file_path": file_path, "symbol": symbol}
        return await gw.invoke("get_change_context", params, state, agent_name=agent_name) or ""

    tools = [
        StructuredTool.from_function(
            coroutine=read_file,
            name="read_file",
            description="读取文件在 PR head 版本的完整内容；当 diff 上下文不足以判断时使用",
        ),
        StructuredTool.from_function(
            coroutine=search_code,
            name="search_code",
            description="在仓库搜索代码，定位调用方/定义，判断输入是否在别处已被校验",
        ),
        StructuredTool.from_function(coroutine=read_diff, name="read_diff", description="读取某文件在本 PR 的 diff"),
        StructuredTool.from_function(
            coroutine=get_change_context,
            name="get_change_context",
            description="读取影响清单：变更符号、调用/导入、仓库引用、候选测试和历史图谱边",
        ),
    ]

    # Level-3 progressive disclosure: pull deeper Skill reference files on demand.
    if skill_loader and skill_name and (skill_refs or []):
        loader = skill_loader
        sname = skill_name

        async def read_reference(ref_path: str) -> str:
            """读取本审查维度 Skill 的深层参考文件（references/ 下）。"""
            try:
                return loader.read_ref(sname, ref_path)
            except Exception as e:
                return f"reference read failed: {e}"

        tools.append(
            StructuredTool.from_function(
                coroutine=read_reference,
                name="read_reference",
                description="读取本维度 Skill 的深层规则参考文件（Level 3，按需）",
            )
        )

    return tools


# Per-reviewer-type cap on findings, to cut low-value nitpick noise. Keep the top-N
# by severity then confidence. Security/perf are allowed more; doc/style capped low.
_MAX_FINDINGS_BY_TYPE = {
    "security": 15,
    "performance": 10,
    "dependency": 10,
    "accessibility": 6,
    "testing": 6,
    "documentation": 4,
    "style": 5,
    "correctness": 6,
    "localization": 6,
}
_SEVERITY_RANK = {"error": 3, "warning": 2, "info": 1}


class ReviewerOutputError(RuntimeError):
    """Raised when a reviewer cannot return recoverable findings JSON."""


class BaseReviewer:
    """Base class for all reviewers.

    Supports single-shot and agentic execution modes.
    """

    def __init__(
        self,
        name: str,
        reviewer_type: str,
        llm: ChatOpenAI,
        registry: SpecRegistry,
        gateway: ToolGateway,
        max_steps: int = 8,
        agentic: bool = False,
        max_tokens: int = 20000,
        event_bus: Any = None,
    ) -> None:
        self.name = name
        self.reviewer_type = reviewer_type
        self._llm = llm
        self._registry = registry
        self._gateway = gateway
        self._max_steps = max_steps
        self._agentic = agentic
        self._max_tokens = max_tokens
        self._events = event_bus
        # Progressive skill loading — set post-construction by the orchestrator
        self._skill_body: str = ""
        self._skill_name: str = ""
        self._skill_refs: list[str] = []
        self._skill_loader: Any = None

    async def execute(self, task: ReviewTask, state: StateStore) -> list[Finding]:
        """Dispatch to single-shot or agentic execution."""
        if self._agentic:
            return await self.execute_agentic(task, state)
        return await self.execute_singleshot(task, state)

    async def execute_singleshot(self, task: ReviewTask, state: StateStore) -> list[Finding]:
        """Single prompt → parse findings (original path)."""
        files = task.files or state.files_changed
        diffs = {}
        for f in files:
            diffs[f] = await self._gateway.invoke("read_diff", {"file_path": f}, state, agent_name=self.name) or ""

        ctx = {
            "registry": self._registry,
            "reviewer_type": self.reviewer_type,
            "agent_name": self.name,
            "files_to_review": files,
            "diffs": diffs,
            "skill_body": self._skill_body,
            "skill_refs": self._skill_refs,
            "target_language": getattr(self, "_target_language", ""),
            "target_framework": getattr(self, "_target_framework", ""),
            "impact_manifest": state.impact_manifest,
        }
        messages = build_reviewer_prompt(ctx)

        chat_messages = [
            SystemMessage(content=messages[0]["content"]),
            HumanMessage(content=messages[1]["content"]),
        ]

        response = await self._llm.ainvoke(chat_messages)
        findings, valid = self._parse_findings_result(response.content)
        if not valid:
            raise ReviewerOutputError(f"{self.name}: invalid JSON output")
        for f in findings:
            f.reviewer = self.name
        return self._merge_detector_findings(findings, diffs)

    async def execute_agentic(self, task: ReviewTask, state: StateStore) -> list[Finding]:
        """Agentic tool loop — model drives tool calls in real time."""
        files = task.files or state.files_changed
        diffs = {}
        for f in files:
            diffs[f] = await self._gateway.invoke("read_diff", {"file_path": f}, state, agent_name=self.name) or ""

        ctx = {
            "registry": self._registry,
            "reviewer_type": self.reviewer_type,
            "agent_name": self.name,
            "files_to_review": files,
            "diffs": diffs,
            "tools_enabled": True,
            "skill_body": self._skill_body,
            "skill_refs": self._skill_refs,
            "target_language": getattr(self, "_target_language", ""),
            "target_framework": getattr(self, "_target_framework", ""),
            "impact_manifest": state.impact_manifest,
        }
        messages = build_reviewer_prompt(ctx)
        chat = [
            SystemMessage(content=messages[0]["content"]),
            HumanMessage(content=messages[1]["content"]),
        ]

        tools = self._build_tools(state)
        tool_map = {t.name: t for t in tools}
        llm = self._llm.bind_tools(tools)

        budget = TokenBudget(self._max_tokens)
        call_counter: dict[str, int] = {}

        for step in range(self._max_steps):
            if budget.exhausted():
                logger.warning(f"{self.name}: token budget exhausted at step {step}")
                break

            resp = await llm.ainvoke(chat)
            chat.append(resp)
            budget.add(resp)

            # Observability: emit step token usage
            self._emit_step_event(state, step, resp)

            tool_calls = getattr(resp, "tool_calls", None) or []
            if not tool_calls:
                findings, valid = self._parse_findings_result(resp.content)
                if valid:
                    for fd in findings:
                        fd.reviewer = self.name
                    return self._merge_detector_findings(findings, diffs)
                # No tool calls and no findings: nudge
                chat.append(HumanMessage(content="请基于已收集的信息，现在只输出 findings JSON（无问题则空数组）。"))
                continue

            for tc in tool_calls:
                name = tc.get("name", "")
                args = tc.get("args", {})
                tc_id = tc.get("id", "")

                # Emit tool event
                if self._events:
                    self._events.emit(
                        "tool.invoked",
                        {
                            "reviewer": self.name,
                            "tool": name,
                            "args": args,
                            "step": step,
                        },
                    )

                # Repeat-call guard
                key = f"{name}:{sorted(args.items()) if isinstance(args, dict) else args}"
                call_counter[key] = call_counter.get(key, 0) + 1
                if call_counter[key] > MAX_TOOL_CALLS_PER_FILE:
                    result = "（已多次调用相同参数，请停止重复调用并基于现有信息给出结论）"
                else:
                    tool = tool_map.get(name)
                    try:
                        result = await tool.ainvoke(args) if tool else f"Unknown tool: {name}"
                    except Exception as e:
                        result = f"Tool error: {e}"

                result = str(result)[:MAX_TOOL_OUTPUT_CHARS]
                chat.append(ToolMessage(content=result, tool_call_id=tc_id))

        # Budget/steps exhausted: force final findings
        chat.append(HumanMessage(content="已达步数/预算上限。请立刻只输出 findings JSON（可为空数组）。"))
        try:
            final = await llm.ainvoke(chat)
            budget.add(final)
            findings, valid = self._parse_findings_result(final.content)
            if not valid:
                raise ReviewerOutputError(f"{self.name}: invalid JSON output after force-finish")
        except ReviewerOutputError:
            raise
        except Exception as e:
            logger.error(f"{self.name}: force-finish failed: {e}")
            findings = []

        for fd in findings:
            fd.reviewer = self.name
        return self._merge_detector_findings(findings, diffs)

    def _build_tools(self, state: StateStore) -> list[StructuredTool]:
        """Wrap gateway read-only tools as LangChain tools (no post_comment)."""
        return build_reviewer_tools(
            self._gateway,
            state,
            self.name,
            self._skill_loader,
            self._skill_name,
            self._skill_refs,
        )

    def _emit_step_event(self, state: StateStore, step: int, resp: Any) -> None:
        """Emit per-step token usage event."""
        if not self._events:
            return
        usage = getattr(resp, "usage_metadata", None) or {}
        tokens = usage.get("total_tokens", 0)
        if not tokens:
            # Fallback: estimate from content length
            content = getattr(resp, "content", "")
            tokens = len(str(content)) // 4
        self._events.emit(
            "reviewer.step",
            {
                "reviewer": self.name,
                "step": step,
                "tokens": tokens,
            },
        )

    @staticmethod
    def _extract_json(content: str) -> Any:
        """Extract JSON from LLM output, handling extra text around it."""
        content = content.strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[1] if "\n" in content else content[3:]
        if content.endswith("```"):
            content = content[:-3]
        content = content.strip()

        try:
            return json.loads(content)
        except json.JSONDecodeError:
            pass

        match = re.search(r"(\{.*\}|\[.*\])", content, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass

        start = content.find("{")
        end = content.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(content[start : end + 1])
            except json.JSONDecodeError:
                pass

        return None

    @staticmethod
    def _recover_truncated_findings(content: str) -> dict[str, list[dict[str, Any]]] | None:
        """Recover complete objects from a findings array cut off at the model limit.

        Recovery is deliberately narrow: only complete JSON objects already
        present inside a top-level list or a ``findings`` array are accepted.
        The unfinished tail is discarded, and prose JSON snippets are ignored.
        """

        stripped = str(content or "").strip()
        if stripped.startswith("```"):
            stripped = stripped.split("\n", 1)[1] if "\n" in stripped else stripped[3:]
        stripped = stripped.removesuffix("```").strip()

        findings_match = re.search(r'"findings"\s*:\s*\[', stripped)
        if findings_match is not None:
            index = findings_match.end()
        elif stripped.startswith("["):
            index = 1
        else:
            return None

        decoder = json.JSONDecoder()
        recovered: list[dict[str, Any]] = []
        while index < len(stripped):
            while index < len(stripped) and (stripped[index].isspace() or stripped[index] == ","):
                index += 1
            if index >= len(stripped) or stripped[index] == "]":
                break
            try:
                item, end = decoder.raw_decode(stripped, index)
            except json.JSONDecodeError:
                break
            if not isinstance(item, dict):
                return None
            recovered.append(item)
            index = end
        return {"findings": recovered} if recovered else None

    def _parse_findings_result(self, content: str) -> tuple[list[Finding], bool]:
        """Parse findings and distinguish valid emptiness from malformed output."""

        data = self._extract_json(content)
        recovered = False
        if data is None:
            data = self._recover_truncated_findings(content)
            recovered = data is not None
        if data is None:
            logger.warning(f"{self.name}: invalid JSON output")
            return [], False

        if recovered:
            logger.warning(f"{self.name}: recovered complete findings from truncated JSON output")
            if self._events:
                self._events.emit(
                    "reviewer.output_recovered",
                    {"reviewer": self.name, "findings_count": len(data.get("findings", []))},
                )

        if isinstance(data, list):
            raw_findings = data
        elif isinstance(data, dict):
            raw_findings = data.get("findings", [])
        else:
            logger.warning(f"{self.name}: JSON output was {type(data).__name__}, expected object or list")
            return [], False
        if not isinstance(raw_findings, list):
            logger.warning(f"{self.name}: findings field was {type(raw_findings).__name__}, expected list")
            return [], False

        findings = []
        for item in raw_findings:
            if not isinstance(item, dict):
                continue
            category = normalize_category(str(item.get("category", "")))
            if self.reviewer_type not in {"security", "dependency"} and is_security_category(category):
                continue
            try:
                findings.append(
                    Finding(
                        file=item.get("file", ""),
                        line=item.get("line", 0),
                        severity=item.get("severity", "info"),
                        category=category,
                        message=item.get("message", ""),
                        suggestion=item.get("suggestion", ""),
                        confidence=item.get("confidence", 0.5),
                    )
                )
            except Exception as e:
                logger.warning(f"{self.name}: skipped invalid finding {item!r}: {e}")
        return self._cap_findings(findings), True

    def _parse_findings(self, content: str) -> list[Finding]:
        """Parse LLM JSON output into Finding objects."""
        findings, _valid = self._parse_findings_result(content)
        return findings

    def _merge_detector_findings(self, findings: list[Finding], diffs: dict[str, str]) -> list[Finding]:
        """Merge zero-token deterministic findings into reviewer output.

        Detector findings are deterministic and high-precision, so they are NOT
        subject to the per-reviewer cap. The cap exists to trim verbose LLM nitpick
        noise — not to drop real vulnerabilities the scanners already found. When the
        LLM fills every cap slot with high-confidence findings, re-capping the merged
        set silently discarded *all* detector findings (secrets, path-traversal, unsafe,
        xss …), which tanked security recall. Cap only the LLM findings; keep every
        deduped detector finding.
        """

        if self.reviewer_type == "security":
            detected = detect_security_findings(diffs)
        elif self.reviewer_type == "dependency":
            detected = detect_dependency_findings(diffs)
        else:
            return findings

        detector_findings = [
            Finding(
                file=item.file,
                line=max(1, item.line),
                severity=item.severity,
                category=normalize_category(item.category),
                message=item.message,
                suggestion=item.suggestion,
                confidence=item.confidence,
                reviewer=self.name,
                status="candidate",
                verified_by="detector",
            )
            for item in detected
        ]
        # `findings` (LLM output) is already capped by _parse_findings; detector
        # findings are appended uncapped. Dedupe keeps the higher-confidence finding
        # per (file, line, category), so LLM+detector overlaps collapse cleanly.
        return self._dedupe_findings(list(findings) + detector_findings)

    def _cap_findings(self, findings: list[Finding]) -> list[Finding]:
        """Keep the highest-value findings for this reviewer type."""

        cap = _MAX_FINDINGS_BY_TYPE.get(self.reviewer_type, 8)
        if len(findings) > cap:
            findings.sort(key=lambda f: (_SEVERITY_RANK.get(f.severity, 0), f.confidence), reverse=True)
            findings = findings[:cap]
        return findings

    @staticmethod
    def _dedupe_findings(findings: list[Finding]) -> list[Finding]:
        """Keep strongest duplicate per `(file, line, category)`."""

        deduped: dict[tuple[str, int, str], Finding] = {}
        for item in findings:
            item.category = normalize_category(item.category)
            key = (item.file, item.line, item.category)
            current = deduped.get(key)
            if current is None or item.confidence > current.confidence:
                deduped[key] = item
        return list(deduped.values())


class SecurityReviewer(BaseReviewer):
    def __init__(
        self,
        llm: ChatOpenAI,
        registry: SpecRegistry,
        gateway: ToolGateway,
        agentic: bool = False,
        max_tokens: int = 20000,
        event_bus: Any = None,
    ) -> None:
        super().__init__(
            name="security_reviewer",
            reviewer_type="security",
            llm=llm,
            registry=registry,
            gateway=gateway,
            max_steps=registry.get_agent("security_reviewer").max_steps,
            agentic=agentic,
            max_tokens=max_tokens,
            event_bus=event_bus,
        )


class PerformanceReviewer(BaseReviewer):
    def __init__(
        self,
        llm: ChatOpenAI,
        registry: SpecRegistry,
        gateway: ToolGateway,
        agentic: bool = False,
        max_tokens: int = 20000,
        event_bus: Any = None,
    ) -> None:
        super().__init__(
            name="performance_reviewer",
            reviewer_type="performance",
            llm=llm,
            registry=registry,
            gateway=gateway,
            max_steps=registry.get_agent("performance_reviewer").max_steps,
            agentic=agentic,
            max_tokens=max_tokens,
            event_bus=event_bus,
        )


class StyleReviewer(BaseReviewer):
    def __init__(
        self,
        llm: ChatOpenAI,
        registry: SpecRegistry,
        gateway: ToolGateway,
        agentic: bool = False,
        max_tokens: int = 20000,
        event_bus: Any = None,
    ) -> None:
        super().__init__(
            name="style_reviewer",
            reviewer_type="style",
            llm=llm,
            registry=registry,
            gateway=gateway,
            max_steps=registry.get_agent("style_reviewer").max_steps,
            agentic=agentic,
            max_tokens=max_tokens,
            event_bus=event_bus,
        )


class CorrectnessReviewer(BaseReviewer):
    def __init__(
        self,
        llm: ChatOpenAI,
        registry: SpecRegistry,
        gateway: ToolGateway,
        agentic: bool = False,
        max_tokens: int = 20000,
        event_bus: Any = None,
    ) -> None:
        super().__init__(
            name="correctness_reviewer",
            reviewer_type="correctness",
            llm=llm,
            registry=registry,
            gateway=gateway,
            max_steps=registry.get_agent("correctness_reviewer").max_steps,
            agentic=agentic,
            max_tokens=max_tokens,
            event_bus=event_bus,
        )


class LocalizationReviewer(BaseReviewer):
    def __init__(
        self,
        llm: ChatOpenAI,
        registry: SpecRegistry,
        gateway: ToolGateway,
        agentic: bool = False,
        max_tokens: int = 20000,
        event_bus: Any = None,
    ) -> None:
        super().__init__(
            name="localization_reviewer",
            reviewer_type="localization",
            llm=llm,
            registry=registry,
            gateway=gateway,
            max_steps=registry.get_agent("localization_reviewer").max_steps,
            agentic=agentic,
            max_tokens=max_tokens,
            event_bus=event_bus,
        )


class TestingReviewer(BaseReviewer):
    def __init__(
        self,
        llm: ChatOpenAI,
        registry: SpecRegistry,
        gateway: ToolGateway,
        agentic: bool = False,
        max_tokens: int = 20000,
        event_bus: Any = None,
    ) -> None:
        super().__init__(
            name="testing_reviewer",
            reviewer_type="testing",
            llm=llm,
            registry=registry,
            gateway=gateway,
            max_steps=registry.get_agent("testing_reviewer").max_steps,
            agentic=agentic,
            max_tokens=max_tokens,
            event_bus=event_bus,
        )


class DocumentationReviewer(BaseReviewer):
    def __init__(
        self,
        llm: ChatOpenAI,
        registry: SpecRegistry,
        gateway: ToolGateway,
        agentic: bool = False,
        max_tokens: int = 20000,
        event_bus: Any = None,
    ) -> None:
        super().__init__(
            name="doc_reviewer",
            reviewer_type="documentation",
            llm=llm,
            registry=registry,
            gateway=gateway,
            max_steps=registry.get_agent("doc_reviewer").max_steps,
            agentic=agentic,
            max_tokens=max_tokens,
            event_bus=event_bus,
        )


class DependencyReviewer(BaseReviewer):
    def __init__(
        self,
        llm: ChatOpenAI,
        registry: SpecRegistry,
        gateway: ToolGateway,
        agentic: bool = False,
        max_tokens: int = 20000,
        event_bus: Any = None,
    ) -> None:
        super().__init__(
            name="dependency_reviewer",
            reviewer_type="dependency",
            llm=llm,
            registry=registry,
            gateway=gateway,
            max_steps=registry.get_agent("dependency_reviewer").max_steps,
            agentic=agentic,
            max_tokens=max_tokens,
            event_bus=event_bus,
        )


class AccessibilityReviewer(BaseReviewer):
    def __init__(
        self,
        llm: ChatOpenAI,
        registry: SpecRegistry,
        gateway: ToolGateway,
        agentic: bool = False,
        max_tokens: int = 20000,
        event_bus: Any = None,
    ) -> None:
        super().__init__(
            name="accessibility_reviewer",
            reviewer_type="accessibility",
            llm=llm,
            registry=registry,
            gateway=gateway,
            max_steps=registry.get_agent("accessibility_reviewer").max_steps,
            agentic=agentic,
            max_tokens=max_tokens,
            event_bus=event_bus,
        )


REVIEWER_MAP: dict[str, type[BaseReviewer]] = {
    "security_reviewer": SecurityReviewer,
    "performance_reviewer": PerformanceReviewer,
    "style_reviewer": StyleReviewer,
    "correctness_reviewer": CorrectnessReviewer,
    "localization_reviewer": LocalizationReviewer,
    "testing_reviewer": TestingReviewer,
    "doc_reviewer": DocumentationReviewer,
    "dependency_reviewer": DependencyReviewer,
    "accessibility_reviewer": AccessibilityReviewer,
}
