"""Independent, fail-closed publication proof for protocol v3.

Saved citations establish provenance, not semantic entailment. This stage asks
an independent model to review the original claim, then validates the returned
proof shape and exact references. It never changes investigation status or
rewrites findings. Transport retries remain the caller's bounded wrapper.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables.base import RunnableBindingBase

from reviewforge.core.change_manifest import is_verified_rename
from reviewforge.core.json_output import strip_reasoning_blocks
from reviewforge.engine.hypothesis import Hypothesis, HypothesisLedger, HypothesisStatus
from reviewforge.engine.investigator import _evidence_segments
from reviewforge.engine.prompts_v4 import load_prompt
from reviewforge.engine.publication_contracts import contract_catalog
from reviewforge.engine.request_budget import budget_snapshot_over_limit, plan_request
from reviewforge.engine.source_audit import audit_hash, run_source_audit
from reviewforge.engine.source_audit import current as audit_current
from reviewforge.engine.verification_guidance import (
    defect_scope_guidance,
    has_python_concurrency,
    is_localization_path,
    localization_guidance,
    python_concurrency_guidance,
    state_guidance,
)

PROOF_VERSION = 1
OBLIGATIONS = ("supported_input", "required_behavior", "introduced_regression", "bad_consequence")
_MAX_CALLS = 4
_MAX_ATTEMPTS = 2
_INPUT_CHARS = 32_000
_MAX_OUTPUT = 4_096
_DEFAULT_AVAILABLE = 16_000
_COMPLETED = {"resolved", "insufficient-evidence"}


def _facts(item: Hypothesis) -> dict[str, Any]:
    return {
        **{
            key: getattr(item, key)
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
        },
        "mechanism": item.mechanism.value,
        "sites": [asdict(site) for site in item.sites],
        "change_anchor": item.change_anchor.to_dict() if item.change_anchor is not None else None,
        "discovery_basis": item.discovery_basis,
        "assessment": item.assessment.to_dict() if item.assessment is not None else None,
    }


def proof_fingerprint(item: Hypothesis, head_sha: str, workspace_digest: str) -> str:
    """Bind every supplied fact and saved observation, including failed reads."""
    value = {
        "proof_version": PROOF_VERSION,
        "head_sha": head_sha,
        "workspace_digest": workspace_digest,
        "candidate": _facts(item),
        "observations": [asdict(observation) for observation in item.observations],
        "counterevidence": item.counterevidence.to_dict() if item.counterevidence is not None else None,
        "investigation_progress": item.investigation_progress,
    }
    source = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def _references(item: Hypothesis) -> dict[str, dict[str, str]]:
    return {
        f"{item.id}/{reference}": {
            "original_ref": reference,
            "observation_id": citation.observation_id,
            "quote": citation.quote,
        }
        for observation in item.observations
        for reference, citation in _evidence_segments(observation).items()
    }


def _source_problem(item: Hypothesis, head_sha: str) -> tuple[str, str] | None:
    ids = [observation.id for observation in item.observations]
    if any(not identity for identity in ids) or len(ids) != len(set(ids)):
        return "output-error", "duplicate-or-empty-observation-id"
    if any(observation.sha != head_sha for observation in item.observations if observation.status == "success"):
        return "output-error", "observation-head-mismatch"
    if not _references(item):
        return "insufficient-evidence", "no-successful-source-evidence"
    assessment = item.assessment
    if assessment is None:
        return "insufficient-evidence", "missing-original-assessment"
    if assessment.comparison != "conflict" or not assessment.expected.strip() or not assessment.actual.strip():
        return "output-error", "inconsistent-original-assessment"
    by_id = {observation.id: observation for observation in item.observations}
    for citations in (assessment.expected_evidence, assessment.actual_evidence):
        if not citations or any(
            citation.observation_id not in by_id
            or by_id[citation.observation_id].status != "success"
            or not citation.quote.strip()
            or citation.quote not in by_id[citation.observation_id].excerpt
            for citation in citations
        ):
            return "output-error", "invalid-original-assessment-citation"
    if item.change_anchor is not None:
        if not is_verified_rename(item.change_anchor, head_sha):
            return "output-error", "invalid-change-anchor"
        if not any(
            by_id[citation.observation_id].tool in {"read_file", "read_diff"} for citation in assessment.actual_evidence
        ):
            return "output-error", "structural-actual-needs-source-evidence"
    return None


def _text(raw: Any, name: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(f"missing-{name}")
    return raw.strip()


def _proof(raw: Any, refs: dict[str, dict[str, str]]) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) != {"statement", "evidence"}:
        raise ValueError("invalid-obligation-shape")
    statement = _text(raw["statement"], "proof-statement")
    evidence = raw["evidence"]
    if (
        not isinstance(evidence, list)
        or not evidence
        or any(not isinstance(reference, str) or reference not in refs for reference in evidence)
        or len(evidence) != len(set(evidence))
    ):
        raise ValueError("invalid-proof-reference")
    return {"statement": statement, "evidence": list(evidence)}


def _validate_review(raw: Any, item: Hypothesis) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) != {
        "hypothesis_id",
        "decision",
        "reason",
        "obligations",
        "counterevidence",
        "missing_obligations",
    }:
        raise ValueError("invalid-review-shape")
    if raw["hypothesis_id"] != item.id or raw["decision"] not in {"accepted", "refuted", "unknown"}:
        raise ValueError("invalid-review-identity-or-decision")
    refs = _references(item)
    obligations = raw["obligations"]
    if not isinstance(obligations, dict) or set(obligations) - set(OBLIGATIONS):
        raise ValueError("invalid-obligations")
    obligations = {name: _proof(proof, refs) for name, proof in obligations.items()}
    missing = raw["missing_obligations"]
    if (
        not isinstance(missing, list)
        or any(name not in OBLIGATIONS for name in missing)
        or len(missing) != len(set(missing))
        or set(missing) & set(obligations)
    ):
        raise ValueError("invalid-missing-obligations")
    counter = raw["counterevidence"]
    if counter is not None:
        if not isinstance(counter, dict) or set(counter) != {"premise", "statement", "evidence"}:
            raise ValueError("invalid-counterevidence")
        if counter["premise"] not in {"trigger", "mechanism", "impact", "regression"}:
            raise ValueError("invalid-counterevidence-premise")
        counter = {
            "premise": counter["premise"],
            **_proof({key: counter[key] for key in ("statement", "evidence")}, refs),
        }
    decision = raw["decision"]
    if decision == "accepted" and (set(obligations) != set(OBLIGATIONS) or counter is not None or missing):
        raise ValueError("incomplete-or-contradictory-acceptance")
    if decision == "refuted" and (counter is None or set(obligations) == set(OBLIGATIONS)):
        raise ValueError("missing-or-contradictory-counterevidence")
    if decision == "unknown" and (not missing or counter is not None):
        raise ValueError("missing-or-contradictory-uncertainty")
    return {
        "decision": decision,
        "reason": _text(raw["reason"], "review-reason"),
        "obligations": obligations,
        "counterevidence": counter,
        "missing_obligations": missing,
        "outcome": "insufficient-evidence" if decision == "unknown" else "resolved",
        "validated": True,
    }


def _scope_matches(record: dict[str, Any], item: Hypothesis, ledger: HypothesisLedger) -> bool:
    return (
        isinstance(record, dict)
        and record.get("proof_version") == (2 if ledger.protocol_version >= 4 else PROOF_VERSION)
        and record.get("fingerprint") == proof_fingerprint(item, ledger.head_sha, ledger.workspace_digest)
        and record.get("head_sha") == ledger.head_sha
        and record.get("workspace_digest") == ledger.workspace_digest
        and record.get("run_id") == ledger.run_id
        and (ledger.protocol_version < 4 or record.get("source_audit_hash") == audit_hash(item, ledger))
    )


def _publication_problem(item: Hypothesis, ledger: HypothesisLedger) -> tuple[str, str] | None:
    problem = _source_problem(item, ledger.head_sha)
    if problem is not None or ledger.protocol_version < 4:
        return problem
    if not audit_current(item, ledger):
        record = ledger.source_audits.get(item.id, {})
        outcome = record.get("outcome", "output-error")
        return (
            outcome if outcome in {"budget-exhausted", "provider-error", "output-error"} else "output-error",
            "independent-source-audit-not-complete",
        )
    facts = ledger.source_audits[item.id]["facts"]
    if not contract_catalog(item, ledger) or not facts["execution"]:
        return "insufficient-evidence", "no-independent-contract-or-execution"
    return None


def _validate_v4_review(raw: Any, item: Hypothesis, ledger: HypothesisLedger) -> dict[str, Any]:
    """Disjoint verdict shapes plus a program-checked independent contract link."""
    if not isinstance(raw, dict):
        raise ValueError("invalid-review-shape")
    decision = raw.get("decision")
    extra = {
        "accepted": {"obligations", "contract_refs"},
        "refuted": {"counterevidence"},
        "unknown": {"missing_obligations"},
    }.get(decision)
    if extra is None or set(raw) != {"hypothesis_id", "decision", "reason"} | extra:
        raise ValueError("invalid-discriminated-verdict")
    normalized = {
        **raw,
        "obligations": raw.get("obligations", {}),
        "counterevidence": raw.get("counterevidence"),
        "missing_obligations": raw.get("missing_obligations", []),
    }
    selected = normalized.pop("contract_refs", None)
    result = _validate_review(normalized, item)
    if decision == "accepted":
        contracts = contract_catalog(item, ledger)
        if (
            not audit_current(item, ledger)
            or not isinstance(selected, list)
            or not selected
            or any(not isinstance(reference, str) or reference not in contracts for reference in selected)
            or len(set(selected)) != len(selected)
        ):
            raise ValueError("missing-independent-contract-binding")
        requirement = set(result["obligations"]["required_behavior"]["evidence"])
        if any(not requirement.intersection(contracts[reference]["evidence"]) for reference in selected):
            raise ValueError("independent-contract-evidence-mismatch")
        result["contract_refs"] = selected
    return result


def _completed_review(record: dict[str, Any], item: Hypothesis, ledger: HypothesisLedger) -> bool:
    if not _scope_matches(record, item, ledger) or record.get("validated") is not True:
        return False
    try:
        raw = {
            "hypothesis_id": item.id,
            **{
                key: record.get(key)
                for key in ("decision", "reason", "obligations", "counterevidence", "missing_obligations")
            },
        }
        if ledger.protocol_version >= 4:
            decision = raw["decision"]
            fields = {
                "accepted": ("obligations", "contract_refs"),
                "refuted": ("counterevidence",),
                "unknown": ("missing_obligations",),
            }.get(decision, ())
            validated = _validate_v4_review(
                {
                    "hypothesis_id": item.id,
                    "decision": decision,
                    "reason": record.get("reason"),
                    **{key: record.get(key) for key in fields},
                },
                item,
                ledger,
            )
        else:
            validated = _validate_review(raw, item)
    except (TypeError, ValueError):
        return False
    return record.get("outcome") == validated["outcome"]


def accepted(item: Hypothesis, ledger: HypothesisLedger) -> bool:
    """Check the current immutable proof again before deterministic publication."""
    if ledger.protocol_version < 3 or item.status != HypothesisStatus.CONFIRMED:
        return False
    if sum(candidate.id == item.id for candidate in ledger.items.values()) != 1:
        return False
    if _publication_problem(item, ledger) is not None:
        return False
    record = ledger.publication_reviews.get(item.id, {})
    return isinstance(record, dict) and record.get("decision") == "accepted" and _completed_review(record, item, ledger)


def completed(item: Hypothesis, ledger: HypothesisLedger) -> bool:
    """A current valid semantic decision or deterministic missing-input receipt."""
    if ledger.protocol_version < 3 or item.status != HypothesisStatus.CONFIRMED:
        return False
    if sum(candidate.id == item.id for candidate in ledger.items.values()) != 1:
        return False
    record = ledger.publication_reviews.get(item.id, {})
    if not _scope_matches(record, item, ledger):
        return False
    problem = _publication_problem(item, ledger)
    if problem is None:
        return _completed_review(record, item, ledger)
    return (
        problem[0] == "insufficient-evidence"
        and record.get("outcome") == problem[0]
        and record.get("reason") == problem[1]
        and record.get("preflight") is True
        and record.get("decision") == "unknown"
        and record.get("obligations") == {}
        and record.get("counterevidence") is None
        and record.get("missing_obligations") == list(OBLIGATIONS)
    )


def retryable(item: Hypothesis, ledger: HypothesisLedger) -> bool:
    """Only a current execution error with another persisted attempt available.

    A stale/missing review is new work, not an error retry. Budget refusals cannot
    replenish the original budget by resuming; they are not retryable.
    """
    record = ledger.publication_reviews.get(item.id, {})
    return (
        ledger.protocol_version >= 3
        and item.status == HypothesisStatus.CONFIRMED
        and _scope_matches(record, item, ledger)
        and record.get("outcome") in {"provider-error", "output-error"}
        and type(record.get("attempts")) is int
        and 0 < record["attempts"] < _MAX_ATTEMPTS
    )


def _constraints(item: Hypothesis) -> list[str]:
    paths = {site.path for site in item.sites}
    paths.update(observation.path for observation in item.observations if observation.status == "success")
    if item.change_anchor is not None:
        paths.update((item.change_anchor.path, item.change_anchor.previous_path))
    constraints = [defect_scope_guidance()]
    if item.mechanism.value == "i18n" or any(is_localization_path(path) for path in paths):
        constraints.append(localization_guidance())
    if any(
        has_python_concurrency(observation.path, observation.excerpt)
        for observation in item.observations
        if observation.status == "success"
    ):
        constraints.append(python_concurrency_guidance())
    if item.mechanism.value == "state-leak":
        constraints.append(state_guidance())
    return constraints


def _packet(item: Hypothesis) -> dict[str, Any]:
    observations = []
    for observation in item.observations:
        if observation.status == "success":
            record = asdict(observation)
            # The exact named quotes are the source body. Replaying excerpt as
            # well would double it without adding any evidence to this review.
            record.pop("excerpt")
            record["references"] = {
                f"{item.id}/{reference}": {"original_ref": reference, "quote": citation.quote}
                for reference, citation in _evidence_segments(observation).items()
            }
            if any(
                entry.get("phase") == "diff-navigation" and entry.get("observation_id") == observation.id
                for entry in item.investigation_progress
            ):
                record["source_view"] = "partial diff excerpt; not a complete hunk"
        else:
            record = {
                "id": observation.id,
                "tool": observation.tool,
                "path": observation.path,
                "status": observation.status,
                "citable": False,
            }
        observations.append(record)
    return {"candidate": _facts(item), "contract_constraints": _constraints(item), "observations": observations}


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate-json-key")
        result[key] = value
    return result


def _parse_response(
    content: Any, *, allow_container_closers: bool = False, framing: dict[str, str] | None = None
) -> list[dict[str, Any]]:
    # Single envelope and unique keys; never extract a substring or invent a
    # value. V4 can close outer containers after a fully closed nested value.
    source = strip_reasoning_blocks(content)
    if source.startswith("```json\n") and source.endswith("\n```"):
        source = source[8:-4]
    try:
        raw = json.loads(source, object_pairs_hook=_unique_object)
    except json.JSONDecodeError:
        if not allow_container_closers:
            raise
        source = _close_containers(source)
        raw = json.loads(source, object_pairs_hook=_unique_object)
        if framing is not None:
            framing["recovery"] = "missing-container-closers"
    if not isinstance(raw, dict) or set(raw) != {"reviews"} or not isinstance(raw["reviews"], list):
        raise ValueError("invalid-review-envelope")
    return raw["reviews"]


def _close_containers(source: str) -> str:
    """Append only missing delimiters, never complete strings or scalar values.

    Every requested ID and every verdict field must still pass the caller's
    exact schema and evidence validators; a syntactic repair grants no proof.
    """
    source = source.strip()
    if not source.startswith("{"):
        raise ValueError("invalid-review-envelope")
    if not source.endswith(("}", "]")):
        raise ValueError("incomplete-review-value")
    stack = []
    quoted = escaped = False
    for char in source:
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in "{[":
            stack.append("}" if char == "{" else "]")
        elif char in "}]":
            if not stack or stack.pop() != char:
                raise ValueError("mismatched-review-delimiter")
    if quoted or not stack:
        raise ValueError("invalid-review-envelope")
    return source + "".join(reversed(stack))


def _usage(response: Any) -> int | None:
    usage = getattr(response, "usage_metadata", None) or {}
    usage = usage or (getattr(response, "response_metadata", {}) or {}).get("token_usage", {})
    total = usage.get("total_tokens")
    if type(total) is int and total >= 0:
        return total
    for input_name, output_name in (("input_tokens", "output_tokens"), ("prompt_tokens", "completion_tokens")):
        if all(type(usage.get(key)) is int and usage[key] >= 0 for key in (input_name, output_name)):
            return usage[input_name] + usage[output_name]
    return None


def _live_available(llm: Any, messages: list[Any]) -> int | None:
    options = {"max_tokens": _MAX_OUTPUT}
    while isinstance(llm, RunnableBindingBase):
        options = llm.kwargs | options
        llm = llm.bound
    viewer = getattr(llm, "request_budget_view", None)
    view = viewer(messages, options) if callable(viewer) else None
    return view.available_tokens if view is not None else None


def _over_budget(ledger: HypothesisLedger) -> bool:
    return budget_snapshot_over_limit(ledger.request_budget or {})


class ProofGate:
    def __init__(
        self,
        llm: Any,
        output_language: str = "en",
        on_update: Callable[[HypothesisLedger], Awaitable[None]] | None = None,
    ):
        self._llm = llm
        self._output_language = output_language
        self._on_update = on_update

    async def _changed(self, ledger: HypothesisLedger) -> None:
        if self._on_update is not None:
            await self._on_update(ledger)

    def _messages(self, ledger: HypothesisLedger, batch: list[Hypothesis], *, forecast: bool = False) -> list[Any]:
        packets = [_packet(item) for item in batch]
        if ledger.protocol_version >= 4:
            for item, packet in zip(batch, packets, strict=True):
                audit = ledger.source_audits.get(item.id, {})
                if forecast and not audit_current(item, ledger):
                    # Sizing only. Unknown future facts are budgeted by the
                    # allocator, never fabricated or sent as source evidence.
                    continue
                packet["independent_source_audit"] = audit["facts"]
                packet["independent_contracts"] = contract_catalog(item, ledger)
        return [
            SystemMessage(
                content=load_prompt(
                    "proof_gate_v4" if ledger.protocol_version >= 4 else "proof_gate_v3",
                    output_language=("简体中文" if self._output_language == "zh-CN" else "English"),
                )
            ),
            HumanMessage(
                content=json.dumps(
                    {
                        "head_sha": ledger.head_sha,
                        "workspace_digest": ledger.workspace_digest,
                        "items": packets,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            ),
        ]

    @staticmethod
    def _record(item: Hypothesis, ledger: HypothesisLedger, old: dict[str, Any], **fields) -> dict[str, Any]:
        return {
            "proof_version": 2 if ledger.protocol_version >= 4 else PROOF_VERSION,
            "fingerprint": proof_fingerprint(item, ledger.head_sha, ledger.workspace_digest),
            "run_id": ledger.run_id,
            "head_sha": ledger.head_sha,
            "workspace_digest": ledger.workspace_digest,
            "attempts": old.get("attempts", 0) + 1,
            "model_attempts": old.get("model_attempts", 0),
            "tokens": old.get("tokens", 0),
            "decision": "unknown",
            "outcome": "insufficient-evidence",
            "reason": "",
            "obligations": {},
            "counterevidence": None,
            "missing_obligations": list(OBLIGATIONS),
            "validated": False,
            **({"source_audit_hash": audit_hash(item, ledger)} if ledger.protocol_version >= 4 else {}),
            **fields,
        }

    async def run(self, ledger: HypothesisLedger) -> None:
        if ledger.protocol_version < 3:
            return
        if ledger.protocol_version >= 4:
            await run_source_audit(self._llm, ledger, self._on_update)
        candidates = [item for item in ledger.items.values() if item.status == HypothesisStatus.CONFIRMED]
        duplicate_ids = {identity for identity, count in Counter(item.id for item in candidates).items() if count > 1}
        pending = []
        for item in candidates:
            old = ledger.publication_reviews.get(item.id, {})
            if not _scope_matches(old, item, ledger):
                old = {}
            if completed(item, ledger):
                continue
            if old.get("attempts", 0) >= _MAX_ATTEMPTS:
                continue
            if old.get("outcome") == "budget-exhausted":
                continue
            problem = (
                ("output-error", "duplicate-hypothesis-id")
                if item.id in duplicate_ids
                else (_publication_problem(item, ledger))
            )
            if problem is not None:
                ledger.publication_reviews[item.id] = self._record(
                    item, ledger, old, outcome=problem[0], reason=problem[1], preflight=True
                )
                continue
            pending.append(item)
        await self._changed(ledger)
        budget = ledger.request_budget or {}
        available = max(
            0,
            budget.get("limit", _DEFAULT_AVAILABLE)
            - budget.get("reported_tokens", 0)
            - budget.get("unreported_reserved_tokens", 0)
            - sum(record.get("reserved_tokens", 0) for record in budget.get("pending", {}).values()),
        )
        spent = calls = 0
        while pending:
            batch = []
            planned = messages = None
            for item in pending:
                trial = [*batch, item]
                trial_messages = self._messages(ledger, trial)
                if sum(len(message.content) for message in trial_messages) > _INPUT_CHARS:
                    break
                live_available = _live_available(self._llm, trial_messages)
                fallback = live_available if not budget and live_available is not None else available - spent
                trial_plan = plan_request(
                    self._llm, trial_messages, max_output_tokens=_MAX_OUTPUT, fallback_available=max(0, fallback)
                )
                # A policy floor gives a useful response room; it is no guarantee
                # that a model can finish an arbitrary proof in this many tokens.
                if trial_plan.max_output_tokens < 512 + 384 * len(trial):
                    break
                batch, planned, messages = trial, trial_plan, trial_messages
            if not batch or calls >= _MAX_CALLS:
                item = pending.pop(0)
                old = ledger.publication_reviews.get(item.id, {})
                if not _scope_matches(old, item, ledger):
                    old = {}
                ledger.publication_reviews[item.id] = self._record(
                    item,
                    ledger,
                    old,
                    outcome="budget-exhausted",
                    reason=("publication-call-cap" if calls >= _MAX_CALLS else "complete-proof-packet-does-not-fit"),
                )
                await self._changed(ledger)
                continue
            del pending[: len(batch)]
            calls += 1
            for item in batch:
                old = ledger.publication_reviews.get(item.id, {})
                if not _scope_matches(old, item, ledger):
                    old = {}
                ledger.publication_reviews[item.id] = self._record(
                    item,
                    ledger,
                    old,
                    outcome="provider-error",
                    reason="publication-request-interrupted",
                    model_attempts=old.get("model_attempts", 0) + 1,
                )
            # Persist the attempt before dispatch so interrupted resumes are bounded.
            await self._changed(ledger)
            try:
                response = await self._llm.ainvoke(messages, max_tokens=planned.max_output_tokens)
            except Exception as exc:
                spent += planned.input_tokens + planned.max_output_tokens
                outcome = "budget-exhausted" if getattr(exc, "outcome", "") == "budget-exhausted" else "provider-error"
                for item in batch:
                    ledger.publication_reviews[item.id].update(
                        outcome=outcome,
                        reason="publication-admission-rejected"
                        if outcome == "budget-exhausted"
                        else "publication-provider-failed",
                    )
                await self._changed(ledger)
                continue
            tokens = _usage(response)
            spent += tokens if tokens is not None else planned.input_tokens + planned.max_output_tokens
            # The batch has one actual bill. Attribute it once, never per candidate.
            ledger.publication_reviews[batch[0].id]["tokens"] += tokens or 0
            if (tokens is not None and tokens > planned.available_tokens) or _over_budget(ledger):
                for item in batch:
                    ledger.publication_reviews[item.id].update(
                        outcome="budget-exhausted", reason="publication-actual-budget-overrun"
                    )
                await self._changed(ledger)
                continue
            try:
                framing: dict[str, str] = {"recovery": "none"}
                reviews = _parse_response(
                    response.content, allow_container_closers=ledger.protocol_version >= 4, framing=framing
                )
                for item in batch:
                    ledger.publication_reviews[item.id]["output_framing"] = dict(framing)
                ids = [review.get("hypothesis_id") for review in reviews if isinstance(review, dict)]
                if len(ids) != len(reviews) or len(ids) != len(set(ids)) or set(ids) != {item.id for item in batch}:
                    raise ValueError("missing-duplicate-or-unexpected-review-id")
                by_id = {review["hypothesis_id"]: review for review in reviews}
            except (TypeError, ValueError):
                for item in batch:
                    ledger.publication_reviews[item.id].update(outcome="output-error", reason="invalid-review-envelope")
            else:
                for item in batch:
                    try:
                        validated = (
                            _validate_v4_review(by_id[item.id], item, ledger)
                            if ledger.protocol_version >= 4
                            else _validate_review(by_id[item.id], item)
                        )
                    except (TypeError, ValueError) as exc:
                        ledger.publication_reviews[item.id].update(outcome="output-error", reason=str(exc))
                    else:
                        ledger.publication_reviews[item.id].update(validated)
            await self._changed(ledger)


__all__ = ["OBLIGATIONS", "PROOF_VERSION", "ProofGate", "accepted", "completed", "proof_fingerprint", "retryable"]
