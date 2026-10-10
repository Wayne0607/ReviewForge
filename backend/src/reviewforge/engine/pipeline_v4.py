"""Hypothesis-pipeline orchestration entrypoint.

Shadow mode runs the same deterministic and LLM stages as hypothesis mode,
persists the ledger, and leaves publication to the legacy pipeline.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import asdict
from typing import Any

from reviewforge.core.state import StateStore
from reviewforge.engine.context_engine import ContextEngine
from reviewforge.engine.context_pack import ContextPack
from reviewforge.engine.declarations_v4 import compile_changeset_v4
from reviewforge.engine.detectors.unified_diff import iter_added_lines, iter_right_lines
from reviewforge.engine.editor import (
    Editor,
    Publication,
    cluster_confirmed,
    confirmed_fact_digest,
    render_review_body,
    resumed_publication,
)
from reviewforge.engine.hypothesis import Hypothesis, HypothesisLedger, HypothesisStatus, Mechanism, Site
from reviewforge.engine.hypothesis_generator import HypothesisGenerator
from reviewforge.engine.investigator import Investigator, build_workspace_executor
from reviewforge.engine.language import resolve_output_language
from reviewforge.engine.lenses import run_lens, select_lenses
from reviewforge.engine.phase0 import scan_changed_files
from reviewforge.engine.publication_delivery import DeliveryOutcome, deliver_saved_publication
from reviewforge.engine.run_health import RunHealth
from reviewforge.engine.security_categories import is_security_category
from reviewforge.engine.semantic_diff import SemanticChangeSet, SemanticUnit
from reviewforge.engine.token_tracker import RunContext, TrackedChatLLM

logger = logging.getLogger(__name__)


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
    usage_by_agent: dict[str, int],
    *,
    resume: bool = False,
    covered_ids: set[str] | None = None,
    published_clusters: set[tuple[str, str]] | None = None,
    inline_used: int = 0,
) -> Publication:
    """Generator → lenses → investigator → editor for one run."""

    router = orchestrator._model_router
    events = orchestrator._events
    ctx = RunContext()
    ctx.set(ledger.run_id, getattr(orchestrator, "_db", None))

    def routed(name: str):
        async def record(usage: dict[str, Any]) -> None:
            usage_by_agent[name] = usage_by_agent.get(name, 0) + int(usage["total_tokens"])

        return TrackedChatLLM(router.get_llm(name), ctx, name, usage_sink=record)

    async def checkpoint(current: HypothesisLedger) -> None:
        await _persist_ledger(orchestrator, current)

    # A frozen first review proves discovery was attempted. Retry only if
    # generation/lens failures remain; finished verdicts stay in the ledger.
    if not resume or ledger.unresolved_units:
        generator = HypothesisGenerator(
            routed("hypothesis_generator"),
            max_input_chars=config.generator_max_input_chars,
            max_hypotheses=config.generator_max_hypotheses,
            context_max_chars=config.context_pack_max_chars,
            output_language=language,
            on_update=checkpoint,
        )
        gen_result = await generator.run(state, pack, changeset, ledger)
        events.emit(
            "hypothesis.generated",
            {
                "pass": 1,
                "source": "generator",
                "accepted": gen_result.accepted,
                "dropped_unanchored": gen_result.dropped_unanchored,
                "dropped_invalid": gen_result.dropped_invalid,
                "dropped_overflow": gen_result.dropped_overflow,
                "blocks": gen_result.blocks,
                "failed_blocks": gen_result.failed_blocks,
                "tokens": usage_by_agent.get("hypothesis_generator", 0),
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
            llm = routed(f"lens_{selection.name}")
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
                on_update=checkpoint,
            )

    executor = build_workspace_executor(workspace, state)
    for item in ledger.items.values():
        if item.status == HypothesisStatus.UNKNOWN and item.retryable:
            item.status = HypothesisStatus.OPEN
    if ledger.open():
        investigator = Investigator(routed("investigator"), executor, output_language=language, changeset=changeset)
        await investigator.run(
            ledger,
            state,
            pack,
            max_hypotheses_per_pr=config.investigator_max_hypotheses_per_pr,
            concurrency=config.investigator_concurrency,
            on_update=checkpoint,
        )

    if resume:
        publication = resumed_publication(
            ledger,
            covered_ids=covered_ids or set(),
            published_clusters=published_clusters or set(),
            inline_used=inline_used,
            max_inline=config.publish_max_inline,
            max_inline_overflow=config.publish_max_inline_overflow,
            output_language=language,
        )
    else:
        editor = Editor(
            routed("editor"),
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
                    "steps": item.investigation_steps,
                    "tokens": item.investigation_tokens,
                    "observations": len(item.observations),
                    "strength": item.evidence_strength,
                },
            )
    return publication


async def _persist_ledger(orchestrator: Any, ledger: HypothesisLedger) -> None:
    database = getattr(orchestrator, "_db", None)
    if database is None:
        return
    if callable(getattr(database, "checkpoint_hypothesis_ledger", None)):
        await database.checkpoint_hypothesis_ledger(ledger)
        return
    if not callable(getattr(database, "append_hypothesis", None)):
        return
    run_id = ledger.run_id
    for hypothesis in ledger.items.values():
        await database.append_hypothesis(
            run_id, hypothesis, head_sha=ledger.head_sha, workspace_digest=ledger.workspace_digest
        )


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

    params, rejected = publication_payload(state, publication, review_body=review_body)
    if not params["comments"] and not review_body:
        return (0, rejected)
    await gateway.invoke("post_review", params, state, agent_name="orchestrator")
    return (len(params["comments"]), rejected)


def publication_payload(
    state: StateStore, publication: Publication, *, review_body: str = ""
) -> tuple[dict[str, Any], int]:
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

    return ({"comments": payload_comments, "body": review_body}, rejected)


def _publication_coverage(
    ledger: HypothesisLedger, publication: Publication, payload: dict[str, Any]
) -> dict[str, Any]:
    """Freeze only confirmed facts actually retained in inline or summary output."""
    accepted = {(item["file_path"], item["line"], item["body"]) for item in payload["comments"]}
    covered = {
        identity
        for comment in publication.comments
        if (comment.path, comment.line, comment.body) in accepted
        for identity in comment.hypothesis_ids
    }
    summary_ids = {identity for identity, _ in publication.summary_items}
    clusters = cluster_confirmed(ledger)
    for cluster in clusters:
        if summary_ids.intersection(cluster.hypothesis_ids):
            covered.update(cluster.hypothesis_ids)
    return {
        "confirmed_ids": sorted(covered),
        "cluster_keys": [list(cluster.key) for cluster in clusters if covered.intersection(cluster.hypothesis_ids)],
        "fact_hashes": {item.id: confirmed_fact_digest(item) for item in ledger.items.values() if item.id in covered},
    }


async def run_hypothesis_pipeline(orchestrator: Any, state: Any) -> RunHealth:
    """Build the immutable workspace, semantic units and deterministic pack."""

    started = time.perf_counter()
    workspace = await orchestrator._gateway.workspace_for(state)
    info = workspace.info
    workspace_payload = {
        "source": info.source,
        "file_count": info.file_count,
        "byte_size": info.byte_size,
        "truncated": info.truncated,
        "digest": info.digest,
    }
    workspace_payload["ms"] = int((time.perf_counter() - started) * 1000)
    workspace_event = orchestrator._events.emit("workspace.built", workspace_payload)

    # Rebuild through the pinned gateway, including after legacy shadow: v4
    # declarations must be code-backed, independent of legacy regex heuristics.
    if state.files_changed:
        await ContextEngine(orchestrator._gateway, getattr(orchestrator, "_db", None), v4_declarations=True).build(
            state
        )
    changeset = compile_changeset_v4(state)
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
    await _persist_ledger(orchestrator, ledger)
    database = getattr(orchestrator, "_db", None)
    saved = await database.get_v4_publication(ledger.run_id) if database and config.mode == "hypothesis" else None
    delivered, rejected = 0, 0
    outcome = DeliveryOutcome()
    records = await database.list_v4_publications(ledger.run_id) if saved else []
    # Finish/reconcile every frozen batch before spending model tokens or
    # preparing new writes. Ambiguous acceptance permits reads only.
    for record in records:
        rejected += int(record["payload"].get("rejected", 0))
        outcome = await deliver_saved_publication(
            database, orchestrator._gateway, state, ledger.run_id, batch_id=record["batch_id"]
        )
        delivered += outcome.delivered
        if outcome.error:
            break
    fact_hashes = {
        identity: digest
        for record in records
        for identity, digest in record["payload"].get("coverage", {}).get("fact_hashes", {}).items()
    }
    covered_ids = {item.id for item in ledger.items.values() if fact_hashes.get(item.id) == confirmed_fact_digest(item)}
    published_clusters = {
        tuple(key) for record in records for key in record["payload"].get("coverage", {}).get("cluster_keys", [])
    }
    needs_resume = bool(ledger.unresolved_units) or any(
        item.status == HypothesisStatus.OPEN
        or (item.status == HypothesisStatus.UNKNOWN and item.retryable)
        or (item.status == HypothesisStatus.CONFIRMED and item.id not in covered_ids)
        for item in ledger.items.values()
    )
    if saved and needs_resume and any("coverage" not in record["payload"] for record in records):
        # Old dev records cannot prove which confirmed facts were published.
        # Do not guess coverage and risk duplicating or dropping findings.
        outcome = DeliveryOutcome(error="frozen review lacks coverage metadata; start a new isolated review run")

    if not saved and state.files_changed:
        try:
            scan = await scan_changed_files(orchestrator._gateway, state)
            _seed_detector_hypotheses(state, changeset, ledger, scan.findings)
        except Exception as exc:
            logger.warning("detector seeding skipped: %s", exc)

    publication = Publication(comments=[], summary_items=[], merged=[], unknown_ids=[], fallback=False)
    language = resolve_output_language(state, config)
    usage_by_agent: dict[str, int] = {}
    if (
        not outcome.error
        and (not saved or needs_resume)
        and getattr(orchestrator, "_model_router", None) is not None
        and changeset.units
    ):
        publication = await _run_llm_stages(
            orchestrator,
            state,
            changeset,
            pack,
            ledger,
            workspace,
            config,
            language,
            usage_by_agent,
            resume=bool(saved),
            covered_ids=covered_ids,
            published_clusters=published_clusters,
            inline_used=delivered,
        )

    await _persist_ledger(orchestrator, ledger)

    review_body = render_review_body(publication, ledger, output_language=language)
    payload, new_rejected = publication_payload(state, publication, review_body=review_body)
    rejected += new_rejected
    if config.mode == "shadow" and database:
        await database.save_shadow_publication(
            ledger.run_id, {"publication": asdict(publication), "payload": payload, "rejected": rejected}
        )
    if config.mode == "hypothesis" and not outcome.error:
        if database and (payload["comments"] or payload["body"]):
            coverage = _publication_coverage(ledger, publication, payload)
            batch_id = (
                hashlib.sha256(json.dumps(coverage["fact_hashes"], sort_keys=True).encode()).hexdigest()[:24]
                if saved
                else ""
            )
            await database.prepare_v4_publication(
                ledger.run_id,
                ledger.head_sha,
                {**payload, "rejected": new_rejected, "coverage": coverage},
                batch_id=batch_id,
            )
            outcome = await deliver_saved_publication(
                database, orchestrator._gateway, state, ledger.run_id, batch_id=batch_id
            )
            delivered += outcome.delivered
        elif payload["comments"] or payload["body"]:
            delivered, rejected = await deliver_publication(
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
            "tokens_by_agent": usage_by_agent,
        },
    )
    return RunHealth.build(
        hypothesis_failures=len(ledger.unresolved_units),
        investigation_unknown_errors=unknown_error,
        investigation_retryable=any(item.retryable for item in hypotheses),
        delivery_failures=rejected,
        delivery_errors=(outcome.error,) if outcome.error else (),
        delivery_retryable=outcome.retryable,
    )
