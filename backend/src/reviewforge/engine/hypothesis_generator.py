"""Hypothesis generator for the hypothesis pipeline.

One bounded pass over the whole PR: the deterministic context pack plus the
right-side diff are rendered per semantic unit (risk-ordered, chunked when the
input exceeds the configured budget) and the model proposes testable
hypotheses.  The generator only *proposes*; it never investigates and never
retries with a "look harder" signal — a block with no hypotheses is a valid
NO_ISSUE result, and a block that fails to parse is marked ``unresolved``.

The generator writes into the shared ``HypothesisLedger``.  Every emitted
hypothesis is ``OPEN`` by construction; investigation (a later stage) is the
only consumer allowed to move it away from ``OPEN``.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from reviewforge.core.json_output import extract_json_value
from reviewforge.core.state import StateStore
from reviewforge.engine.context_pack import ContextPack
from reviewforge.engine.detectors.unified_diff import iter_right_lines
from reviewforge.engine.hypothesis import Hypothesis, HypothesisLedger, Mechanism, Site
from reviewforge.engine.prompts_v4 import load_prompt
from reviewforge.engine.semantic_diff import SemanticChangeSet, SemanticUnit

_EXCERPT_MIN_CHARS = 12
_SEVERITIES = frozenset({"error", "warning", "info"})
_SEVERITY_PRIORITY = {"info": 0, "warning": 1, "error": 2}

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


def _render_changes(units: list[SemanticUnit], right_lines: dict[str, dict[int, str]], max_lines: int) -> str:
    """Render the RIGHT-side diff for a set of units with line numbers."""

    sections: list[str] = []
    for unit in units:
        lines = _unit_right_lines(unit, right_lines, max_lines)
        header = f"### {unit.path} — symbol={unit.symbol or '-'} unit_id={unit.id}"
        if not lines:
            sections.append(f"{header}\n(no RIGHT-side lines)")
            continue
        body = "\n".join(f"{line:>5} | {content}" for line, content in lines)
        sections.append(f"{header}\n{body}")
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
    ) -> None:
        self._llm = llm
        self._max_input_chars = max(1, int(max_input_chars))
        self._max_hypotheses = max(1, int(max_hypotheses))
        self._context_max_chars = max(1, int(context_max_chars))
        self._output_language = output_language
        self._source = source
        self._prompt_template = prompt_template
        self._skill_body = skill_body

    def _system_prompt(self) -> str:
        lens_name = self._source[len("lens:") :] if self._source.startswith("lens:") else self._source
        prompt = load_prompt(
            self._prompt_template,
            output_language=_language_directive(self._output_language),
            lens=lens_name,
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
        per_unit_context = max(1_000, self._context_max_chars // max(1, len(units)))
        changes_by_unit = {unit.id: _render_changes([unit], right_lines, 400) for unit in units}
        context_by_unit = {unit.id: pack.render_for_unit(unit.id, max_chars=per_unit_context) for unit in units}

        blocks = self._chunk_units(units, changes_by_unit, context_by_unit)
        result.blocks = len(blocks)

        for block in blocks:
            user = self._render_user_message(pack, block, changes_by_unit, context_by_unit, ledger)
            parsed = await self._invoke_once(user)
            if parsed is None:
                parsed = await self._invoke_repair(user)
            if parsed is None:
                result.failed_blocks += 1
                for unit in block:
                    ledger.unresolved_units[unit.id] = "generator parse failure"
                    result.unresolved_units.append(unit.id)
                continue
            result.dropped_overflow += self._consume(parsed, ledger, result, right_lines)
        return result

    def _chunk_units(
        self,
        units: list[SemanticUnit],
        changes_by_unit: dict[str, str],
        context_by_unit: dict[str, str],
    ) -> list[list[SemanticUnit]]:
        blocks: list[list[SemanticUnit]] = []
        current: list[SemanticUnit] = []
        current_chars = _BLOCK_OVERHEAD
        for unit in units:
            unit_chars = len(changes_by_unit.get(unit.id, "")) + len(context_by_unit.get(unit.id, ""))
            if current and current_chars + unit_chars > self._max_input_chars:
                blocks.append(current)
                current = []
                current_chars = _BLOCK_OVERHEAD
            current.append(unit)
            current_chars += unit_chars
        if current:
            blocks.append(current)
        return blocks

    def _render_user_message(
        self,
        pack: ContextPack,
        block: list[SemanticUnit],
        changes_by_unit: dict[str, str],
        context_by_unit: dict[str, str],
        ledger: HypothesisLedger,
    ) -> str:
        sections: list[str] = []
        sections.append("## PR intent\n" + (pack.pr_intent or "（无）/(none)"))
        changes = "\n\n".join(changes_by_unit[unit.id] for unit in block)
        sections.append("## Changes\n" + changes)
        context = "\n\n".join(context_by_unit[unit.id] for unit in block if context_by_unit.get(unit.id))
        sections.append("## Context\n" + (context or "（无）/(none)"))
        sections.append("## Unchecked\n" + _render_unchecked(pack))
        sections.append("## Existing hypotheses\n" + _render_existing(ledger))
        return "\n\n".join(sections)

    async def _invoke_once(self, user: str) -> dict[str, Any] | None:
        messages = [
            SystemMessage(content=self._system_prompt()),
            HumanMessage(content=user),
        ]
        response = await self._llm.ainvoke(messages)
        return extract_json_value(getattr(response, "content", "") or "", required_key="hypotheses", allow_list=False)

    async def _invoke_repair(self, user: str) -> dict[str, Any] | None:
        messages = [
            SystemMessage(content=self._system_prompt()),
            HumanMessage(content=user),
            HumanMessage(
                content=(
                    "Your previous response was not valid hypotheses JSON. "
                    'Return only JSON with a "hypotheses" array and a "no_issue_units" array; '
                    "use an empty array when there is nothing to report."
                )
            ),
        ]
        response = await self._llm.ainvoke(messages)
        return extract_json_value(getattr(response, "content", "") or "", required_key="hypotheses", allow_list=False)

    def _consume(
        self,
        parsed: dict[str, Any],
        ledger: HypothesisLedger,
        result: HypothesisGenerationResult,
        right_lines: dict[str, dict[int, str]],
    ) -> int:
        valid: list[tuple[dict[str, Any], list[tuple[str, int, str]]]] = []

        raw_hypotheses = parsed.get("hypotheses")
        if isinstance(raw_hypotheses, list):
            for item in raw_hypotheses:
                payload, sites = self._validate_hypothesis(item, right_lines)
                if payload is None:
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
            identity = _identity(payload["unit_id"], payload["mechanism"], payload["anchor_symbol"])
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
            result.accepted += 1

        raw_no_issue = parsed.get("no_issue_units")
        if isinstance(raw_no_issue, list):
            for item in raw_no_issue:
                if not isinstance(item, dict):
                    continue
                unit_id = str(item.get("unit_id") or "").strip()
                if unit_id:
                    ledger.no_issue_units[unit_id] = str(item.get("checked") or "").strip()

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
