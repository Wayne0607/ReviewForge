"""Blind source reconstruction before protocol-v4 publication decisions.

The reconstruction is not a defect verdict or executable proof. Keeping the
candidate and prior assessment out of this request breaks their shared framing;
the subsequent gate must compare the original claim to independently described
source behavior. Both requests use the same bounded publication budget.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from reviewforge.core.json_output import strip_reasoning_blocks
from reviewforge.engine.execution_policy import output_capacity, repeated_state
from reviewforge.engine.hypothesis import HypothesisLedger, HypothesisStatus
from reviewforge.engine.prompts_v4 import load_prompt
from reviewforge.engine.proof_packet_limits import fits_proof_input
from reviewforge.engine.request_budget import plan_request, snapshot_available

_KINDS = {"caller", "test", "schema", "configuration", "format", "language"}


def audit_hash(item, ledger: HypothesisLedger) -> str:
    value = ledger.source_audits.get(item.id)
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def validate(raw: Any, item, *, protocol_version=4) -> dict[str, Any]:
    from reviewforge.engine.proof_gate import _proof, _references, _text
    from reviewforge.engine.session_tools import MAX_RESULT_BYTES

    if protocol_version == 9 and len(json.dumps(raw, ensure_ascii=False).encode()) > MAX_RESULT_BYTES:
        raise ValueError("source-audit-transport-record-too-large")

    if not isinstance(raw, dict) or set(raw) != {"hypothesis_id", "execution", "contracts", "gaps"}:
        raise ValueError("invalid-source-audit-shape")
    if raw["hypothesis_id"] != item.id:
        raise ValueError("invalid-source-audit-id")
    refs = _references(item)
    result = {"hypothesis_id": item.id}
    for name in ("execution", "contracts", "gaps"):
        rows = raw[name]
        if not isinstance(rows, list) or (protocol_version < 9 and len(rows) > 8):
            raise ValueError("invalid-source-audit-rows")
        result[name] = []
        for row in rows:
            if name == "gaps":
                statement = _text(row, "source-gap")
                result[name].append(statement)
                continue
            if name == "contracts":
                if not isinstance(row, dict) or set(row) != {"kind", "statement", "evidence"}:
                    raise ValueError("invalid-source-contract")
                if row["kind"] not in _KINDS:
                    raise ValueError("invalid-source-contract-kind")
            proof = (
                _proof({key: row.get(key) for key in ("statement", "evidence")}, refs)
                if isinstance(row, dict)
                else None
            )
            if (
                proof is None
                or (protocol_version < 9 and len(proof["statement"]) > 600)
                or (protocol_version < 10 and len(proof["evidence"]) > 8)
            ):
                raise ValueError("invalid-source-fact")
            if name == "execution" and set(row) != {"statement", "evidence"}:
                raise ValueError("invalid-source-execution")
            result[name].append({"kind": row["kind"], **proof} if name == "contracts" else proof)
    # Gaps are caveats, not evidence statements. Preserve an unevenly sized
    # explanation verbatim within the original 8 * 600 character total rather
    # than discard valid evidence because one caveat exceeded a style limit.
    if protocol_version < 9 and sum(map(len, result["gaps"])) > 8 * 600:
        raise ValueError("unbounded-source-gap")
    if not any(result[name] for name in ("execution", "contracts", "gaps")):
        raise ValueError("empty-source-audit")
    return result


def current(item, ledger: HypothesisLedger) -> bool:
    from reviewforge.engine.proof_gate import proof_fingerprint

    record = ledger.source_audits.get(item.id, {})
    if (
        record.get("version") != (2 if ledger.protocol_version >= 9 else 1)
        or record.get("run_id") != ledger.run_id
        or record.get("outcome") != "resolved"
    ):
        return False
    if record.get("input_fingerprint"):
        if record["input_fingerprint"] != input_fingerprint(item, ledger):
            return False
    elif record.get("fingerprint") != proof_fingerprint(item, ledger.head_sha, ledger.workspace_digest):
        return False
    try:
        validate(record.get("facts"), item, protocol_version=ledger.protocol_version)
    except (TypeError, ValueError):
        return False
    return True


def consume(raw, item, *, protocol_version=4):
    """Keep independently valid source records; never launder a bad citation.

    The envelope and each retained record remain strict. Invalid records become
    explicit gaps and audit metadata, not facts. A final proof still needs a
    surviving independently sourced contract and execution observation.
    """
    from reviewforge.engine.session_tools import MAX_RESULT_BYTES

    if protocol_version == 9 and len(json.dumps(raw, ensure_ascii=False).encode()) > MAX_RESULT_BYTES:
        raise ValueError("source-audit-transport-record-too-large")
    if not isinstance(raw, dict) or set(raw) != {"hypothesis_id", "execution", "contracts", "gaps"}:
        raise ValueError("invalid-source-audit-shape")
    if raw["hypothesis_id"] != item.id or any(
        not isinstance(raw[name], list) or (protocol_version < 9 and len(raw[name]) > 8)
        for name in ("execution", "contracts", "gaps")
    ):
        raise ValueError("invalid-source-audit-envelope")
    kept = {"hypothesis_id": item.id, "execution": [], "contracts": [], "gaps": raw["gaps"][:]}
    rejected = []
    # Validate model gaps before adding program diagnostics.
    validate({**kept, "gaps": kept["gaps"] or ["Validation placeholder"]}, item, protocol_version=protocol_version)
    for name in ("execution", "contracts"):
        for index, row in enumerate(raw[name]):
            trial = {"hypothesis_id": item.id, "execution": [], "contracts": [], "gaps": [], name: [row]}
            try:
                valid = validate(trial, item, protocol_version=protocol_version)
            except (TypeError, ValueError) as exc:
                rejected.append({"array": name, "index": index, "reason": str(exc)})
            else:
                kept[name].extend(valid[name])
    if rejected:
        kept["gaps"] = (kept["gaps"] if protocol_version >= 9 else kept["gaps"][:7]) + [
            "Some source reconstruction records failed schema/reference validation; they supply no evidence."
        ]
    return validate(kept, item, protocol_version=protocol_version), rejected


def messages(batch, ledger):
    from reviewforge.engine.proof_gate import _packet

    # Deliberately enumerate fields; do not copy a packet and remove a few
    # known candidate fields. Future candidate additions stay private here.
    entries = []
    for item in batch:
        packet = _packet(item)
        entries.append(
            {
                "hypothesis_id": item.id,
                "observations": packet["observations"],
                "applicable_rules": packet["contract_constraints"],
            }
        )
    return [
        SystemMessage(
            content=load_prompt(
                "source_audit_v10"
                if ledger.protocol_version >= 10
                else "source_audit_v9"
                if ledger.protocol_version >= 9
                else "source_audit_v4"
            )
        ),
        HumanMessage(content=json.dumps({"head_sha": ledger.head_sha, "items": entries}, ensure_ascii=False)),
    ]


def input_fingerprint(item, ledger: HypothesisLedger) -> str:
    """Bind the blind request to exactly its sources/rules, not prior verdicts."""
    value = [{"role": message.type, "content": message.content} for message in messages([item], ledger)]
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


async def run_source_audit(llm, ledger, on_update=None):
    from reviewforge.engine.proof_gate import _over_budget, _source_problem, _unique_object, _usage, proof_fingerprint

    async def save():
        if on_update is not None:
            await on_update(ledger)

    unlimited = ledger.protocol_version >= 10
    pending = []
    for item in ledger.items.values():
        if item.status != HypothesisStatus.CONFIRMED or _source_problem(item, ledger.head_sha) is not None:
            continue
        if current(item, ledger):
            continue
        fingerprint = proof_fingerprint(item, ledger.head_sha, ledger.workspace_digest)
        old = ledger.source_audits.get(item.id, {})
        same_input = old.get("input_fingerprint") == input_fingerprint(item, ledger)
        if old.get("run_id") != ledger.run_id or not same_input and old.get("fingerprint") != fingerprint:
            old = {}
        if (
            (not unlimited and old.get("attempts", 0) >= 2)
            or old.get("outcome") == "budget-exhausted"
            or old.get("reason") == "no-progress-cycle"
        ):
            continue
        pending.append((item, old, fingerprint))
    spent = 0
    calls = ledger.source_audit_batches
    while pending:
        batch = []
        request = plan = None
        refusal_reason = "source-audit-packet-too-large"
        for item, _, _ in pending:
            trial = messages([*batch, item], ledger)
            if unlimited:
                feedback = [
                    {
                        "hypothesis_id": candidate.id,
                        "error": old.get("reason"),
                        "rejected_result_untrusted": old.get("rejected_result"),
                    }
                    for candidate, old, _ in pending[: len(batch) + 1]
                    if old.get("rejected_result") is not None
                ]
                if feedback:
                    trial.append(
                        HumanMessage(
                            content=json.dumps(
                                {
                                    "controller_receipt": feedback,
                                    "instruction": "Correct the reported errors using the same sources.",
                                },
                                ensure_ascii=False,
                            )
                        )
                    )
            if not fits_proof_input(trial, cases=len(batch) + 1, protocol_version=ledger.protocol_version):
                break
            budget = ledger.request_budget
            available = snapshot_available(
                budget, fallback=None if ledger.protocol_version >= 8 else max(0, 16_000 - spent)
            )
            # Leave useful room for comparing the claim to the reconstruction.
            trial_plan = plan_request(
                llm,
                trial,
                max_output_tokens=output_capacity(ledger.protocol_version, 4096),
                fallback_available=available,
            )
            if (
                trial_plan.available_tokens is not None
                and trial_plan.available_tokens - trial_plan.input_tokens < 2048 + 512 * (len(batch) + 1)
            ):
                refusal_reason = "source-audit-request-budget-insufficient"
                break
            batch.append(item)
            request, plan = trial, trial_plan
        selected = pending[: len(batch)] if batch and (unlimited or calls < 4) else pending[:1]
        del pending[: len(selected)]
        refused = not batch or (not unlimited and calls >= 4)
        if not unlimited and calls >= 4:
            refusal_reason = "source-audit-call-cap"
        if not refused:
            calls += 1
            # Checkpoint the run-wide allowance before dispatch. Feedback and
            # resumed execution share the original four-batch ceiling.
            ledger.source_audit_batches = calls
        for item, old, fingerprint in selected:
            ledger.source_audits[item.id] = {
                "version": 2 if ledger.protocol_version >= 9 else 1,
                "run_id": ledger.run_id,
                "fingerprint": fingerprint,
                "input_fingerprint": input_fingerprint(item, ledger),
                "attempts": old.get("attempts", 0) + 1,
                **({"transition_fingerprints": old.get("transition_fingerprints", [])} if unlimited else {}),
                "tokens": old.get("tokens", 0),
                "outcome": (
                    "output-error"
                    if refused and ledger.protocol_version >= 9 and refusal_reason == "source-audit-packet-too-large"
                    else "budget-exhausted"
                    if refused
                    else "provider-error"
                ),
                "reason": (
                    refusal_reason
                    if refused and ledger.protocol_version >= 9
                    else "source-audit-packet-or-budget-limit"
                    if refused
                    else "source-audit-interrupted"
                ),
            }
        await save()
        if refused:
            continue
        try:
            response = await llm.ainvoke(request, max_tokens=plan.max_output_tokens)
        except Exception as exc:
            spent += plan.input_tokens + plan.max_output_tokens
            for item in batch:
                ledger.source_audits[item.id].update(
                    outcome="budget-exhausted"
                    if getattr(exc, "outcome", "") == "budget-exhausted"
                    else "provider-error",
                    reason="source-audit-request-failed",
                )
            await save()
            continue
        tokens = _usage(response)
        spent += tokens if tokens is not None else plan.input_tokens + plan.max_output_tokens
        ledger.source_audits[batch[0].id]["tokens"] += tokens or 0
        try:
            if _over_budget(ledger) or (
                tokens is not None and plan.available_tokens is not None and tokens > plan.available_tokens
            ):
                raise OverflowError
            text = strip_reasoning_blocks(response.content)
            if text.startswith("```json\n") and text.endswith("\n```"):
                text = text[8:-4]
            raw = json.loads(text, object_pairs_hook=_unique_object)
            if not isinstance(raw, dict) or set(raw) != {"audits"} or not isinstance(raw["audits"], list):
                raise ValueError
            ids = [row.get("hypothesis_id") for row in raw["audits"] if isinstance(row, dict)]
            if len(ids) != len(raw["audits"]) or len(ids) != len(set(ids)) or set(ids) != {item.id for item in batch}:
                raise ValueError
            by_id = {row["hypothesis_id"]: row for row in raw["audits"]}
        except (TypeError, ValueError, OverflowError) as exc:
            for item in batch:
                ledger.source_audits[item.id].update(
                    outcome="budget-exhausted" if isinstance(exc, OverflowError) else "output-error",
                    reason="invalid-source-audit-envelope-or-budget",
                )
        else:
            for item in batch:
                try:
                    facts, rejected = consume(by_id[item.id], item, protocol_version=ledger.protocol_version)
                except (TypeError, ValueError) as exc:
                    ledger.source_audits[item.id].update(outcome="output-error", reason=str(exc))
                else:
                    ledger.source_audits[item.id].update(
                        outcome="resolved", reason="source-reconstructed", facts=facts, rejected_records=rejected
                    )
        if unlimited:
            for item in batch:
                record = ledger.source_audits[item.id]
                if record.get("outcome") == "output-error":
                    record["rejected_result"] = response.content
                    if repeated_state(
                        record,
                        state={"input": record["input_fingerprint"], "reason": record["reason"]},
                        receipt=response.content,
                    ):
                        record["reason"] = "no-progress-cycle"
                    else:
                        pending.append((item, record, record["fingerprint"]))
        await save()
