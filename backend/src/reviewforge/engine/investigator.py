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
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import StructuredTool

from reviewforge.core.change_manifest import is_verified_rename, render_change_fact
from reviewforge.core.json_output import extract_json_value
from reviewforge.core.state import StateStore
from reviewforge.engine.context_pack import ContextPack
from reviewforge.engine.detectors.unified_diff import iter_right_lines, select_diff_hunks
from reviewforge.engine.hypothesis import (
    ContractAssessment,
    CounterEvidence,
    EvidenceCitation,
    Hypothesis,
    HypothesisLedger,
    Mechanism,
    Observation,
    Site,
)
from reviewforge.engine.investigation_evidence import EvidenceScope, SharedEvidenceCache, diff_evidence_window
from reviewforge.engine.investigation_tasks import (
    InvestigationTask,
    build_investigation_tasks,
    context_units,
    fair_investigation_tasks,
    render_investigation_context,
)
from reviewforge.engine.prompts_v4 import load_prompt
from reviewforge.engine.request_budget import plan_request
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
_MIN_USEFUL_OUTPUT = 512
_CONTEXT_CHARS = 12_000
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
    counterevidence: CounterEvidence | None = None
    outcome: str = ""
    progress: list[dict[str, Any]] = field(default_factory=list)
    unreported_tokens: int = 0

    def __post_init__(self) -> None:
        if not self.outcome:
            self.outcome = "insufficient-evidence" if self.verdict == "unknown" else "resolved"


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
        if name == "read_change":
            change = state.file_changes.get(args["path"])
            if change is None or not is_verified_rename(change, state.head_sha):
                raise ValueError("structural change has not been verified against this PR head")
            facts = render_change_fact(change)
            if len(facts) > _OBS_EXCERPT_CHARS:
                raise ValueError("verified change metadata exceeds the saved Observation limit")
            return facts
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
        protocol_version: int = 1,
        closing_llm: BaseChatModel | None = None,
        evidence_cache: SharedEvidenceCache | None = None,
    ) -> None:
        self._llm = llm
        self._executor = executor
        self._output_language = output_language
        self._max_steps = None if max_steps is None else max(1, int(max_steps))
        self._changeset = changeset
        self._protocol_version = protocol_version
        self._closing_llm = closing_llm
        self._observations: list[Observation] = []
        self._obs_counter = 0
        self._tool_counts: dict[str, int] = {}
        self._state: StateStore | None = None
        self._tokens = 0
        self._input_overhead = 0
        self._read_focus: dict[str, int] = {}
        self._evidence_cache = evidence_cache
        self._progress: list[dict[str, Any]] = []
        self._unreported_tokens = 0
        self._task_request_budget: Any = None
        self._closure_constraints: list[str] = []

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

        tools = [
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
        if (
            self._protocol_version >= 3
            and self._state is not None
            and any(is_verified_rename(change, self._state.head_sha) for change in self._state.file_changes.values())
        ):

            async def read_change(path: str) -> str:
                return await self._run_tool("read_change", {"path": path})

            tools.append(
                StructuredTool.from_function(
                    coroutine=read_change,
                    name="read_change",
                    description="Read verified PR rename metadata bound to the comparison base and head. "
                    "This proves paths and blob identity, not consumer compatibility or runtime behavior.",
                )
            )
        return tools

    async def _run_tool(self, name: str, args: dict[str, Any]) -> str:
        if name == "read_file":
            args = self._focus_read_args(args)
        query = _query_string(name, args)
        if self._protocol_version >= 3:
            existing = next((obs for obs in self._observations if obs.query == query), None)
            if existing is not None:
                return self._observation_reply(existing) + "\nAlready recorded; choose a different missing fact."
        if self._tool_counts.get(query, 0) >= 2:
            return "Repeated tool call limit reached; use existing observations or investigate a different fact."
        self._tool_counts[query] = self._tool_counts.get(query, 0) + 1
        failed = False
        shared_result_id = ""
        reused = False
        diff_navigation = {}

        async def execute(name: str, args: dict[str, Any]) -> str:
            text = str(await self._executor(name, args) or "")
            if self._protocol_version >= 3 and name == "read_diff" and isinstance(args.get("start"), int):
                text, _ = diff_evidence_window(text, args["start"])
            return text

        try:
            if self._protocol_version >= 3 and self._evidence_cache is not None:
                scope = self._evidence_cache.scope
                if self._state is None or self._state.head_sha != scope.head_sha:
                    raise ValueError("investigation source does not match the shared evidence head")
                shared = await self._evidence_cache.execute(name, args, execute, scope=scope)
                text, shared_result_id, reused = shared.text, shared.shared_result_id, shared.reused
            else:
                text = await execute(name, args)
            if self._protocol_version >= 3 and name == "read_diff" and self._state is not None:
                patch = (self._state.file_diffs or {}).get(args.get("path"), "") or ""
                _, diff_navigation = diff_evidence_window(patch, int(args.get("start") or 1))
        except Exception as exc:
            # Error bodies may contain credentials or arbitrary source. Keep
            # them out of evidence and logs; retain only the trusted failure
            # kind and internal origin so transport/allocation/parser failures
            # can be diagnosed separately.
            error_trace = exc.__traceback__
            while error_trace is not None and error_trace.tb_next is not None:
                error_trace = error_trace.tb_next
            origin = "<unknown>"
            origin_line = 0
            if error_trace is not None:
                origin = error_trace.tb_frame.f_code.co_filename.replace("\\", "/").rsplit("/", 1)[-1]
                origin_line = error_trace.tb_lineno
            logger.warning("investigator tool %s failed: %s (%s:%d)", name, type(exc).__name__, origin, origin_line)
            text = ""
            failed = True
        text = text[:_TOOL_RESULT_CHARS]
        if failed:
            status = "error"
        elif _is_not_found(text):
            status = "not_found"
        else:
            status = "success"
        excerpt, excerpt_range = text[:_OBS_EXCERPT_CHARS], None
        if name == "read_file" and status == "success":
            excerpt, excerpt_range = self._saved_read_excerpt(text, args)
        observation = Observation(
            id=f"obs_{self._obs_counter}",
            tool=name,
            query=query,
            path=_observation_path(args),
            line_range=_line_range(args),
            sha=self._state.head_sha if self._state else "",
            result_digest=hashlib.sha256(text.encode("utf-8")).hexdigest()[:16],
            excerpt=excerpt,
            status=status,
            excerpt_line_range=excerpt_range,
            **({"shared_result_id": shared_result_id} if self._protocol_version >= 3 else {}),
        )
        self._obs_counter += 1
        self._observations.append(observation)
        if self._protocol_version >= 3:
            self._progress.append(
                {
                    "phase": "evidence",
                    "query": query,
                    "observation_id": observation.id,
                    "status": status,
                    "shared_result_id": shared_result_id,
                    "reused": reused,
                }
            )
            if diff_navigation:
                self._progress.append({"phase": "diff-navigation", "observation_id": observation.id, **diff_navigation})
        return self._observation_reply(observation, text=text)

    def _observation_reply(self, observation: Observation, *, text: str | None = None) -> str:
        text = observation.excerpt if text is None else text
        name, status = observation.tool, observation.status
        label = f"[{observation.id}]"
        if name == "read_file" and observation.line_range:
            label += f" {observation.path} (requested lines {observation.line_range[0]}-{observation.line_range[1]})"
        segments = _evidence_segments(observation)
        if not segments:
            return f"{label} status={status}\n{text}" if text else f"{label} status={status} (no content)"
        view = "\n\n".join(f"[{reference}]\n{citation.quote}" for reference, citation in segments.items())
        reply = f"{label} Saved evidence (copy the exact reference IDs into assessment):\n{view}"
        navigation = next(
            (
                item
                for item in reversed(self._progress)
                if item.get("phase") == "diff-navigation" and item.get("observation_id") == observation.id
            ),
            None,
        )
        if navigation:
            reply += (
                "\nNavigation only: partial hunk "
                + navigation["hunk_header"]
                + "; omitted source is not evidence. Complete before/after pairing is not established by this excerpt."
            )
        if observation.excerpt_line_range:
            start, saved_end = observation.excerpt_line_range
            reply += f"\nSaved source lines {start}-{saved_end}; the last line may be incomplete."
        if len(text) > _OBS_EXCERPT_CHARS:
            reply += (
                "\n[End saved evidence excerpt]\nResult exceeds the saved excerpt; omitted source is not shown. "
                "Use read_file/read_diff with a narrower line range or a more specific search "
                "to record the required evidence."
            )
            if observation.excerpt_line_range:
                requested_start, end = observation.line_range or (1, 0)
                end = end or 2**63 - 1
                focus = self._saved_read_focus(observation.path)
                if focus is not None and saved_end < focus <= end:
                    reply += (
                        f"\nNavigation only: Saved search location {observation.path}:{focus} is outside this excerpt; "
                        f"read_file start={max(1, requested_start, focus - 3)} end={min(end, focus + 8)} to record it."
                    )
        return reply[:_TOOL_RESULT_CHARS]

    def _saved_read_excerpt(self, text: str, args: dict[str, Any]) -> tuple[str, tuple[int, int]]:
        # Read arguments, digest and the 6000-character tool cap stay intact.
        # Within that returned source, prefer an already saved positive hit
        # when the first 1200 characters would omit it. Never fetch extra text.
        first = max(1, int(args.get("start") or 1))
        lines = text.splitlines(keepends=True)
        excerpt = text[:_OBS_EXCERPT_CHARS]
        path = str(args.get("path") or "")
        focus = self._saved_read_focus(path)
        if self._protocol_version >= 3:
            focus = focus or self._read_focus.get(path)
        offset = focus - first if focus is not None else -1
        if 0 <= offset < len(lines) and len("".join(lines[: offset + 1])) > _OBS_EXCERPT_CHARS:
            begin = max(0, offset - 3)
            # A very long preceding line must not displace the known hit again.
            if len("".join(lines[begin:offset])) >= _OBS_EXCERPT_CHARS:
                begin = offset
            excerpt = "".join(lines[begin : offset + 9])[:_OBS_EXCERPT_CHARS]
            first += begin
        return excerpt, (first, first + len(excerpt.splitlines()) - 1)

    def _prepare_read_focus(self, hypothesis: Hypothesis, pack: ContextPack) -> None:
        self._read_focus = {}
        units = (
            context_units(hypothesis, self._changeset, pack) if self._protocol_version >= 2 else [hypothesis.unit_id]
        )
        for unit_id in units:
            context = pack.units.get(unit_id)
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
        focus = self._saved_read_focus(path) or self._read_focus.get(path)
        if focus is None:
            return args
        return {**args, "start": max(1, focus - 3), "end": focus + 8}

    def _saved_read_focus(self, path: str) -> int | None:
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
                    return int(match[2])
        return None

    def _system_prompt(self) -> str:
        name = (
            "investigator_v3"
            if self._protocol_version >= 3
            else "investigator_v2"
            if self._protocol_version >= 2
            else "investigator"
        )
        prompt = load_prompt(name, output_language=_language_directive(self._output_language))
        if self._protocol_version >= 4:
            prompt += "\n\n" + load_prompt("contract_search_v4")
        return prompt

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
        context_chars = _CONTEXT_CHARS
        basis_navigation = ""
        if self._protocol_version >= 3:
            if hypothesis.discovery_basis:
                basis_navigation = json.dumps(hypothesis.discovery_basis, ensure_ascii=False)[:2_000]
            context_chars = max(0, context_chars - len(self._render_observations()) - len(basis_navigation))
            diff_section = "Before/after hunk requested as recorded evidence; inspect its saved status and excerpts."
            if hypothesis.change_anchor is not None:
                diff_section = (
                    "This is a verified rename without a text hunk. Read the recorded read_change evidence "
                    "and actual consumer source; identical file contents do not establish rename safety."
                )
        context = (
            render_investigation_context(hypothesis, self._changeset, pack, max_chars=context_chars)
            if self._protocol_version >= 2
            else pack.render_for_unit(hypothesis.unit_id, max_chars=12_000)
        )
        candidate = hypothesis.to_dict()
        if self._protocol_version >= 3:
            # Evidence is delivered once through code-written saved references.
            candidate.pop("observations", None)
            candidate.pop("investigation_progress", None)
            candidate.pop("discovery_basis", None)
            if hypothesis.change_anchor is not None:
                candidate["change_anchor"] = {"id": hypothesis.change_anchor.id, "path": hypothesis.change_anchor.path}
        hypothesis_body = json.dumps(candidate, ensure_ascii=False, indent=2)
        sections = [
            "## Hypothesis\n" + hypothesis_body,
            "## Diff hunk(s)\n" + diff_section,
            "## Context\n" + (context or "（无）/(none)"),
            defect_scope_guidance(),
        ]
        if basis_navigation:
            sections.append("## Discovery proof plan (navigation only; not saved evidence)\n" + basis_navigation)
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
        units = (
            context_units(hypothesis, self._changeset, pack) if self._protocol_version >= 2 else [hypothesis.unit_id]
        )
        if any(has_state_navigation(pack, unit_id) for unit_id in units):
            sections.append("## Verification guidance\n" + state_guidance())
        return "\n\n".join(sections)

    async def _bootstrap(self, hypothesis: Hypothesis, pack: ContextPack) -> bool:
        """Replace the first tool round with the known diff and actual anchor.

        These are real executor reads, not Context promotion. Two anchor reads
        and at most one validated trigger-location read replace the first round.
        """
        if hypothesis.change_anchor is not None:
            change = hypothesis.change_anchor
            await self._run_tool("read_change", {"path": change.path})
            location = self._discovery_trigger_location(hypothesis, pack)
            path, line = location if location is not None else (change.path, 1)
            await self._run_tool("read_file", {"path": path, "start": max(1, line - 3), "end": line + 12})
            calls = 2
            if path != change.path:
                await self._run_tool("read_file", {"path": change.path, "start": 1, "end": 16})
                calls += 1
            self._progress.append({"phase": "bootstrap", "steps": 1, "tool_calls": calls, "surface": "structural"})
            return True
        declared = (
            next((unit for unit in self._changeset.units if unit.id == hypothesis.unit_id), None)
            if self._changeset is not None
            else None
        )
        primary = min(
            hypothesis.sites,
            key=lambda site: (declared is None or site.path != declared.path, site.path, site.line),
        )
        start, end = max(1, primary.line - 3), primary.line + 8
        if self._changeset is not None:
            enclosing = [
                unit
                for unit in self._changeset.units
                if unit.path == primary.path and unit.start_line <= primary.line <= unit.end_line
            ]
            if enclosing:
                unit = min(enclosing, key=lambda unit: (unit.end_line - unit.start_line, unit.id))
                if unit.end_line - unit.start_line <= 30:
                    start, end = max(1, unit.start_line - 1), unit.end_line + 1
        self._read_focus[primary.path] = primary.line
        await self._run_tool("read_diff", {"path": primary.path, "start": primary.line, "end": primary.line})
        await self._run_tool("read_file", {"path": primary.path, "start": start, "end": end})
        tool_calls = 2
        location = self._discovery_trigger_location(hypothesis, pack)
        if location is not None:
            path, line = location
            already_saved = any(
                obs.tool == "read_file"
                and obs.status == "success"
                and obs.path == path
                and obs.excerpt_line_range is not None
                and obs.excerpt_line_range[0] <= line <= obs.excerpt_line_range[1]
                for obs in self._observations
            )
            if not already_saved:
                self._progress.append({"phase": "trigger-navigation", "path": path, "line": line})
                await self._run_tool("read_file", {"path": path, "start": max(1, line - 3), "end": line + 8})
                tool_calls += 1
        self._progress.append({"phase": "bootstrap", "steps": 1, "tool_calls": tool_calls})
        return True

    def _discovery_trigger_location(self, hypothesis: Hypothesis, pack: ContextPack) -> tuple[str, int] | None:
        basis = hypothesis.discovery_basis.get("trigger_basis")
        if not isinstance(basis, dict):
            return None
        path, line, quote = basis.get("path"), basis.get("line"), basis.get("excerpt")
        if (
            not isinstance(path, str)
            or type(line) is not int
            or line <= 0
            or not isinstance(quote, str)
            or not quote.strip()
        ):
            return None
        if basis.get("side") == "after" and self._state is not None:
            if any(
                number == line and quote in content
                for number, content in iter_right_lines((self._state.file_diffs or {}).get(path, "") or "")
            ):
                return path, line
        if basis.get("side") == "context" and self._state is not None:
            for context in pack.units.values():
                for slice_ in context.slices:
                    if (
                        slice_.path != path
                        or slice_.sha != self._state.head_sha
                        or not slice_.start_line <= line <= slice_.end_line
                    ):
                        continue
                    lines = slice_.text.splitlines()
                    offset = line - slice_.start_line
                    if offset < len(lines) and quote in lines[offset]:
                        return path, line
        return None

    def _model_progress(self, content: str, known_tools: dict[str, StructuredTool], step: int) -> dict[str, Any] | None:
        parsed = extract_json_value(content, required_key="progress", allow_list=False)
        if not isinstance(parsed, dict):
            return None
        value = parsed["progress"]
        if not isinstance(value, dict) or value.get("missing_premise") not in {
            "trigger",
            "mechanism",
            "impact",
            "regression",
            "contract",
        }:
            raise ValueError("progress requires one missing proof premise")
        question = value.get("question")
        if not isinstance(question, str) or not question.strip() or len(question) > 600:
            raise ValueError("progress requires a bounded evidence question")
        navigation = value.get("next_evidence")
        if not isinstance(navigation, dict) or navigation.get("tool") not in known_tools:
            raise ValueError("progress requires a read-only next evidence tool")
        args = navigation.get("args")
        if not isinstance(args, dict):
            raise ValueError("progress requires tool arguments")
        known_tools[navigation["tool"]].args_schema.model_validate(args)
        record = {
            "phase": "proof-duty",
            "step": step,
            "missing_premise": value["missing_premise"],
            "question": question.strip(),
            "next_evidence": {"tool": navigation["tool"], "args": args},
        }
        self._progress.append(record)
        return record

    async def _follow_caller(self, observation: Observation) -> str:
        """Read one concrete production call hit, never an inferred contract."""
        if observation.tool != "find_callers" or observation.status != "success":
            return ""
        try:
            symbol = json.loads(observation.query.partition(" ")[2])["symbol"]
        except (ValueError, KeyError, TypeError):
            return ""
        calls = []
        for line in observation.excerpt.splitlines():
            match = re.match(r"^- (.+?):(\d+):\s*(.*)$", line)
            if not match or not re.search(rf"\b{re.escape(symbol)}\s*\(", match[3]):
                continue
            path, number, source = match[1], int(match[2]), match[3]
            if source.lstrip().startswith(("//", "#", "/*", "*")):
                continue
            if re.match(r"\s*" + re.escape(symbol) + r"\s*\(", source):
                # A naked multi-line signature is common in Go interfaces.
                # Only a complete single-line bare call is safe to auto-follow;
                # leave ambiguous hits available for explicit model navigation.
                if ")" not in source or re.search(r"\)\s*(?::|[A-Za-z_{])", source):
                    continue
            if re.search(r"(?:^|/)(?:tests?|__tests__|testdata)(?:/|$)|(?:_test|\.test|\.spec)\.", path):
                continue
            if re.match(
                r"\s*(?:(?:async|static|public|private|protected|override|export)\s+)*(?:def|func|function)\b", source
            ):
                continue
            if source.rstrip().endswith(";") and re.match(r"\s*[\w<>,\[\].?]+\s+" + re.escape(symbol), source):
                continue
            already_read = any(
                obs.tool == "read_file"
                and obs.status == "success"
                and obs.path == path
                and obs.excerpt_line_range is not None
                and obs.excerpt_line_range[0] <= number <= obs.excerpt_line_range[1]
                for obs in self._observations
            )
            if not already_read and number > 0:
                calls.append((path, number))
        if not calls:
            return ""
        path, number = calls[0]
        self._progress.append(
            {"phase": "caller-followup", "from_observation": observation.id, "path": path, "line": number}
        )
        result = await self._run_tool("read_file", {"path": path, "start": max(1, number - 8), "end": number + 12})
        return "Known production caller read as recorded evidence:\n" + result

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
        """Keep each v3 request attempt inside the original task allowance."""
        if self._protocol_version < 3:
            return await self._investigate(hypothesis, state, pack, changed_paths=changed_paths)
        from reviewforge.engine.request_budget import investigation_request_scope

        steps = self._max_steps if self._max_steps is not None else budget_steps(hypothesis)
        self._unreported_tokens = 0
        with investigation_request_scope(steps * 4_000) as task_budget:
            self._task_request_budget = task_budget
            try:
                result = await self._investigate(hypothesis, state, pack, changed_paths=changed_paths)
            finally:
                self._unreported_tokens = int(task_budget.snapshot()["unreported_reserved_tokens"])
                self._task_request_budget = None
            result.unreported_tokens = self._unreported_tokens
            if self._unreported_tokens:
                result.progress.append(
                    {"phase": "budget", "event": "unreported-attempt-usage", "tokens": self._unreported_tokens}
                )
            return result

    def _accounted_tokens(self) -> int:
        unknown = self._unreported_tokens
        if self._task_request_budget is not None:
            unknown = int(self._task_request_budget.snapshot()["unreported_reserved_tokens"])
        return self._tokens + unknown

    async def _investigate(
        self,
        hypothesis: Hypothesis,
        state: StateStore,
        pack: ContextPack,
        *,
        changed_paths: set[str] | None = None,
    ) -> InvestigationResult:
        """Answer one hypothesis's open_question within its budget_steps."""

        self._state = state
        self._closure_constraints = self._closing_contract_constraints(hypothesis, state, pack)
        self._prepare_read_focus(hypothesis, pack)
        self._observations = [
            obs for obs in hypothesis.observations if self._protocol_version < 3 or obs.sha == state.head_sha
        ]
        self._progress = list(hypothesis.investigation_progress) if self._protocol_version >= 3 else []
        self._tokens = 0
        self._input_overhead = 0
        self._tool_counts = {}
        self._obs_counter = max(
            (int(obs.id[4:]) + 1 for obs in self._observations if obs.id.startswith("obs_") and obs.id[4:].isdigit()),
            default=0,
        )
        if hypothesis.change_anchor is not None and (
            self._protocol_version < 3
            or state.file_changes.get(hypothesis.change_anchor.path) != hypothesis.change_anchor
            or not is_verified_rename(hypothesis.change_anchor, state.head_sha)
        ):
            return self._result(
                verdict="unknown",
                reason="structural change provenance mismatch",
                severity=hypothesis.severity,
                steps=0,
                outcome="tool-error",
            )
        changed = changed_paths if changed_paths is not None else _changed_paths(state)
        steps = self._max_steps if self._max_steps is not None else budget_steps(hypothesis)
        token_limit = steps * 4_000
        first_step = 0
        if self._protocol_version >= 3:
            first_step = int(await self._bootstrap(hypothesis, pack))

        chat = (
            []
            if self._protocol_version >= 4
            else [
                SystemMessage(content=self._system_prompt()),
                HumanMessage(content=self._render_user(hypothesis, state, pack)),
            ]
        )
        initial = list(chat)
        if self._protocol_version == 3:
            chat.append(HumanMessage(content="## Recorded observations\n" + self._render_observations()))
        tools = self._build_tools()
        known_tools = {tool.name: tool for tool in tools}
        bound = self._llm.bind_tools(tools)
        schema_tokens = self._estimate_text_tokens(
            json.dumps(
                [{"name": tool.name, "description": tool.description, "parameters": tool.args} for tool in tools],
                ensure_ascii=False,
            )
        )

        for step in range(first_step, steps):
            if self._protocol_version >= 4:
                # The saved evidence is the state, not the expanding tool chat.
                # Preserve every Observation while discarding duplicate Context
                # bodies and the previous model's speculative transcript.
                chat = self._exploration_chat(hypothesis, pack)
            closing = self._closing_chat(initial, token_limit, hypothesis=hypothesis)
            # A tool round is useful only if we can still ask for a verdict.
            # Reserve the closing input and output within the SAME budget,
            # before spending on more tools. The output reserve is capped at
            # a quarter of the total budget so small budgets can collect facts.
            remaining = token_limit - self._accounted_tokens()
            closing_output = min(4_000, token_limit // 4)
            if self._protocol_version >= 3:
                closer = self._closing_llm if self._closing_llm is not None else self._llm
                closing_plan = plan_request(
                    closer,
                    closing,
                    max_output_tokens=closing_output,
                    fallback_available=remaining,
                )
                reserve = closing_plan.input_tokens + closing_plan.max_output_tokens
                exploration_plan = plan_request(
                    bound,
                    chat,
                    fallback_available=remaining,
                    reserve_tokens=reserve,
                )
                output_tokens = exploration_plan.max_output_tokens
                self._progress.append(
                    {
                        "phase": "budget",
                        "event": "request-plan",
                        "stage": "exploration",
                        "input_tokens": exploration_plan.input_tokens,
                        "available_tokens": exploration_plan.available_tokens,
                        "max_output_tokens": output_tokens,
                        "closing_reserve_tokens": reserve,
                    }
                )
            else:
                input_tokens = self._estimate_input_tokens(chat) + schema_tokens + self._input_overhead
                closing_tokens = self._estimate_input_tokens(closing) + self._input_overhead
                output_tokens = min(4_000, remaining - input_tokens - closing_tokens - closing_output)
            if output_tokens < (_MIN_USEFUL_OUTPUT if self._protocol_version >= 3 else 1):
                if self._protocol_version >= 3:
                    self._progress.append(
                        {"phase": "budget", "event": "close-before-small-output", "output_tokens": output_tokens}
                    )
                return await self._close(closing, hypothesis, changed, token_limit=token_limit, steps=step)
            try:
                response = await bound.ainvoke(chat, max_tokens=output_tokens)
            except Exception as exc:
                if getattr(exc, "outcome", "") == "budget-exhausted":
                    return self._result(
                        verdict="unknown",
                        reason="run-budget-exhausted",
                        severity=hypothesis.severity,
                        steps=step,
                        outcome="budget-exhausted",
                    )
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
            if self._accounted_tokens() > token_limit:
                return self._result(
                    verdict="unknown",
                    reason="token-exhausted",
                    severity=hypothesis.severity,
                    steps=step + 1,
                    outcome="budget-exhausted",
                )
            tool_calls = getattr(response, "tool_calls", None) or []
            if self._protocol_version >= 3:
                content = getattr(response, "content", "") or ""
                proof = self._parse_verdict(content)
                if proof is not None and (
                    proof.get("assessment") is not None or proof.get("counterevidence") is not None
                ):
                    return self._finalize(proof, hypothesis, changed, steps=step + 1)
                try:
                    progress = self._model_progress(content, known_tools, step + 1)
                except (ValueError, TypeError):
                    return self._invalid_output("invalid-progress", hypothesis, step + 1)
                if not tool_calls and progress is not None:
                    navigation = progress["next_evidence"]
                    args = (
                        self._focus_read_args(navigation["args"])
                        if navigation["tool"] == "read_file"
                        else navigation["args"]
                    )
                    if any(obs.query == _query_string(navigation["tool"], args) for obs in self._observations):
                        self._progress.append({"phase": "budget", "event": "close-after-repeated-evidence"})
                        return await self._close(
                            self._closing_chat(initial, token_limit, hypothesis=hypothesis),
                            hypothesis,
                            changed,
                            token_limit=token_limit,
                            steps=step + 1,
                        )
                    reply = await self._run_tool(navigation["tool"], navigation["args"])
                    chat.append(HumanMessage(content=reply))
                    follow = await self._follow_caller(self._observations[-1])
                    if follow:
                        chat.append(HumanMessage(content=follow))
                    continue
            if not tool_calls:
                parsed = self._parse_verdict(getattr(response, "content", "") or "")
                if parsed is not None:
                    return self._finalize(parsed, hypothesis, changed, steps=step + 1)
                return await self._close(
                    self._closing_chat(initial, token_limit, hypothesis=hypothesis),
                    hypothesis,
                    changed,
                    token_limit=token_limit,
                    steps=step + 1,
                )
            if self._accounted_tokens() >= token_limit:
                return self._result(
                    verdict="unknown",
                    reason="token-exhausted",
                    severity=hypothesis.severity,
                    steps=step + 1,
                    outcome="budget-exhausted",
                )
            followups = []
            before_observations = len(self._observations)
            for tool_call in tool_calls:
                name = str(tool_call.get("name", ""))
                args = tool_call.get("args", {}) or {}
                if name not in known_tools:
                    result = f"Unknown tool: {name}"
                else:
                    result = await self._run_tool(name, args)
                chat.append(ToolMessage(content=result, tool_call_id=tool_call.get("id", "")))
                if self._protocol_version >= 3 and name == "find_callers" and self._observations:
                    followups.append(self._observations[-1])
            if followups:
                # At most one deterministic caller read per original tool round.
                for observation in followups:
                    follow = await self._follow_caller(observation)
                    if follow:
                        chat.append(HumanMessage(content=follow))
                        break
            if self._protocol_version >= 3 and len(self._observations) == before_observations:
                self._progress.append({"phase": "budget", "event": "close-after-repeated-evidence"})
                return await self._close(
                    self._closing_chat(initial, token_limit, hypothesis=hypothesis),
                    hypothesis,
                    changed,
                    token_limit=token_limit,
                    steps=step + 1,
                )

        return await self._close(
            self._closing_chat(initial, token_limit, hypothesis=hypothesis),
            hypothesis,
            changed,
            token_limit=token_limit,
            steps=steps,
        )

    def _render_observations(self) -> str:
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
                "excerpt_line_range": obs.excerpt_line_range,
                "status": obs.status,
                **({"shared_result_id": obs.shared_result_id} if obs.shared_result_id else {}),
                **(
                    {"navigation_only": item}
                    if (
                        item := next(
                            (
                                entry
                                for entry in self._progress
                                if entry.get("phase") == "diff-navigation" and entry.get("observation_id") == obs.id
                            ),
                            None,
                        )
                    )
                    is not None
                    else {}
                ),
                **(
                    {"evidence": {reference: citation.quote for reference, citation in _evidence_segments(obs).items()}}
                    if obs.status == "success"
                    else {"excerpt": obs.excerpt}
                ),
            }
            for obs in self._observations
        ]
        return json.dumps(observations, ensure_ascii=False)

    def _closing_contract_constraints(self, hypothesis: Hypothesis, state: StateStore, pack: ContextPack) -> list[str]:
        """Keep applicable contract rules, never initial Context or author intent."""
        constraints = []
        boundary = self._resource_boundary(hypothesis)
        if boundary:
            constraints.append(boundary)
        unit = (
            next((unit for unit in self._changeset.units if unit.id == hypothesis.unit_id), None)
            if self._changeset
            else None
        )
        if hypothesis.mechanism is Mechanism.I18N or (
            unit is not None and unit.kind is UnitKind.RESOURCE and is_localization_path(unit.path)
        ):
            constraints.append(localization_guidance())
        paths = {site.path for site in hypothesis.sites}
        if unit is not None:
            paths.add(unit.path)
        if any(has_python_concurrency(path, (state.file_diffs or {}).get(path, "")) for path in paths):
            constraints.append(python_concurrency_guidance())
        units = (
            context_units(hypothesis, self._changeset, pack) if self._protocol_version >= 2 else [hypothesis.unit_id]
        )
        if any(has_state_navigation(pack, unit_id) for unit_id in units):
            constraints.append(state_guidance())
        return constraints

    def _evidence_packet(self, hypothesis: Hypothesis) -> dict[str, Any]:
        """One exact saved-evidence representation for exploration and closure."""
        candidate = {
            key: getattr(hypothesis, key)
            for key in (
                "id",
                "identity",
                "unit_id",
                "claim",
                "trigger",
                "impact",
                "open_question",
                "refutation",
                "severity",
            )
        }
        candidate["mechanism"] = hypothesis.mechanism.value
        if hypothesis.discovery_basis.get("violation"):
            candidate["proposed_violation"] = hypothesis.discovery_basis["violation"]
        candidate["sites"] = [
            {"path": site.path, "line": site.line, "excerpt": site.excerpt} for site in hypothesis.sites
        ]
        if hypothesis.change_anchor is not None:
            candidate["change_anchor"] = {
                "id": hypothesis.change_anchor.id,
                "path": hypothesis.change_anchor.path,
                "previous_path": hypothesis.change_anchor.previous_path,
            }
        observations = []
        for observation in self._observations:
            record = {"id": observation.id, "tool": observation.tool, "status": observation.status}
            if observation.path:
                record["path"] = observation.path
            if observation.status == "success":
                record.update(
                    {
                        "sha": observation.sha,
                        "line_range": observation.excerpt_line_range or observation.line_range,
                        "evidence": {
                            reference: citation.quote for reference, citation in _evidence_segments(observation).items()
                        },
                    }
                )
                if any(
                    entry.get("phase") == "diff-navigation" and entry.get("observation_id") == observation.id
                    for entry in self._progress
                ):
                    record["source_view"] = "partial diff excerpt; not a complete hunk"
            observations.append(record)
        return {
            "head_sha": self._state.head_sha if self._state is not None else "",
            "candidate": candidate,
            "contract_constraints": self._closure_constraints,
            "observations": observations,
        }

    def _exploration_chat(self, hypothesis: Hypothesis, pack: ContextPack) -> list[Any]:
        from reviewforge.engine.exploration_context import context_navigation

        packet = self._evidence_packet(hypothesis)
        packet["review_scope"] = defect_scope_guidance()
        for record, observation in zip(packet["observations"], self._observations, strict=True):
            # Queries preserve both positive navigation and failed/repeated-read
            # state. Only successful named evidence remains citable.
            record["query"] = observation.query
        packet["context_navigation"] = context_navigation(
            hypothesis,
            self._changeset,
            pack,
            head_sha=self._state.head_sha if self._state is not None else "",
        )
        packet["proof_duty"] = next(
            (entry for entry in reversed(self._progress) if entry.get("phase") == "proof-duty"), None
        )
        return [
            SystemMessage(content=self._system_prompt()),
            HumanMessage(content=json.dumps(packet, ensure_ascii=False, separators=(",", ":"))),
        ]

    def _closing_chat(self, initial: list[Any], token_limit: int, *, hypothesis: Hypothesis | None = None) -> list[Any]:
        if self._protocol_version >= 3 and hypothesis is not None:
            packet = self._evidence_packet(hypothesis)
            return [
                SystemMessage(
                    content=load_prompt(
                        "investigator_closure_v3", output_language=_language_directive(self._output_language)
                    )
                ),
                HumanMessage(content=json.dumps(packet, ensure_ascii=False, separators=(",", ":"))),
            ]
        return [
            *initial,
            HumanMessage(
                content="## Recorded observations\n"
                + self._render_observations()
                + f"\nRemaining investigation budget: {max(0, token_limit - self._accounted_tokens())} tokens. "
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
        closer = self._closing_llm if self._closing_llm is not None else self._llm
        if self._protocol_version >= 3:
            closing_plan = plan_request(
                closer,
                chat,
                fallback_available=token_limit - self._accounted_tokens(),
            )
            available_output = closing_plan.max_output_tokens
            self._progress.append(
                {
                    "phase": "budget",
                    "event": "request-plan",
                    "stage": "closing",
                    "input_tokens": closing_plan.input_tokens,
                    "available_tokens": closing_plan.available_tokens,
                    "max_output_tokens": available_output,
                }
            )
        else:
            input_tokens = self._estimate_input_tokens(chat) + self._input_overhead
            available_output = token_limit - self._accounted_tokens() - input_tokens
        if available_output < (_MIN_USEFUL_OUTPUT if self._protocol_version >= 3 else 1):
            return self._result(
                verdict="unknown",
                reason="token-exhausted",
                severity=hypothesis.severity,
                steps=steps,
                outcome="budget-exhausted",
            )
        try:
            final = await closer.ainvoke(chat, max_tokens=min(4_000, available_output))
            self._record_tokens(final, chat)
            parsed = self._parse_verdict(getattr(final, "content", "") or "")
        except Exception as exc:
            if getattr(exc, "outcome", "") == "budget-exhausted":
                return self._result(
                    verdict="unknown",
                    reason="run-budget-exhausted",
                    severity=hypothesis.severity,
                    steps=steps,
                    outcome="budget-exhausted",
                )
            logger.warning("investigator final call failed for %s: %s", hypothesis.identity, exc)
            return self._result(
                verdict="unknown", reason="provider error", severity=hypothesis.severity, steps=steps, retryable=True
            )
        if self._accounted_tokens() > token_limit:
            return self._result(
                verdict="unknown",
                reason="token-exhausted",
                severity=hypothesis.severity,
                steps=steps,
                outcome="budget-exhausted",
            )
        if parsed is None:
            return self._invalid_output("step-exhausted", hypothesis, steps)
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
                # The provider adapter replays this opaque assistant field.
                # Count it as variable history, not fixed provider overhead.
                **(
                    {"reasoning_content": message.additional_kwargs["reasoning_content"]}
                    if isinstance(message, AIMessage) and "reasoning_content" in message.additional_kwargs
                    else {}
                ),
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
        elif self._protocol_version < 3:
            self._tokens += self._estimate_input_tokens(chat) + schema_tokens + self._estimate_input_tokens([response])

    @staticmethod
    def _parse_verdict(content: str) -> dict[str, Any] | None:
        parsed = extract_json_value(content or "", required_key="assessment", allow_list=False)
        if not isinstance(parsed, dict):
            parsed = extract_json_value(content or "", required_key="counterevidence", allow_list=False)
        if not isinstance(parsed, dict):
            parsed = extract_json_value(content or "", required_key="verdict", allow_list=False)
        return parsed if isinstance(parsed, dict) else None

    def _finalize(
        self, parsed: dict[str, Any], hypothesis: Hypothesis, changed: set[str], *, steps: int
    ) -> InvestigationResult:
        if self._protocol_version >= 2 and parsed.get("assessment") is not None:
            raw = parsed["assessment"]
            if not isinstance(raw, dict) or raw.get("comparison") not in {"conflict", "compatible", "unresolved"}:
                return self._invalid_output("incomplete-assessment", hypothesis, steps)
        if self._protocol_version >= 2 and parsed.get("counterevidence") is not None:
            return self._finalize_counterevidence(parsed, hypothesis, changed, steps=steps)
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
        validation_failed = False
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
                validation_failed = True
                evidence_ids, evidence_quote = [], ""
            else:
                try:
                    assessment = self._assessment_from_output(parsed.get("assessment"))
                except _UnknownEvidenceReferenceError:
                    verdict, reason = "unknown", "ungrounded-assessment"
                    validation_failed = True
                except ValueError:
                    verdict, reason = "unknown", "incomplete-assessment"
                    validation_failed = True
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
                        validation_failed = True
                    elif assessment.comparison != required:
                        verdict, reason = "unknown", "inconsistent-assessment"
                        validation_failed = True
                    elif (
                        self._protocol_version >= 3
                        and hypothesis.change_anchor is not None
                        and verdict == "confirmed"
                        and not any(
                            by_id[citation.observation_id].tool in {"read_file", "read_diff"}
                            for citation in assessment.actual_evidence
                        )
                    ):
                        # A rename and an old-name consumer do not establish
                        # actual resolution failure. This is a proof-shape
                        # gate, not a semantic guarantee or absence inference.
                        verdict, reason = "unknown", "structural-actual-needs-source-evidence"
                        validation_failed = True
                    else:
                        evidence_ids = list(dict.fromkeys(citation.observation_id for citation in proof))
                        evidence_quote = assessment.actual_evidence[0].quote
                        outside_diff = any(
                            self._quote_outside_diff(by_id[citation.observation_id], citation.quote, changed)
                            for citation in proof
                        )
                        strength = (
                            "strong"
                            if (
                                outside_diff
                                or (self._protocol_version < 3 and hypothesis.source.startswith("detector"))
                            )
                            else "weak"
                        )

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
            outcome="output-error" if validation_failed else "",
        )

    def _invalid_output(self, reason: str, hypothesis: Hypothesis, steps: int) -> InvestigationResult:
        return self._result(
            verdict="unknown", reason=reason, severity=hypothesis.severity, steps=steps, outcome="output-error"
        )

    def _finalize_counterevidence(
        self, parsed: dict[str, Any], hypothesis: Hypothesis, changed: set[str], *, steps: int
    ) -> InvestigationResult:
        # An explicit historical UNKNOWN is never silently upgraded.
        if str(parsed.get("verdict", "")).strip().lower() == "unknown":
            return self._result(
                verdict="unknown", reason=str(parsed.get("reason", "")), severity=hypothesis.severity, steps=steps
            )
        if parsed.get("assessment") is not None or parsed.get("verdict", "refuted") != "refuted":
            return self._invalid_output("inconsistent-assessment", hypothesis, steps)
        try:
            raw = parsed["counterevidence"]
            if not isinstance(raw, dict):
                raise ValueError("counterevidence must be an object")
            # Resolve IDs using the identical two-premise citation converter.
            proof = self._assessment_from_output(
                {
                    "expected": raw.get("statement"),
                    "actual": raw.get("statement"),
                    "comparison": "compatible",
                    "expected_evidence": raw.get("evidence"),
                    "actual_evidence": raw.get("evidence"),
                }
            ).actual_evidence
            counter = CounterEvidence.from_dict({**raw, "evidence": [vars(citation) for citation in proof]})
        except _UnknownEvidenceReferenceError:
            return self._invalid_output("ungrounded-assessment", hypothesis, steps)
        except (ValueError, TypeError):
            return self._invalid_output("incomplete-assessment", hypothesis, steps)
        by_id = {observation.id: observation for observation in self._observations}
        if any(
            citation.observation_id not in by_id
            or by_id[citation.observation_id].status != "success"
            or citation.quote not in by_id[citation.observation_id].excerpt
            for citation in proof
        ):
            return self._invalid_output("ungrounded-assessment", hypothesis, steps)
        # Explicit old quote fields remain enforceable, even with a valid new proof.
        if "evidence_ids" in parsed or "evidence_quote" in parsed:
            quote = parsed.get("evidence_quote")
            ids = parsed.get("evidence_ids")
            if (
                not isinstance(quote, str)
                or not isinstance(ids, list)
                or not any(
                    identity in by_id
                    and by_id[identity].status == "success"
                    and quote
                    and quote in by_id[identity].excerpt
                    for identity in ids
                    if isinstance(identity, str)
                )
            ):
                return self._invalid_output("ungrounded", hypothesis, steps)
        severity = parsed.get("severity", hypothesis.severity)
        if severity not in _SEVERITY_RANK:
            severity = hypothesis.severity
        outside = any(
            self._quote_outside_diff(by_id[citation.observation_id], citation.quote, changed) for citation in proof
        )
        return self._result(
            verdict="refuted",
            answer=str(parsed.get("answer", "")),
            reason=str(parsed.get("reason", "")),
            severity=severity,
            evidence_ids=list(dict.fromkeys(citation.observation_id for citation in proof)),
            evidence_quote=proof[0].quote,
            strength="strong" if outside else "weak",
            steps=steps,
            counterevidence=counter,
            additional_sites=_parse_additional_sites(parsed.get("additional_sites"), self._state),
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
        counterevidence: CounterEvidence | None = None,
        outcome: str = "",
    ) -> InvestigationResult:
        if not outcome:
            if verdict != "unknown":
                outcome = "resolved"
            elif retryable:
                outcome = "provider-error"
            elif any(observation.status == "error" for observation in self._observations):
                outcome = "tool-error"
            else:
                outcome = "insufficient-evidence"
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
            counterevidence=counterevidence,
            outcome=outcome,
            progress=list(self._progress),
            unreported_tokens=self._unreported_tokens,
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
        token_budget: int | None = None,
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
        tasks = (
            build_investigation_tasks(targets, head_sha=ledger.head_sha, workspace_digest=ledger.workspace_digest)
            if ledger.protocol_version >= 2
            else [InvestigationTask(item.identity, item, (item.identity,)) for item in targets]
        )
        evidence_cache = None
        if ledger.protocol_version >= 3:
            if ledger.head_sha != state.head_sha or (
                pack.workspace_digest and ledger.workspace_digest != pack.workspace_digest
            ):
                raise ValueError("investigation ledger/context does not match the fixed head workspace")
            tasks = fair_investigation_tasks(tasks)
            evidence_cache = SharedEvidenceCache(EvidenceScope(ledger.run_id, ledger.head_sha, ledger.workspace_digest))
        semaphore = asyncio.Semaphore(max(1, concurrency))
        available_tokens = None if token_budget is None else max(0, token_budget)

        async def _investigate(index: int, hypothesis: Hypothesis) -> InvestigationResult:
            nonlocal available_tokens
            if index >= max(0, max_hypotheses_per_pr):
                return self._result(
                    verdict="unknown",
                    reason="budget-exhausted",
                    severity=hypothesis.severity,
                    outcome="policy-deferred" if ledger.protocol_version >= 2 else "budget-exhausted",
                )
            async with semaphore:
                reserve = (self._max_steps if self._max_steps is not None else budget_steps(hypothesis)) * 4_000
                if available_tokens is not None:
                    if available_tokens < reserve:
                        return self._result(
                            verdict="unknown",
                            reason="run-budget-exhausted",
                            severity=hypothesis.severity,
                            outcome="policy-deferred",
                        )
                    available_tokens -= reserve
                worker = Investigator(
                    self._llm,
                    self._executor,
                    output_language=self._output_language,
                    max_steps=self._max_steps,
                    changeset=self._changeset,
                    protocol_version=ledger.protocol_version,
                    closing_llm=self._closing_llm,
                    evidence_cache=evidence_cache,
                )
                try:
                    result = await worker.investigate(hypothesis, state, pack, changed_paths=changed)
                except Exception as exc:
                    logger.warning("investigation failed for %s: %s", hypothesis.identity, exc)
                    result = worker._result(
                        verdict="unknown",
                        reason=f"investigation error: {exc}",
                        severity=hypothesis.severity,
                        retryable=True,
                        outcome="output-error",
                    )
                if available_tokens is not None:
                    available_tokens += reserve - result.tokens - result.unreported_tokens
                return result

        async def complete(index: int, task: InvestigationTask) -> InvestigationResult:
            hypothesis = task.hypothesis
            result = await _investigate(index, hypothesis)
            verdict = dict(
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
                counterevidence=result.counterevidence,
                investigation_outcome=result.outcome,
                **({"investigation_progress": result.progress} if ledger.protocol_version >= 3 else {}),
                **(
                    {"investigation_unreported_tokens": result.unreported_tokens}
                    if ledger.protocol_version >= 3
                    else {}
                ),
            )
            if ledger.protocol_version >= 2:
                ledger.apply_task_verdict(task.id, task.members, hypothesis.identity, **verdict)
            else:
                ledger.apply_verdict(hypothesis.identity, **verdict)
            if on_update is not None:
                await on_update(ledger)
            return result

        jobs = [asyncio.create_task(complete(index, task)) for index, task in enumerate(tasks)]
        try:
            return list(await asyncio.gather(*jobs))
        finally:
            for job in jobs:
                if not job.done():
                    job.cancel()
            await asyncio.gather(*jobs, return_exceptions=True)
            if evidence_cache is not None:
                await evidence_cache.close()


__all__ = ["InvestigationResult", "Investigator", "build_workspace_executor", "budget_steps"]
