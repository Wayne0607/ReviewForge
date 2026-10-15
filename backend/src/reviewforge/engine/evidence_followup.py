"""One evidence-only continuation inside the original investigation allowance.

A source-audit gap is a navigation request, never evidence or permission to
revise a claim. Only fresh saved source facts invalidate the original proof
receipt; failed searches and repeated text remain in the followup audit trail.
The persisted task marker is written before dispatch and is never retried.
"""

from __future__ import annotations

import asyncio
import copy
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from typing import Any

from reviewforge.engine.hypothesis import Hypothesis, HypothesisLedger, HypothesisStatus, Observation
from reviewforge.engine.investigator import InvestigationResult
from reviewforge.engine.proof_gate import _publication_problem, _scope_matches, completed, proof_fingerprint
from reviewforge.engine.source_audit import audit_hash, current

_GAP_PROBLEM = ("insufficient-evidence", "no-independent-contract-or-execution")
_COUNTERS = ("total_steps", "total_tokens", "total_unreported_tokens")
_LIMITS = ("budget_steps_limit", "token_limit")


def _preflight(item: Hypothesis, ledger: HypothesisLedger) -> bool:
    record = ledger.publication_reviews.get(item.id)
    return (
        item.status == HypothesisStatus.CONFIRMED
        and isinstance(record, dict)
        and record.get("preflight") is True
        and record.get("decision") == "unknown"
        and (record.get("outcome"), record.get("reason")) == _GAP_PROBLEM
        and isinstance(ledger.source_audits.get(item.id), dict)
        and current(item, ledger)
        and _scope_matches(record, item, ledger)
        and _publication_problem(item, ledger) == _GAP_PROBLEM
        and completed(item, ledger)
    )


def _original_task(item: Hypothesis, ledger: HypothesisLedger) -> dict[str, Any] | None:
    task = ledger.investigation_tasks.get(item.investigation_task_id)
    if not isinstance(task, dict) or "evidence_followup" in task:
        return None
    # A historical task without frozen limits/counters cannot be upgraded into
    # a fresh allowance. Bool is deliberately not accepted as a numeric limit.
    metered = (
        ledger.protocol_version >= 8
        and task.get("budget_policy") == "usage-only-v1"
        and task.get("token_limit") is None
    )
    limits = ("budget_steps_limit",) if metered else _LIMITS
    if any(type(task.get(key)) is not int or task[key] < 0 for key in (*_COUNTERS, *limits)):
        return None
    members = task.get("members")
    if (
        not isinstance(members, list)
        or not members
        or any(not isinstance(key, str) or key not in ledger.items for key in members)
        or len(set(members)) != len(members)
        or item.identity not in members
        or task.get("charged_to") not in members
        or set(members)
        != {
            member.identity
            for member in ledger.items.values()
            if member.investigation_task_id == item.investigation_task_id
        }
    ):
        return None
    if task["budget_steps_limit"] <= task["total_steps"] or (
        not metered and task["token_limit"] <= task["total_tokens"] + task["total_unreported_tokens"]
    ):
        return None
    # Equal task members share sources. Never invalidate a completed semantic
    # verdict on another member merely to follow a gap on this candidate.
    for identity in members:
        member = ledger.items[identity]
        if (
            member.investigation_task_id != item.investigation_task_id
            or not _preflight(member, ledger)
            or member.observations != item.observations
            or member.investigation_progress != item.investigation_progress
        ):
            return None
    return task


def _source_key(observation: Observation) -> tuple[Any, ...]:
    # An expanded requested window with the same actually saved source range
    # is not a new fact. Query, id and transport digest cannot create novelty.
    return (
        observation.path,
        observation.sha,
        observation.tool,
        observation.excerpt,
        observation.excerpt_line_range or observation.line_range,
    )


def _complete_observation(observation: Any, head_sha: str) -> bool:
    if not isinstance(observation, Observation) or observation.sha != head_sha:
        return False
    for field in ("id", "tool", "query", "result_digest"):
        if not isinstance(getattr(observation, field), str) or not getattr(observation, field).strip():
            return False
    if not isinstance(observation.path, str) or not isinstance(observation.excerpt, str):
        return False
    if observation.status not in {"success", "not_found", "error"} or len(observation.excerpt) > 1200:
        return False
    for bounds in (observation.line_range, observation.excerpt_line_range):
        if bounds is not None and (
            not isinstance(bounds, tuple) or len(bounds) != 2 or any(type(value) is not int for value in bounds)
        ):
            return False
    return True


def _new_facts(result: InvestigationResult, original: Hypothesis, head_sha: str) -> int:
    if not isinstance(result.observations, list) or not all(
        _complete_observation(observation, head_sha) for observation in result.observations
    ):
        raise ValueError("incomplete-or-wrong-head-observation")
    by_id = {observation.id: observation for observation in result.observations}
    if len(by_id) != len(result.observations):
        raise ValueError("duplicate-observation-id")
    if any(by_id.get(observation.id) != observation for observation in original.observations):
        raise ValueError("original-observation-changed-or-lost")
    if (
        not isinstance(result.progress, list)
        or any(not isinstance(entry, dict) for entry in result.progress)
        or result.progress[: len(original.investigation_progress)] != original.investigation_progress
    ):
        raise ValueError("original-progress-changed-or-lost")
    original_facts = {
        _source_key(observation) for observation in original.observations if observation.status == "success"
    }
    returned_facts = {
        _source_key(observation)
        for observation in result.observations
        if observation.status == "success" and observation.excerpt.strip()
    }
    return len(returned_facts - original_facts)


def _save_segment(record: dict[str, Any], result: InvestigationResult) -> bool:
    """Retain the returned segment, including unsuccessful queries, for audit."""
    # Account first: malformed evidence must not erase a valid usage report.
    valid_usage = True
    for name in ("tokens", "unreported_tokens", "steps"):
        value = getattr(result, name)
        if type(value) is not int or value < 0:
            valid_usage = False
            record["usage_error"] = "invalid-segment-accounting"
        else:
            record[name] = value
    record["observations"] = [asdict(observation) for observation in result.observations]
    record["progress"] = copy.deepcopy(result.progress)
    record["outcome"] = result.outcome
    for row in reversed(result.progress):
        if row.get("phase") == "evidence-collection" and row.get("event") == "budget-final":
            record["budget_snapshot"] = copy.deepcopy(row.get("budget"))
            break
    return valid_usage


async def run_evidence_followup(
    worker: Any,
    ledger: HypothesisLedger,
    state: Any,
    pack: Any,
    on_update: Callable[[HypothesisLedger], Awaitable[None]] | None = None,
) -> bool:
    """Add sources once per original task; return whether independent audit is due.

    The caller serializes this phase and reruns the independent audit/proof gate
    only when True is returned. No task, status, claim or assessment is created
    or reopened here, and the worker retains its physical PR budget.
    """
    if (
        ledger.protocol_version < 4
        or getattr(state, "head_sha", None) != ledger.head_sha
        or getattr(pack, "workspace_digest", None) != ledger.workspace_digest
    ):
        return False
    changed = False
    resolved_worker = None
    for item in list(ledger.items.values()):
        if not _preflight(item, ledger):
            continue
        gaps = ledger.source_audits[item.id]["facts"]["gaps"]
        task = _original_task(item, ledger)
        if not gaps or task is None:
            continue
        original = copy.deepcopy(item)
        scope = {
            identity: proof_fingerprint(ledger.items[identity], ledger.head_sha, ledger.workspace_digest)
            for identity in task["members"]
        }
        record = {
            "state": "started",
            "hypothesis_id": item.id,
            "audit_hash": audit_hash(item, ledger),
            "proof_fingerprint": scope[item.identity],
            "run_id": ledger.run_id,
            "head_sha": ledger.head_sha,
            "workspace_digest": ledger.workspace_digest,
            "charged_to": task["charged_to"],
            "allocated": {
                "tokens": None
                if task["token_limit"] is None
                else task["token_limit"] - task["total_tokens"] - task["total_unreported_tokens"],
                "steps": task["budget_steps_limit"] - task["total_steps"],
            },
            "original_audit": copy.deepcopy(ledger.source_audits[item.id]),
            "original_review": copy.deepcopy(ledger.publication_reviews[item.id]),
            "tokens": 0,
            "unreported_tokens": 0,
            "steps": 0,
            "new_facts": 0,
        }
        task["evidence_followup"] = record
        if on_update is not None:
            await on_update(ledger)
        returned = None
        interrupted = False
        dispatched = False
        try:
            if resolved_worker is None:
                resolved_worker = worker if hasattr(worker, "collect_evidence") else worker()
            dispatched = True
            returned = await resolved_worker.collect_evidence(
                copy.deepcopy(original),
                state,
                pack,
                token_limit=record["allocated"]["tokens"],
                max_steps=record["allocated"]["steps"],
                gaps=copy.deepcopy(gaps),
            )
            valid_usage = _save_segment(record, returned)
            if not valid_usage:
                raise ValueError("invalid-segment-accounting")
            if (
                record["allocated"]["tokens"] is not None
                and record["tokens"] + record["unreported_tokens"] > record["allocated"]["tokens"]
            ) or record["steps"] > record["allocated"]["steps"]:
                record["rejection_reason"] = "segment-exceeded-allocated-budget"
                raise ValueError(record["rejection_reason"])
            count = _new_facts(returned, original, ledger.head_sha)
            if any(
                proof_fingerprint(ledger.items[identity], ledger.head_sha, ledger.workspace_digest) != fingerprint
                for identity, fingerprint in scope.items()
            ):
                raise ValueError("original-proof-scope-changed")
            if count:
                for identity in task["members"]:
                    member = ledger.items[identity]
                    member.observations = copy.deepcopy(returned.observations)
                    member.investigation_progress = copy.deepcopy(returned.progress)
                changed = True
            record.update(state="completed", new_facts=count)
        except (Exception, asyncio.CancelledError) as exc:
            interrupted = returned is None
            record.update(state="interrupted" if interrupted else "rejected", error_type=type(exc).__name__)
            if interrupted and dispatched:
                # This snapshot is provided by the same worker invocation, not
                # a compensating model call. Preserve spend even on cancellation.
                try:
                    snapshot = resolved_worker.evidence_followup_snapshot()
                    _save_segment(record, snapshot)
                except (Exception, asyncio.CancelledError) as snapshot_error:
                    # Fail closed without converting cancellation into a new
                    # exception, or pretending an unavailable spend report is 0.
                    record["snapshot_error_type"] = type(snapshot_error).__name__
                    record["usage_unavailable"] = True
            if isinstance(exc, asyncio.CancelledError):
                raise
        finally:
            # Segment counters are deltas; shared aliases never pay again.
            task["total_tokens"] += record["tokens"]
            task["total_unreported_tokens"] += record["unreported_tokens"]
            task["total_steps"] += record["steps"]
            charged = ledger.items[task["charged_to"]]
            charged.investigation_tokens += record["tokens"]
            charged.investigation_unreported_tokens += record["unreported_tokens"]
            charged.investigation_steps += record["steps"]
            if interrupted:
                record["state"] = "interrupted"
            if on_update is not None:
                await on_update(ledger)
    return changed
