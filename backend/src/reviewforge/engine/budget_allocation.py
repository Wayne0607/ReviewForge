"""Forecast downstream work without spending tokens or inventing evidence.

The allocator protects useful request capacity, not successful model reasoning.
Unknown reconstruction output is sized explicitly; it is never sent to a model.
Every physical request still intersects the PR, task and external hard budgets.
"""

from __future__ import annotations

from typing import Any

from reviewforge.engine.hypothesis import Hypothesis, HypothesisLedger
from reviewforge.engine.proof_gate import ProofGate
from reviewforge.engine.request_budget import plan_request
from reviewforge.engine.source_audit import current, messages


def verification_forecast(
    llm: Any, ledger: HypothesisLedger, items: list[Hypothesis], *, language: str, future_candidate: bool = False
) -> dict[str, int]:
    """Use the real prompts, packets, options and batching limits for sizing.

    For an unfinished blind audit, reserve its useful output and its possible
    replay in both the audit and contract catalog. This is a forecast, not a
    guarantee against arbitrary future source growth or provider overrun.
    """
    gate = ProofGate(llm, output_language=language)
    batches: list[list[Hypothesis]] = []
    batch: list[Hypothesis] = []
    unfit = 0
    for item in items:
        trial = [*batch, item]
        fits = (
            len(trial) <= 9
            and sum(len(message.content) for message in messages(trial, ledger)) <= 32_000
            and sum(len(message.content) for message in gate._messages(ledger, trial, forecast=True)) <= 32_000
        )
        if not fits and batch:
            batches.append(batch)
            batch = []
        if not batch and (
            sum(len(message.content) for message in messages([item], ledger)) > 32_000
            or sum(len(message.content) for message in gate._messages(ledger, [item], forecast=True)) > 32_000
        ):
            unfit += 1
        else:
            batch.append(item)
    if batch:
        batches.append(batch)
    if not batches and future_candidate:
        batches = [[]]  # Prompt overhead plus one potential future result.
    reserve = 0
    for batch in batches[:4]:
        needs_audit = [item for item in batch if not current(item, ledger)]
        unknown = bool(needs_audit) or not batch
        count = max(1, len(batch))
        audit_output = min(4096, 2048 + 512 * max(1, len(needs_audit))) if unknown else 0
        if unknown:
            audit_input = plan_request(
                llm,
                messages(needs_audit, ledger),
                max_output_tokens=4096,
                fallback_available=ledger.request_budget.get("limit", 250_000),
            ).input_tokens
            reserve += audit_input + audit_output
        final_input = plan_request(
            llm,
            gate._messages(ledger, batch, forecast=True),
            max_output_tokens=4096,
            fallback_available=ledger.request_budget.get("limit", 250_000),
        ).input_tokens
        reserve += final_input + 2 * audit_output + min(4096, 512 + 384 * count)
    return {
        "verification_tokens": reserve,
        "verification_candidates": len(items),
        "verification_batches": min(4, len(batches)),
        "verification_unfit": unfit + sum(len(batch) for batch in batches[4:]),
        "future_candidate": int(future_candidate),
    }
