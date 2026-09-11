"""Hypothesis-pipeline orchestration entrypoint.

Shadow mode runs the deterministic stages plus the LLM stages (generator → lens
→ investigator) and persists the ledger, but never publishes.  ``hypothesis``
mode is rejected until T9 wires editor + publication.
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass
from typing import Any

from reviewforge.core.state import StateStore
from reviewforge.engine.context_pack import ContextPack
from reviewforge.engine.detectors.unified_diff import iter_added_lines, iter_right_lines
from reviewforge.engine.editor import Editor, Publication, render_review_body
from reviewforge.engine.hypothesis import Hypothesis, HypothesisLedger, HypothesisStatus, Mechanism, Site
from reviewforge.engine.hypothesis_generator import HypothesisGenerator
from reviewforge.engine.investigator import Investigator, build_workspace_executor
from reviewforge.engine.language import resolve_output_language
from reviewforge.engine.lenses import run_lens, select_lenses
from reviewforge.engine.phase0 import scan_changed_files
from reviewforge.engine.run_health import RunHealth
from reviewforge.engine.security_categories import is_security_category
from reviewforge.engine.semantic_diff import SemanticChangeSet, SemanticUnit, compile_semantic_changeset
from reviewforge.tools.workspace import WorkspaceUnavailable

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _UnavailableWorkspace:
    digest: str
    source: str = "api-fallback"


_SEVERITY_ALIASES = {
    "critical": "error",
    "high": "error",
    "medium": "warning",
    "moderate": "warning",
    "low": "info",
}


def _normalize_severity(value: str) -> str:
    severity = str(value or "").strip().lower()
    severity = _SEVERITY_ALIASES.get(severity, severity)
    return severity if severity in {"error", "warning", "info"} else "info"


def _mechanism_for(category: str) -> Mechanism | None:
    if is_security_category(category):
        return Mechanism.SECURITY_SINK
    if category in {"missing-alt", "missing-label", "a11y", "accessibility"}:
        return Mechanism.A11Y
    # dependency / quality detections have no Mechanism in the enum (§4.3).
    return None


def _unit_for(units: list[SemanticUnit], path: str, line: int) -> SemanticUnit | None:
    enclosing = [unit for unit in units if unit.path == path and unit.start_line <= line <= unit.end_line]
    if enclosing:
        return min(enclosing, key=lambda unit: unit.end_line - unit.start_line)
    same_file = [unit for unit in units if unit.path == path]
    return same_file[0] if same_file else None


def _line_content(diff: str, line_no: int) -> str:
    for line, content in iter_added_lines(diff or ""):
        if line == line_no:
            return content
    return ""


def _seed_detector_hypotheses(
    state: StateStore,
    changeset: SemanticChangeSet,
    ledger: HypothesisLedger,
    findings: list[Any],
) -> int:
    """Turn deterministic detector findings into CONFIRMED, strong hypotheses."""

    diffs = state.file_diffs or {}
    seeded = 0
    for finding in findings:
        mechanism = _mechanism_for(finding.category)
        if mechanism is None:
            continue
        unit = _unit_for(changeset.units, finding.file, max(1, finding.line))
        unit_id = unit.id if unit is not None else f"{finding.file}:line{max(1, finding.line)}"
        anchor = (unit.symbol if unit else "") or f"line{max(1, finding.line)}"
        excerpt = _line_content(diffs.get(finding.file, ""), max(1, finding.line)) or str(finding.message)
        identity = f"{unit_id}::{mechanism.value}::{anchor}"
        hypothesis = Hypothesis(
            id="h_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:8],
            identity=identity,
            unit_id=unit_id,
            mechanism=mechanism,
            claim=str(finding.message),
            trigger="",
            impact=str(finding.suggestion or finding.message),
            open_question="",
            refutation="",
            sites=[Site(path=str(finding.file), line=max(1, finding.line), excerpt=excerpt)],
            severity=_normalize_severity(finding.severity),
            source=f"detector:{finding.category}",
            status=HypothesisStatus.CONFIRMED,
            evidence_strength="strong",
        )
        ledger.upsert(hypothesis)
        seeded += 1
    return seeded


async def _run_llm_stages(
    orchestrator: Any,
    state: StateStore,
    changeset: SemanticChangeSet,
    pack: ContextPack,
    ledger: HypothesisLedger,
    workspace: Any,
    config: Any,
    language: str,
) -> Publication:
    """Generator → lenses → investigator → editor for one run."""

    router = orchestrator._model_router
    events = orchestrator._events

    generator = HypothesisGenerator(
        router.get_llm("hypothesis_generator"),
        max_input_chars=config.generator_max_input_chars,
        max_hypotheses=config.generator_max_hypotheses,
        context_max_chars=config.context_pack_max_chars,
        output_language=language,
    )
    gen_result = await generator.run(state, pack, changeset, ledger)
    events.emit(
        "hypothesis.generated",
        {
            "pass": 1,
            "source": "generator",
            "accepted": gen_result.accepted,
            "dropped_unanchored": gen_result.dropped_unanchored,
            "dropped_overflow": gen_result.dropped_overflow,
            "tokens": 0,
        },
    )

    selections = select_lenses(state, changeset, max_lenses=config.max_lenses)
    events.emit(
        "lens.selected",
        {
            "lenses": [selection.name for selection in selections],
            "reasons": [selection.reason for selection in selections],
        },
    )
    for selection in selections:
        llm = router.get_llm(f"lens_{selection.name}")
        await run_lens(
            llm,
            selection.name,
            selection,
            state,
            pack,
            changeset,
            ledger,
            output_language=language,
            max_hypotheses=config.generator_max_hypotheses,
        )

    executor = build_workspace_executor(workspace, state)
    investigator = Investigator(router.get_llm("investigator"), executor, output_language=language)
    await investigator.run(
        ledger,
        state,
        pack,
        max_hypotheses_per_pr=config.investigator_max_hypotheses_per_pr,
        concurrency=config.investigator_concurrency,
    )

    editor = Editor(
        router.get_llm("editor"),
        output_language=language,
        max_inline=config.publish_max_inline,
        max_inline_overflow=config.publish_max_inline_overflow,
    )
    publication = await editor.run(ledger, pack)
    editor_stats = {
        "inline": len(publication.comments),
        "summary": len(publication.summary_items),
        "merged": len(publication.merged),
        "fallback": publication.fallback,
    }
    events.emit("editor.completed", editor_stats)

    for item in ledger.items.values():
        if item.source.startswith("detector"):
            continue
        if item.status == HypothesisStatus.UNKNOWN and item.verdict_reason == "budget-exhausted":
            events.emit("investigation.skipped", {"hypothesis_id": item.id, "reason": "budget-exhausted"})
        elif item.attempts > 0:
            events.emit(
                "investigation.completed",
                {
                    "hypothesis_id": item.id,
                    "verdict": item.status.value,
                    "steps": item.attempts,
                    "tokens": 0,
                    "observations": len(item.observations),
                    "strength": item.evidence_strength,
                },
            )
    return publication


async def _persist_ledger(orchestrator: Any, ledger: HypothesisLedger) -> None:
    database = getattr(orchestrator, "_db", None)
    if database is None or not callable(getattr(database, "append_hypothesis", None)):
        return
    run_id = ledger.run_id
    for hypothesis in ledger.items.values():
        await database.append_hypothesis(run_id, hypothesis)


async def deliver_publication(
    gateway: Any,
    state: StateStore,
    publication: Publication,
    *,
    review_body: str = "",
) -> tuple[int, int]:
    """Coordinate-validate and deliver inline comments (mirrors ``_post_comments``).

    Comments whose line is not a visible RIGHT-side diff coordinate are dropped;
    the survivors are delivered as one bounded ``post_review`` batch carrying the
    optional review ``body`` (the rendered ``<details>`` summary).  Returns
    ``(delivered, rejected)``.
    """

    right_lines: dict[str, set[int]] = {}
    for comment in publication.comments:
        if comment.path in right_lines:
            continue
        patch = (state.file_diffs or {}).get(comment.path)
        right_lines[comment.path] = {line for line, _content in iter_right_lines(patch or "")}

    payload_comments: list[dict[str, Any]] = []
    rejected = 0
    for comment in publication.comments:
        if comment.line <= 0 or comment.line not in right_lines.get(comment.path, set()):
            rejected += 1
            continue
        payload_comments.append({"file_path": comment.path, "line": comment.line, "body": comment.body})

    if not payload_comments and not review_body:
        return (0, rejected)
    params: dict[str, Any] = {"comments": payload_comments}
    if review_body:
        params["body"] = review_body
    await gateway.invoke("post_review", params, state, agent_name="orchestrator")
    return (len(payload_comments), rejected)


async def run_hypothesis_pipeline(orchestrator: Any, state: Any) -> RunHealth:
    """Build the immutable workspace, semantic units and deterministic pack."""

    started = time.perf_counter()
    try:
        workspace = await orchestrator._gateway.workspace_for(state)
        info = workspace.info
        workspace_payload = {
            "source": info.source,
            "file_count": info.file_count,
            "byte_size": info.byte_size,
            "truncated": info.truncated,
            "digest": info.digest,
        }
    except WorkspaceUnavailable:
        workspace = _UnavailableWorkspace(digest=str(state.head_sha or ""))
        workspace_payload = {
            "source": "unavailable",
            "file_count": 0,
            "byte_size": 0,
            "truncated": True,
            "digest": str(state.head_sha or ""),
        }
    workspace_payload["ms"] = int((time.perf_counter() - started) * 1000)
    workspace_event = orchestrator._events.emit("workspace.built", workspace_payload)

    changeset = compile_semantic_changeset(state)
    config = orchestrator._pipeline_v4_config
    pack = ContextPack.build(
        changeset,
        workspace,
        state,
        max_slices=config.context_pack_max_slices,
    )
    rendered = pack.render_all(max_chars=config.context_pack_max_chars)
    slices = sum(len(context.slices) for context in pack.units.values())
    truncated_units = sum(bool(context.truncated_kinds) for context in pack.units.values())
    orchestrator._events.emit(
        "context_pack.built",
        {
            "units": len(pack.units),
            "slices": slices,
            "truncated_units": truncated_units,
            "chars": len(rendered),
        },
    )
    if state.ledger is None:
        state.ledger = HypothesisLedger(
            run_id=workspace_event.run_id,
            head_sha=str(state.head_sha or ""),
            workspace_digest=pack.workspace_digest,
        )
    elif state.ledger.head_sha != str(state.head_sha or ""):
        raise ValueError("restored hypothesis ledger does not match PR head")
    ledger = state.ledger

    if state.files_changed:
        try:
            scan = await scan_changed_files(orchestrator._gateway, state)
            _seed_detector_hypotheses(state, changeset, ledger, scan.findings)
        except Exception as exc:
            logger.warning("detector seeding skipped: %s", exc)

    publication = Publication(comments=[], summary_items=[], merged=[], unknown_ids=[], fallback=False)
    language = resolve_output_language(state, config)
    if getattr(orchestrator, "_model_router", None) is not None and changeset.units:
        publication = await _run_llm_stages(orchestrator, state, changeset, pack, ledger, workspace, config, language)

    await _persist_ledger(orchestrator, ledger)

    delivered = 0
    if config.mode == "hypothesis":
        review_body = render_review_body(publication, ledger, output_language=language)
        delivered, _rejected = await deliver_publication(
            orchestrator._gateway, state, publication, review_body=review_body
        )

    hypotheses = list(ledger.items.values())
    unknown_error = sum(
        1
        for hypothesis in hypotheses
        if hypothesis.status == HypothesisStatus.UNKNOWN and hypothesis.severity == "error"
    )
    generated = [hypothesis for hypothesis in hypotheses if not hypothesis.source.startswith("detector")]
    orchestrator._events.emit(
        "pipeline_v4.completed",
        {
            "mode": config.mode,
            "hypotheses_total": len(hypotheses),
            "confirmed": sum(1 for hypothesis in hypotheses if hypothesis.status == HypothesisStatus.CONFIRMED),
            "refuted": sum(1 for hypothesis in hypotheses if hypothesis.status == HypothesisStatus.REFUTED),
            "unknown": sum(1 for hypothesis in hypotheses if hypothesis.status == HypothesisStatus.UNKNOWN),
            "generated": len(generated),
            "published": delivered,
            "tokens_by_agent": {},
        },
    )
    return RunHealth.build(
        hypothesis_failures=len(ledger.unresolved_units),
        investigation_unknown_errors=unknown_error,
    )
