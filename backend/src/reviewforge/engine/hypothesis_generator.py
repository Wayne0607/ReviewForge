"""Hypothesis generator for the hypothesis pipeline.

One bounded pass over the whole PR: the deterministic context pack plus the
before/after hunks are shared once per file in each block, with separate
RIGHT-side anchors per semantic unit (risk-ordered, chunked when the input
exceeds the configured budget), and the model proposes testable
hypotheses.  The generator only *proposes*; it never investigates and never
retries with a "look harder" signal — explicit clean assessments are valid
NO_ISSUE results. Units omitted from an otherwise valid response, or in a
block that fails to parse, are marked ``unresolved``.

The generator writes into the shared ``HypothesisLedger``.  Every emitted
hypothesis is ``OPEN`` by construction; investigation (a later stage) is the
only consumer allowed to move it away from ``OPEN``.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from reviewforge.core.json_output import extract_json_value
from reviewforge.core.state import StateStore
from reviewforge.engine.context_pack import ContextPack
from reviewforge.engine.declarations_v4 import extract_code_definitions
from reviewforge.engine.detectors.unified_diff import iter_right_lines, render_numbered_diff, select_diff_hunks
from reviewforge.engine.hypothesis import Hypothesis, HypothesisLedger, Mechanism, Site
from reviewforge.engine.prompts_v4 import load_prompt
from reviewforge.engine.semantic_diff import SemanticChangeSet, SemanticUnit
from reviewforge.engine.symbol_extractor import _find_enclosing_function
from reviewforge.tools.workspace import WorkspaceUnavailable

_EXCERPT_MIN_CHARS = 12
_SEVERITIES = frozenset({"error", "warning", "info"})
_SEVERITY_PRIORITY = {"info": 0, "warning": 1, "error": 2}
logger = logging.getLogger(__name__)

AnchorResolver = Callable[[str, str, int], Awaitable[str]]


def build_anchor_resolver(workspace: Any, changeset: SemanticChangeSet) -> AnchorResolver:
    """Resolve identity anchors from immutable source, as required by SPEC §4.3."""
    unit_symbols = {unit.id: unit.symbol for unit in changeset.units}
    owners: dict[str, dict[int, str]] = {}

    async def resolve(unit_id: str, path: str, line: int) -> str:
        if path not in owners:
            reader = getattr(workspace, "read_async", None)
            try:
                source = await reader(path) if callable(reader) else None
            except (WorkspaceUnavailable, OSError):
                source = None
            owners[path] = (
                _find_enclosing_function(source.splitlines(), extract_code_definitions(source, path))
                if isinstance(source, str)
                else {}
            )
        function = owners[path].get(line - 1, "<module>")
        return function if function != "<module>" else unit_symbols.get(unit_id, "")

    return resolve


# The lead-in text shared by every block (PR intent plus the unchecked-summary).
# It is measured separately so block budgeting never starves the diff itself.
_BLOCK_OVERHEAD = 4_000


@dataclass
class HypothesisGenerationResult:
    """Deterministic accounting for one generator run."""

    accepted: int = 0
    dropped_unanchored: int = 0
    dropped_invalid: int = 0
    dropped_overflow: int = 0
    blocks: int = 0
    failed_blocks: int = 0
    unresolved_units: list[str] = field(default_factory=list)


def _right_lines_by_path(state: StateStore) -> dict[str, dict[int, str]]:
    """Map ``path -> {RIGHT-side line: content}`` from the per-run patch cache."""

    result: dict[str, dict[int, str]] = {}
    for path, diff in (state.file_diffs or {}).items():
        result[path] = {line: content for line, content in iter_right_lines(diff or "")}
    return result


def _unit_right_lines(
    unit: SemanticUnit, right_lines: dict[str, dict[int, str]], max_lines: int
) -> list[tuple[int, str]]:
    """Return the RIGHT-side lines relevant to one unit, bounded."""

    by_line = right_lines.get(unit.path, {})
    if not by_line:
        return []
    start = max(1, int(getattr(unit, "start_line", 0) or 0) - 3)
    end = int(getattr(unit, "end_line", 0) or 0) + 3
    if end > start:
        window = sorted((line, content) for line, content in by_line.items() if start <= line <= end)
        if window:
            return window[:max_lines]
    return sorted(by_line.items())[:max_lines]


def _shared_patch(units: list[SemanticUnit], diff: str) -> str:
    """Keep both sides of intersecting hunks, including deletion-only changes.

    This is source material, not an anchor map: only ``iter_right_lines`` may
    supply coordinates for generated sites. A removed guard or lock must still
    be visible when reasoning about the behavior introduced by the PR.
    """

    ranges = [(max(1, unit.start_line - 3), max(1, unit.end_line + 3)) for unit in units]
    # Resource/file units may not have symbol coordinates. Keep their complete
    # changes, even when other units in the same file have narrower windows.
    if any(not unit.start_line for unit in units):
        ranges.append((0, 2**63 - 1))
    return select_diff_hunks(diff, ranges)


def _render_changes(units: list[SemanticUnit], right_lines: dict[str, dict[int, str]], diffs: dict[str, str]) -> str:
    """Share each hunk once while retaining each unit's identity and anchors."""

    sections: list[str] = []
    by_path: dict[str, list[SemanticUnit]] = {}
    for unit in units:
        by_path.setdefault(unit.path, []).append(unit)
    for path, file_units in by_path.items():
        patch = _shared_patch(file_units, diffs.get(path, "") or "")
        sections.append(f"### Shared before/after diff: {path}\n{render_numbered_diff(patch) or '(no matching hunk)'}")
    for unit in units:
        lines = _unit_right_lines(unit, right_lines, max(1, len(diffs.get(unit.path, "").splitlines())))
        header = f"### {unit.path} — symbol={unit.symbol or '-'} unit_id={unit.id}"
        body = ", ".join(str(line) for line, _content in lines) or "(no RIGHT-side lines)"
        sections.append(f"{header}\nRIGHT-side anchor lines (code in shared diff above): {body}")
    return "\n\n".join(sections)


def _render_unchecked(pack: ContextPack) -> str:
    """Aggregate the context kinds that were dropped during pack construction."""

    entries: list[str] = []
    for unit_id in sorted(pack.units):
        context = pack.units[unit_id]
        if context.truncated_kinds:
            entries.append(f"- {unit_id}: {', '.join(kind for kind in context.truncated_kinds)}")
    if not entries:
        return "（全部上下文均已交付）/(all requested context delivered)"
    return "\n".join(entries)


def _render_existing(ledger: HypothesisLedger) -> str:
    items = sorted(ledger.items.values(), key=lambda hypothesis: hypothesis.identity)
    if not items:
        return "（无）/(none)"
    lines = [f"- {hypothesis.identity} :: {hypothesis.claim}" for hypothesis in items]
    return "\n".join(lines)


def _language_directive(language: str) -> str:
    if language == "zh-CN":
        return "简体中文"
    return "English"


def _identity(unit_id: str, mechanism: Mechanism, anchor: str) -> str:
    return f"{unit_id}::{mechanism.value}::{anchor}"


def _new_hypothesis_id(identity: str) -> str:
    return "h_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:8]


def _parse_mechanism(value: Any) -> Mechanism | None:
    if isinstance(value, Mechanism):
        return value
    if not isinstance(value, str):
        return None
    try:
        return Mechanism(value.strip().lower())
    except ValueError:
        return None


def _validate_sites(raw_sites: Any, right_lines: dict[str, dict[int, str]]) -> list[tuple[str, int, str]]:
    """Keep sites whose excerpt is a verbatim RIGHT-side substring of the cited line.

    A site survives only when its ``path`` and ``line`` resolve to a RIGHT-side
    line in the patch and ``excerpt`` (>=12 chars) is a substring of that line's
    content.  This is what pins the model to the actual diff instead of letting
    it hallucinate coordinates or quote invented code.
    """

    if not isinstance(raw_sites, list):
        return []
    kept: list[tuple[str, int, str]] = []
    for site in raw_sites:
        if not isinstance(site, dict):
            continue
        path = str(site.get("path") or "").strip()
        excerpt = str(site.get("excerpt") or "")
        try:
            line = int(site.get("line"))
        except (TypeError, ValueError):
            continue
        if not path or line <= 0 or len(excerpt) < _EXCERPT_MIN_CHARS:
            continue
        content = right_lines.get(path, {}).get(line)
        if content is None or excerpt not in content:
            continue
        kept.append((path, line, excerpt))
    return kept


class HypothesisGenerator:
    """Propose hypotheses from the deterministic context pack.

    ``llm`` is injected so the orchestrator can supply a token-tracked, routed
    model and tests can supply ``MockChatLLM``.  ``output_language`` must
    already be resolved to ``en`` or ``zh-CN`` by the caller (the orchestrator
    resolves ``auto`` via :func:`resolve_output_language`).
    """

    def __init__(
        self,
        llm: BaseChatModel,
        *,
        max_input_chars: int = 120_000,
        max_hypotheses: int = 12,
        context_max_chars: int = 40_000,
        output_language: str = "en",
        source: str = "generator",
        prompt_template: str = "generator",
        skill_body: str = "",
        on_update: Callable[[HypothesisLedger], Awaitable[None]] | None = None,
        anchor_resolver: AnchorResolver | None = None,
    ) -> None:
        self._llm = llm
        self._max_input_chars = max(1, int(max_input_chars))
        self._max_hypotheses = max(1, int(max_hypotheses))
        self._context_max_chars = max(1, int(context_max_chars))
        self._output_language = output_language
        self._source = source
        self._prompt_template = prompt_template
        self._skill_body = skill_body
        self._on_update = on_update
        self._anchor_resolver = anchor_resolver

    def _system_prompt(self) -> str:
        lens_name = self._source[len("lens:") :] if self._source.startswith("lens:") else self._source
        prompt = load_prompt(
            self._prompt_template,
            output_language=_language_directive(self._output_language),
            lens=lens_name,
            max_hypotheses=self._max_hypotheses,
        )
        if self._skill_body:
            prompt = f"{prompt}\n\n## 本维度专项规则\n{self._skill_body}"
        return prompt

    async def run(
        self,
        state: StateStore,
        pack: ContextPack,
        changeset: SemanticChangeSet,
        ledger: HypothesisLedger,
    ) -> HypothesisGenerationResult:
        units = sorted(changeset.units, key=lambda unit: (-unit.risk_score, unit.id))
        result = HypothesisGenerationResult()
        if not units:
            return result

        right_lines = _right_lines_by_path(state)
        diffs = state.file_diffs or {}
        # Match the pack's global, risk-ordered water filling. A per-unit floor
        # multiplied by many units must not exceed the advertised pack budget.
        pack.render_all(max_chars=self._context_max_chars)
        remaining = self._context_max_chars
        context_by_unit = {}
        for unit in units:
            context = pack.render_for_unit(unit.id, max_chars=max(0, remaining))
            context_by_unit[unit.id] = context
            remaining -= len(context) + (2 if context else 0)

        blocks = self._chunk_units(units, right_lines, diffs, pack, context_by_unit)
        while blocks:
            block = blocks.pop(0)
            user = self._render_user_message(pack, block, right_lines, diffs, context_by_unit, ledger)
            if len(user) > self._max_input_chars and len(block) > 1:
                split = len(block) // 2
                blocks[0:0] = [block[:split], block[split:]]
                continue
            result.blocks += 1
            failure = f"{self._source} parse failure"
            if len(user) > self._max_input_chars:
                parsed = None
                failure = f"{self._source} input-too-large"
            else:
                try:
                    parsed, original = await self._invoke_once(user)
                    if parsed is None:
                        parsed = await self._invoke_repair(original)
                except Exception as exc:
                    parsed = None
                    failure = f"{self._source} provider error: {type(exc).__name__}"
                    logger.warning("%s generation failed: %s", self._source, type(exc).__name__)
            if parsed is None:
                result.failed_blocks += 1
                for unit in block:
                    ledger.unresolved_units[unit.id] = failure
                    result.unresolved_units.append(unit.id)
                if self._on_update is not None:
                    await self._on_update(ledger)
                continue
            reported = self._reported_units(parsed)
            missing = [unit for unit in block if unit.id not in reported]
            if missing:
                result.failed_blocks += 1
                logger.warning("%s response omitted %d unit assessment(s)", self._source, len(missing))
            for unit in block:
                previous = ledger.unresolved_units.get(unit.id, "")
                if unit.id not in reported:
                    # An omitted answer is not a clean assessment. Preserve a
                    # failure from another pass (e.g. generator vs. lens).
                    if not previous or previous.startswith(f"{self._source} "):
                        ledger.unresolved_units[unit.id] = f"{self._source} missing unit assessment"
                    ledger.no_issue_units.pop(unit.id, None)
                    result.unresolved_units.append(unit.id)
                elif previous.startswith(f"{self._source} "):
                    ledger.unresolved_units.pop(unit.id)
            result.dropped_overflow += await self._consume(
                parsed, ledger, result, right_lines, {unit.id: unit for unit in block}
            )
            if self._on_update is not None:
                await self._on_update(ledger)
        return result

    def _chunk_units(
        self,
        units: list[SemanticUnit],
        right_lines: dict[str, dict[int, str]],
        diffs: dict[str, str],
        pack: ContextPack,
        context_by_unit: dict[str, str],
    ) -> list[list[SemanticUnit]]:
        blocks: list[list[SemanticUnit]] = []
        current: list[SemanticUnit] = []
        for unit in units:
            candidate = [*current, unit]
            # Summing standalone unit inputs would count a shared hunk many
            # times and split a PR that actually fits in one generation pass.
            candidate_chars = (
                _BLOCK_OVERHEAD
                + len(_render_changes(candidate, right_lines, diffs))
                + len(pack.render_shared((item.id for item in candidate), context_by_unit))
            )
            if current and candidate_chars > self._max_input_chars:
                blocks.append(current)
                current = [unit]
            else:
                current = candidate
        if current:
            blocks.append(current)
        return blocks

    def _render_user_message(
        self,
        pack: ContextPack,
        block: list[SemanticUnit],
        right_lines: dict[str, dict[int, str]],
        diffs: dict[str, str],
        context_by_unit: dict[str, str],
        ledger: HypothesisLedger,
    ) -> str:
        sections: list[str] = []
        sections.append("## PR intent\n" + (pack.pr_intent or "（无）/(none)"))
        changes = _render_changes(block, right_lines, diffs)
        allowed = ", ".join(unit.id for unit in block)
        sections.append(
            "## Changes\nAllowed unit_id values (copy exactly; do not construct a file:symbol ID):\n"
            + allowed
            + "\n\n"
            + changes
        )
        context = pack.render_shared((unit.id for unit in block), context_by_unit)
        sections.append("## Context\n" + (context or "（无）/(none)"))
        sections.append("## Unchecked\n" + _render_unchecked(pack))
        sections.append("## Existing hypotheses\n" + _render_existing(ledger))
        sections.append(
            f"## Required assessments ({len(block)})\n"
            "Return each ID in hypotheses or no_issue_units.checked, including tests/fixtures. "
            "Existing hypotheses/context do not count as this response's assessment.\n" + allowed
        )
        return "\n\n".join(sections)

    async def _invoke_once(self, user: str) -> tuple[dict[str, Any] | None, str]:
        messages = [
            SystemMessage(content=self._system_prompt()),
            HumanMessage(content=user),
        ]
        response = await self._llm.ainvoke(messages, max_tokens=8192)
        content = getattr(response, "content", "") or ""
        return self._parse_response(content), content

    async def _invoke_repair(self, original: str) -> dict[str, Any] | None:
        messages = [
            SystemMessage(
                content=(
                    "Repair the JSON formatting of the supplied response only. "
                    'The schema is an object with "hypotheses" and "no_issue_units" arrays. '
                    "Preserve every entry and its facts verbatim; do not review code again, "
                    "add hypotheses, invent missing field values, or discard incomplete entries. "
                    "If the response is truncated or lacks enough information for a faithful repair, "
                    "return null. Output only the repaired JSON or null."
                )
            ),
            AIMessage(content=original),
            HumanMessage(content="Repair the supplied response without changing its contents."),
        ]
        response = await self._llm.ainvoke(messages, max_tokens=8192)
        return self._parse_response(getattr(response, "content", "") or "")

    @staticmethod
    def _parse_response(content: str) -> dict[str, Any] | None:
        parsed = extract_json_value(content, required_key="hypotheses", allow_list=False)
        if not isinstance(parsed, dict) or not isinstance(parsed.get("hypotheses"), list):
            return None
        if not isinstance(parsed.get("no_issue_units", []), list):
            return None
        return parsed

    @staticmethod
    def _reported_units(parsed: dict[str, Any]) -> set[str]:
        """Count explicit unit answers separately from candidate acceptance.

        A reported hypothesis may be dropped by the SPEC's existing site,
        schema or overflow rules; that does not make it an omitted answer.
        A clean answer must carry its checked boundary. Earlier ledger rows
        cannot stand in for an answer to the current block/pass.
        """

        reported = {
            str(item.get("unit_id") or "").strip() for item in parsed.get("hypotheses", []) if isinstance(item, dict)
        }
        reported.update(
            str(item.get("unit_id") or "").strip()
            for item in parsed.get("no_issue_units", [])
            if isinstance(item, dict) and str(item.get("checked") or "").strip()
        )
        return reported

    async def _consume(
        self,
        parsed: dict[str, Any],
        ledger: HypothesisLedger,
        result: HypothesisGenerationResult,
        right_lines: dict[str, dict[int, str]],
        units: dict[str, SemanticUnit],
    ) -> int:
        valid: list[tuple[dict[str, Any], list[tuple[str, int, str]]]] = []

        raw_hypotheses = parsed.get("hypotheses")
        if isinstance(raw_hypotheses, list):
            for item in raw_hypotheses:
                payload, sites = self._validate_hypothesis(item, right_lines)
                if payload is None or payload["unit_id"] not in units:
                    result.dropped_invalid += 1
                    continue
                if not sites:
                    result.dropped_unanchored += 1
                    continue
                valid.append((payload, sites))

        valid.sort(key=lambda entry: (-_SEVERITY_PRIORITY.get(entry[0]["severity"], 0), entry[0]["unit_id"]))
        kept = valid[: self._max_hypotheses]
        overflow = len(valid) - len(kept)

        for payload, sites in kept:
            unit = units[payload["unit_id"]]
            # The model can describe an anchor, but cannot define its identity.
            # Prefer a primary site in the declared unit, then use stable order.
            primary = min(sites, key=lambda site: (site[0] != unit.path, site[0], site[1]))
            anchor = (
                await self._anchor_resolver(unit.id, primary[0], primary[1])
                if self._anchor_resolver is not None
                else unit.symbol
            )
            identity = _identity(payload["unit_id"], payload["mechanism"], anchor)
            hypothesis = Hypothesis(
                id=_new_hypothesis_id(identity),
                identity=identity,
                unit_id=payload["unit_id"],
                mechanism=payload["mechanism"],
                claim=payload["claim"],
                trigger=payload["trigger"],
                impact=payload["impact"],
                open_question=payload["open_question"],
                refutation=payload["refutation"],
                sites=[Site(path=path, line=line, excerpt=excerpt) for path, line, excerpt in sites],
                severity=payload["severity"],
                source=self._source,
            )
            ledger.upsert(hypothesis)
            ledger.no_issue_units.pop(hypothesis.unit_id, None)
            result.accepted += 1
            if self._on_update is not None:
                await self._on_update(ledger)

        raw_no_issue = parsed.get("no_issue_units")
        if isinstance(raw_no_issue, list):
            for item in raw_no_issue:
                if not isinstance(item, dict):
                    continue
                unit_id = str(item.get("unit_id") or "").strip()
                checked = str(item.get("checked") or "").strip()
                has_hypothesis = any(hypothesis.unit_id == unit_id for hypothesis in ledger.items.values())
                if unit_id in units and checked and not has_hypothesis and unit_id not in ledger.unresolved_units:
                    ledger.no_issue_units[unit_id] = checked

        return overflow

    def _validate_hypothesis(
        self,
        item: Any,
        right_lines: dict[str, dict[int, str]],
    ) -> tuple[dict[str, Any] | None, list[tuple[str, int, str]]]:
        if not isinstance(item, dict):
            return None, []
        unit_id = str(item.get("unit_id") or "").strip()
        mechanism = _parse_mechanism(item.get("mechanism"))
        claim = str(item.get("claim") or "").strip()
        trigger = str(item.get("trigger") or "").strip()
        refutation = str(item.get("refutation") or "").strip()
        open_question = str(item.get("open_question") or "").strip()
        if not unit_id or mechanism is None or not (claim and trigger and refutation and open_question):
            return None, []

        sites = _validate_sites(item.get("sites"), right_lines)
        severity = str(item.get("severity") or "").strip().lower()
        if severity not in _SEVERITIES:
            severity = "info"
        payload = {
            "unit_id": unit_id,
            "mechanism": mechanism,
            "anchor_symbol": str(item.get("anchor_symbol") or "").strip(),
            "claim": claim,
            "trigger": trigger,
            "impact": str(item.get("impact") or "").strip(),
            "open_question": open_question,
            "refutation": refutation,
            "severity": severity,
        }
        return payload, sites


__all__ = ["HypothesisGenerationResult", "HypothesisGenerator"]
