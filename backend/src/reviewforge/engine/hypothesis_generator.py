"""Hypothesis generator for the hypothesis pipeline.

Protocols v1/v2 keep their original PR pass and risk-ordered input chunking.
Protocols v3/v4 deliver bounded groups of related changes, exact per-unit source
scope and domain responsibilities, sharing each complete before/after hunk.
Explicit before/RIGHT coordinates and declaration presence keep new code and
old implementations distinct. V3 candidate violation obligations separately bind
the proposed requirement, supported input, incompatible after behavior and harm;
an observed before/after delta alone does not enter the hypothesis queue.
These checks validate source and schema, never semantic proof. V4 uses strict
JSON batches for multiple targets and flat native records for a single target,
preserving complete candidate records
without treating a malformed call stream as clean coverage. Protocol v3 keeps
its text output and violation obligations. The generator
only *proposes*; it never investigates and never
retries with a "look harder" signal — explicit clean assessments are valid
NO_ISSUE results. Units omitted from an otherwise valid response, or in a
block that fails to parse, are marked ``unresolved``.

The generator writes into the shared ``HypothesisLedger``.  Every emitted
hypothesis is ``OPEN`` by construction; investigation (a later stage) is the
only consumer allowed to move it away from ``OPEN``.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.runnables.base import RunnableBindingBase

from reviewforge.core.change_manifest import FileChange, is_verified_rename
from reviewforge.core.json_output import extract_json_value
from reviewforge.core.state import StateStore
from reviewforge.engine.context_pack import ContextPack
from reviewforge.engine.declarations_v4 import extract_code_definitions
from reviewforge.engine.detectors.unified_diff import iter_right_lines, render_numbered_diff, select_diff_hunks
from reviewforge.engine.discovery import (
    DiscoveryBlock,
    DiscoveryLimits,
    compact_overview,
    compact_source_coordinates,
    plan_discovery,
    split_discovery,
)
from reviewforge.engine.discovery_aliases import (
    expand_response_refs,
    render_source_aliases,
    source_alias_legend,
    source_aliases,
    target_site_choices,
    target_source_choices,
)
from reviewforge.engine.discovery_output import frame_discovery_output
from reviewforge.engine.discovery_presentation import present_discovery_block, present_pr_overview
from reviewforge.engine.discovery_submission import (
    bind_discovery_submission,
    discovery_submission_tools,
    parse_discovery_submission,
)
from reviewforge.engine.hypothesis import Hypothesis, HypothesisLedger, Mechanism, Site
from reviewforge.engine.prompts_v4 import load_prompt
from reviewforge.engine.request_budget import RequestBudgetExceededError, RequestPlan, plan_request
from reviewforge.engine.semantic_diff import SemanticChangeSet, SemanticUnit
from reviewforge.engine.symbol_extractor import _find_enclosing_function
from reviewforge.engine.verification_guidance import (
    defect_scope_guidance,
    has_python_concurrency,
    has_state_navigation,
    investigation_capabilities,
    is_localization_path,
    localization_guidance,
    python_concurrency_guidance,
    state_guidance,
)
from reviewforge.tools.workspace import WorkspaceUnavailable

_EXCERPT_MIN_CHARS = 12


def discovery_uses_native(protocol_version: int, unit_count: int) -> bool:
    """Choose the wire contract before planning; required tools do not guarantee N calls."""
    return protocol_version >= 4 and unit_count == 1


def _discovery_output_floor(unit_count: int, allowance: int, *, protocol_version: int = 3) -> int:
    """Policy estimate for useful structured coverage, not a truncation guarantee."""
    if protocol_version >= 4:
        # V4 emits leads, without v3's six-field violation proof, and uses short
        # source aliases. Reserve per-unit coverage plus incremental candidates.
        # This is only a useful-output floor; the unchanged hard cap is 8192 and
        # incomplete/invalid responses still leave their units unresolved.
        return min(8192, 256 + 192 * unit_count + 384 * min(allowance, unit_count))
    return min(8192, 256 + 256 * unit_count + 640 * min(allowance, unit_count))


def _discovery_available(ledger: HypothesisLedger) -> int | None:
    """Fallback snapshot only; a live physical budget view remains authoritative."""
    saved = ledger.request_budget
    if ledger.protocol_version >= 8 and not saved:
        return None
    if saved.get("allocation_policy") == "usage-only-v1":
        from reviewforge.engine.request_budget import snapshot_available

        return snapshot_available(saved, fallback=250_000)
    pending = list(saved.get("pending", {}).values())
    remaining = (
        saved.get("limit", 250_000)
        - saved.get("reported_tokens", 0)
        - saved.get("unreported_reserved_tokens", 0)
        - saved.get("editor_reserve", 16_000)
        - sum(item["reserved_tokens"] for item in pending)
    )
    if saved.get("allocation_policy") == "adaptive-v1":
        return max(0, remaining - saved.get("investigation_reserve", 0))
    phase = saved.get("discovery_limit", 80_000) - saved.get("charged_by_phase", {}).get("discovery", 0)
    phase -= sum(item["reserved_tokens"] for item in pending if item["phase"] == "discovery")
    return max(0, min(remaining, phase))


def _discovery_request_plan(llm, messages, ledger: HypothesisLedger) -> RequestPlan:
    """Use live configuration, pending reservations and task limits when present."""
    node, options = llm, {"max_tokens": 8192}
    while isinstance(node, RunnableBindingBase):
        options = node.kwargs | options
        node = node.bound
    viewer = getattr(node, "request_budget_view", None)
    view = viewer(messages, options) if callable(viewer) else None
    available = view.available_tokens if view is not None else _discovery_available(ledger)
    return plan_request(llm, messages, max_output_tokens=8192, fallback_available=available)


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
    capacity_deferred_units: list[str] = field(default_factory=list)


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
    lines = [f"- {hypothesis.identity} [{hypothesis.status.value}] :: {hypothesis.claim}" for hypothesis in items]
    return "\n".join(lines)


def _language_directive(language: str) -> str:
    if language == "zh-CN":
        return "简体中文"
    return "English"


def _identity(unit_id: str, mechanism: Mechanism, anchor: str) -> str:
    return f"{unit_id}::{mechanism.value}::{anchor}"


def _new_hypothesis_id(identity: str) -> str:
    return "h_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:8]


def _coordinate_ranges(lines: list[int]) -> str:
    ranges = []
    for line in sorted(set(lines)):
        if ranges and line == ranges[-1][1] + 1:
            ranges[-1][1] = line
        else:
            ranges.append([line, line])
    return ", ".join(str(start) if start == end else f"{start}-{end}" for start, end in ranges)


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
        json_object_output: bool | None = None,
        discovery_target_input_chars: int = 32_000,
        discovery_diff_max_chars: int = 20_000,
        discovery_context_max_chars: int = 8_000,
        discovery_max_units: int = 12,
        discovery_max_blocks: int = 8,
        discovery_max_hypotheses_per_block: int = 6,
        unit_responsibilities: dict[str, list[str]] | None = None,
        responsibility_rules: dict[str, str] | None = None,
        executor: Any = None,
        evidence_cache: Any = None,
        session_request_limit: int = 32,
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
        self._json_object_output = (
            bool(getattr(llm, "json_object_output", False)) if json_object_output is None else json_object_output
        )
        self._discovery_limits = DiscoveryLimits(
            target_input_chars=max(1, min(int(discovery_target_input_chars), self._max_input_chars)),
            hard_input_chars=self._max_input_chars,
            diff_max_chars=max(1, int(discovery_diff_max_chars)),
            context_max_chars=max(0, int(discovery_context_max_chars)),
            global_context_max_chars=max(0, int(context_max_chars)),
            max_units=max(1, int(discovery_max_units)),
        )
        self._discovery_max_blocks = max(1, int(discovery_max_blocks))
        self._discovery_max_hypotheses_per_block = max(1, int(discovery_max_hypotheses_per_block))
        self._unit_responsibilities = copy.deepcopy(unit_responsibilities or {})
        self._responsibility_rules = dict(responsibility_rules or {})
        self._executor = executor
        self._evidence_cache = evidence_cache
        self._session_request_limit = max(1, int(session_request_limit))

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
        return prompt + "\n\n" + defect_scope_guidance() + "\n\n" + investigation_capabilities()

    async def run(
        self,
        state: StateStore,
        pack: ContextPack,
        changeset: SemanticChangeSet,
        ledger: HypothesisLedger,
        *,
        resume: bool = False,
        overview_changeset: SemanticChangeSet | None = None,
    ) -> HypothesisGenerationResult:
        if ledger.protocol_version >= 6:
            from reviewforge.engine.discovery_workflow import run_discovery

            return await run_discovery(self, state, pack, changeset, ledger, overview_changeset=overview_changeset)
        if ledger.protocol_version >= 5:
            from reviewforge.engine.discovery_session import run_discovery

            return await run_discovery(self, state, pack, changeset, ledger, overview_changeset=overview_changeset)
        if ledger.protocol_version >= 3:
            return await self._run_discovery(
                state, pack, changeset, ledger, resume=resume, overview_changeset=overview_changeset
            )
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
                        if isinstance(original, str) and not original.strip():
                            failure = f"{self._source} empty model response"
                        else:
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

    def _discovery_system_prompt(
        self,
        allowance: int,
        responsibilities: list[str] | None = None,
        *,
        protocol_version: int = 3,
        unit_count: int = 1,
    ) -> str:
        template = "discovery_v3"
        if protocol_version >= 4:
            template = "discovery_v4" if discovery_uses_native(protocol_version, unit_count) else "discovery_batch_v4"
        prompt = load_prompt(
            template,
            output_language=_language_directive(self._output_language),
            max_hypotheses=allowance,
            lens=self._source[len("lens:") :] if self._source.startswith("lens:") else "general correctness",
        )
        if self._skill_body:
            prompt += f"\n\n## Specialist rules\n{self._skill_body}"
        seen = set()
        for name in responsibilities or []:
            body = self._responsibility_rules.get(name, "")
            if body and body not in seen:
                seen.add(body)
                prompt += f"\n\n## Responsibility rules: {name} (rules, not evidence)\n{body}"
        # v3 includes scope/capabilities in its single output contract. Keep the
        # legacy prompts' overlays separate; repeating them here costs every block.
        return prompt

    def _render_discovery_message(
        self, block: DiscoveryBlock, ledger: HypothesisLedger, allowance: int, guidance: str = ""
    ) -> str:
        shown = present_discovery_block(block) if ledger.protocol_version >= 4 else None
        aliases = source_aliases(block.source_refs, scoped=True) if shown else {}
        anchors = []
        for unit in block.units:
            target = block.unit_facts.get(unit.id, [])
            coordinates = _coordinate_ranges([fact.line for fact in target if fact.side == "after"])
            before = _coordinate_ranges([fact.line for fact in target if fact.side == "before"])
            change = block.structural_changes.get(unit.id)
            anchors.append(
                json.dumps(
                    [
                        unit.id,
                        unit.path,
                        unit.symbol or "",
                        block.unit_before_states.get(unit.id),
                        before,
                        coordinates,
                        self._unit_responsibilities.get(unit.id, []),
                        change.id if change else "",
                        *(target_source_choices(block, unit.id, aliases) if shown else []),
                        *([target_site_choices(block, unit.id, aliases)] if shown else []),
                    ],
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
        # Cross-block rows retain identity/trigger/impact navigation but cannot
        # consume the changed-source budget or count as a fresh assessment.
        existing = []
        for item in sorted(ledger.items.values(), key=lambda item: item.identity):
            existing.append(f"- {item.identity} [{item.status.value}] trigger={item.trigger}; impact={item.impact}")
        existing_text = "\n".join(existing)
        if len(existing_text) > 2_000:
            existing_text = (
                existing_text[:1_900] + "\n(existing-hypothesis navigation truncated; not proof of coverage)"
            )
        columns = "unit_id,path,symbol,before_state,LEFT_ranges,RIGHT_ranges,responsibilities,change_id"
        if shown:
            columns += ",allowed_changed_before_refs,allowed_after_refs,allowed_site_refs"
        sections = [
            "## PR overview\n"
            + (
                "PR-wide navigation only; these paths/ranges/symbols are not assessment targets.\n"
                + present_pr_overview(block.overview)
                if shown
                else compact_overview(block.overview)
            ),
            "## Source relationship navigation (not evidence)\n"
            + ((shown.navigation if shown else block.navigation) or "(none collected)"),
            "## Units\nAllowed IDs/target scopes; each JSON row is "
            f"[{columns}]. "
            "Empty ranges mean no target source delivered on that side; "
            "[] responsibilities means general correctness.\n" + "\n".join(anchors),
            "## Changes\n" + (shown.diff_text if shown else compact_source_coordinates(block.diff_text)),
            "## Context\n"
            + (
                (shown.context_text if shown else compact_source_coordinates(block.context_text))
                or "(no context delivered)"
            ),
            "## Structured difference focus (attention cues, not defect verdicts)\n"
            + (block.focus or "Compare before/after behavior."),
            "## Unchecked\n" + ("\n".join(block.unchecked) or "(no collected direction omitted)"),
            "## Existing hypotheses\n" + (existing_text or "(none)"),
        ]
        if shown:
            behavior_slot = (
                "behavior refs"
                if discovery_uses_native(ledger.protocol_version, len(block.units))
                else "behavior.source_facts"
            )
            change_slot = (
                "structural fact selections"
                if discovery_uses_native(ledger.protocol_version, len(block.units))
                else "behavior.change_facts"
            )
            sections[2] += (
                "\nAllowed refs are navigation to the target operation, not completed evidence checks. "
                f"Select relevant refs yourself in {behavior_slot}: at least one from each nonempty "
                "allowed side. Empty before choices mean no removed before citation is required, not global "
                "prior absence. Structural change rows have empty choices and still need their separate "
                f"{change_slot} and consumer-source checks. Sites must select allowed_site_refs."
            )
        if block.change_text:
            sections.insert(
                3, "## Structural changes (typed facts; not source lines or Observations)\n" + block.change_text
            )
        if shown:
            sections.append(
                "## Source reference files\n"
                "fN.Bline = before; fN.Rline = RIGHT/head; fN.Cline = Context. "
                "A .sN suffix identifies a separate excerpt segment; copy it exactly.\n"
                + source_alias_legend(block.source_refs)
            )
        if block.oversize:
            sections.append(
                "## Input size\nIndivisible shared hunk exceeds the soft diff target; "
                "supplied intact under the original hard cap."
            )
        if guidance:
            sections.append(guidance)
        sections.append(
            f"## Required assessments ({len(block.units)})\n"
            f"New candidate allowance this block: {allowance}. Assess every Units row using the output contract."
        )
        if shown:
            sections[-1] += "\nAllowed unit_id values for this block (copy exactly; no others): " + json.dumps(
                [unit.id for unit in block.units], ensure_ascii=False, separators=(",", ":")
            )
        rendered = "\n\n".join(sections)
        return render_source_aliases(rendered, aliases) if ledger.protocol_version >= 4 else rendered

    async def _run_discovery(
        self,
        state: StateStore,
        pack: ContextPack,
        changeset: SemanticChangeSet,
        ledger: HypothesisLedger,
        *,
        resume: bool,
        overview_changeset: SemanticChangeSet | None,
    ) -> HypothesisGenerationResult:
        result = HypothesisGenerationResult()
        completed = set()
        policy_deferred = set()
        if resume:
            for record in ledger.discovery_blocks.values():
                if record.get("source") == self._source:
                    completed.update(
                        unit_id for unit_id, row in record.get("units", {}).items() if row.get("status") == "assessed"
                    )
                    policy_deferred.update(
                        unit_id
                        for unit_id, row in record.get("units", {}).items()
                        if row.get("status") == "capacity-deferred"
                    )
        pending = SemanticChangeSet(
            repo=changeset.repo,
            pr_number=changeset.pr_number,
            head_sha=changeset.head_sha,
            units=[unit for unit in changeset.units if unit.id not in completed | policy_deferred],
        )
        blocks = plan_discovery(
            pending,
            pack,
            state.file_diffs or {},
            limits=self._discovery_limits,
            overview_changeset=overview_changeset or changeset,
            file_changes={
                path: change
                for path, change in (getattr(state, "file_changes", {}) or {}).items()
                if isinstance(change, FileChange)
                and change.path == path
                and change.repo == state.repo
                and change.head_repo == (state.head_repo or state.repo)
                and change.head_sha == state.head_sha
                and is_verified_rename(change, ledger.head_sha)
            },
        )
        right_lines = _right_lines_by_path(state)
        attempted_blocks = sum(
            record.get("source") == self._source
            and (record.get("request_attempted") or "attempted_responsibilities" in record)
            for record in ledger.discovery_blocks.values()
        )
        planned_context_chars = sum(len(block.context_text) for block in blocks)
        for index, block in enumerate(blocks):
            key = f"{self._source}:{block.id}"
            if key in ledger.discovery_blocks:
                # A repeated delivery retains the earlier failure and request
                # accounting. Recovery is another attempt, not a fresh budget.
                recovery = 2
                while f"{key}:attempt-{recovery}" in ledger.discovery_blocks:
                    recovery += 1
                key = f"{key}:attempt-{recovery}"
            record = {
                "source": self._source,
                "unit_ids": [unit.id for unit in block.units],
                "diff_chars": len(block.diff_text),
                **(
                    {
                        "change_chars": len(block.change_text),
                        "structural_changes": {
                            unit_id: change.to_dict() for unit_id, change in block.structural_changes.items()
                        },
                    }
                    if block.structural_changes
                    else {}
                ),
                "context_chars": len(block.context_text),
                "oversize": block.oversize,
                "units": {},
                "status": "pending",
                "source_ranges": {
                    unit.id: {
                        "before_state": block.unit_before_states.get(unit.id),
                        "before": _coordinate_ranges(
                            [fact.line for fact in block.unit_facts.get(unit.id, []) if fact.side == "before"]
                        ),
                        "after": _coordinate_ranges(
                            [fact.line for fact in block.unit_facts.get(unit.id, []) if fact.side == "after"]
                        ),
                    }
                    for unit in block.units
                },
            }
            ledger.discovery_blocks[key] = record
            aliases = source_aliases(block.source_refs, scoped=True) if ledger.protocol_version >= 4 else {}
            if aliases:
                record["source_ref_aliases"] = aliases
            used = sum(item.source == "generator" or item.source.startswith("lens:") for item in ledger.items.values())
            allowance = min(self._discovery_max_hypotheses_per_block, max(0, self._max_hypotheses - used))
            responsibilities = list(
                dict.fromkeys(name for unit in block.units for name in self._unit_responsibilities.get(unit.id, []))
            )
            record["requested_responsibilities"] = {
                unit.id: self._unit_responsibilities.get(unit.id, []) for unit in block.units
            }
            user = self._render_discovery_message(
                block,
                ledger,
                allowance,
                self._verification_guidance(
                    block.units, state.file_diffs or {}, pack, delivered_rules=set(responsibilities)
                ),
            )
            record["input_chars"] = len(user)
            messages = [
                SystemMessage(
                    content=self._discovery_system_prompt(
                        allowance,
                        responsibilities,
                        protocol_version=ledger.protocol_version,
                        unit_count=len(block.units),
                    )
                ),
                HumanMessage(content=user),
            ]
            record["system_chars"] = len(messages[0].content)
            failure = block.failure
            if any(not self._responsibility_rules.get(name, "").strip() for name in responsibilities):
                failure = "missing responsibility rules"
            if not failure and attempted_blocks >= self._discovery_max_blocks:
                failure = "capacity-deferred: discovery block limit"
            elif not failure and len(user) > self._max_input_chars:
                failure = "input-too-large"
            request_llm = self._llm
            submission_tools = []
            if ledger.protocol_version >= 4:
                record["submission_mode"] = (
                    "native" if discovery_uses_native(ledger.protocol_version, len(block.units)) else "batch-text"
                )
            if not failure and discovery_uses_native(ledger.protocol_version, len(block.units)):
                try:
                    submission_tools = discovery_submission_tools(block, aliases)
                    request_llm = bind_discovery_submission(self._llm, submission_tools)
                    record["submission_tools"] = [tool.name for tool in submission_tools]
                except Exception as exc:
                    failure = f"provider error: {type(exc).__name__}"
            if not failure:
                plan = _discovery_request_plan(request_llm, messages, ledger)
                floor = _discovery_output_floor(len(block.units), allowance, protocol_version=ledger.protocol_version)
                record["request_plan"] = {
                    "input_tokens": plan.input_tokens,
                    "available_tokens": plan.available_tokens,
                    "max_output_tokens": plan.max_output_tokens,
                    "useful_output_floor": floor,
                    "floor_is_policy_estimate": True,
                }
                if plan.max_output_tokens < floor:
                    children = split_discovery(block, self._discovery_limits)
                    replacement_context = sum(len(child.context_text) for child in children)
                    total_context = planned_context_chars - len(block.context_text) + replacement_context
                    if children and total_context <= self._discovery_limits.global_context_max_chars:
                        planned_context_chars = total_context
                        blocks[index + 1 : index + 1] = children
                        record["status"] = "partitioned"
                        record["split_reason"] = "budget-exhausted: input plus useful output does not fit"
                        record["child_ids"] = [child.id for child in children]
                        record["units"] = {unit.id: {"status": "partitioned"} for unit in block.units}
                        if self._on_update is not None:
                            await self._on_update(ledger)
                        continue
                    failure = "budget-exhausted: input plus useful output does not fit"
                    if children:
                        record["split_rejected"] = "preserved context exceeds aggregate context budget"
                    elif len(block.units) > 1:
                        record["split_rejected"] = "no complete source-preserving partition fits context limits"
            if failure:
                record["status"] = "capacity-deferred" if failure.startswith("capacity-deferred") else "unresolved"
                for unit in block.units:
                    self._discovery_unresolved(ledger, result, unit.id, failure)
                    record["units"][unit.id] = {"status": record["status"], "reason": failure}
                result.failed_blocks += 1
                if self._on_update is not None:
                    await self._on_update(ledger)
                continue
            result.blocks += 1
            attempted_blocks += 1
            record["request_attempted"] = True
            record["attempted_responsibilities"] = copy.deepcopy(record["requested_responsibilities"])
            try:
                response = await request_llm.ainvoke(
                    messages,
                    max_tokens=plan.max_output_tokens,
                )
                record["delivered_responsibilities"] = copy.deepcopy(record["requested_responsibilities"])
                original = getattr(response, "content", "") or ""
                framed = None
                if ledger.protocol_version >= 4:
                    framed, submission_errors = parse_discovery_submission(response, submission_tools, block, aliases)
                    if submission_errors:
                        record["submission_errors"] = submission_errors
                if framed is None:
                    framed = frame_discovery_output(original)
                parsed = framed.payload if framed is not None else None
                if framed is not None:
                    record["output_framing"] = {"complete": framed.complete, "recovery": framed.recovery}
                    if framed.rejected_sites:
                        record["rejected_sites"] = list(framed.rejected_sites)
                if parsed is None and ledger.protocol_version < 4 and isinstance(original, str) and original.strip():
                    parsed = await self._invoke_repair(original, ledger=ledger, request_floor=floor, record=record)
                if parsed is None:
                    failure = (
                        "empty model response"
                        if isinstance(original, str) and not original.strip()
                        else "parse failure"
                    )
            except Exception as exc:
                parsed = None
                failure = (
                    "budget-exhausted"
                    if getattr(exc, "outcome", "") == "budget-exhausted"
                    else f"provider error: {type(exc).__name__}"
                )
                logger.warning("%s discovery failed: %s", self._source, type(exc).__name__)
            if parsed is None:
                record["status"] = "unresolved"
                for unit in block.units:
                    self._discovery_unresolved(ledger, result, unit.id, failure)
                    record["units"][unit.id] = {"status": "unresolved", "reason": failure}
                result.failed_blocks += 1
            else:
                if aliases:
                    parsed = expand_response_refs(parsed, aliases)
                await self._consume_discovery(parsed, block, ledger, result, right_lines, allowance, record)
            if self._on_update is not None:
                await self._on_update(ledger)
        return result

    def _discovery_unresolved(
        self, ledger: HypothesisLedger, result: HypothesisGenerationResult, unit_id: str, reason: str
    ) -> None:
        previous = ledger.unresolved_units.get(unit_id, "")
        if not previous or previous.startswith(f"{self._source} "):
            ledger.unresolved_units[unit_id] = f"{self._source} {reason}"
        ledger.no_issue_units.pop(unit_id, None)
        if unit_id not in result.unresolved_units:
            result.unresolved_units.append(unit_id)
        if reason.startswith("capacity-deferred") and unit_id not in result.capacity_deferred_units:
            result.capacity_deferred_units.append(unit_id)

    @staticmethod
    def _valid_behavior(
        item: dict[str, Any],
        unit: SemanticUnit,
        block: DiscoveryBlock,
        *,
        require_consumer: bool = False,
        protocol_version: int = 3,
    ) -> bool:
        behavior = item.get("behavior")
        if (
            not isinstance(behavior, dict)
            or protocol_version < 5
            and any(
                not isinstance(behavior.get(field), str) or not behavior[field].strip() or len(behavior[field]) > 600
                for field in ("before", "after", "observable")
            )
        ):
            return False
        facts = behavior.get("source_facts")
        # A resource unit can contain several distinct before/after pairs.
        # Short v4 refs need bounded metadata, not the old four-quote ceiling;
        # every fact still passes the same source and target checks below.
        max_facts = 12 if protocol_version >= 4 else 4
        if (
            not isinstance(facts, list)
            or not 1 <= len(facts) <= max_facts
            or not all(block.source_matches(fact) for fact in facts)
        ):
            return False
        if unit.id in block.structural_changes:
            return block.valid_change_facts(unit.id, behavior.get("change_facts")) and any(
                block.structural_source_matches(unit.id, fact, consumer=require_consumer) for fact in facts
            )
        if behavior.get("change_facts"):
            return False
        sides = {fact["side"] for fact in facts if fact["path"] == unit.path}
        target = block.unit_facts.get(unit.id, [])
        if not target:
            return False
        for side in ("before", "after"):
            relevant = [fact for fact in target if fact.side == side]
            # For an additive hunk the unchanged before context is not the
            # changed operation. Do not demand an unrelated citation merely to
            # fill a slot. A deletion/replacement still needs its exact before.
            if protocol_version >= 4 and side == "before" and not any(fact.changed for fact in relevant):
                continue
            if relevant and (
                side not in sides
                or not any(
                    fact["side"] == side
                    and block.unit_source_matches(unit.id, fact, changed=any(source.changed for source in relevant))
                    for fact in facts
                )
            ):
                return False
        return True

    @staticmethod
    def _valid_violation(item: dict[str, Any], block: DiscoveryBlock) -> bool:
        """Check discovery's source-bound obligations, never certify a defect.

        Before/after deltas alone are not an expected contract or harmful
        outcome. Whether the supplied source actually supports each statement
        remains an independent investigation/proof-gate question.
        """
        violation = item.get("violation")
        prose = ("required_behavior", "actual_incompatible_behavior", "negative_consequence", "supported_input")
        bases = ("requirement_basis", "input_basis")
        return (
            isinstance(violation, dict)
            and set(violation) == set(prose + bases)
            and all(
                isinstance(violation.get(field), str)
                and bool(violation[field].strip())
                and len(violation[field]) <= 600
                for field in prose
            )
            and all(
                isinstance(violation.get(field), list)
                and 1 <= len(violation[field]) <= 4
                and all(block.source_matches(fact) for fact in violation[field])
                for field in bases
            )
        )

    async def _consume_discovery(
        self,
        parsed: dict[str, Any],
        block: DiscoveryBlock,
        ledger: HypothesisLedger,
        result: HypothesisGenerationResult,
        right_lines: dict[str, dict[int, str]],
        allowance: int,
        record: dict[str, Any],
    ) -> None:
        units = {unit.id: unit for unit in block.units}
        answers: dict[str, list[tuple[str, dict[str, Any]]]] = {key: [] for key in units}
        failed: dict[str, str] = {}
        invalid_other = False
        for kind, array in (
            ("hypothesis", parsed.get("hypotheses", [])),
            ("no_issue", parsed.get("no_issue_units", [])),
            ("deferred", parsed.get("deferred_units", [])),
        ):
            if not isinstance(array, list):
                invalid_other = True
                continue
            for raw in array:
                unit_id = raw.get("unit_id") if isinstance(raw, dict) else None
                if not isinstance(unit_id, str) or unit_id not in units:
                    result.dropped_invalid += 1
                    invalid_other = True
                    continue
                answers[unit_id].append((kind, raw))
        candidates = []
        validated = []
        for unit_id, entries in answers.items():
            unit = units[unit_id]
            if not entries:
                failed[unit_id] = "missing unit assessment"
                continue
            # A valid candidate can coexist with an explicitly unchecked
            # boundary. Only a clean assessment conflicts with another answer.
            if len(entries) > 1 and any(kind == "no_issue" for kind, _ in entries):
                failed[unit_id] = "contradictory unit assessments"
                result.dropped_invalid += sum(kind == "hypothesis" for kind, _ in entries)
                continue
            for kind, raw in entries:
                if kind == "deferred":
                    if not isinstance(raw.get("reason"), str) or not raw["reason"].strip() or len(raw["reason"]) > 600:
                        failed[unit_id] = "invalid deferred assessment"
                    else:
                        failed[unit_id] = "deferred: " + raw["reason"].strip()
                        validated.append((kind, raw))
                    continue
                raw = self._resolve_discovery_sources(raw, block)
                if not self._valid_behavior(
                    raw, unit, block, require_consumer=kind == "no_issue", protocol_version=ledger.protocol_version
                ):
                    failed[unit_id] = "invalid source-backed behavior assessment"
                    result.dropped_invalid += int(kind == "hypothesis")
                    continue
                if kind == "no_issue":
                    if (
                        not isinstance(raw.get("checked"), str)
                        or not raw["checked"].strip()
                        or len(raw["checked"]) > 600
                    ):
                        failed[unit_id] = "missing concrete checked boundary"
                    else:
                        validated.append((kind, raw))
                    continue
                if ledger.protocol_version < 4 and not self._valid_violation(raw, block):
                    failed[unit_id] = "invalid source-backed violation obligations"
                    result.dropped_invalid += 1
                    continue
                payload, sites = self._validate_hypothesis(raw, right_lines)
                required = ("claim", "trigger", "impact", "open_question", "refutation", "discrepancy")
                if ledger.protocol_version >= 7:
                    # A lead already states the alleged violation in claim,
                    # trigger and impact. Duplicate prose is not new evidence.
                    required = required[:-1]
                if (
                    payload is None
                    or any(
                        not isinstance(raw.get(field), str) or not raw[field].strip() or len(raw[field]) > 600
                        for field in required
                    )
                    or not isinstance(raw.get("severity"), str)
                    or raw["severity"] not in _SEVERITIES
                    or not block.source_matches(raw.get("trigger_basis"))
                    or unit_id in block.structural_changes
                    and not block.structural_source_matches(unit_id, raw.get("trigger_basis"))
                ):
                    failed[unit_id] = "invalid candidate schema or source basis"
                    result.dropped_invalid += 1
                    continue
                if unit_id in block.structural_changes:
                    valid_location = raw.get("sites") == []
                else:
                    valid_location = not (
                        not sites
                        or not any(
                            block.unit_source_matches(
                                unit_id, {"side": "after", "path": path, "line": line, "excerpt": excerpt}
                            )
                            for path, line, excerpt in sites
                        )
                        or not isinstance(raw.get("sites"), list)
                        or any(
                            not isinstance(site, dict)
                            or not isinstance(site.get("path"), str)
                            or not isinstance(site.get("line"), int)
                            or isinstance(site["line"], bool)
                            or not isinstance(site.get("excerpt"), str)
                            for site in raw["sites"]
                        )
                        or not all(
                            block.source_matches({"side": "after", "path": path, "line": line, "excerpt": excerpt})
                            for path, line, excerpt in sites
                        )
                    )
                if not valid_location:
                    failed[unit_id] = "invalid candidate source location"
                    result.dropped_unanchored += 1
                    continue
                candidates.append(raw)
        candidates.sort(key=lambda raw: (-_SEVERITY_PRIORITY[raw["severity"]], raw["unit_id"], raw["claim"]))
        repeated = []
        if ledger.protocol_version >= 7:
            for raw in candidates:
                # A saved valid sibling remains part of the unit transaction.
                # Rechecking it cannot consume another candidate slot, or a
                # repair at the PR cap could never finish that unit.
                if any(
                    item.unit_id == raw["unit_id"]
                    and item.mechanism == raw["mechanism"]
                    and all(
                        getattr(item, field) == raw[field].strip()
                        for field in ("claim", "trigger", "impact", "open_question", "refutation")
                    )
                    and [(site.path, site.line, site.excerpt) for site in item.sites]
                    == [(site["path"], site["line"], site["excerpt"]) for site in raw["sites"]]
                    for item in ledger.items.values()
                ):
                    repeated.append(raw)
        fresh = [raw for raw in candidates if raw not in repeated]
        kept = [*repeated, *fresh[:allowance]]
        for raw in fresh[allowance:]:
            result.dropped_overflow += 1
            failed[raw["unit_id"]] = "capacity-deferred: PR candidate limit"
        if kept:
            await self._consume(
                {"hypotheses": kept, "no_issue_units": []},
                ledger,
                result,
                right_lines,
                units,
                structural_changes=block.structural_changes,
            )
        if ledger.protocol_version >= 7:
            record["validated_assessments"] = [*validated, *(("hypothesis", raw) for raw in kept)]
        for unit_id, entries in answers.items():
            if record.get("output_framing", {}).get("complete") is False:
                # Complete prefix candidates are still useful leads; a damaged
                # tail can hide contradictory/remaining assessments, so none of
                # this response establishes complete coverage or a clean unit.
                failed[unit_id] = "incomplete output record stream"
            if invalid_other and unit_id not in failed:
                failed[unit_id] = "invalid block output entries"
            if unit_id in failed:
                reason = failed[unit_id]
                status = "capacity-deferred" if reason.startswith("capacity-deferred") else "unresolved"
                self._discovery_unresolved(ledger, result, unit_id, reason)
                record["units"][unit_id] = {"status": status, "reason": reason}
                retained = [raw for raw in kept if raw["unit_id"] == unit_id]
                if retained:
                    record["units"][unit_id]["candidates"] = retained
                continue
            prior = ledger.unresolved_units.get(unit_id, "")
            if prior.startswith(f"{self._source} "):
                ledger.unresolved_units.pop(unit_id, None)
            kind, raw = entries[0]
            record["units"][unit_id] = {"status": "assessed", "assessment": kind, "behavior": raw["behavior"]}
            for previous in ledger.discovery_blocks.values():
                old_row = previous.get("units", {}).get(unit_id)
                if (
                    previous is not record
                    and previous.get("source") == self._source
                    and isinstance(old_row, dict)
                    and old_row.get("status") in {"unresolved", "capacity-deferred"}
                ):
                    old_row["status"] = "recovered"
                    old_row["resolved_by"] = block.id
                    if all(row.get("status") in {"assessed", "recovered"} for row in previous["units"].values()):
                        previous["status"] = "recovered"
            if kind == "hypothesis":
                record["units"][unit_id]["candidates"] = [raw for raw in kept if raw["unit_id"] == unit_id]
            elif unit_id not in ledger.unresolved_units and not any(
                item.unit_id == unit_id for item in ledger.items.values()
            ):
                ledger.no_issue_units[unit_id] = raw["checked"].strip()
        record["status"] = "partial" if failed or invalid_other else "completed"
        if failed or invalid_other:
            result.failed_blocks += 1

    @staticmethod
    def _resolve_discovery_sources(raw: dict[str, Any], block: DiscoveryBlock) -> dict[str, Any]:
        """Resolve navigation IDs before the same source/target schema checks.

        Unknown IDs stay invalid. Model-provided coordinates cannot override
        an ID, and a before/context reference never becomes a RIGHT site.
        """
        item = copy.deepcopy(raw)
        behavior = item.get("behavior")
        if isinstance(behavior, dict) and isinstance(behavior.get("source_facts"), list):
            behavior["source_facts"] = [block.resolve_source_fact(fact) for fact in behavior["source_facts"]]
        if "trigger_basis" in item:
            item["trigger_basis"] = block.resolve_source_fact(item["trigger_basis"])
        violation = item.get("violation")
        if isinstance(violation, dict):
            for field in ("requirement_basis", "input_basis"):
                if isinstance(violation.get(field), list):
                    violation[field] = [block.resolve_source_fact(fact) for fact in violation[field]]
        if isinstance(item.get("sites"), list):
            sites = []
            for site in item["sites"]:
                if isinstance(site, str) or isinstance(site, dict) and "source_ref" in site:
                    fact = block.resolve_source_fact(site)
                    site = (
                        {field: fact[field] for field in ("path", "line", "excerpt")}
                        if fact is not None and fact["side"] == "after"
                        else None
                    )
                sites.append(site)
            item["sites"] = sites
        return item

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
                + len(self._verification_guidance(candidate, diffs, pack))
            )
            if current and candidate_chars > self._max_input_chars:
                blocks.append(current)
                current = [unit]
            else:
                current = candidate
        if current:
            blocks.append(current)
        return blocks

    def _verification_guidance(
        self,
        units: list[SemanticUnit],
        diffs: dict[str, str],
        pack: ContextPack | None = None,
        *,
        delivered_rules: set[str] | None = None,
    ) -> str:
        # Lenses carry their domain guides in the system prompt. State-flow
        # proof rules follow collected navigation in all discovery roles.
        guides = []
        if self._prompt_template == "generator":
            if any(is_localization_path(unit.path) for unit in units) and "localization" not in (
                delivered_rules or set()
            ):
                guides.append(localization_guidance())
            if any(
                has_python_concurrency(unit.path, diffs.get(unit.path, "")) for unit in units
            ) and "concurrency" not in (delivered_rules or set()):
                guides.append(python_concurrency_guidance())
        if pack is not None and any(has_state_navigation(pack, unit.id) for unit in units):
            guides.append(state_guidance())
        return "## Verification guidance\n" + "\n\n".join(guides) if guides else ""

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
        guidance = self._verification_guidance(block, diffs, pack)
        if guidance:
            sections.append(guidance)
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

    async def _invoke_repair(
        self,
        original: str,
        *,
        ledger: HypothesisLedger | None = None,
        request_floor: int = 0,
        record: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        failure = (
            'return {"unrepairable":true}. Output only the repaired JSON object or that failure object.'
            if self._json_object_output
            else "return null. Output only the repaired JSON or null."
        )
        messages = [
            SystemMessage(
                content=(
                    "Repair the JSON formatting of the supplied response only. "
                    'The schema is an object with "hypotheses" and "no_issue_units" arrays. '
                    "Preserve every entry and its facts verbatim; do not review code again, "
                    "add hypotheses, invent missing field values, or discard incomplete entries. "
                    "If the response is truncated or lacks enough information for a faithful repair, " + failure
                )
            ),
            AIMessage(content=original),
            HumanMessage(content="Repair the supplied response without changing its contents."),
        ]
        output_tokens = 8192
        if ledger is not None and ledger.protocol_version >= 3:
            plan = _discovery_request_plan(self._llm, messages, ledger)
            if record is not None:
                record["repair_plan"] = {
                    "input_tokens": plan.input_tokens,
                    "available_tokens": plan.available_tokens,
                    "max_output_tokens": plan.max_output_tokens,
                    "useful_output_floor": request_floor,
                    "floor_is_policy_estimate": True,
                }
            if plan.max_output_tokens < request_floor:
                raise RequestBudgetExceededError("format repair input plus useful output does not fit")
            output_tokens = plan.max_output_tokens
        response = await self._llm.ainvoke(messages, max_tokens=output_tokens)
        content = getattr(response, "content", "") or ""
        if ledger is not None and ledger.protocol_version >= 3:
            # A model formatting pass must not bypass the original response's
            # duplicate-key and complete-record checks. Keep the repair's audit
            # separate from the original provider output and its framing.
            framed = frame_discovery_output(content)
            if record is not None:
                record["repair_output_framing"] = {
                    "complete": framed.complete if framed is not None else False,
                    "recovery": framed.recovery if framed is not None else "invalid",
                }
            return framed.payload if framed is not None and framed.complete else None
        return self._parse_response(content)

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
        *,
        structural_changes: dict[str, FileChange] | None = None,
    ) -> int:
        valid: list[tuple[dict[str, Any], list[tuple[str, int, str]]]] = []

        raw_hypotheses = parsed.get("hypotheses")
        if isinstance(raw_hypotheses, list):
            for item in raw_hypotheses:
                payload, sites = self._validate_hypothesis(item, right_lines)
                if payload is None or payload["unit_id"] not in units:
                    result.dropped_invalid += 1
                    continue
                change = (structural_changes or {}).get(payload["unit_id"])
                if change is not None and (
                    ledger.protocol_version < 3
                    or not is_verified_rename(change, ledger.head_sha)
                    or item.get("sites") != []
                ):
                    result.dropped_invalid += 1
                    continue
                if not sites and change is None:
                    result.dropped_unanchored += 1
                    continue
                if ledger.protocol_version >= 3:
                    payload["discovery_basis"] = copy.deepcopy(
                        {
                            field: item[field]
                            for field in ("behavior", "discrepancy", "trigger_basis", "violation")
                            if field in item
                        }
                    )
                payload["change_anchor"] = change
                valid.append((payload, sites))

        valid.sort(key=lambda entry: (-_SEVERITY_PRIORITY.get(entry[0]["severity"], 0), entry[0]["unit_id"]))
        kept = valid[: self._max_hypotheses]
        overflow = len(valid) - len(kept)

        for payload, sites in kept:
            unit = units[payload["unit_id"]]
            # The model can describe an anchor, but cannot define its identity.
            # Prefer a primary site in the declared unit, then use stable order.
            change = payload.get("change_anchor")
            if change is not None:
                anchor = f"file-change:{change.id}"
            elif ledger.protocol_version >= 3:
                primary = min(
                    sites,
                    key=lambda site: (
                        site[0] != unit.path
                        or bool(unit.start_line)
                        and not unit.start_line <= site[1] <= unit.end_line,
                        site[0],
                        site[1],
                    ),
                )
            else:
                primary = min(sites, key=lambda site: (site[0] != unit.path, site[0], site[1]))
            if change is None:
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
                **({"discovery_basis": payload["discovery_basis"]} if ledger.protocol_version >= 3 else {}),
                **({"change_anchor": change} if change is not None else {}),
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
