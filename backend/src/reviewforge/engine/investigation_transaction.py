"""Protocol 9: evidence acquisition and durable result delivery are separate.

Evidence steps bound exploration, not the opportunity to deliver its result.
The final evidence response may still read source. Delivery then has its own
two persisted dispatches, with the actual rejected receipt as feedback. A valid
native decision is immutable and revalidated on replay, never re-asked.
"""

from __future__ import annotations

import copy
import json
from dataclasses import asdict

from pydantic import ValidationError

from reviewforge.core.change_manifest import is_verified_rename
from reviewforge.engine.investigation_session import request
from reviewforge.engine.native_submission import inspect_tool_calls
from reviewforge.engine.request_budget import plan_request
from reviewforge.engine.review_session import actions, exception_origin, fingerprint
from reviewforge.engine.session_tools import MAX_RESULT_BYTES, case_identity, decision_payload, decision_tools

PHASE = "case-transaction-v9"
MAX_DELIVERY_REQUESTS = 2


def _latest(progress):
    return next((item for item in reversed(progress) if item.get("phase") == PHASE), None)


def _record(worker, hypothesis, pack, limit):
    record = _latest(worker._progress)
    binding = {
        "policy": PHASE,
        "case_id": case_identity(hypothesis, worker._state.head_sha),
        "head_sha": worker._state.head_sha,
        "workspace_digest": pack.workspace_digest,
    }
    if record is None:
        binding["evidence_limit"] = limit
        record = {
            "phase": PHASE,
            "binding": binding,
            "binding_id": fingerprint(binding),
            "evidence_steps": worker._starting_steps,
            "delivery_attempts": 0,
            "state": "collecting" if limit > worker._starting_steps else "finalizing",
            "feedback": [],
        }
        worker._progress.append(record)
    saved = record.get("binding", {})
    if (
        record.get("binding_id") != fingerprint(saved)
        or any(saved.get(key) != value for key, value in binding.items() if key != "evidence_limit")
        or type(saved.get("evidence_limit")) is not int
        or saved["evidence_limit"] < 0
        or type(record.get("evidence_steps")) is not int
        or not 0 <= record["evidence_steps"] <= saved["evidence_limit"]
        or type(record.get("delivery_attempts")) is not int
        or not 0 <= record["delivery_attempts"] <= MAX_DELIVERY_REQUESTS
        or record.get("state") not in {"collecting", "finalizing", "delivering", "complete", "rejected"}
    ):
        raise ValueError("case-transaction-binding-mismatch")
    record["evidence_steps"] = min(saved["evidence_limit"], max(record["evidence_steps"], worker._starting_steps))
    if "decision" in record and record.get("decision_id") != fingerprint(record["decision"]):
        raise ValueError("case-transaction-decision-mismatch")
    return record


async def investigate(worker, hypothesis, state, pack, *, changed_paths=None):
    from reviewforge.engine.investigator import _changed_paths, budget_steps

    worker._prepare_investigation(hypothesis, state, pack)
    if hypothesis.change_anchor is not None and (
        state.file_changes.get(hypothesis.change_anchor.path) != hypothesis.change_anchor
        or not is_verified_rename(hypothesis.change_anchor, state.head_sha)
    ):
        return worker._result(
            verdict="unknown",
            reason="structural change provenance mismatch",
            severity=hypothesis.severity,
            outcome="tool-error",
        )
    changed = changed_paths if changed_paths is not None else _changed_paths(state)
    steps = worker._max_steps if worker._max_steps is not None else budget_steps(hypothesis)
    record = _record(worker, hypothesis, pack, worker._starting_steps + steps)
    baseline = record["evidence_steps"]
    limit = record["binding"]["evidence_limit"]
    schemas = {tool.name: tool.args_schema for tool in decision_tools(hypothesis, state.head_sha, protocol_version=9)}

    def used():
        return record["evidence_steps"] - baseline

    async def save():
        if worker._on_progress is not None:
            await worker._on_progress(worker._observations, worker._progress)

    def result_error(reason):
        return worker._invalid_output(reason, hypothesis, used())

    async def deliver():
        call = record["decision"]
        count = record.get("evidence_count")
        if (
            type(count) is not int
            or count < 0
            or count > len(worker._observations)
            or record.get("evidence_id") != fingerprint([asdict(item) for item in worker._observations[:count]])
            or len({item.id for item in worker._observations}) != len(worker._observations)
        ):
            raise ValueError("case-transaction-evidence-mismatch")
        # Every replay checks identity/schema and the original evidence gates.
        args = schemas[call["name"]].model_validate(call["args"]).model_dump()
        result = worker._finalize(decision_payload(call["name"], args, hypothesis), hypothesis, changed, steps=used())
        record["state"] = "rejected" if result.outcome == "output-error" else "complete"
        record["outcome"] = result.outcome
        await save()
        result.progress = list(worker._progress)
        return result

    if "decision" in record:
        return await deliver()
    if record["state"] in {"complete", "rejected", "delivering"}:
        raise ValueError("case-transaction-missing-decision")
    await save()
    if record["state"] == "collecting" and record["evidence_steps"] == 0 and limit:
        record["evidence_steps"] += 1
        await save()  # An interrupted bootstrap never gains another evidence step.
        await worker._bootstrap(hypothesis, pack)
        await save()
    stagnant = 0
    while True:
        if record["evidence_steps"] >= limit:
            record["state"] = "finalizing"
        final = record["state"] == "finalizing"
        if final:
            if record["delivery_attempts"] >= MAX_DELIVERY_REQUESTS:
                await save()
                return result_error("case-delivery-protocol-limit")
            record["delivery_attempts"] += 1
        else:
            record["evidence_steps"] += 1
        if worker._task_request_budget is not None:
            worker._task_request_budget.progress_steps = record["evidence_steps"]
        worker._refresh_allocation(hypothesis)
        bound, chat, tools = request(
            worker,
            hypothesis,
            pack,
            final=final,
            turn=record["evidence_steps"],
            feedback=record["feedback"],
            status=record["state"],
        )
        plan = plan_request(
            bound,
            chat,
            max_output_tokens=worker._closing_output(hypothesis) if final else 2048,
            fallback_available=None,
        )
        worker._progress.append(
            {
                "phase": PHASE + "-dispatch",
                "evidence_step": record["evidence_steps"],
                "delivery_attempt": record["delivery_attempts"],
                "final": final,
                "input_tokens": plan.input_tokens,
                "max_output_tokens": plan.max_output_tokens,
            }
        )
        await save()  # Persist both counters before any physical request/retry.
        if plan.max_output_tokens < 512:
            return worker._result(
                verdict="unknown",
                reason="insufficient-request-budget",
                severity=hypothesis.severity,
                steps=used(),
                outcome="budget-exhausted",
            )
        try:
            response = await bound.ainvoke(chat, max_tokens=plan.max_output_tokens)
            worker._record_tokens(response, chat)
        except Exception as exc:
            record["feedback"] = [{"dispatch_error": exception_origin(exc)}]
            await save()
            exhausted = getattr(exc, "outcome", "") == "budget-exhausted"
            return worker._result(
                verdict="unknown",
                reason="case-session-dispatch-failed",
                severity=hypothesis.severity,
                steps=used(),
                outcome="budget-exhausted" if exhausted else "provider-error",
                retryable=not exhausted,
            )
        calls, errors = actions(response, tools)
        # Keep the actual failed attempt, not just a repeated original draft.
        raw_calls, malformed = inspect_tool_calls(response)
        raw = {"calls": raw_calls, "errors": errors, "malformed": malformed}
        record["last_receipt"] = (
            copy.deepcopy(raw)
            if len(json.dumps(raw, ensure_ascii=False).encode()) <= MAX_RESULT_BYTES
            else {"errors": [{"reason": "result-transport-record-too-large"}]}
        )
        feedback = [{"error": error["reason"]} for error in errors]
        results = [call for call in calls if call["name"] in schemas]
        if results:
            if len(results) != 1 or len(calls) != 1 or errors:
                feedback.append({"error": "one-exclusive-case-result-required"})
            else:
                call = results[0]
                try:
                    args = schemas[call["name"]].model_validate(call["args"]).model_dump()
                except ValidationError as exc:
                    feedback.append(
                        {
                            "error": "invalid-case-result",
                            "details": exc.errors(include_url=False, include_input=False, include_context=False),
                        }
                    )
                else:
                    record["decision"] = {"name": call["name"], "args": args}
                    record["decision_id"] = fingerprint(record["decision"])
                    record["evidence_count"] = len(worker._observations)
                    record["evidence_id"] = fingerprint([asdict(item) for item in worker._observations])
                    record["state"] = "delivering"
                    await save()
                    return await deliver()
            # A declared result is a delivery attempt; do not restart evidence
            # collection or re-ask a successfully delivered semantic decision.
            record["state"] = "finalizing"
        elif not final and not errors:
            before = len(worker._observations)
            for call in calls[:8]:
                reply = await worker._run_tool(call["name"], call["args"])
                added = worker._observations[before:]
                feedback.append(
                    {
                        "action": call["name"],
                        "observations": [item.id for item in added],
                        **({"error": reply} if not added else {}),
                    }
                )
                before = len(worker._observations)
            stagnant = 0 if any(item.get("observations") for item in feedback) else stagnant + 1
            if len(calls) > 8:
                feedback.append({"error": "read-action-batch-limit", "max_actions": 8})
            if stagnant >= 2:
                record["state"] = "finalizing"
        # In finalization only terminal tools were registered. Unregistered
        # source reads are feedback, never executed and never silently dropped.
        if final or results or errors:
            feedback.append({"previous_receipt": record["last_receipt"]})
        record["feedback"] = feedback
        await save()
