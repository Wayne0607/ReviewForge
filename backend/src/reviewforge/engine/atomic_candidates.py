"""Model-declared independent proof obligations, before case identities freeze.

Only an explicit structured decomposition creates children. Parent prose is
untrusted navigation, and neither investigation nor proof can rewrite an
accepted child's original question into another case.
"""

from __future__ import annotations

import copy
from typing import Any, Literal, get_args

from pydantic import ConfigDict, Field, ValidationError, create_model

from reviewforge.engine.review_session import fingerprint


class AtomicRecord(dict):
    """Controller-produced metadata; ordinary model dictionaries cannot forge it."""


def atomic_proposal_schema(hypothesis_schema):
    fields = {
        name: (field.annotation, copy.deepcopy(field))
        for name, field in hypothesis_schema.model_fields.items()
        if name not in {"kind", "unit_id"}
    }
    claim = create_model(
        "AtomicClaim",
        __config__=ConfigDict(extra="forbid", strict=True),
        claim_id=(str, Field(min_length=1)),
        **fields,
    )
    unit = hypothesis_schema.model_fields["unit_id"]
    return create_model(
        "AtomicHypothesesAssessment",
        __config__=ConfigDict(extra="forbid", strict=True),
        kind=(Literal["atomic_hypotheses"], ...),
        unit_id=(unit.annotation, copy.deepcopy(unit)),
        proposal_id=(str, Field(min_length=1)),
        proposal_summary=(str, Field(min_length=1)),
        atomic_claims=(list[claim], Field(min_length=1)),
    )


def _error(unit_id, proposal_id, claim_id, reason, *, fields=()):
    return {
        "action": "submit_assessments",
        "unit_id": unit_id,
        "proposal_id": proposal_id,
        "claim_id": claim_id,
        "error": reason,
        **({"fields": list(fields)} if fields else {}),
    }


def expand_atomic_records(work, records, schemas):
    """Validate children independently; never throw away a valid sibling."""
    expanded, feedback = [], []
    for raw in records:
        if not isinstance(raw, dict) or raw.get("kind") != "atomic_hypotheses":
            expanded.append(raw)
            continue
        unit_id, proposal_id = raw.get("unit_id", ""), raw.get("proposal_id", "")
        parent_schema = schemas["atomic_hypotheses"]
        envelope_schema = create_model(
            "AtomicEnvelope", __base__=parent_schema, atomic_claims=(list[Any], Field(min_length=1))
        )
        try:
            envelope = envelope_schema.model_validate(raw).model_dump()
        except ValidationError as exc:
            feedback.append(_error(unit_id, proposal_id, "", "invalid-atomic-proposal-schema", fields=_fields(exc)))
            continue
        scope = {
            "run_id": work.workspace.ledger.run_id,
            "head_sha": work.workspace.ledger.head_sha,
            "workspace_digest": work.workspace.ledger.workspace_digest,
            "task_id": work.session.record["task_id"],
            "unit_id": unit_id,
            "proposal_id": proposal_id,
        }
        identity = "proposal_" + fingerprint(scope)[:24]
        proposals = work.flow.setdefault("atomic_proposals", {})
        entry = proposals.get(identity)
        claims = envelope["atomic_claims"]
        identifiers = [claim.get("claim_id") if isinstance(claim, dict) else None for claim in claims]
        valid_ids = [value for value in identifiers if isinstance(value, str) and value.strip()]
        if entry is None:
            entry = proposals[identity] = {
                "unit_id": unit_id,
                "proposal_id": proposal_id,
                "summary": envelope["proposal_summary"],
                "claim_ids": list(dict.fromkeys(valid_ids)),
                "accepted": {},
                "unidentified": len(identifiers) - len(set(valid_ids)),
            }
        elif entry["summary"] != envelope["proposal_summary"]:
            feedback.append(_error(unit_id, proposal_id, "", "atomic-proposal-frozen"))
            continue
        seen = set()
        claim_schema = get_args(parent_schema.model_fields["atomic_claims"].annotation)[0]
        for child, claim_id in zip(claims, identifiers, strict=True):
            if not isinstance(claim_id, str) or not claim_id.strip() or claim_id in seen:
                feedback.append(_error(unit_id, proposal_id, claim_id or "", "invalid-atomic-claim-identity"))
                continue
            seen.add(claim_id)
            if claim_id not in entry["claim_ids"]:
                if not entry["unidentified"]:
                    feedback.append(_error(unit_id, proposal_id, claim_id, "atomic-proposal-membership-frozen"))
                    continue
                entry["claim_ids"].append(claim_id)
                entry["unidentified"] -= 1
            try:
                values = claim_schema.model_validate(child).model_dump(exclude_none=True)
            except ValidationError as exc:
                feedback.append(
                    _error(unit_id, proposal_id, claim_id, "invalid-atomic-claim-schema", fields=_fields(exc))
                )
                continue
            values.pop("claim_id")
            values.update(kind="hypothesis", unit_id=unit_id)
            signature = _claim_fingerprint(values)
            accepted = entry["accepted"].get(claim_id)
            if accepted:
                if accepted != signature:
                    feedback.append(_error(unit_id, proposal_id, claim_id, "atomic-claim-frozen"))
                else:
                    feedback.append(
                        {
                            "action": "submit_assessments",
                            "unit_id": unit_id,
                            "proposal_id": proposal_id,
                            "claim_id": claim_id,
                            "result": "atomic-claim-already-accepted",
                        }
                    )
                continue
            values["atomic_origin"] = {
                "proposal_identity": identity,
                "proposal_id": proposal_id,
                "proposal_summary": entry["summary"],
                "claim_id": claim_id,
                "task_id": scope["task_id"],
                "unit_id": unit_id,
                "submitted_fingerprint": signature,
                "trust": "untrusted-model-proposal",
            }
            expanded.append(AtomicRecord(values))
    return expanded, feedback


def _claim_fingerprint(values):
    value = copy.deepcopy(values)
    for name in ("source_refs", "site_refs", "change_fields"):
        if name in value:
            value[name] = sorted(set(value[name]))
    return fingerprint(value)


def _fields(exc):
    return [{"field": list(error["loc"]), "type": error["type"]} for error in exc.errors(include_input=False)]


def finalize_atomic_records(work, validated):
    for kind, raw in validated:
        origin = raw.get("atomic_origin") if kind == "hypothesis" else None
        if not origin:
            continue
        entry = work.flow["atomic_proposals"][origin["proposal_identity"]]
        entry["accepted"][origin["claim_id"]] = origin["submitted_fingerprint"]


def pending_atomic_claims(work, unit_id):
    return [
        {
            "proposal_id": entry["proposal_id"],
            "claim_ids": [key for key in entry["claim_ids"] if key not in entry["accepted"]],
            "unidentified_claims": entry["unidentified"],
        }
        for entry in work.flow.get("atomic_proposals", {}).values()
        if entry["unit_id"] == unit_id and (entry["unidentified"] or set(entry["claim_ids"]) - entry["accepted"].keys())
    ]


def atomic_coverage(work, ledger, unit_id):
    cases = {}
    for item in ledger.items.values():
        origin = item.discovery_basis.get("atomic_origin")
        if origin and item.unit_id == unit_id:
            cases[(origin["proposal_identity"], origin["claim_id"])] = item.identity
    return [
        {
            "proposal_id": entry["proposal_id"],
            "claim_id": claim_id,
            "status": "accepted" if claim_id in entry["accepted"] else "pending",
            "case_identity": cases.get((identity, claim_id), ""),
        }
        for identity, entry in work.flow.get("atomic_proposals", {}).items()
        if entry["unit_id"] == unit_id
        for claim_id in entry["claim_ids"]
    ]


def bind_atomic_identity(hypothesis, ledger):
    origin = hypothesis.discovery_basis.get("atomic_origin")
    if not origin:
        return hypothesis
    binding = {
        "run_id": ledger.run_id,
        "head_sha": ledger.head_sha,
        "workspace_digest": ledger.workspace_digest,
        "task_id": origin["task_id"],
        "unit_id": hypothesis.unit_id,
        "proposal_id": origin["proposal_id"],
    }
    if (
        origin.get("proposal_identity") != "proposal_" + fingerprint(binding)[:24]
        or origin["unit_id"] != hypothesis.unit_id
    ):
        raise ValueError("atomic-proposal-binding-mismatch")
    result = copy.deepcopy(hypothesis)
    base_identity = result.coverage_identity or result.identity
    prefix, separator, anchor = base_identity.rpartition("::")
    digest = fingerprint({"proposal": origin["proposal_identity"], "claim_id": origin["claim_id"]})[:24]
    result.coverage_identity = base_identity
    result.identity = f"{prefix}::atomic_{digest}::{anchor}" if separator else f"{base_identity}::atomic_{digest}"
    result.id = "h_" + fingerprint(result.identity)[:16]
    current = ledger.items.get(result.identity)
    if current is not None and (
        current.discovery_basis.get("atomic_origin") != origin
        or any(
            getattr(current, name) != getattr(result, name)
            for name in (
                "claim",
                "trigger",
                "impact",
                "open_question",
                "refutation",
                "sites",
                "mechanism",
            )
        )
    ):
        raise ValueError("atomic-case-frozen")
    return result
