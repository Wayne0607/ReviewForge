"""Investigator — answer each OPEN hypothesis's ``open_question`` with tools.

This is the only LLM stage that uses tools, and the only filtering stage.  Each
hypothesis is judged ``confirmed`` / ``refuted`` / ``unknown`` on the basis of
code-written :class:`Observation` records; grounding is decided by code, never
by the model, so an ungrounded "confirmed" can never reach the editor.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from itertools import groupby, zip_longest
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import StructuredTool

from reviewforge.core.json_output import extract_json_value
from reviewforge.core.state import StateStore
from reviewforge.engine.context_pack import ContextPack
from reviewforge.engine.detectors.unified_diff import iter_right_lines, select_diff_hunks
from reviewforge.engine.hypothesis import (
    ContractAssessment,
    EvidenceCitation,
    Hypothesis,
    HypothesisLedger,
    Mechanism,
    Observation,
    Site,
)
from reviewforge.engine.prompts_v4 import load_prompt
from reviewforge.engine.semantic_diff import SemanticChangeSet, UnitKind
from reviewforge.engine.verification_guidance import (
    defect_scope_guidance,
    has_python_concurrency,
    has_state_navigation,
    is_localization_path,
    localization_guidance,
    python_concurrency_guidance,
    state_guidance,
)
from reviewforge.tools.workspace import _bounded_range

logger = logging.getLogger(__name__)

ToolExecutor = Callable[[str, dict[str, Any]], Awaitable[str]]

_SEVERITY_BASE_STEPS = {"error": 6, "warning": 4, "info": 2}
_SEVERITY_RANK = {"info": 0, "warning": 1, "error": 2}
_MAX_STEPS = 8
_TOOL_RESULT_CHARS = 6_000
_OBS_EXCERPT_CHARS = 1_200
_EVIDENCE_SEGMENT_CHARS = 400
_NOT_FOUND_MARKERS = frozenset({"no results", "not found", "no matches", "no definition found", "no callers found"})

_OUT_OF_DIFF_TOKENS = ("caller", "callee", "parent", "base class", "interface", "schema")
_REQUIRES_CONTEXT_TOKENS = ("caller", "parent", "schema", "base class")


class _UnknownEvidenceReferenceError(ValueError):
    pass


def _evidence_segments(observation: Observation) -> dict[str, EvidenceCitation]:
    """Name saved source spans; never synthesize quotes or include unsaved text."""
    if observation.status != "success":
        return {}
    if observation.tool in {"grep", "find_callers"}:
        blocks = observation.excerpt.splitlines(keepends=True)
    elif observation.tool == "find_definition":
        blocks = re.split(r"(?m)(?=^- )", observation.excerpt)
    else:
        blocks = [observation.excerpt]
    chunks = []
    for block in blocks:
        pending = ""
        for line in block.splitlines(keepends=True):
            if pending and len(pending) + len(line) > _EVIDENCE_SEGMENT_CHARS:
                chunks.append(pending)
                pending = ""
            pending += line
        if pending:
            chunks.append(pending)
    return {
        f"{observation.id}:e{index}": EvidenceCitation(observation.id, quote)
        for index, quote in enumerate(chunks, 1)
        if quote.strip()
    }


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
    tokens: int = 0
    observations: list[Observation] = field(default_factory=list)
    retryable: bool = False
    assessment: ContractAssessment | None = None


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
    # A search scope is not a source file. Search hit locations are resolved
    # from the actual result when checking the quoted evidence below.
    return str(args.get("path") or "")


def _line_range(args: dict[str, Any]) -> tuple[int, int] | None:
    start, end = args.get("start"), args.get("end")
    if isinstance(start, int) or isinstance(end, int):
        return (int(start or 0), int(end or 0))
    return None


def _is_not_found(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return True
    return stripped.lower() in _NOT_FOUND_MARKERS


def _parse_additional_sites(raw: Any, state: StateStore | None) -> list[Site]:
    sites: list[Site] = []
    if not isinstance(raw, list):
        return sites
    right_lines = {
        path: dict(iter_right_lines(diff or "")) for path, diff in ((state.file_diffs or {}) if state else {}).items()
    }
    for item in raw:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path", "")).strip()
        excerpt = str(item.get("excerpt", "")).strip()
        line = item.get("line")
        content = right_lines.get(path, {}).get(line) if type(line) is int else None
        if content is not None and len(excerpt) >= 12 and excerpt in content:
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
            # Read pinned source, then select the window locally. Workspace's
            # display-oriented range reader inserts "N: " on every line, which
            # is not source and breaks exact multiline Observation citations.
            # Keep source whitespace intact and location in separate metadata.
            if getattr(workspace, "source", "") == "api-fallback":
                content = await workspace.read_async(args["path"])
            else:
                content = workspace.read(args["path"])
            if content is None or (args.get("start") is None and args.get("end") is None):
                return content or ""
            lines = content.splitlines(keepends=True)
            first, last = _bounded_range(lines, args.get("start") or 1, args.get("end") or len(lines))
            return "".join(lines[first - 1 : last])
        if name == "grep":
            globs = [args["glob"]] if args.get("glob") else None
            hits = workspace.grep(args["pattern"], globs=globs, max_hits=int(args.get("max_hits", 10)), diverse=True)
            return "\n".join(f"- {hit.path}:{hit.line}: {hit.text}" for hit in hits) or "No results"
        if name == "find_definition":
            hits = workspace.find_symbol_definitions(args["symbol"], language=language)
            return (
                "\n".join(f"- {hit.symbol} [{hit.symbol_type}] {hit.path}:{hit.line}\n  {hit.excerpt}" for hit in hits)
                or "No definition found"
            )
        if name == "find_callers":
            hits = workspace.find_callers(
                args["symbol"], language=language, max_hits=int(args.get("max_hits", 10)), diverse=True
            )
            return "\n".join(f"- {hit.path}:{hit.line}: {hit.text}" for hit in hits) or "No callers found"
        if name == "read_diff":
            patch = (state.file_diffs or {}).get(args["path"], "") or ""
            if args.get("start") is None and args.get("end") is None:
                return patch
            start = int(args["start"]) if args.get("start") is not None else 0
            end = int(args["end"]) if args.get("end") is not None else 2**63 - 1
            if start < 0 or end < start:
                raise ValueError("read_diff requires an ordered RIGHT-line window")
            return select_diff_hunks(patch, [(start, end)])
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
        changeset: SemanticChangeSet | None = None,
    ) -> None:
        self._llm = llm
        self._executor = executor
        self._output_language = output_language
        self._max_steps = None if max_steps is None else max(1, int(max_steps))
        self._changeset = changeset
        self._observations: list[Observation] = []
        self._obs_counter = 0
        self._tool_counts: dict[str, int] = {}
        self._state: StateStore | None = None
        self._tokens = 0
        self._input_overhead = 0
        self._read_focus: dict[str, int] = {}

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

        async def read_diff(path: str, start: int | None = None, end: int | None = None) -> str:
            payload: dict[str, Any] = {"path": path}
            if start is not None:
                payload["start"] = start
            if end is not None:
                payload["end"] = end
            return await self._run_tool("read_diff", payload)

        return [
            StructuredTool.from_function(
                coroutine=read_file,
                name="read_file",
                description="Read pinned head source. Supply 1-based start/end for an explicit window; otherwise "
                "focus on the latest saved search hit, hypothesis site or related Context location when known. "
                "Returned path/range identify the requested window; only its saved excerpt is citable.",
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
                coroutine=read_diff,
                name="read_diff",
                description="读取 PR before/after diff；可用 start/end RIGHT 行窗口定位 hunk，保留删除行与上下文",
            ),
        ]

    async def _run_tool(self, name: str, args: dict[str, Any]) -> str:
        if name == "read_file":
            args = self._focus_read_args(args)
        query = _query_string(name, args)
        if self._tool_counts.get(query, 0) >= 2:
            return "Repeated tool call limit reached; use existing observations or investigate a different fact."
        self._tool_counts[query] = self._tool_counts.get(query, 0) + 1
        failed = False
        try:
            text = str(await self._executor(name, args) or "")
        except Exception:
            text = ""
            failed = True
        text = text[:_TOOL_RESULT_CHARS]
        if failed:
            status = "error"
        elif _is_not_found(text):
            status = "not_found"
        else:
            status = "success"
        observation = Observation(
            id=f"obs_{self._obs_counter}",
            tool=name,
            query=query,
            path=_observation_path(args),
            line_range=_line_range(args),
            sha=self._state.head_sha if self._state else "",
            result_digest=hashlib.sha256(text.encode("utf-8")).hexdigest()[:16],
            excerpt=text[:_OBS_EXCERPT_CHARS],
            status=status,
        )
        self._obs_counter += 1
        self._observations.append(observation)
        label = f"[{observation.id}]"
        if name == "read_file" and observation.line_range:
            label += f" {observation.path} (requested lines {observation.line_range[0]}-{observation.line_range[1]})"
        segments = _evidence_segments(observation)
        if not segments:
            return f"{label} status={status}\n{text}" if text else f"{label} status={status} (no content)"
        view = "\n\n".join(f"[{reference}]\n{citation.quote}" for reference, citation in segments.items())
        reply = f"{label} Saved evidence (copy the exact reference IDs into assessment):\n{view}"
        if len(text) > _OBS_EXCERPT_CHARS:
            reply += (
                "\n[End saved evidence excerpt]\nResult exceeds the saved excerpt; omitted source is not shown. "
                "Use read_file/read_diff with a narrower line range or a more specific search "
                "to record the required evidence."
            )
        return reply[:_TOOL_RESULT_CHARS]

    def _prepare_read_focus(self, hypothesis: Hypothesis, pack: ContextPack) -> None:
        self._read_focus = {}
        context = pack.units.get(hypothesis.unit_id)
        if context is not None:
            for item in context.slices:
                if item.start_line > 0:
                    self._read_focus.setdefault(item.path, item.start_line)
        # Compiler-associated sites take priority over the related slices.
        if self._changeset and any(unit.id == hypothesis.unit_id for unit in self._changeset.units):
            for site in reversed(hypothesis.sites):
                if site.line > 0:
                    self._read_focus[site.path] = site.line

    def _focus_read_args(self, args: dict[str, Any]) -> dict[str, Any]:
        if args.get("start") is not None or args.get("end") is not None:
            return args
        path = args.get("path")
        focus = self._read_focus.get(path)
        # Search scopes are not files. Use concrete locations in saved positive
        # hits, never an absent result or model-authored filename/line guess.
        for observation in reversed(self._observations):
            if observation.status != "success" or observation.tool not in {"grep", "find_callers", "find_definition"}:
                continue
            for line in observation.excerpt.splitlines():
                if observation.tool == "find_definition":
                    match = re.match(r"^- .* \[[^]]+\] (.+):(\d+)$", line)
                else:
                    match = re.match(r"^- (.+?):(\d+):", line)
                if match and match[1] == path and int(match[2]) > 0:
                    focus = int(match[2])
                    return {**args, "start": max(1, focus - 3), "end": focus + 8}
        if focus is None:
            return args
        return {**args, "start": max(1, focus - 3), "end": focus + 8}

    def _system_prompt(self) -> str:
        return load_prompt("investigator", output_language=_language_directive(self._output_language))

    def _render_user(self, hypothesis: Hypothesis, state: StateStore, pack: ContextPack) -> str:
        diffs = state.file_diffs or {}
        ranges: dict[str, list[tuple[int, int]]] = {}
        for site in hypothesis.sites:
            ranges.setdefault(site.path, []).append((site.line, site.line))
        if self._changeset is not None:
            for unit in self._changeset.units:
                if unit.id == hypothesis.unit_id:
                    ranges.setdefault(unit.path, []).append((max(1, unit.start_line - 3), unit.end_line + 3))
        sections = []
        for path, windows in sorted(ranges.items()):
            patch = select_diff_hunks(diffs.get(path, ""), windows)
            if patch:
                sections.append(f"### {path}\n{patch}")
        diff_section = "\n\n".join(sections) or "(no matching hunk; use read_diff to inspect the file's changes)"
        context = pack.render_for_unit(hypothesis.unit_id, max_chars=12_000)
        hypothesis_body = json.dumps(hypothesis.to_dict(), ensure_ascii=False, indent=2)
        sections = [
            "## Hypothesis\n" + hypothesis_body,
            "## Diff hunk(s)\n" + diff_section,
            "## Context\n" + (context or "（无）/(none)"),
            defect_scope_guidance(),
        ]
        if pack.pr_intent:
            sections.append("## PR intent (author context, not defect evidence)\n" + pack.pr_intent[:2_000])
        boundary = self._resource_boundary(hypothesis)
        if boundary:
            sections.append(boundary)
        unit = (
            next((unit for unit in self._changeset.units if unit.id == hypothesis.unit_id), None)
            if self._changeset
            else None
        )
        if hypothesis.mechanism is Mechanism.I18N or (
            unit is not None and unit.kind is UnitKind.RESOURCE and is_localization_path(unit.path)
        ):
            sections.append("## Verification guidance\n" + localization_guidance())
        if any(has_python_concurrency(path, diffs.get(path, "")) for path in ranges):
            sections.append("## Verification guidance\n" + python_concurrency_guidance())
        if has_state_navigation(pack, hypothesis.unit_id):
            sections.append("## Verification guidance\n" + state_guidance())
        return "\n\n".join(sections)

    def _resource_boundary(self, hypothesis: Hypothesis) -> str:
        # Resource contents can violate a local contract without a runtime
        # caller. Supply compiler facts, not a verdict or a blanket i18n waiver.
        if hypothesis.mechanism is not Mechanism.I18N or self._changeset is None:
            return ""
        unit = next((unit for unit in self._changeset.units if unit.id == hypothesis.unit_id), None)
        if unit is None or unit.kind is not UnitKind.RESOURCE:
            return ""
        paths = {unit.path, *(site.path for site in hypothesis.sites)}
        facts = {
            item.path: {"path": item.path, "provenance": item.provenance.note}
            for item in self._changeset.units
            if item.kind is UnitKind.RESOURCE and item.path in paths
        }
        return (
            "## Verification boundary\n"
            + json.dumps(list(facts.values()), ensure_ascii=False)
            + "\nFor a direct language/script violation, compare changed text with each resource's declared locale. "
            "A runtime consumer reference is not needed to establish this local contract violation. "
            "Do not invent an absent locale or apply one site's locale to another. "
            "Format/parameter claims still require the actual runtime consumer and formatter contract. "
            "If the open_question asks only about an ancillary caller, answer it honestly but judge the claim "
            "at its relevant contract. Record the changed source/diff as exact Observation evidence; "
            "these metadata alone prove no defect."
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
        self._prepare_read_focus(hypothesis, pack)
        self._observations = list(hypothesis.observations)
        self._tokens = 0
        self._input_overhead = 0
        self._tool_counts = {}
        self._obs_counter = max(
            (
                int(obs.id[4:]) + 1
                for obs in hypothesis.observations
                if obs.id.startswith("obs_") and obs.id[4:].isdigit()
            ),
            default=0,
        )
        changed = changed_paths if changed_paths is not None else _changed_paths(state)
        steps = self._max_steps if self._max_steps is not None else budget_steps(hypothesis)
        token_limit = steps * 4_000

        chat = [
            SystemMessage(content=self._system_prompt()),
            HumanMessage(content=self._render_user(hypothesis, state, pack)),
        ]
        initial = list(chat)
        tools = self._build_tools()
        known_names = {tool.name for tool in tools}
        bound = self._llm.bind_tools(tools)
        schema_tokens = self._estimate_text_tokens(
            json.dumps(
                [{"name": tool.name, "description": tool.description, "parameters": tool.args} for tool in tools],
                ensure_ascii=False,
            )
        )

        for step in range(steps):
            closing = self._closing_chat(initial, token_limit)
            # A tool round is useful only if we can still ask for a verdict.
            # Reserve the closing input and output within the SAME budget,
            # before spending on more tools. The output reserve is capped at
            # a quarter of the total budget so small budgets can collect facts.
            input_tokens = self._estimate_input_tokens(chat) + schema_tokens + self._input_overhead
            closing_tokens = self._estimate_input_tokens(closing) + self._input_overhead
            remaining = token_limit - self._tokens
            closing_output = min(4_000, token_limit // 4)
            output_tokens = min(4_000, remaining - input_tokens - closing_tokens - closing_output)
            if output_tokens <= 0:
                return await self._close(closing, hypothesis, changed, token_limit=token_limit, steps=step)
            try:
                response = await bound.ainvoke(chat, max_tokens=output_tokens)
            except Exception as exc:
                logger.warning("investigator provider error for %s: %s", hypothesis.identity, exc)
                return self._result(
                    verdict="unknown",
                    reason=f"provider error: {exc}",
                    severity=hypothesis.severity,
                    steps=step,
                    retryable=True,
                )
            self._record_tokens(response, chat, schema_tokens=schema_tokens)
            chat.append(response)
            if self._tokens > token_limit:
                return self._result(
                    verdict="unknown", reason="token-exhausted", severity=hypothesis.severity, steps=step + 1
                )
            tool_calls = getattr(response, "tool_calls", None) or []
            if not tool_calls:
                parsed = self._parse_verdict(getattr(response, "content", "") or "")
                if parsed is not None:
                    return self._finalize(parsed, hypothesis, changed, steps=step + 1)
                return await self._close(
                    self._closing_chat(initial, token_limit),
                    hypothesis,
                    changed,
                    token_limit=token_limit,
                    steps=step + 1,
                )
            if self._tokens >= token_limit:
                return self._result(
                    verdict="unknown", reason="token-exhausted", severity=hypothesis.severity, steps=step + 1
                )
            for tool_call in tool_calls:
                name = str(tool_call.get("name", ""))
                args = tool_call.get("args", {}) or {}
                if name not in known_names:
                    result = f"Unknown tool: {name}"
                else:
                    result = await self._run_tool(name, args)
                chat.append(ToolMessage(content=result, tool_call_id=tool_call.get("id", "")))

        return await self._close(
            self._closing_chat(initial, token_limit), hypothesis, changed, token_limit=token_limit, steps=steps
        )

    def _closing_chat(self, initial: list[Any], token_limit: int) -> list[Any]:
        # Replay the hypothesis/context and code-written excerpts, rather than
        # every 6000-character tool result and the model's speculative history.
        # Nothing outside a saved Observation becomes citable during closure.
        observations = [
            {
                "id": obs.id,
                "tool": obs.tool,
                "query": obs.query,
                "path": obs.path,
                "line_range": obs.line_range,
                "status": obs.status,
                **(
                    {"evidence": {reference: citation.quote for reference, citation in _evidence_segments(obs).items()}}
                    if obs.status == "success"
                    else {"excerpt": obs.excerpt}
                ),
            }
            for obs in self._observations
        ]
        return [
            *initial,
            HumanMessage(
                content="## Recorded observations\n"
                + json.dumps(observations, ensure_ascii=False)
                + f"\nRemaining investigation budget: {max(0, token_limit - self._tokens)} tokens. "
                "Finish now. Answer the one open_question using these saved observations. "
                "Assessment JSON only; no tools. Missing proof: comparison=unresolved or assessment=null."
            ),
        ]

    async def _close(
        self,
        chat: list[Any],
        hypothesis: Hypothesis,
        changed: set[str],
        *,
        token_limit: int,
        steps: int,
    ) -> InvestigationResult:
        input_tokens = self._estimate_input_tokens(chat) + self._input_overhead
        if self._tokens + input_tokens >= token_limit:
            return self._result(verdict="unknown", reason="token-exhausted", severity=hypothesis.severity, steps=steps)
        try:
            final = await self._llm.ainvoke(chat, max_tokens=min(4_000, token_limit - self._tokens - input_tokens))
            self._record_tokens(final, chat)
            parsed = self._parse_verdict(getattr(final, "content", "") or "")
        except Exception as exc:
            logger.warning("investigator final call failed for %s: %s", hypothesis.identity, exc)
            return self._result(
                verdict="unknown", reason="provider error", severity=hypothesis.severity, steps=steps, retryable=True
            )
        if self._tokens > token_limit:
            return self._result(verdict="unknown", reason="token-exhausted", severity=hypothesis.severity, steps=steps)
        if parsed is None:
            return self._result(verdict="unknown", reason="step-exhausted", severity=hypothesis.severity, steps=steps)
        return self._finalize(parsed, hypothesis, changed, steps=steps)

    @staticmethod
    def _estimate_text_tokens(text: str) -> int:
        # A forecast, not a provider tokenizer. The old len/4 estimate missed
        # tool arguments/schema and badly underestimated Chinese instructions.
        non_ascii = sum(ord(char) > 127 for char in text)
        return (len(text) - non_ascii + 3) // 4 + non_ascii

    @classmethod
    def _estimate_input_tokens(cls, chat: list[Any]) -> int:
        payload = [
            {
                "role": message.type,
                "content": message.content,
                "tool_calls": getattr(message, "tool_calls", []),
                "tool_call_id": getattr(message, "tool_call_id", ""),
            }
            for message in chat
        ]
        return cls._estimate_text_tokens(json.dumps(payload, ensure_ascii=False, default=str)) + 12 * len(chat)

    def _record_tokens(self, response: Any, chat: list[Any], *, schema_tokens: int = 0) -> None:
        usage = getattr(response, "usage_metadata", None) or {}
        usage = usage or (getattr(response, "response_metadata", {}) or {}).get("token_usage", {})
        total = usage.get("total_tokens")
        measured_input = usage.get("input_tokens", usage.get("prompt_tokens"))
        if measured_input is not None:
            self._input_overhead = max(
                self._input_overhead, int(measured_input) - self._estimate_input_tokens(chat) - schema_tokens
            )
        if total is None and measured_input is not None:
            output = usage.get("output_tokens", usage.get("completion_tokens"))
            if output is not None:
                total = int(measured_input) + int(output)
        if total is not None:
            self._tokens += max(0, int(total))
        else:
            self._tokens += self._estimate_input_tokens(chat) + schema_tokens + self._estimate_input_tokens([response])

    @staticmethod
    def _parse_verdict(content: str) -> dict[str, Any] | None:
        parsed = extract_json_value(content or "", required_key="assessment", allow_list=False)
        if not isinstance(parsed, dict):
            parsed = extract_json_value(content or "", required_key="verdict", allow_list=False)
        return parsed if isinstance(parsed, dict) else None

    def _finalize(
        self, parsed: dict[str, Any], hypothesis: Hypothesis, changed: set[str], *, steps: int
    ) -> InvestigationResult:
        if "verdict" in parsed:
            # Explicit legacy decisions must still agree with the assessment;
            # never repair a contradictory or UNKNOWN historical response.
            verdict = str(parsed["verdict"]).strip().lower()
        else:
            raw_assessment = parsed.get("assessment")
            comparison = raw_assessment.get("comparison") if isinstance(raw_assessment, dict) else None
            verdict = (
                {"conflict": "confirmed", "compatible": "refuted", "unresolved": "unknown"}.get(
                    comparison.strip(), "unknown"
                )
                if isinstance(comparison, str)
                else "unknown"
            )
        if verdict not in {"confirmed", "refuted", "unknown"}:
            verdict = "unknown"
        answer = str(parsed.get("answer", "")).strip()
        reason = str(parsed.get("reason", "")).strip()
        severity = str(parsed.get("severity", "")).strip().lower()
        if severity not in {"error", "warning", "info"}:
            severity = hypothesis.severity
        raw_ids = parsed.get("evidence_ids")
        evidence_ids = [item for item in raw_ids if isinstance(item, str) and item] if isinstance(raw_ids, list) else []
        evidence_quote = str(parsed.get("evidence_quote", "")).strip()
        additional_sites = _parse_additional_sites(parsed.get("additional_sites"), self._state)

        strength = "none"
        assessment = None
        if verdict in {"confirmed", "refuted"}:
            by_id = {observation.id: observation for observation in self._observations}
            cited = [by_id[identity] for identity in evidence_ids if identity in by_id]
            success_cited = [observation for observation in cited if observation.status == "success"]
            grounded = [
                observation for observation in success_cited if evidence_quote and evidence_quote in observation.excerpt
            ]
            # The two-premise protocol carries its own exact citations. Retain
            # strict validation of legacy quote fields when explicitly sent,
            # but do not require a third duplicate model-authored citation.
            legacy_citation = "evidence_ids" in parsed or "evidence_quote" in parsed
            if legacy_citation and not grounded:
                verdict = "unknown"
                reason = "ungrounded"
                evidence_ids, evidence_quote = [], ""
            else:
                try:
                    assessment = self._assessment_from_output(parsed.get("assessment"))
                except _UnknownEvidenceReferenceError:
                    verdict, reason = "unknown", "ungrounded-assessment"
                except ValueError:
                    verdict, reason = "unknown", "incomplete-assessment"
                if assessment is not None:
                    required = "conflict" if verdict == "confirmed" else "compatible"
                    proof = [*assessment.expected_evidence, *assessment.actual_evidence]
                    if any(
                        citation.observation_id not in by_id
                        or by_id[citation.observation_id].status != "success"
                        or citation.quote not in by_id[citation.observation_id].excerpt
                        for citation in proof
                    ):
                        verdict, reason = "unknown", "ungrounded-assessment"
                    elif assessment.comparison != required:
                        verdict, reason = "unknown", "inconsistent-assessment"
                    else:
                        evidence_ids = list(dict.fromkeys(citation.observation_id for citation in proof))
                        evidence_quote = assessment.actual_evidence[0].quote
                        outside_diff = any(
                            self._quote_outside_diff(by_id[citation.observation_id], citation.quote, changed)
                            for citation in proof
                        )
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
            assessment=assessment,
        )

    def _assessment_from_output(self, raw: Any) -> ContractAssessment:
        if not isinstance(raw, dict):
            return ContractAssessment.from_dict(raw)
        references = {
            reference: citation
            for observation in self._observations
            for reference, citation in _evidence_segments(observation).items()
        }
        converted = dict(raw)
        for key in ("expected_evidence", "actual_evidence"):
            values = raw.get(key)
            if not isinstance(values, list):
                continue
            citations = []
            for value in values:
                if isinstance(value, str):
                    citation = references.get(value)
                    if citation is None:
                        raise _UnknownEvidenceReferenceError(value)
                    citations.append({"observation_id": citation.observation_id, "quote": citation.quote})
                else:
                    # Explicit legacy quotes still go through exact validation.
                    citations.append(value)
            converted[key] = citations
        return ContractAssessment.from_dict(converted)

    @staticmethod
    def _quote_outside_diff(observation: Observation, quote: str, changed: set[str]) -> bool:
        if observation.path:
            return observation.path not in changed
        if observation.tool in {"grep", "find_callers"}:
            # Executor search results have one concrete path:line per hit. An
            # unrelated hit must not promote evidence quoted from the diff.
            for line in observation.excerpt.splitlines():
                path, separator, rest = line.removeprefix("- ").partition(":")
                number, separator2, text = rest.partition(":")
                if (
                    separator
                    and separator2
                    and number.isdigit()
                    and (quote in text or quote.rstrip("\r\n") == line)
                    and path not in changed
                ):
                    return True
        if observation.tool == "find_definition":
            for section in re.split(r"(?m)(?=^- )", observation.excerpt):
                header, _, text = section.partition("\n")
                _, separator, location = header.partition("] ")
                path, colon, number = location.rpartition(":")
                if separator and colon and number.isdigit() and quote in section and path not in changed:
                    return True
        return False

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
        assessment: ContractAssessment | None = None,
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
            tokens=self._tokens,
            observations=list(self._observations),
            retryable=retryable,
            assessment=assessment,
        )

    async def run(
        self,
        ledger: HypothesisLedger,
        state: StateStore,
        pack: ContextPack,
        *,
        changed_paths: set[str] | None = None,
        max_hypotheses_per_pr: int = 12,
        concurrency: int = 1,
        on_update: Callable[[HypothesisLedger], Awaitable[None]] | None = None,
    ) -> list[InvestigationResult]:
        """Investigate every OPEN hypothesis concurrently, then apply verdicts.

        Each hypothesis gets its own investigator instance so per-call state
        (observation list, counter) cannot race across concurrent jobs; the LLM
        and tool executor are shared and read-only.
        """

        changed = changed_paths if changed_paths is not None else _changed_paths(state)
        ranked = sorted(
            ledger.open(),
            key=lambda hypothesis: (
                -_SEVERITY_RANK.get(hypothesis.severity, 0),
                -len(hypothesis.sites),
                hypothesis.identity,
            ),
        )
        targets = []
        # Site count alone lets one widespread mechanism consume the entire
        # cap. Preserve severity and within-mechanism rank, then take one item
        # per mechanism per round. This selects work; it never filters verdicts.
        for _, severity_items in groupby(ranked, key=lambda item: _SEVERITY_RANK.get(item.severity, 0)):
            mechanisms: dict[Mechanism, list[Hypothesis]] = {}
            for item in severity_items:
                mechanisms.setdefault(item.mechanism, []).append(item)
            for round_items in zip_longest(*mechanisms.values()):
                targets.extend(item for item in round_items if item is not None)
        semaphore = asyncio.Semaphore(max(1, concurrency))

        async def _investigate(index: int, hypothesis: Hypothesis) -> InvestigationResult:
            if index >= max(0, max_hypotheses_per_pr):
                return self._result(verdict="unknown", reason="budget-exhausted", severity=hypothesis.severity)
            async with semaphore:
                worker = Investigator(
                    self._llm,
                    self._executor,
                    output_language=self._output_language,
                    max_steps=self._max_steps,
                    changeset=self._changeset,
                )
                try:
                    return await worker.investigate(hypothesis, state, pack, changed_paths=changed)
                except Exception as exc:
                    logger.warning("investigation failed for %s: %s", hypothesis.identity, exc)
                    return self._result(
                        verdict="unknown",
                        reason=f"investigation error: {exc}",
                        severity=hypothesis.severity,
                        retryable=True,
                    )

        async def complete(index: int, hypothesis: Hypothesis) -> InvestigationResult:
            result = await _investigate(index, hypothesis)
            ledger.apply_verdict(
                hypothesis.identity,
                status=result.verdict,
                evidence_strength=result.strength,
                verdict_reason=result.reason,
                observations=result.observations,
                severity=result.severity,
                additional_sites=result.additional_sites,
                investigation_steps=result.steps,
                investigation_tokens=result.tokens,
                retryable=result.retryable,
                assessment=result.assessment,
            )
            if on_update is not None:
                await on_update(ledger)
            return result

        jobs = [asyncio.create_task(complete(index, hypothesis)) for index, hypothesis in enumerate(targets)]
        try:
            return list(await asyncio.gather(*jobs))
        finally:
            for job in jobs:
                if not job.done():
                    job.cancel()
            await asyncio.gather(*jobs, return_exceptions=True)


__all__ = ["InvestigationResult", "Investigator", "build_workspace_executor", "budget_steps"]
