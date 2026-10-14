"""Hypothesis-pipeline orchestration entrypoint.

Shadow mode runs the same deterministic and LLM stages as hypothesis mode,
persists the ledger, and leaves publication to the legacy pipeline.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from copy import deepcopy
from dataclasses import asdict
from typing import Any

from reviewforge.core.state import StateStore
from reviewforge.engine.budget_allocation import verification_forecast
from reviewforge.engine.context_engine import ContextEngine
from reviewforge.engine.context_pack import ContextPack
from reviewforge.engine.declarations_v4 import compile_changeset_v4
from reviewforge.engine.detectors.unified_diff import iter_added_lines, iter_right_lines
from reviewforge.engine.editor import (
    V4_PUBLICATION_RENDERER_VERSION,
    Editor,
    Publication,
    cluster_confirmed,
    confirmed_fact_digest,
    render_review_body,
    resumed_publication,
)
from reviewforge.engine.evidence_followup import run_evidence_followup
from reviewforge.engine.hypothesis import Hypothesis, HypothesisLedger, HypothesisStatus, Mechanism, Site
from reviewforge.engine.hypothesis_generator import HypothesisGenerator, build_anchor_resolver
from reviewforge.engine.investigation_tasks import build_investigation_tasks, fair_investigation_tasks
from reviewforge.engine.investigator import Investigator, build_workspace_executor
from reviewforge.engine.language import resolve_output_language
from reviewforge.engine.lenses import build_discovery_responsibilities, run_lens, select_lenses
from reviewforge.engine.llm_protocol_v4 import request_options
from reviewforge.engine.model_router import ModelRouter
from reviewforge.engine.phase0 import scan_changed_files
from reviewforge.engine.proof_gate import PROOF_VERSION, ProofGate, accepted, completed, proof_fingerprint
from reviewforge.engine.proof_gate import _scope_matches as proof_scope_matches
from reviewforge.engine.proof_gate import retryable as proof_retryable
from reviewforge.engine.provider_retry import RetryChatLLM, RetryPolicy
from reviewforge.engine.publication_delivery import DeliveryOutcome, deliver_saved_publication
from reviewforge.engine.request_budget import BudgetedChatLLM, RequestBudget
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
    """Treat v3 pattern matches as proposals requiring investigation and proof."""

    diffs = state.file_diffs or {}
    seeded = 0
    for finding in findings:
        mechanism = _mechanism_for(finding.category)
        if mechanism is None:
            continue
        unit = _unit_for(changeset.units, finding.file, max(1, finding.line))
        unit_id = unit.id if unit is not None else f"{finding.file}:line{max(1, finding.line)}"
        anchor = (unit.symbol if unit else "") or f"line{max(1, finding.line)}"
        excerpt = _line_content(diffs.get(finding.file, ""), max(1, finding.line))
        if ledger.protocol_version >= 3 and (unit is None or not excerpt):
            ledger.unresolved_units[unit_id] = "detector match has no attributable changed source"
            continue
        excerpt = excerpt or str(finding.message)
        identity = f"{unit_id}::{mechanism.value}::{anchor}"
        hypothesis = Hypothesis(
            id="h_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:8],
            identity=identity,
            unit_id=unit_id,
            mechanism=mechanism,
            claim=str(finding.message),
            trigger="Execution reaches this changed operation; supported input remains to be verified."
            if ledger.protocol_version >= 3
            else "",
            impact=str(finding.suggestion or finding.message),
            open_question=(
                "Do the actual operation, supported input and consumer contract "
                "establish a defect introduced by this change?"
            )
            if ledger.protocol_version >= 3
            else "",
            refutation=(
                "Positive source shows a different operation, a protecting condition, "
                "or no introduced violation of the required contract."
            )
            if ledger.protocol_version >= 3
            else "",
            sites=[Site(path=str(finding.file), line=max(1, finding.line), excerpt=excerpt)],
            severity=_normalize_severity(finding.severity),
            source=f"detector:{finding.category}",
            status=HypothesisStatus.OPEN if ledger.protocol_version >= 3 else HypothesisStatus.CONFIRMED,
            evidence_strength="none" if ledger.protocol_version >= 3 else "strong",
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
    """Discover with specialist responsibilities, investigate, then edit.

    Saved v1/v2 protocols retain their separate generator and lens passes.
    """

    router = orchestrator._model_router
    events = orchestrator._events
    ctx = RunContext()
    ctx.set(ledger.run_id, getattr(orchestrator, "_db", None))
    request_budget = (
        RequestBudget(
            config.max_tokens_per_pr,
            discovery_limit=config.discovery_max_tokens,
            saved=ledger.request_budget,
            adaptive=ledger.protocol_version >= 4 and getattr(config, "budget_policy", "adaptive") == "adaptive",
        )
        if ledger.protocol_version >= 3
        else None
    )

    async def budget_changed(snapshot: dict[str, Any]) -> None:
        ledger.request_budget = snapshot
        events.emit("pipeline_v4.request_budget", snapshot)
        await _persist_ledger(orchestrator, ledger)

    def routed(name: str, *, closing: bool = False):
        async def record(usage: dict[str, Any]) -> None:
            usage_by_agent[name] = usage_by_agent.get(name, 0) + int(usage["total_tokens"])

        async def attempt_changed(attempt: dict[str, Any]) -> None:
            events.emit("pipeline_v4.provider_attempt", {"agent": name, "closing": closing, **attempt})

        options = {}
        if ledger.protocol_version >= 2:
            effective = router.effective(name) if callable(getattr(router, "effective", None)) else {}
            options = request_options(
                effective.get("model", ""),
                name,
                closing=closing,
                json_mode=config.json_output_mode,
                closure_thinking=config.investigator_closure_thinking,
                protocol_version=ledger.protocol_version,
            )
            events.emit("pipeline_v4.request_options", {"agent": name, "closing": closing, "options": options})
        raw = (
            router.get_llm(name, max_retries=0)
            if request_budget and isinstance(router, ModelRouter)
            else router.get_llm(name)
        )
        if request_budget is None:
            return TrackedChatLLM(raw, ctx, name, usage_sink=record, request_options=options)
        phase = "editor" if name == "editor" else "investigation" if name == "investigator" else "discovery"
        admitted = BudgetedChatLLM(raw, request_budget, phase=phase, on_change=budget_changed)
        tracked = TrackedChatLLM(admitted, ctx, name, usage_sink=record, request_options=options, receipt_first=True)
        return RetryChatLLM(
            tracked,
            policy=RetryPolicy(
                max_attempts=config.provider_max_attempts,
                retry_404=config.provider_retry_404,
                retry_after_max_seconds=config.provider_retry_after_max_seconds,
            ),
            attempt_sink=attempt_changed,
        )

    active_sources: dict[str, list[Any]] = {}
    discovery_active = not resume or bool(ledger.unresolved_units)
    allocation_models = (
        (routed("investigator"), routed("investigator", closing=True), routed("editor"))
        if request_budget is not None and request_budget.adaptive
        else None
    )

    def refresh_allocation() -> None:
        if request_budget is None or not request_budget.adaptive:
            return
        from dataclasses import replace

        candidates = []
        starters = []
        selected = fair_investigation_tasks(
            build_investigation_tasks(ledger.open(), head_sha=ledger.head_sha, workspace_digest=ledger.workspace_digest)
        )[: config.investigator_max_hypotheses_per_pr]
        opens = [task.hypothesis for task in selected]
        confirmed = [item for item in ledger.items.values() if item.status == HypothesisStatus.CONFIRMED]
        pending = []
        for task in selected:
            for identity in task.members:
                item = ledger.items[identity]
                if task.hypothesis.identity in active_sources:
                    item = replace(item, observations=list(active_sources[task.hypothesis.identity]))
                pending.append(item)
        for item in [*confirmed, *pending]:
            if item.identity in active_sources:
                item = replace(item, observations=list(active_sources[item.identity]))
            if item.status == HypothesisStatus.CONFIRMED and completed(item, ledger):
                continue
            candidates.append(item)
        for item in opens:
            preview = Investigator(
                allocation_models[0],
                executor,
                output_language=language,
                changeset=changeset,
                protocol_version=ledger.protocol_version,
                closing_llm=allocation_models[1],
                adaptive_budget=True,
            )
            _, _, starter = preview.workload_allowance(item, state, pack)
            starters.append(starter)
        future = (
            discovery_active
            and bool(changeset.units)
            and (not ledger.discovery_blocks or bool(ledger.unresolved_units))
        )
        if ledger.protocol_version >= 5:
            assessed = {
                unit_id
                for block in ledger.discovery_blocks.values()
                for unit_id, row in block.get("units", {}).items()
                if row.get("status") in {"assessed", "recovered"}
            }
            future = discovery_active and any(unit.id not in assessed for unit in changeset.units)
        forecast = verification_forecast(
            allocation_models[2], ledger, candidates, language=language, future_candidate=future
        )
        investigation = sum(starters)
        if future and not starters:
            investigation = 8_192  # Bootstrap allowance until an actual candidate exists.
        request_budget.allocate(
            verification=forecast["verification_tokens"],
            investigation=investigation,
            basis={
                **forecast,
                "investigation_starter_tokens": investigation,
                "discovery_target_tokens": config.discovery_max_tokens,
            },
        )
        ledger.request_budget = request_budget.snapshot()

    def evidence_changed(item, observations) -> None:
        active_sources[item.identity] = list(observations)
        refresh_allocation()

    async def checkpoint(current: HypothesisLedger) -> None:
        refresh_allocation()
        await _persist_ledger(orchestrator, current)

    executor = build_workspace_executor(workspace, state)
    refresh_allocation()
    if request_budget is not None:
        await budget_changed(request_budget.snapshot())

    # A frozen first review proves discovery was attempted. Retry only if
    # generation/lens failures remain; finished verdicts stay in the ledger.
    if not resume or ledger.unresolved_units:
        anchor_resolver = build_anchor_resolver(workspace, changeset)
        selections = select_lenses(state, changeset, max_lenses=config.max_lenses)
        events.emit(
            "lens.selected",
            {
                "lenses": [selection.name for selection in selections],
                "reasons": [selection.reason for selection in selections],
                "mode": "integrated-discovery" if ledger.protocol_version >= 3 else "standalone",
            },
        )
        discovery_options = {}
        if ledger.protocol_version >= 3:
            responsibilities, rules = build_discovery_responsibilities(state, changeset, max_lenses=config.max_lenses)
            discovery_options = {
                "discovery_target_input_chars": config.discovery_target_input_chars,
                "discovery_max_blocks": config.discovery_max_blocks,
                "unit_responsibilities": responsibilities,
                "responsibility_rules": rules,
            }
            if ledger.protocol_version >= 5:
                discovery_options["executor"] = executor
                discovery_options["session_request_limit"] = config.session_max_requests
        generator = HypothesisGenerator(
            routed("hypothesis_generator"),
            max_input_chars=config.generator_max_input_chars,
            max_hypotheses=config.generator_max_hypotheses,
            context_max_chars=config.context_pack_max_chars,
            output_language=language,
            on_update=checkpoint,
            anchor_resolver=anchor_resolver,
            **discovery_options,
        )
        gen_result = await generator.run(
            state,
            pack,
            changeset,
            ledger,
            **({"resume": bool(ledger.discovery_blocks)} if ledger.protocol_version >= 3 else {}),
        )
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

        for selection in selections if ledger.protocol_version < 3 else []:
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
                anchor_resolver=anchor_resolver,
                max_input_chars=config.generator_max_input_chars,
                context_max_chars=config.context_pack_max_chars,
            )

    discovery_active = False
    refresh_allocation()
    for item in ledger.items.values():
        if item.status == HypothesisStatus.UNKNOWN and item.retryable:
            item.status = HypothesisStatus.OPEN
    if ledger.open():
        from reviewforge.engine.review_session import EvidenceStore

        investigator = Investigator(
            routed("investigator"),
            executor,
            output_language=language,
            changeset=changeset,
            protocol_version=ledger.protocol_version,
            closing_llm=routed("investigator", closing=True) if ledger.protocol_version >= 2 else None,
            evidence_store=EvidenceStore(ledger) if ledger.protocol_version >= 5 else None,
        )
        await investigator.run(
            ledger,
            state,
            pack,
            max_hypotheses_per_pr=config.investigator_max_hypotheses_per_pr,
            concurrency=config.investigator_concurrency,
            on_update=checkpoint,
            token_budget=(
                request_budget.available("investigation")
                if request_budget is not None
                else max(0, config.max_tokens_per_pr - sum(usage_by_agent.values()) - 16_000)
                if ledger.protocol_version >= 2
                else None
            ),
            request_budget=request_budget,
            on_evidence=evidence_changed if request_budget is not None and request_budget.adaptive else None,
        )

    if ledger.protocol_version >= 3:
        gate = ProofGate(routed("editor"), output_language=language, on_update=checkpoint)
        await gate.run(ledger)
        if ledger.protocol_version >= 4:

            def collector():
                return Investigator(
                    routed("investigator"),
                    executor,
                    output_language=language,
                    changeset=changeset,
                    protocol_version=ledger.protocol_version,
                    on_evidence=evidence_changed if request_budget is not None and request_budget.adaptive else None,
                )

            changed_evidence = await run_evidence_followup(collector, ledger, state, pack, on_update=checkpoint)
            if changed_evidence:
                # New saved source invalidates prior permission. The independent
                # reconstruction and original gate must judge it again.
                await gate.run(ledger)
            events.emit("evidence_followup.completed", {"new_source_evidence": changed_evidence})
        events.emit(
            "publication_proof.completed",
            {
                "accepted": sum(accepted(item, ledger) for item in ledger.items.values()),
                "reviews": {
                    key: {field: review.get(field) for field in ("decision", "outcome", "reason", "attempts")}
                    for key, review in ledger.publication_reviews.items()
                },
            },
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
        if ledger.protocol_version < 3 and item.source.startswith("detector"):
            continue
        if item.status == HypothesisStatus.UNKNOWN and (
            item.verdict_reason == "budget-exhausted" or item.investigation_outcome == "policy-deferred"
        ):
            events.emit(
                "investigation.skipped",
                {"hypothesis_id": item.id, "reason": item.verdict_reason, "task_id": item.investigation_task_id},
            )
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
        "fact_hashes": {
            item.id: confirmed_fact_digest(item, ledger=ledger) for item in ledger.items.values() if item.id in covered
        },
        **(
            {
                "publication_proof_version": 2 if ledger.protocol_version >= 4 else PROOF_VERSION,
                "proof_fingerprints": {
                    identity: ledger.publication_reviews[identity]["fingerprint"] for identity in covered
                },
            }
            if ledger.protocol_version >= 3
            else {}
        ),
        **({"publication_renderer_version": V4_PUBLICATION_RENDERER_VERSION} if ledger.protocol_version >= 4 else {}),
    }


def _frozen_publication_problem(ledger: HypothesisLedger, coverage: dict[str, Any]) -> str:
    """A frozen pending body must match the proof and renderer that produced it."""
    version = 2 if ledger.protocol_version >= 4 else PROOF_VERSION
    error = "frozen review lacks matching independent proof verification"
    if ledger.protocol_version >= 4:
        error += "; start a new isolated review run"
        identities = coverage.get("confirmed_ids")
        hashes = coverage.get("fact_hashes")
        fingerprints = coverage.get("proof_fingerprints")
        if (
            coverage.get("publication_renderer_version") != V4_PUBLICATION_RENDERER_VERSION
            or not isinstance(identities, list)
            or any(not isinstance(identity, str) for identity in identities)
            or len(set(identities)) != len(identities)
            or not isinstance(hashes, dict)
            or set(hashes) != set(identities)
            or not isinstance(fingerprints, dict)
            or set(fingerprints) != set(identities)
        ):
            return error
    by_id = {item.id: item for item in ledger.items.values()}
    if coverage.get("publication_proof_version") != version or any(
        identity not in by_id
        or not accepted(by_id[identity], ledger)
        or coverage.get("proof_fingerprints", {}).get(identity)
        != ledger.publication_reviews[identity].get("fingerprint")
        or (
            ledger.protocol_version >= 4
            and coverage["fact_hashes"].get(identity) != confirmed_fact_digest(by_id[identity], ledger=ledger)
        )
        for identity in coverage.get("confirmed_ids", [])
    ):
        return error
    return ""


def _covered_publication_ids(ledger: HypothesisLedger, records: list[dict[str, Any]]) -> set[str]:
    """Compare each fact with its last receipt, including delivered old renderers."""
    receipts = {
        identity: (digest, record)
        for record in records
        for identity, digest in record["payload"].get("coverage", {}).get("fact_hashes", {}).items()
    }
    covered = set()
    for item in ledger.items.values():
        if item.id not in receipts:
            continue
        digest, record = receipts[item.id]
        coverage = record["payload"].get("coverage", {})
        # Old delivered facts stay covered only while their original candidate,
        # assessment and sites still match. New facts can still earn a supplement.
        legacy_renderer = (
            ledger.protocol_version >= 4
            and record["status"] == "delivered"
            and coverage.get("publication_renderer_version") in (None, 1)
            and item.id in coverage.get("confirmed_ids", [])
        )
        if digest == confirmed_fact_digest(item, ledger=ledger, legacy_renderer=legacy_renderer):
            covered.add(item.id)
    return covered


def _legacy_delivered_coverage_missing(record: dict[str, Any]) -> bool:
    """Without the old fact hash and cluster, safe migration cannot be inferred."""
    coverage = record["payload"].get("coverage", {})
    if record["status"] != "delivered" or coverage.get("publication_renderer_version") not in (None, 1):
        return False
    identities = coverage.get("confirmed_ids")
    hashes = coverage.get("fact_hashes")
    clusters = coverage.get("cluster_keys")
    return (
        not isinstance(identities, list)
        or any(not isinstance(identity, str) for identity in identities)
        or not isinstance(hashes, dict)
        or set(hashes) != set(identities)
        or any(not isinstance(value, str) or not value for value in hashes.values())
        or not isinstance(clusters, list)
        or any(not isinstance(key, list) or len(key) != 2 for key in clusters)
        or (bool(identities) and not clusters)
    )


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

    config = orchestrator._pipeline_v4_config
    protocol_version = state.ledger.protocol_version if state.ledger is not None else config.protocol_version
    structural_checks = {}
    if protocol_version >= 3:
        metadata_started = time.perf_counter()
        hydrate_changes = getattr(orchestrator._gateway, "ensure_file_changes", None)
        if callable(hydrate_changes):
            await hydrate_changes(state)
        structural_checks = {
            "head_sha": state.head_sha,
            "elapsed_ms": int((time.perf_counter() - metadata_started) * 1000),
            "hydrator_available": callable(hydrate_changes),
            "changes": {
                path: change.to_dict() for path, change in state.file_changes.items() if change.status == "renamed"
            },
        }
        orchestrator._events.emit("file_changes.verified", structural_checks)

    # Rebuild through the pinned gateway, including after legacy shadow: v4
    # declarations must be code-backed, independent of legacy regex heuristics.
    if state.files_changed:
        await ContextEngine(orchestrator._gateway, getattr(orchestrator, "_db", None), v4_declarations=True).build(
            state
        )
    changeset = compile_changeset_v4(state)
    pack = ContextPack.build(
        changeset,
        workspace,
        state,
        max_slices=config.context_pack_max_slices,
        **({"protocol_version": protocol_version} if protocol_version >= 3 else {}),
    )
    # Rendering marks omitted slices. Protocol 3 renders independently budgeted
    # blocks later, so this aggregate telemetry must not mark their inputs absent.
    telemetry_pack = deepcopy(pack) if protocol_version >= 3 else pack
    rendered = telemetry_pack.render_all(max_chars=config.context_pack_max_chars)
    slices = sum(len(context.slices) for context in pack.units.values())
    truncated_units = sum(bool(context.truncated_kinds) for context in telemetry_pack.units.values())
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
            protocol_version=config.protocol_version,
        )
    elif state.ledger.head_sha != str(state.head_sha or ""):
        raise ValueError("restored hypothesis ledger does not match PR head")
    elif state.ledger.protocol_version >= 3 and state.ledger.workspace_digest != pack.workspace_digest:
        raise ValueError("restored hypothesis ledger does not match pinned workspace")
    ledger = state.ledger
    if ledger.protocol_version >= 3:
        ledger.structural_checks = structural_checks
    for item in ledger.items.values():
        if item.change_anchor is not None and state.file_changes.get(item.change_anchor.path) != item.change_anchor:
            raise ValueError("restored structural hypothesis does not match the verified PR change")
    await _persist_ledger(orchestrator, ledger)
    database = getattr(orchestrator, "_db", None)
    saved = await database.get_v4_publication(ledger.run_id) if database and config.mode == "hypothesis" else None
    delivered, rejected = 0, 0
    outcome = DeliveryOutcome()
    records = await database.list_v4_publications(ledger.run_id) if saved else []
    # Finish/reconcile every frozen batch before spending model tokens or
    # preparing new writes. Ambiguous acceptance permits reads only.
    for record in records:
        if ledger.protocol_version >= 3 and record["status"] != "delivered":
            coverage = record["payload"].get("coverage", {})
            if problem := _frozen_publication_problem(ledger, coverage):
                outcome = DeliveryOutcome(error=problem)
                break
        rejected += int(record["payload"].get("rejected", 0))
        outcome = await deliver_saved_publication(
            database, orchestrator._gateway, state, ledger.run_id, batch_id=record["batch_id"]
        )
        delivered += outcome.delivered
        if outcome.error:
            break
    covered_ids = _covered_publication_ids(ledger, records)
    published_clusters = {
        tuple(key) for record in records for key in record["payload"].get("coverage", {}).get("cluster_keys", [])
    }

    def publication_pending(item: Hypothesis) -> bool:
        if ledger.protocol_version < 3:
            return item.id not in covered_ids
        if accepted(item, ledger):
            return item.id not in covered_ids
        if completed(item, ledger):
            return False
        if proof_retryable(item, ledger):
            return True
        review = ledger.publication_reviews.get(item.id, {})
        # Absent/stale receipts are new work. Current budget refusals and capped
        # execution errors cannot acquire another attempt merely by resuming.
        if ledger.protocol_version >= 4:
            return not proof_scope_matches(review, item, ledger)
        return (
            review.get("proof_version") != PROOF_VERSION
            or review.get("fingerprint") != proof_fingerprint(item, ledger.head_sha, ledger.workspace_digest)
            or review.get("run_id") != ledger.run_id
            or review.get("head_sha") != ledger.head_sha
            or review.get("workspace_digest") != ledger.workspace_digest
        )

    needs_resume = bool(ledger.unresolved_units) or any(
        item.status == HypothesisStatus.OPEN
        or (item.status == HypothesisStatus.UNKNOWN and item.retryable)
        or (item.status == HypothesisStatus.CONFIRMED and publication_pending(item))
        for item in ledger.items.values()
    )
    if saved and needs_resume and any("coverage" not in record["payload"] for record in records):
        # Old dev records cannot prove which confirmed facts were published.
        # Do not guess coverage and risk duplicating or dropping findings.
        outcome = DeliveryOutcome(error="frozen review lacks coverage metadata; start a new isolated review run")
    if ledger.protocol_version >= 4 and any(_legacy_delivered_coverage_missing(record) for record in records):
        outcome = DeliveryOutcome(
            error="delivered review lacks comparable fact coverage; start a new isolated review run"
        )

    if not saved and state.files_changed:
        try:
            scan = await scan_changed_files(orchestrator._gateway, state)
            _seed_detector_hypotheses(state, changeset, ledger, scan.findings)
        except Exception as exc:
            logger.warning("detector seeding skipped: %s", exc)

    publication = Publication(comments=[], summary_items=[], merged=[], unknown_ids=[], fallback=False)
    language = resolve_output_language(state, config)
    usage_by_agent: dict[str, int] = {}
    stages_ran = False
    if (
        not outcome.error
        and (not saved or needs_resume)
        and getattr(orchestrator, "_model_router", None) is not None
        and changeset.units
    ):
        stages_ran = True
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
    new_publication = not (
        saved
        and ledger.protocol_version >= 3
        and (
            not stages_ran
            or (
                not publication.comments
                and not publication.summary_items
                and payload["body"] in {record["payload"].get("body", "") for record in records}
            )
        )
    )
    if config.mode == "hypothesis" and not outcome.error and new_publication:
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
    operational_outcomes = {"provider-error", "output-error", "budget-exhausted", "tool-error"}
    investigation_failures = sum(
        task.get("outcome") in operational_outcomes for task in ledger.investigation_tasks.values()
    ) + sum(
        item.status == HypothesisStatus.OPEN
        or (item.status == HypothesisStatus.UNKNOWN and not item.investigation_outcome)
        for item in hypotheses
    )
    proof_failures = (
        sum(item.status == HypothesisStatus.CONFIRMED and not completed(item, ledger) for item in hypotheses)
        if ledger.protocol_version >= 3
        else 0
    )
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
            "protocol_version": ledger.protocol_version,
            **({"request_budget": ledger.request_budget} if ledger.protocol_version >= 3 else {}),
            "investigation_tasks": len(ledger.investigation_tasks),
            "investigation_failures": investigation_failures,
            "policy_deferred": sum(item.investigation_outcome == "policy-deferred" for item in hypotheses),
            **(
                {
                    "publication_verified": sum(accepted(item, ledger) for item in hypotheses),
                    "publication_proof_failures": proof_failures,
                }
                if ledger.protocol_version >= 3
                else {}
            ),
        },
    )
    return RunHealth.build(
        publication_failures=proof_failures,
        publication_retryable=any(proof_retryable(item, ledger) for item in hypotheses),
        hypothesis_failures=len(ledger.unresolved_units),
        investigation_unknown_errors=unknown_error,
        investigation_retryable=any(item.retryable for item in hypotheses),
        protocol_version=ledger.protocol_version,
        investigation_failures=investigation_failures,
        investigation_unknown_total=sum(item.status == HypothesisStatus.UNKNOWN for item in hypotheses),
        investigation_deferred=sum(item.investigation_outcome == "policy-deferred" for item in hypotheses),
        budget_failures=int(
            (
                ledger.request_budget.get("reported_tokens", 0)
                + ledger.request_budget.get("unreported_reserved_tokens", 0)
                > config.max_tokens_per_pr
                or ledger.request_budget.get("denied_requests", 0) > 0
            )
            if ledger.protocol_version >= 3
            else ledger.protocol_version >= 2 and sum(usage_by_agent.values()) > config.max_tokens_per_pr
        ),
        delivery_failures=rejected,
        delivery_errors=(outcome.error,) if outcome.error else (),
        delivery_retryable=outcome.retryable,
    )
