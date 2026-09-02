"""Deterministic, repository-aware context for the hypothesis pipeline.

``ContextPack`` is deliberately independent from the workspace implementation.
The workspace supplied by T2 exposes a small read-only interface, and this
module only relies on that interface (``read``, ``grep``, ``find_callers`` and
``find_symbol_definitions``).  Keeping the pack builder synchronous and
duck-typed makes the deterministic collection rules easy to test before the
pipeline is wired into the orchestrator.

The pack is not a second discovery engine.  It starts with the semantic units
and calls out to the immutable PR-head workspace for the bounded slices which
let a later model decide whether a change is correct.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

from reviewforge.engine.semantic_diff import SemanticChangeSet, SemanticUnit, UnitKind
from reviewforge.engine.symbol_extractor import extract_definitions

_DEFAULT_MAX_SLICES = 12
_DEFAULT_MAX_SLICE_LINES = 60
_DEFAULT_MAX_CHARS = 40_000
_DEFAULT_MAX_CALLERS = 4
_PR_INTENT_MAX_CHARS = 2_000

# Collection order is part of the deterministic contract.  In particular, do
# not alphabetise these kinds: later kinds are the first to be marked as
# truncated when the per-unit slice budget is exhausted.
_KIND_ORDER = (
    "caller",
    "callee",
    "base_class",
    "interface",
    "sibling",
    "lock_usage",
    "field_usage",
    "test",
    "schema",
    "config",
)

_CLASS_DECLARATION = re.compile(
    r"^\s*(?:(?:export|default|abstract|declare|public|private|protected|internal|final)\s+)*"
    r"class\s+(?P<name>[A-Za-z_$][\w$]*)(?P<tail>[^\{\n]*)",
    re.MULTILINE,
)
_PYTHON_CLASS = re.compile(
    r"^\s*class\s+(?P<name>[A-Za-z_]\w*)\s*(?:\((?P<bases>[^)]*)\))?\s*:",
    re.MULTILINE,
)
_RUBY_CLASS = re.compile(r"^\s*class\s+(?P<name>[A-Za-z_]\w*)(?:\s*<\s*(?P<base>[^\s#]+))?", re.MULTILINE)
_GO_TYPE = re.compile(
    r"^\s*type\s+(?P<name>[A-Za-z_]\w*)(?:\s*\[[^\]]+\])?\s+(?P<kind>struct|interface)\s*\{",
    re.MULTILINE,
)
_WORD = re.compile(r"[A-Za-z_$][\w$]*")
_LOCK_TOKEN = re.compile(r"(?i)\b(?:mu|mutex|lock|sync)\b(?:\s*\.\s*[A-Za-z_]\w*)?")
_IDENTIFIER = re.compile(r"[A-Za-z_$][\w$]*")


@dataclass
class ContextSlice:
    """One bounded source excerpt selected for a semantic unit."""

    kind: str
    path: str
    start_line: int
    end_line: int
    symbol: str
    text: str
    reason: str
    sha: str


@dataclass
class UnitContext:
    """All deterministic context collected for one semantic unit."""

    unit_id: str
    slices: list[ContextSlice] = field(default_factory=list)
    truncated_kinds: list[str] = field(default_factory=list)
    pr_intent: str = ""


@dataclass
class ContextPack:
    """A bounded context package shared by the hypothesis generator."""

    units: dict[str, UnitContext] = field(default_factory=dict)
    pr_intent: str = ""
    workspace_digest: str = ""
    _unit_risks: dict[str, float] = field(default_factory=dict, init=False, repr=False, compare=False)

    @classmethod
    def build(
        cls,
        changeset: SemanticChangeSet,
        workspace: Any,
        state: Any | None = None,
        *,
        pr_intent: str | None = None,
        pr_title: str = "",
        pr_body: str = "",
        linked_issues: Iterable[Any] | None = None,
        max_slices: int = _DEFAULT_MAX_SLICES,
        max_slice_lines: int = _DEFAULT_MAX_SLICE_LINES,
        max_callers: int = _DEFAULT_MAX_CALLERS,
    ) -> ContextPack:
        """Build a deterministic pack from semantic units and a PR workspace.

        ``state`` is optional because Phase 1 tests build packs from a small
        semantic changeset.  When provided, its PR metadata is used to render
        ``pr_intent``.  Explicit keyword metadata takes precedence over state
        fields.  No workspace method is called when the workspace reports the
        T2 ``api-fallback`` source: the resulting pack records the complete
        degradation in ``truncated_kinds`` as required by the SPEC.
        """

        # A few callers naturally pass the already-rendered intent as the
        # third positional argument.  Keep that form compatible while the
        # orchestrator uses the richer StateStore form.
        if isinstance(state, str) and pr_intent is None:
            pr_intent, state = state, None

        resolved_intent = _resolve_pr_intent(
            state=state,
            explicit=pr_intent,
            title=pr_title,
            body=pr_body,
            linked_issues=linked_issues,
        )
        pack = cls(
            pr_intent=resolved_intent,
            workspace_digest=_workspace_value(workspace, "digest")
            or _workspace_value(_workspace_value(workspace, "info"), "digest")
            or str(_value(changeset, "head_sha", "") or ""),
        )
        pack_builder = _PackBuilder(
            changeset=changeset,
            workspace=workspace,
            pr_intent=resolved_intent,
            max_slices=max(0, int(max_slices)),
            max_slice_lines=max(1, int(max_slice_lines)),
            max_callers=max(0, int(max_callers)),
        )

        units = _value(changeset, "units", []) or []
        for unit in units:
            unit_id = _unit_id(unit)
            pack.units[unit_id] = pack_builder.build_unit(unit, unit_id)
            pack._unit_risks[unit_id] = _unit_risk(unit)
        return pack

    def render_for_unit(self, unit_id: str, *, max_chars: int) -> str:
        """Render one unit using stable slice headers and a hard char bound."""

        context = self.units.get(unit_id)
        if context is None:
            return ""
        return _render_unit(context, max(0, int(max_chars)))

    def render_all(self, *, max_chars: int) -> str:
        """Render units by descending risk, filling the global char budget."""

        remaining = max(0, int(max_chars))
        rendered: list[str] = []
        ordered = sorted(
            self.units.items(),
            key=lambda item: (-self._unit_risks.get(item[0], 0.0), item[0]),
        )
        for position, (_unit_id, context) in enumerate(ordered):
            if remaining <= 0:
                _mark_truncated(context, 0)
                for _remaining_id, remaining_context in ordered[position + 1 :]:
                    _mark_truncated(remaining_context, 0)
                break
            chunks = _render_unit_parts(context)
            if not chunks:
                continue
            if rendered:
                separator = "\n\n"
                if len(separator) > remaining:
                    rendered.append(separator[:remaining])
                    _mark_truncated(context, 0)
                    for _remaining_id, remaining_context in ordered[position + 1 :]:
                        _mark_truncated(remaining_context, 0)
                    remaining = 0
                    break
                rendered.append(separator)
                remaining -= len(separator)
            header = chunks[0]
            if len(header) > remaining:
                rendered.append(header[:remaining])
                _mark_truncated(context, 0)
                for _remaining_id, remaining_context in ordered[position + 1 :]:
                    _mark_truncated(remaining_context, 0)
                remaining = 0
                break
            rendered.append(header)
            remaining -= len(header)
            for slice_index, chunk in enumerate(chunks[1:]):
                piece = "\n\n" + chunk
                if len(piece) > remaining:
                    rendered.append(piece[:remaining])
                    _mark_truncated(context, slice_index)
                    for _remaining_id, remaining_context in ordered[position + 1 :]:
                        _mark_truncated(remaining_context, 0)
                    remaining = 0
                    break
                rendered.append(piece)
                remaining -= len(piece)
            if remaining <= 0:
                for _remaining_id, remaining_context in ordered[position + 1 :]:
                    _mark_truncated(remaining_context, 0)
                break
        return "".join(rendered)[: max(0, int(max_chars))]


class _PackBuilder:
    """Internal deterministic collector kept separate from the data model."""

    def __init__(
        self,
        *,
        changeset: SemanticChangeSet,
        workspace: Any,
        pr_intent: str,
        max_slices: int,
        max_slice_lines: int,
        max_callers: int,
    ) -> None:
        self.changeset = changeset
        self.workspace = workspace
        self.pr_intent = pr_intent
        self.max_slices = max_slices
        self.max_slice_lines = max_slice_lines
        self.max_callers = max_callers
        self._source_cache: dict[str, str | None] = {}
        self.sha = (
            _workspace_value(workspace, "head_sha")
            or _workspace_value(_workspace_value(workspace, "info"), "head_sha")
            or str(_value(changeset, "head_sha", "") or "")
        )
        self.degraded = _workspace_degraded(workspace)

    def build_unit(self, unit: SemanticUnit, unit_id: str) -> UnitContext:
        context = UnitContext(unit_id=unit_id, pr_intent=self.pr_intent)
        if self.degraded:
            context.truncated_kinds = ["all"]
            return context

        candidates: list[ContextSlice] = []
        candidates.extend(self._collect_callers(unit))
        candidates.extend(self._collect_callees(unit))
        base, interfaces = self._collect_inheritance(unit)
        candidates.extend(base)
        candidates.extend(interfaces)
        candidates.extend(self._collect_siblings(unit))
        lock_usage, field_usage = self._collect_state_usage(unit)
        candidates.extend(lock_usage)
        candidates.extend(field_usage)
        candidates.extend(self._collect_tests(unit))
        schema, config = self._collect_resources(unit)
        candidates.extend(schema)
        candidates.extend(config)

        candidates = _deduplicate_slices(candidates)
        context.slices = candidates[: self.max_slices]
        if len(candidates) > self.max_slices:
            kept = len(context.slices)
            context.truncated_kinds = _ordered_unique(slice_.kind for slice_ in candidates[kept:])
        return context

    def _read(self, path: str) -> str | None:
        path = str(path or "")
        if not path:
            return None
        if path in self._source_cache:
            return self._source_cache[path]
        reader = getattr(self.workspace, "read", None)
        if not callable(reader):
            self._source_cache[path] = None
            return None
        try:
            content = reader(path)
        except Exception:
            content = None
        if content is not None and not isinstance(content, str):
            try:
                content = content.decode("utf-8")
            except (AttributeError, UnicodeDecodeError):
                content = str(content)
        result = content if isinstance(content, str) else None
        self._source_cache[path] = result
        return result

    def _slice_from_source(
        self,
        *,
        kind: str,
        path: str,
        start_line: int,
        end_line: int,
        symbol: str,
        reason: str,
        source: str | None = None,
        fallback_text: str = "",
    ) -> ContextSlice | None:
        start = max(1, int(start_line or 1))
        end = max(start, int(end_line or start))
        content = source if source is not None else self._read(path)
        if content is not None:
            lines = content.splitlines()
            if not lines:
                return None
            if start > len(lines):
                return None
            end = min(end, len(lines))
            clipped_end = min(end, start + self.max_slice_lines - 1)
            text = "\n".join(lines[start - 1 : clipped_end])
            end = clipped_end
        else:
            text = str(fallback_text or "")
            if not text:
                return None
            lines = text.splitlines()
            text = "\n".join(lines[: self.max_slice_lines])
            end = start + max(0, len(text.splitlines()) - 1)
        if not text:
            return None
        return ContextSlice(
            kind=kind,
            path=str(path),
            start_line=start,
            end_line=end,
            symbol=str(symbol or ""),
            text=text,
            reason=str(reason or ""),
            sha=self.sha,
        )

    def _collect_callers(self, unit: SemanticUnit) -> list[ContextSlice]:
        symbol = str(_value(unit, "symbol", "") or "")
        if not symbol or self.max_callers <= 0:
            return []
        hits = _workspace_call(
            self.workspace,
            "find_callers",
            symbol,
            language=str(_value(unit, "language", "") or ""),
            max_hits=self.max_callers,
        )
        normalised = sorted(
            (_normalise_hit(hit) for hit in hits),
            key=lambda hit: (hit["path"], hit["line"], hit["symbol"], hit["text"]),
        )
        result: list[ContextSlice] = []
        for hit in normalised[: self.max_callers]:
            path = hit["path"] or str(_value(unit, "path", "") or "")
            line = hit["line"]
            if line <= 0:
                continue
            result_slice = self._slice_from_source(
                kind="caller",
                path=path,
                start_line=max(1, line - 12),
                end_line=line + 12,
                symbol=symbol,
                reason=f"calls {symbol} at line {line}",
                fallback_text=hit["text"],
            )
            if result_slice is not None:
                result.append(result_slice)
        return result

    def _collect_callees(self, unit: SemanticUnit) -> list[ContextSlice]:
        added_lines = {int(line) for line in (_value(unit, "added_lines", []) or []) if _is_int_like(line)}
        if not added_lines:
            return []
        calls: list[dict[str, Any]] = []
        for call in _value(unit, "calls", []) or []:
            item = _normalise_call(call)
            if item["callee"] and (item["line"] in added_lines or item["line"] <= 0):
                calls.append(item)
        calls.sort(key=lambda item: (item["line"], item["callee"], item["path"]))

        result: list[ContextSlice] = []
        seen_symbols: set[str] = set()
        language = str(_value(unit, "language", "") or "")
        for call in calls:
            raw_name = call["callee"]
            lookup_names = _lookup_symbol_names(raw_name)
            hits: list[Any] = []
            resolved_name = raw_name
            for lookup_name in lookup_names:
                hits = _workspace_call(
                    self.workspace,
                    "find_symbol_definitions",
                    lookup_name,
                    language=language,
                )
                if hits:
                    resolved_name = lookup_name
                    break
            if not hits or resolved_name in seen_symbols:
                continue
            seen_symbols.add(resolved_name)
            normalised = sorted(
                (_normalise_hit(hit) for hit in hits),
                key=lambda hit: (hit["path"], hit["line"], hit["start_line"], hit["symbol"]),
            )
            for hit in normalised:
                path = hit["path"]
                if not path:
                    continue
                definition_line = hit["line"] or hit["start_line"]
                if definition_line <= 0:
                    continue
                definition_start = hit["start_line"] or definition_line
                source = self._read(path)
                result_slice = self._slice_from_source(
                    kind="callee",
                    path=path,
                    start_line=definition_start,
                    end_line=definition_line + 8,
                    symbol=resolved_name,
                    reason=f"defines {resolved_name}",
                    source=source,
                    fallback_text=hit["text"],
                )
                if result_slice is not None:
                    result.append(result_slice)
        return result

    def _collect_inheritance(self, unit: SemanticUnit) -> tuple[list[ContextSlice], list[ContextSlice]]:
        path = str(_value(unit, "path", "") or "")
        source = self._read(path)
        if not path or source is None:
            return [], []
        owner, relations = _find_owner_relations(source, path, unit)
        if not owner or not relations:
            return [], []
        language = str(_value(unit, "language", "") or "")
        bases: list[ContextSlice] = []
        interfaces: list[ContextSlice] = []
        for relation, name in relations:
            simple_name = _simple_name(name)
            if not simple_name:
                continue
            hits = _workspace_call(
                self.workspace,
                "find_symbol_definitions",
                simple_name,
                language=language,
            )
            normalised = sorted(
                (_normalise_hit(hit) for hit in hits),
                key=lambda hit: (hit["path"], hit["line"], hit["start_line"], hit["symbol"]),
            )
            if not normalised:
                # T2 may legitimately return no cross-file hit.  A parent in
                # the changed file is still useful and does not require a new
                # workspace capability.
                local = _local_definition_hit(source, path, simple_name)
                if local is not None:
                    normalised = [local]
            for hit in normalised:
                parent_path = hit["path"] or path
                parent_source = self._read(parent_path)
                text, start, end = _signature_list(
                    parent_source,
                    parent_path,
                    simple_name,
                    hit["line"] or hit["start_line"],
                    self.max_slice_lines,
                    hit["text"],
                )
                if not text:
                    continue
                result_slice = ContextSlice(
                    kind=relation,
                    path=parent_path,
                    start_line=start,
                    end_line=end,
                    symbol=simple_name,
                    text=text,
                    reason=f"{relation.replace('_', ' ')} {simple_name} for {owner}",
                    sha=self.sha,
                )
                if relation == "interface":
                    interfaces.append(result_slice)
                else:
                    bases.append(result_slice)
        return bases, interfaces

    def _collect_siblings(self, unit: SemanticUnit) -> list[ContextSlice]:
        path = str(_value(unit, "path", "") or "")
        source = self._read(path)
        symbol = str(_value(unit, "symbol", "") or "")
        if not path or source is None or not symbol:
            return []
        try:
            definitions = extract_definitions(source, path)
        except Exception:
            definitions = []
        if not definitions:
            return []
        target_line = _unit_anchor_line(unit)
        owner_class = _enclosing_class(definitions, target_line, symbol)
        target_group = _sibling_group(symbol)
        candidates = []
        for definition in definitions:
            name = str(getattr(definition, "name", "") or "")
            if not name or name == symbol or getattr(definition, "symbol_type", "") != "function":
                continue
            if _sibling_group(name) != target_group:
                continue
            line = int(getattr(definition, "line", 0) or 0)
            if line <= 0:
                continue
            if owner_class is not None and not _inside_definition(definition, owner_class):
                continue
            candidates.append(definition)
        candidates.sort(key=lambda item: (int(getattr(item, "line", 0) or 0), str(getattr(item, "name", "") or "")))
        result: list[ContextSlice] = []
        for definition in candidates[:3]:
            start = int(getattr(definition, "start_line", 0) or getattr(definition, "line", 0) or 1)
            line = int(getattr(definition, "line", 0) or start)
            end = int(getattr(definition, "end_line", 0) or start + 19)
            sibling_name = str(getattr(definition, "name", "") or "")
            result_slice = self._slice_from_source(
                kind="sibling",
                path=path,
                start_line=start,
                end_line=min(end, start + 19),
                symbol=sibling_name,
                reason=f"sibling of {symbol}",
                source=source,
            )
            if result_slice is not None:
                result.append(result_slice)
        return result

    def _collect_state_usage(self, unit: SemanticUnit) -> tuple[list[ContextSlice], list[ContextSlice]]:
        path = str(_value(unit, "path", "") or "")
        source = self._read(path)
        if not path or source is None:
            return [], []
        lines = source.splitlines()
        added_lines = sorted({int(line) for line in (_value(unit, "added_lines", []) or []) if _is_int_like(line)})
        added_text = "\n".join(lines[line - 1] for line in added_lines if 0 < line <= len(lines))
        if not added_text:
            return [], []

        lock_names = {_normalise_token(match.group(0)) for match in _LOCK_TOKEN.finditer(added_text)}
        lock_names.discard("")
        fields = _field_names(source, str(_value(unit, "language", "") or ""))
        added_identifiers = set(_IDENTIFIER.findall(added_text))
        field_names = sorted(fields.intersection(added_identifiers))
        lock_result = self._usage_slices(
            kind="lock_usage",
            path=path,
            source=source,
            names=sorted(lock_names),
            reason_prefix="uses",
        )
        field_result = self._usage_slices(
            kind="field_usage",
            path=path,
            source=source,
            names=field_names,
            reason_prefix="uses",
        )
        return lock_result, field_result

    def _usage_slices(
        self,
        *,
        kind: str,
        path: str,
        source: str,
        names: list[str],
        reason_prefix: str,
    ) -> list[ContextSlice]:
        if not names:
            return []
        patterns = {name: re.compile(rf"\b{re.escape(name)}\b", re.IGNORECASE) for name in names}
        usage_lines: dict[int, str] = {}
        for line_number, line in enumerate(source.splitlines(), start=1):
            if any(pattern.search(line) for pattern in patterns.values()):
                usage_lines[line_number] = next(name for name, pattern in patterns.items() if pattern.search(line))
        result: list[ContextSlice] = []
        for line_number, name in sorted(usage_lines.items()):
            result_slice = self._slice_from_source(
                kind=kind,
                path=path,
                start_line=max(1, line_number - 3),
                end_line=line_number + 3,
                symbol=name,
                reason=f"{reason_prefix} {name} at line {line_number}",
                source=source,
            )
            if result_slice is not None:
                result.append(result_slice)
        return result

    def _collect_tests(self, unit: SemanticUnit) -> list[ContextSlice]:
        symbol = str(_value(unit, "symbol", "") or "")
        if not symbol:
            return []
        pattern = re.compile(rf"\b{re.escape(_simple_name(symbol))}\b")
        candidates: list[tuple[str, int, str, int, str]] = []
        for path in sorted({str(item) for item in (_value(unit, "candidate_tests", []) or []) if item}):
            source = self._read(path)
            if source is None:
                continue
            lines = source.splitlines()
            matching_lines = [number for number, line in enumerate(lines, start=1) if pattern.search(line)]
            if not matching_lines:
                continue
            try:
                definitions = extract_definitions(source, path)
            except Exception:
                definitions = []
            for definition in definitions:
                name = str(getattr(definition, "name", "") or "")
                start = int(getattr(definition, "start_line", 0) or getattr(definition, "line", 0) or 0)
                end = int(getattr(definition, "end_line", 0) or 0)
                if not name or start <= 0:
                    continue
                if end < start:
                    end = min(len(lines), start + self.max_slice_lines - 1)
                if any(start <= line <= end for line in matching_lines):
                    candidates.append((path, start, name, end, source))
            if not any(candidate[0] == path for candidate in candidates):
                line = matching_lines[0]
                candidates.append((path, max(1, line - 3), "test", min(len(lines), line + 3), source))
        candidates.sort(key=lambda item: (item[0], item[1], item[2]))
        result: list[ContextSlice] = []
        seen: set[tuple[str, int, str]] = set()
        for path, start, name, end, source in candidates:
            key = (path, start, name)
            if key in seen:
                continue
            seen.add(key)
            result_slice = self._slice_from_source(
                kind="test",
                path=path,
                start_line=start,
                end_line=min(end, start + self.max_slice_lines - 1),
                symbol=name,
                reason=f"test {path} references {symbol}",
                source=source,
            )
            if result_slice is not None:
                result.append(result_slice)
            if len(result) >= 2:
                break
        return result

    def _collect_resources(self, unit: SemanticUnit) -> tuple[list[ContextSlice], list[ContextSlice]]:
        if _unit_kind(unit) != UnitKind.RESOURCE.value:
            return [], []
        path = str(_value(unit, "path", "") or "")
        stem = _resource_stem(path)
        if not path or not stem:
            return [], []
        globs = [
            "**/*.schema.*",
            "**/schema/**",
            "**/schemas/**",
            "**/migration*/*",
            "**/migrations/**",
            "**/*.yaml",
            "**/*.yml",
            "**/*.json",
            "**/*.toml",
            "**/*.ini",
            "**/*.conf",
            "**/config/**",
        ]
        hits = _workspace_call(
            self.workspace,
            "grep",
            re.escape(stem),
            globs=globs,
            max_hits=12,
            context=3,
        )
        normalised = sorted(
            (_normalise_hit(hit) for hit in hits),
            key=lambda hit: (hit["path"], hit["line"], hit["text"]),
        )
        schema: list[ContextSlice] = []
        config: list[ContextSlice] = []
        for hit in normalised:
            hit_path = hit["path"]
            if not hit_path or hit_path == path:
                continue
            lower = hit_path.lower()
            kind = "schema" if any(token in lower for token in ("schema", "migration")) else "config"
            line = hit["line"] or 1
            result_slice = self._slice_from_source(
                kind=kind,
                path=hit_path,
                start_line=max(1, line - 12),
                end_line=line + 12,
                symbol=stem,
                reason=f"related {kind} for {path}",
                fallback_text=hit["text"],
            )
            if result_slice is not None:
                (schema if kind == "schema" else config).append(result_slice)
        return schema, config


def _render_unit(context: UnitContext, max_chars: int) -> str:
    if max_chars <= 0:
        return ""
    text = "\n\n".join(_render_unit_parts(context))
    return text[:max_chars]


def _render_unit_parts(context: UnitContext) -> list[str]:
    parts: list[str] = [f"## Unit {context.unit_id}"]
    for slice_ in context.slices:
        parts.append(
            "\n\n".join(
                (
                    f"### {slice_.kind} {slice_.path}:{slice_.start_line}-{slice_.end_line} — {slice_.reason}",
                    slice_.text,
                )
            )
        )
    return parts


def _mark_truncated(context: UnitContext, start_index: int) -> None:
    """Record kinds omitted by the global render water level."""

    kinds = [slice_.kind for slice_ in context.slices[max(0, start_index) :]]
    context.truncated_kinds = _ordered_unique([*context.truncated_kinds, *kinds])


def _resolve_pr_intent(
    *,
    state: Any | None,
    explicit: str | None,
    title: str,
    body: str,
    linked_issues: Iterable[Any] | None,
) -> str:
    if explicit is not None:
        return str(explicit)[:_PR_INTENT_MAX_CHARS]
    if state is not None:
        if not title:
            title = str(_value(state, "pr_title", "") or "")
        if not body:
            body = str(_value(state, "pr_body", "") or "")
        if linked_issues is None:
            linked_issues = _value(state, "linked_issues", []) or []
    titles: list[str] = []
    for item in linked_issues or []:
        value = _value(item, "title", item if isinstance(item, str) else "")
        if value:
            titles.append(str(value))
    titles = _ordered_unique(sorted(titles))
    sections: list[str] = []
    if title:
        sections.append(f"Title: {title}")
    if body:
        sections.append(f"Body:\n{body}")
    if titles:
        sections.append("Linked issues:\n" + "\n".join(f"- {item}" for item in titles))
    return "\n\n".join(sections)[:_PR_INTENT_MAX_CHARS]


def _workspace_degraded(workspace: Any) -> bool:
    if workspace is None:
        return True
    info = _workspace_value(workspace, "info")
    source = _workspace_value(workspace, "source") or _workspace_value(info, "source")
    return str(source or "").lower() == "api-fallback" or bool(
        _workspace_value(workspace, "degraded") or _workspace_value(info, "degraded")
    )


def _workspace_value(workspace: Any, name: str) -> Any:
    if workspace is None:
        return None
    return _value(workspace, name, None)


def _workspace_call(workspace: Any, method_name: str, *args: Any, **kwargs: Any) -> list[Any]:
    method = getattr(workspace, method_name, None)
    if not callable(method):
        return []
    try:
        result = method(*args, **kwargs)
    except TypeError:
        # Small test doubles and the T2 implementation may choose positional
        # keyword signatures.  Retry only the same operation without optional
        # keywords; never turn an exception into a positive finding.
        try:
            result = method(*args)
        except Exception:
            return []
    except Exception:
        return []
    if result is None:
        return []
    if isinstance(result, dict):
        for key in ("hits", "results", "items"):
            value = result.get(key)
            if isinstance(value, (list, tuple)):
                return list(value)
        return [result]
    if isinstance(result, (list, tuple)):
        return list(result)
    return [result]


def _normalise_hit(hit: Any) -> dict[str, Any]:
    path = str(_value(hit, "path", _value(hit, "file_path", "")) or "")
    line = _to_int(_value(hit, "line", _value(hit, "line_number", 0)))
    start_line = _to_int(_value(hit, "start_line", line))
    end_line = _to_int(_value(hit, "end_line", 0))
    symbol = str(_value(hit, "symbol", _value(hit, "name", "")) or "")
    text = str(_value(hit, "text", _value(hit, "content", _value(hit, "excerpt", ""))) or "")
    return {
        "path": path,
        "line": line,
        "start_line": start_line,
        "end_line": end_line,
        "symbol": symbol,
        "text": text,
    }


def _normalise_call(call: Any) -> dict[str, Any]:
    return {
        "caller": str(_value(call, "caller", "") or ""),
        "callee": str(_value(call, "callee", _value(call, "name", "")) or ""),
        "line": _to_int(_value(call, "line", 0)),
        "path": str(_value(call, "file_path", _value(call, "path", "")) or ""),
    }


def _value(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _to_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _is_int_like(value: Any) -> bool:
    try:
        int(value)
    except (TypeError, ValueError):
        return False
    return True


def _unit_id(unit: Any) -> str:
    explicit = str(_value(unit, "id", "") or "")
    if explicit:
        return explicit
    return f"{_value(unit, 'path', '')}:{_value(unit, 'symbol', '')}"


def _unit_risk(unit: Any) -> float:
    try:
        return float(_value(unit, "risk_score", 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _unit_kind(unit: Any) -> str:
    value = _value(unit, "kind", UnitKind.SYMBOL)
    return str(getattr(value, "value", value) or UnitKind.SYMBOL.value)


def _unit_anchor_line(unit: Any) -> int:
    line = _to_int(_value(unit, "start_line", 0)) or _to_int(_value(unit, "line", 0))
    if line > 0:
        return line
    added = [_to_int(item) for item in (_value(unit, "added_lines", []) or []) if _is_int_like(item)]
    return min(added, default=1)


def _lookup_symbol_names(name: str) -> list[str]:
    raw = str(name or "").strip()
    if not raw:
        return []
    simple = _simple_name(raw)
    return _ordered_unique([raw, simple])


def _simple_name(name: str) -> str:
    value = re.sub(r"\s*<.*>\s*", "", str(name or "")).strip()
    value = value.rstrip("?[]")
    return re.split(r"[.:/]", value)[-1].lstrip("*")


def _normalise_token(token: str) -> str:
    token = re.sub(r"\s+", "", token or "").lower()
    if "." in token:
        token = token.split(".", 1)[0]
    return token


def _ordered_unique(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        value = str(value)
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _deduplicate_slices(slices: Iterable[ContextSlice]) -> list[ContextSlice]:
    result: list[ContextSlice] = []
    seen: set[tuple[str, str, int, int, str, str]] = set()
    for slice_ in slices:
        key = (slice_.kind, slice_.path, slice_.start_line, slice_.end_line, slice_.symbol, slice_.text)
        if key not in seen:
            seen.add(key)
            result.append(slice_)
    return result


def _enclosing_class(definitions: list[Any], line: int, symbol: str) -> Any | None:
    classes = [item for item in definitions if getattr(item, "symbol_type", "") == "class"]
    exact = next((item for item in classes if getattr(item, "name", "") == symbol), None)
    if exact is not None:
        return exact
    containing = [item for item in classes if _definition_start(item) <= line <= _definition_end(item, line)]
    return (
        min(containing, key=lambda item: _definition_end(item, line) - _definition_start(item)) if containing else None
    )


def _inside_definition(item: Any, owner: Any) -> bool:
    line = _to_int(getattr(item, "line", 0))
    return _definition_start(owner) <= line <= _definition_end(owner, line)


def _definition_start(item: Any) -> int:
    return _to_int(getattr(item, "start_line", 0)) or _to_int(getattr(item, "line", 0)) or 1


def _definition_end(item: Any, fallback: int) -> int:
    return _to_int(getattr(item, "end_line", 0)) or max(_definition_start(item), fallback + 10_000)


def _sibling_group(name: str) -> str:
    value = str(name or "")
    lower = value.lower()
    for verb in ("create", "update", "delete", "get", "set"):
        if lower.startswith(verb) and len(value) > len(verb):
            return lower[len(verb) :].lstrip("_-")
    return lower


def _find_owner_relations(source: str, path: str, unit: Any) -> tuple[str, list[tuple[str, str]]]:
    lines = source.splitlines()
    anchor = _unit_anchor_line(unit)
    language = str(_value(unit, "language", "") or "").lower()
    declarations: list[tuple[int, str, list[tuple[str, str]]]] = []

    if language == "python" or path.lower().endswith(".py"):
        for match in _PYTHON_CLASS.finditer(source):
            bases = _split_names(match.group("bases") or "")
            declarations.append(
                (
                    source[: match.start()].count("\n") + 1,
                    match.group("name"),
                    [("base_class", item) for item in bases],
                )
            )
    elif language == "ruby" or path.lower().endswith((".rb", ".rake")):
        for match in _RUBY_CLASS.finditer(source):
            base = match.group("base") or ""
            declarations.append(
                (source[: match.start()].count("\n") + 1, match.group("name"), [("base_class", base)] if base else [])
            )
    elif language == "go" or path.lower().endswith(".go"):
        for match in _GO_TYPE.finditer(source):
            line = source[: match.start()].count("\n") + 1
            close = _matching_brace(lines, line)
            embedded: list[str] = []
            for body_line in lines[line:close]:
                stripped = body_line.strip().rstrip(",")
                if re.fullmatch(r"\*?[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)?", stripped):
                    embedded.append(stripped)
            declarations.append(
                (
                    line,
                    match.group("name"),
                    [("interface" if match.group("kind") == "interface" else "base_class", item) for item in embedded],
                )
            )
    else:
        for match in _CLASS_DECLARATION.finditer(source):
            tail = match.group("tail") or ""
            relations: list[tuple[str, str]] = []
            extends = re.search(r"\bextends\s+([^\s{]+)", tail)
            if extends:
                relations.append(("base_class", extends.group(1)))
            implements = re.search(r"\bimplements\s+([^\{]+)", tail)
            if implements:
                relations.extend(("interface", item) for item in _split_names(implements.group(1)))
            declarations.append((source[: match.start()].count("\n") + 1, match.group("name"), relations))
        interface_pattern = re.compile(
            r"^\s*(?:(?:export|public|private|protected|abstract|declare)\s+)*"
            r"interface\s+(?P<name>[A-Za-z_$][\w$]*)(?P<tail>[^\{\n]*)",
            re.MULTILINE,
        )
        for match in interface_pattern.finditer(source):
            tail = match.group("tail") or ""
            extends = re.search(r"\bextends\s+([^\{]+)", tail)
            relations = [("interface", item) for item in _split_names(extends.group(1))] if extends else []
            declarations.append((source[: match.start()].count("\n") + 1, match.group("name"), relations))

    if not declarations:
        return "", []
    matching = [item for item in declarations if item[0] <= anchor]
    selected = max(matching or declarations, key=lambda item: item[0])
    return selected[1], selected[2]


def _split_names(value: str) -> list[str]:
    names: list[str] = []
    for piece in re.split(r",|&", value):
        cleaned = re.sub(r"\b(?:public|private|protected|readonly|abstract)\b", "", piece).strip()
        cleaned = re.sub(r"<.*>", "", cleaned).strip()
        if cleaned:
            names.append(cleaned)
    return names


def _matching_brace(lines: list[str], start_line: int) -> int:
    depth = 0
    started = False
    for index in range(max(0, start_line - 1), len(lines)):
        depth += lines[index].count("{") - lines[index].count("}")
        if "{" in lines[index]:
            started = True
        if started and depth <= 0:
            return index
    return len(lines)


def _local_definition_hit(source: str, path: str, name: str) -> dict[str, Any] | None:
    try:
        definitions = extract_definitions(source, path)
    except Exception:
        definitions = []
    for definition in definitions:
        if str(getattr(definition, "name", "") or "") == name:
            line = _to_int(getattr(definition, "line", 0))
            return {
                "path": path,
                "line": line,
                "start_line": _to_int(getattr(definition, "start_line", 0)) or line,
                "end_line": _to_int(getattr(definition, "end_line", 0)),
                "symbol": name,
                "text": "",
            }
    return None


def _signature_list(
    source: str | None,
    path: str,
    name: str,
    line: int,
    max_lines: int,
    fallback_text: str,
) -> tuple[str, int, int]:
    if source is None:
        text = "\n".join(str(fallback_text or "").splitlines()[:max_lines])
        return text, max(1, line), max(1, line) + max(0, len(text.splitlines()) - 1) if text else max(1, line)
    lines = source.splitlines()
    if not lines:
        return "", 1, 1
    try:
        definitions = extract_definitions(source, path)
    except Exception:
        definitions = []
    target = next(
        (
            item
            for item in definitions
            if str(getattr(item, "name", "") or "") == name and abs(_to_int(getattr(item, "line", 0)) - line) <= 1
        ),
        None,
    )
    start = _definition_start(target) if target is not None else max(1, line)
    end = _to_int(getattr(target, "line", 0)) if target is not None else start
    # Keep the parent declaration and method signatures, but drop method bodies.
    class_end = _definition_end(target, end) if target is not None else min(len(lines), start + max_lines - 1)
    method_lines: list[int] = [start]
    if target is not None:
        for item in definitions:
            item_line = _to_int(getattr(item, "line", 0))
            if getattr(item, "symbol_type", "") != "function" or not start < item_line <= class_end:
                continue
            method_lines.extend(range(_definition_start(item), item_line + 1))
    selected = sorted({item for item in method_lines if 1 <= item <= len(lines)})[:max_lines]
    if not selected:
        return "", start, start
    text = "\n".join(lines[index - 1] for index in selected)
    return text, selected[0], selected[-1]


def _field_names(source: str, language: str) -> set[str]:
    names = set(re.findall(r"\bself\.([A-Za-z_]\w*)", source))
    names.update(re.findall(r"\bthis\.([A-Za-z_$][\w$]*)", source))
    for line in source.splitlines():
        stripped = line.strip()
        python_field = re.match(r"(?:self\.)?([A-Za-z_]\w*)\s*=", stripped)
        if python_field and not stripped.startswith(("def ", "class ")):
            names.add(python_field.group(1))
        member = re.match(
            r"(?:(?:public|private|protected|static|readonly|final|const|let|var|volatile|transient)\s+)*"
            r"(?:[A-Za-z_$][\w$<>,.?\[\]]*\s+)?([A-Za-z_$][\w$]*)\s*(?:=|:|;)",
            stripped,
        )
        if member:
            names.add(member.group(1))
        if language.lower() == "go":
            go_field = re.match(r"\*?([A-Za-z_]\w*)\s+[A-Za-z0-9_.*\[\]]+", stripped)
            if go_field:
                names.add(go_field.group(1))
    return names


def _resource_stem(path: str) -> str:
    name = PurePosixPath(path.replace("\\", "/")).name
    for suffix in (".schema", ".config", ".migration"):
        if name.lower().endswith(suffix):
            name = name[: -len(suffix)]
    return PurePosixPath(name).stem
