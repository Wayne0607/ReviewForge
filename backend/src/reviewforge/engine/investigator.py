"""Investigator — answer each OPEN hypothesis's ``open_question`` with tools.

This is the only LLM stage that uses tools, and the only filtering stage.  Each
hypothesis is judged ``confirmed`` / ``refuted`` / ``unknown`` on the basis of
code-written :class:`Observation` records; grounding is decided by code, never
by the model, so an ungrounded "confirmed" can never reach the editor.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import StructuredTool

from reviewforge.core.json_output import extract_json_value
from reviewforge.core.state import StateStore
from reviewforge.engine.context_pack import ContextPack
from reviewforge.engine.hypothesis import Hypothesis, HypothesisLedger, Observation, Site
from reviewforge.engine.prompts_v4 import load_prompt

logger = logging.getLogger(__name__)

ToolExecutor = Callable[[str, dict[str, Any]], Awaitable[str]]

_SEVERITY_BASE_STEPS = {"error": 6, "warning": 4, "info": 2}
_SEVERITY_RANK = {"info": 0, "warning": 1, "error": 2}
_MAX_STEPS = 8
_TOOL_RESULT_CHARS = 6_000
_OBS_EXCERPT_CHARS = 1_200
_NOT_FOUND_MARKERS = ("no results", "not found", "no matches", "no definition", "no callers")

_OUT_OF_DIFF_TOKENS = ("caller", "callee", "parent", "base class", "interface", "schema")
_REQUIRES_CONTEXT_TOKENS = ("caller", "parent", "schema", "base class")


@dataclass
class InvestigationResult:
    verdict: str
    answer: str = ""
    reason: str = ""
    evidence_ids: list[str] = field(default_factory=list)
    evidence_quote: str = ""
    severity: str = "info"
    additional_sites: list[Site] = field(default_factory=list)
    strength: str = "none"
    steps: int = 0
    observations: list[Observation] = field(default_factory=list)
    retryable: bool = False


def budget_steps(hypothesis: Hypothesis) -> int:
    """Allocate tool-loop steps by severity, with context bonuses, capped at 8."""

    base = _SEVERITY_BASE_STEPS.get(hypothesis.severity, 2)
    bonus = 0
    if any(token in hypothesis.refutation.lower() for token in _OUT_OF_DIFF_TOKENS):
        bonus += 2
    if any(token in hypothesis.open_question.lower() for token in _REQUIRES_CONTEXT_TOKENS):
        bonus += 2
    return min(_MAX_STEPS, base + bonus)


def _language_directive(language: str) -> str:
    return "简体中文" if language == "zh-CN" else "English"


def _query_string(name: str, args: dict[str, Any]) -> str:
    return f"{name} {json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)}"


def _observation_path(args: dict[str, Any]) -> str:
    return str(args.get("path") or args.get("glob") or "")


def _line_range(args: dict[str, Any]) -> tuple[int, int] | None:
    start, end = args.get("start"), args.get("end")
    if isinstance(start, int) or isinstance(end, int):
        return (int(start or 0), int(end or 0))
    return None


def _is_not_found(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return True
    return any(marker in stripped.lower() for marker in _NOT_FOUND_MARKERS)


def _parse_additional_sites(raw: Any) -> list[Site]:
    sites: list[Site] = []
    if not isinstance(raw, list):
        return sites
    for item in raw:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path", "")).strip()
        excerpt = str(item.get("excerpt", "")).strip()
        line = item.get("line")
        if path and isinstance(line, int) and line > 0:
            sites.append(Site(path=path, line=line, excerpt=excerpt))
    return sites


def _changed_paths(state: StateStore) -> set[str]:
    paths = set(state.files_changed or [])
    if state.file_diffs:
        paths.update(state.file_diffs.keys())
    return paths


def build_workspace_executor(workspace: Any, state: StateStore, *, language: str = "") -> ToolExecutor:
    """Bind the SPEC §4.6 tool names to a pinned :class:`PRHeadWorkspace`."""

    async def _execute(name: str, args: dict[str, Any]) -> str:
        if name == "read_file":
            content = workspace.read(args["path"], start=args.get("start"), end=args.get("end"))
            return content or ""
        if name == "grep":
            globs = [args["glob"]] if args.get("glob") else None
            hits = workspace.grep(args["pattern"], globs=globs, max_hits=int(args.get("max_hits", 10)))
            return "\n".join(f"- {hit.path}:{hit.line}: {hit.text}" for hit in hits) or "No results"
        if name == "find_definition":
            hits = workspace.find_symbol_definitions(args["symbol"], language=language)
            return (
                "\n".join(f"- {hit.symbol} [{hit.symbol_type}] {hit.path}:{hit.line}\n  {hit.excerpt}" for hit in hits)
                or "No definition found"
            )
        if name == "find_callers":
            hits = workspace.find_callers(args["symbol"], language=language, max_hits=int(args.get("max_hits", 10)))
            return "\n".join(f"- {hit.path}:{hit.line}: {hit.text}" for hit in hits) or "No callers found"
        if name == "read_diff":
            return (state.file_diffs or {}).get(args["path"], "") or ""
        raise KeyError(f"unknown investigator tool: {name}")

    return _execute


class Investigator:
    """Judge one hypothesis against tool evidence and record observations."""

    def __init__(
        self,
        llm: BaseChatModel,
        executor: ToolExecutor,
        *,
        output_language: str = "en",
        max_steps: int | None = None,
    ) -> None:
        self._llm = llm
        self._executor = executor
        self._output_language = output_language
        self._max_steps = None if max_steps is None else max(1, int(max_steps))
        self._observations: list[Observation] = []
        self._obs_counter = 0
        self._state: StateStore | None = None

    def _build_tools(self) -> list[StructuredTool]:
        async def read_file(path: str, start: int | None = None, end: int | None = None) -> str:
            payload: dict[str, Any] = {"path": path}
            if start is not None:
                payload["start"] = start
            if end is not None:
                payload["end"] = end
            return await self._run_tool("read_file", payload)

        async def grep(pattern: str, glob: str = "", max_hits: int = 10) -> str:
            return await self._run_tool("grep", {"pattern": pattern, "glob": glob, "max_hits": max_hits})

        async def find_definition(symbol: str) -> str:
            return await self._run_tool("find_definition", {"symbol": symbol})

        async def find_callers(symbol: str, max_hits: int = 10) -> str:
            return await self._run_tool("find_callers", {"symbol": symbol, "max_hits": max_hits})

        async def read_diff(path: str) -> str:
            return await self._run_tool("read_diff", {"path": path})

        return [
            StructuredTool.from_function(
                coroutine=read_file, name="read_file", description="读取仓库 head 版本文件的完整内容或 1-based 行窗口"
            ),
            StructuredTool.from_function(
                coroutine=grep, name="grep", description="按正则或字面串在仓库中搜索匹配行（可用 glob 限定文件）"
            ),
            StructuredTool.from_function(
                coroutine=find_definition, name="find_definition", description="按符号名查找其定义（含签名与上下文）"
            ),
            StructuredTool.from_function(
                coroutine=find_callers, name="find_callers", description="查找某符号的调用位置"
            ),
            StructuredTool.from_function(
                coroutine=read_diff, name="read_diff", description="读取某文件在本 PR 的 diff"
            ),
        ]

    async def _run_tool(self, name: str, args: dict[str, Any]) -> str:
        error_text = ""
        try:
            text = str(await self._executor(name, args) or "")
        except Exception as exc:
            text = ""
            error_text = str(exc)
        text = text[:_TOOL_RESULT_CHARS]
        if error_text:
            status = "error"
        elif _is_not_found(text):
            status = "not_found"
        else:
            status = "success"
        observation = Observation(
            id=f"obs_{self._obs_counter}",
            tool=name,
            query=_query_string(name, args),
            path=_observation_path(args),
            line_range=_line_range(args),
            sha=self._state.head_sha if self._state else "",
            result_digest=hashlib.sha256(text.encode("utf-8")).hexdigest()[:16],
            excerpt=text[:_OBS_EXCERPT_CHARS],
            status=status,
        )
        self._obs_counter += 1
        self._observations.append(observation)
        return f"[{observation.id}] {text}" if text else f"[{observation.id}] (no content)"

    def _system_prompt(self) -> str:
        return load_prompt("investigator", output_language=_language_directive(self._output_language))

    def _render_user(self, hypothesis: Hypothesis, state: StateStore, pack: ContextPack) -> str:
        diffs = state.file_diffs or {}
        diff_section = (
            "\n\n".join(diffs.get(path, "") for path in sorted({site.path for site in hypothesis.sites}))
            or "（无）/(none)"
        )
        context = pack.render_for_unit(hypothesis.unit_id, max_chars=12_000)
        hypothesis_body = json.dumps(hypothesis.to_dict(), ensure_ascii=False, indent=2)
        return "\n\n".join(
            [
                "## Hypothesis\n" + hypothesis_body,
                "## Diff hunk(s)\n" + diff_section,
                "## Context\n" + (context or "（无）/(none)"),
            ]
        )

    async def investigate(
        self,
        hypothesis: Hypothesis,
        state: StateStore,
        pack: ContextPack,
        *,
        changed_paths: set[str] | None = None,
    ) -> InvestigationResult:
        """Answer one hypothesis's open_question within its budget_steps."""

        self._state = state
        self._observations = []
        self._obs_counter = 0
        changed = changed_paths if changed_paths is not None else _changed_paths(state)
        steps = self._max_steps if self._max_steps is not None else budget_steps(hypothesis)

        chat = [
            SystemMessage(content=self._system_prompt()),
            HumanMessage(content=self._render_user(hypothesis, state, pack)),
        ]
        tools = self._build_tools()
        known_names = {tool.name for tool in tools}
        bound = self._llm.bind_tools(tools)

        for step in range(steps):
            try:
                response = await bound.ainvoke(chat)
            except Exception as exc:
                logger.warning("investigator provider error for %s: %s", hypothesis.identity, exc)
                return self._result(
                    verdict="unknown",
                    reason=f"provider error: {exc}",
                    severity=hypothesis.severity,
                    steps=step,
                    retryable=True,
                )
            chat.append(response)
            tool_calls = getattr(response, "tool_calls", None) or []
            if not tool_calls:
                parsed = self._parse_verdict(getattr(response, "content", "") or "")
                if parsed is not None:
                    return self._finalize(parsed, hypothesis, changed, steps=step + 1)
                chat.append(HumanMessage(content="请基于已收集的证据，现在只输出调查结论 JSON（不再调用工具）。"))
                continue
            for tool_call in tool_calls:
                name = str(tool_call.get("name", ""))
                args = tool_call.get("args", {}) or {}
                if name not in known_names:
                    result = f"Unknown tool: {name}"
                else:
                    result = await self._run_tool(name, args)
                chat.append(ToolMessage(content=result, tool_call_id=tool_call.get("id", "")))

        chat.append(HumanMessage(content="已达到步数上限。请只输出调查结论 JSON（不再调用工具）。"))
        try:
            final = await self._llm.ainvoke(chat)
            parsed = self._parse_verdict(getattr(final, "content", "") or "")
        except Exception as exc:
            logger.warning("investigator final call failed for %s: %s", hypothesis.identity, exc)
            parsed = None
        if parsed is None:
            return self._result(verdict="unknown", reason="step-exhausted", severity=hypothesis.severity, steps=steps)
        return self._finalize(parsed, hypothesis, changed, steps=steps)

    @staticmethod
    def _parse_verdict(content: str) -> dict[str, Any] | None:
        parsed = extract_json_value(content or "", required_key="verdict", allow_list=False)
        return parsed if isinstance(parsed, dict) else None

    def _finalize(
        self, parsed: dict[str, Any], hypothesis: Hypothesis, changed: set[str], *, steps: int
    ) -> InvestigationResult:
        verdict = str(parsed.get("verdict", "")).strip().lower()
        if verdict not in {"confirmed", "refuted", "unknown"}:
            verdict = "unknown"
        answer = str(parsed.get("answer", "")).strip()
        reason = str(parsed.get("reason", "")).strip()
        severity = str(parsed.get("severity", "")).strip().lower()
        if severity not in {"error", "warning", "info"}:
            severity = hypothesis.severity
        evidence_ids = [str(item) for item in (parsed.get("evidence_ids") or []) if item]
        evidence_quote = str(parsed.get("evidence_quote", "")).strip()
        additional_sites = _parse_additional_sites(parsed.get("additional_sites"))

        strength = "none"
        if verdict in {"confirmed", "refuted"}:
            by_id = {observation.id: observation for observation in self._observations}
            cited = [by_id[identity] for identity in evidence_ids if identity in by_id]
            success_cited = [observation for observation in cited if observation.status == "success"]
            grounded = any(evidence_quote and evidence_quote in observation.excerpt for observation in success_cited)
            if not success_cited or not grounded:
                verdict = "unknown"
                reason = "ungrounded"
                evidence_ids, evidence_quote = [], ""
            else:
                outside_diff = any(observation.path not in changed for observation in success_cited)
                strength = "strong" if (outside_diff or hypothesis.source.startswith("detector")) else "weak"

        return self._result(
            verdict=verdict,
            answer=answer,
            reason=reason,
            severity=severity,
            evidence_ids=evidence_ids,
            evidence_quote=evidence_quote,
            additional_sites=additional_sites,
            strength=strength,
            steps=steps,
        )

    def _result(
        self,
        *,
        verdict: str,
        answer: str = "",
        reason: str = "",
        severity: str = "info",
        evidence_ids: list[str] | None = None,
        evidence_quote: str = "",
        additional_sites: list[Site] | None = None,
        strength: str = "none",
        steps: int = 0,
        retryable: bool = False,
    ) -> InvestigationResult:
        return InvestigationResult(
            verdict=verdict,
            answer=answer,
            reason=reason,
            severity=severity,
            evidence_ids=list(evidence_ids or []),
            evidence_quote=evidence_quote,
            additional_sites=list(additional_sites or []),
            strength=strength,
            steps=steps,
            observations=list(self._observations),
            retryable=retryable,
        )

    async def run(
        self,
        ledger: HypothesisLedger,
        state: StateStore,
        pack: ContextPack,
        *,
        changed_paths: set[str] | None = None,
        max_hypotheses_per_pr: int = 12,
    ) -> list[InvestigationResult]:
        """Investigate every OPEN hypothesis, apply verdicts, and mark overflow."""

        changed = changed_paths if changed_paths is not None else _changed_paths(state)
        targets = sorted(
            ledger.open(),
            key=lambda hypothesis: (
                -_SEVERITY_RANK.get(hypothesis.severity, 0),
                -len(hypothesis.sites),
                hypothesis.identity,
            ),
        )
        results: list[InvestigationResult] = []
        for index, hypothesis in enumerate(targets):
            if index >= max(0, max_hypotheses_per_pr):
                ledger.apply_verdict(
                    hypothesis.identity,
                    status="unknown",
                    evidence_strength="none",
                    verdict_reason="budget-exhausted",
                )
                results.append(self._result(verdict="unknown", reason="budget-exhausted", severity=hypothesis.severity))
                continue
            result = await self.investigate(hypothesis, state, pack, changed_paths=changed)
            ledger.apply_verdict(
                hypothesis.identity,
                status=result.verdict,
                evidence_strength=result.strength,
                verdict_reason=result.reason,
                observations=result.observations,
                severity=result.severity,
                additional_sites=result.additional_sites,
            )
            results.append(result)
        return results


__all__ = ["InvestigationResult", "Investigator", "build_workspace_executor", "budget_steps"]
