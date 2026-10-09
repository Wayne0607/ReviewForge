"""Specialist lenses for the hypothesis pipeline.

A lens is a risk-triggered, single-shot reviewer. It reuses the generator's
parsing, validation and ledger upsert but scopes its input to the units that
triggered it, uses the ``lens`` prompt template, and tags every hypothesis with
``source="lens:<name>"``.  Lenses are strictly additive — they write OPEN
hypotheses into the shared ledger and never investigate.

Triggering is purely deterministic: path and added-line rules plus the unit
risk signals.  The LLM does the actual reasoning; the trigger only decides
whether a cheap second pass is worth an extra call.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from langchain_core.language_models import BaseChatModel

from reviewforge.core.state import StateStore
from reviewforge.engine.context_pack import ContextPack
from reviewforge.engine.detectors.unified_diff import iter_added_lines
from reviewforge.engine.hypothesis import HypothesisLedger
from reviewforge.engine.hypothesis_generator import HypothesisGenerationResult, HypothesisGenerator
from reviewforge.engine.semantic_diff import SemanticChangeSet, SemanticUnit

_LOCALIZATION_PATH = re.compile(r"\.(properties|po)$|messages_[^/]+\.json$|/locale/", re.IGNORECASE)
_MANIFEST_PATH = re.compile(
    r"(^|/)(package\.json|requirements[^/]*\.txt|setup\.py|pyproject\.toml|poetry\.lock|pipfile(\.lock)?|"
    r"go\.mod|go\.sum|gemfile(\.lock)?|cargo\.toml|cargo\.lock|yarn\.lock|package-lock\.json|pnpm-lock\.yaml)$",
    re.IGNORECASE,
)
_ACCESSIBILITY_EXTS = (".tsx", ".jsx", ".vue", ".svelte")
_ACCESSIBILITY_TOKENS = ("<button", "<input", "aria-", "role=")
_CONCURRENCY_TOKENS = ("go func", "Mutex", "RLock", "asyncio.gather", "Promise.all", "threading", "forEach(async")
_SECURITY_SINKS = re.compile(
    r"\b(eval|exec)\s*\(|pickle\.loads?\s*\(|yaml\.load\s*\(|dangerouslySetInnerHTML|innerHTML\s*=|"
    r"os\.system\s*\(|subprocess\.(?:call|run|Popen|check_output)\s*\(|shell=True"
)

# Fixed selection order keeps the result deterministic before the risk sort.
_LENS_ORDER = ("security", "localization", "accessibility", "concurrency", "dependency")

# lens name -> skills/<dir> whose SKILL.md is injected into the lens prompt.
_LENS_SKILLS = {
    "security": "security_rules",
    "localization": "localization_rules",
}

_REASONS = {
    "security": "security-sensitive symbol or sink in added lines",
    "localization": "changed localization resource paths",
    "accessibility": "changed markup with interactive/a11y elements",
    "concurrency": "added concurrency primitives",
    "dependency": "changed manifest/lockfile",
}


@dataclass(frozen=True)
class LensSelection:
    """One lens that should run, with the units that triggered it."""

    name: str
    units: list[str]
    reason: str
    risk: float


def _added_content(diff: str) -> str:
    return "\n".join(content for _line, content in iter_added_lines(diff or ""))


def _security_trigger(unit: SemanticUnit, added: str) -> bool:
    if any(str(signal.get("type", "")).startswith("security") for signal in unit.risk_signals):
        return True
    return bool(_SECURITY_SINKS.search(added))


def _localization_trigger(unit: SemanticUnit, _added: str) -> bool:
    return bool(_LOCALIZATION_PATH.search(unit.path))


def _accessibility_trigger(unit: SemanticUnit, added: str) -> bool:
    if not unit.path.lower().endswith(_ACCESSIBILITY_EXTS):
        return False
    return any(token in added for token in _ACCESSIBILITY_TOKENS)


def _concurrency_trigger(_unit: SemanticUnit, added: str) -> bool:
    return any(token in added for token in _CONCURRENCY_TOKENS)


def _dependency_trigger(unit: SemanticUnit, _added: str) -> bool:
    return bool(_MANIFEST_PATH.search(unit.path))


_TRIGGERS = {
    "security": _security_trigger,
    "localization": _localization_trigger,
    "accessibility": _accessibility_trigger,
    "concurrency": _concurrency_trigger,
    "dependency": _dependency_trigger,
}


def select_lenses(state: StateStore, changeset: SemanticChangeSet, *, max_lenses: int = 3) -> list[LensSelection]:
    """Return the lenses triggered by this change set, capped and risk-ordered."""

    diffs = state.file_diffs or {}
    selections: list[LensSelection] = []
    for name in _LENS_ORDER:
        trigger = _TRIGGERS[name]
        triggered = [unit for unit in changeset.units if trigger(unit, _added_content(diffs.get(unit.path, "")))]
        if triggered:
            selections.append(
                LensSelection(
                    name=name,
                    units=[unit.id for unit in triggered],
                    reason=_REASONS[name],
                    risk=sum(unit.risk_score for unit in triggered),
                )
            )
    selections.sort(key=lambda selection: (-selection.risk, selection.name))
    return selections[: max(1, max_lenses)]


def lens_skill_body(name: str) -> str:
    """Return the SKILL.md body for a lens, or an empty string."""

    directory = _LENS_SKILLS.get(name)
    if not directory:
        return ""
    path = Path(__file__).resolve().parent.parent / "skills" / directory / "SKILL.md"
    return path.read_text(encoding="utf-8") if path.exists() else ""


def build_lens_generator(
    llm: BaseChatModel, name: str, *, max_hypotheses: int = 12, output_language: str = "en"
) -> HypothesisGenerator:
    return HypothesisGenerator(
        llm,
        max_hypotheses=max_hypotheses,
        output_language=output_language,
        source=f"lens:{name}",
        prompt_template="lens",
        skill_body=lens_skill_body(name),
    )


async def run_lens(
    llm: BaseChatModel,
    name: str,
    selection: LensSelection,
    state: StateStore,
    pack: ContextPack,
    changeset: SemanticChangeSet,
    ledger: HypothesisLedger,
    *,
    output_language: str = "en",
    max_hypotheses: int = 12,
) -> HypothesisGenerationResult:
    """Execute one lens over the units that triggered it and upsert into the ledger."""

    triggered_ids = set(selection.units)
    filtered = SemanticChangeSet(
        repo=changeset.repo,
        pr_number=changeset.pr_number,
        head_sha=changeset.head_sha,
        units=[unit for unit in changeset.units if unit.id in triggered_ids],
    )
    generator = build_lens_generator(llm, name, max_hypotheses=max_hypotheses, output_language=output_language)
    return await generator.run(state, pack, filtered, ledger)


__all__ = ["LensSelection", "build_lens_generator", "lens_skill_body", "run_lens", "select_lenses"]
