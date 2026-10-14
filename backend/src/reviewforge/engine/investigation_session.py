"""Case-bound read/decide loop; no forced empty proof fields or semantic rerolls."""

from __future__ import annotations

import json

from langchain_core.messages import HumanMessage, SystemMessage

from reviewforge.core.change_manifest import is_verified_rename
from reviewforge.engine.discovery_submission import bind_discovery_submission
from reviewforge.engine.prompts_v4 import load_prompt
from reviewforge.engine.request_budget import plan_request
from reviewforge.engine.review_session import ReviewSession, actions, controller_feedback, exception_origin
from reviewforge.engine.session_tools import case_identity, decision_payload, decision_tools


def request(worker, hypothesis, pack, *, final=False, turn=1, feedback=None, status="pending"):
    """Use the actual same tools/messages for allocation forecasts and dispatch."""
    state = worker._state
    tools = [*decision_tools(hypothesis, state.head_sha), *([] if final else worker._build_tools())]
    model = worker._closing_llm if final and worker._closing_llm is not None else worker._llm
    bound = bind_discovery_submission(model, tools)
    packet = json.loads(worker._exploration_chat(hypothesis, pack)[-1].content)
    packet["case_id"] = case_identity(hypothesis, state.head_sha)
    packet["session"] = {"state": status, "turn": turn, "last_turn": final}
    prompt = load_prompt("session_investigation_v5", output_language=worker._output_language)
    if worker._closure_constraints:
        prompt += "\nApplicable contract constraints:\n" + "\n".join(worker._closure_constraints)
    chat = [SystemMessage(content=prompt), HumanMessage(content=json.dumps(packet, ensure_ascii=False))]
    if feedback:
        chat.append(controller_feedback(feedback, tools))
    return bound, chat, tools


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
            steps=0,
            outcome="tool-error",
        )
    changed = changed_paths if changed_paths is not None else _changed_paths(state)
    steps = worker._max_steps if worker._max_steps is not None else budget_steps(hypothesis)
    token_limit = worker._token_limit if worker._token_limit is not None else steps * 4000
    bootstrap = int(await worker._bootstrap(hypothesis, pack))
    terminal = decision_tools(hypothesis, state.head_sha)
    schemas = {tool.name: tool.args_schema for tool in terminal}
    record = {}
    session = ReviewSession(
        record,
        task_id=case_identity(hypothesis, state.head_sha),
        phase="investigation",
        max_turns=max(0, steps - bootstrap),
    )
    feedback = []
    stagnant = 0
    while session.dispatch():
        turn = bootstrap + session.record["turns"]
        final = turn >= steps
        if worker._task_request_budget is not None:
            worker._task_request_budget.progress_steps = worker._starting_steps + turn
        worker._refresh_allocation(hypothesis)
        bound, chat, tools = request(
            worker, hypothesis, pack, final=final, turn=turn, feedback=feedback, status=session.record["status"]
        )
        plan = plan_request(
            bound,
            chat,
            max_output_tokens=worker._closing_output(hypothesis) if final else 2048,
            fallback_available=token_limit - worker._accounted_tokens(),
        )
        worker._progress.append(
            {
                "phase": "session",
                "event": "request-plan",
                "turn": turn,
                "last_turn": final,
                "input_tokens": plan.input_tokens,
                "max_output_tokens": plan.max_output_tokens,
                "case_id": case_identity(hypothesis, state.head_sha),
            }
        )
        if worker._on_progress is not None:
            await worker._on_progress(worker._observations, worker._progress)
        if plan.max_output_tokens < 512:
            return worker._result(
                verdict="unknown",
                reason="insufficient-request-budget",
                severity=hypothesis.severity,
                steps=turn - 1,
                outcome="budget-exhausted",
            )
        try:
            response = await bound.ainvoke(chat, max_tokens=plan.max_output_tokens)
            worker._record_tokens(response, chat)
        except Exception as exc:
            worker._progress.append({"phase": "session-dispatch-error", **exception_origin(exc)})
            exhausted = getattr(exc, "outcome", "") == "budget-exhausted"
            return worker._result(
                verdict="unknown",
                reason="run-budget-exhausted" if exhausted else type(exc).__name__,
                severity=hypothesis.severity,
                steps=turn,
                outcome="budget-exhausted" if exhausted else "provider-error",
                retryable=not exhausted,
            )
        if worker._accounted_tokens() > token_limit:
            return worker._result(
                verdict="unknown",
                reason="token-exhausted",
                severity=hypothesis.severity,
                steps=turn,
                outcome="budget-exhausted",
            )
        calls, errors = actions(response, tools)
        feedback = [{"error": error["reason"]} for error in errors]
        results = [call for call in calls if call["name"] in schemas]
        if results:
            if len(results) != 1 or len(calls) != 1 or errors:
                feedback.append({"error": "one-exclusive-case-result-required"})
            else:
                call = results[0]
                try:
                    args = schemas[call["name"]].model_validate(call["args"]).model_dump()
                    payload = decision_payload(call["name"], args, hypothesis)
                except (ValueError, TypeError):
                    feedback.append({"error": "invalid-case-result-schema-or-identity"})
                else:
                    result = worker._finalize(payload, hypothesis, changed, steps=turn)
                    worker._progress.append(
                        {
                            "phase": "session-case-binding",
                            "case_id": args["case_id"],
                            "decision": call["name"],
                            "controller_bound": True,
                        }
                    )
                    if result.outcome != "output-error":
                        result.progress = list(worker._progress)
                        return result
                    feedback.append({"error": result.reason})
        before = len(worker._observations)
        for call in calls:
            if call["name"] in schemas:
                continue
            session.record["status"] = "needs-evidence"
            reply = await worker._run_tool(call["name"], call["args"])
            feedback.append({"action": call["name"], "result": reply})
        added = worker._observations[before:]
        if worker._evidence_store is not None:
            for observation in worker._observations:
                worker._evidence_store.observation(observation)
        worker._progress.append(
            {
                "phase": "session",
                "event": "needs-evidence" if added else "protocol-feedback",
                "errors": errors,
                "new_observations": len(added),
            }
        )
        stagnant = 0 if added else stagnant + 1
        if stagnant >= 2:
            return worker._invalid_output("session-no-progress", hypothesis, turn)
    return worker._result(
        verdict="unknown",
        reason="session-step-limit",
        severity=hypothesis.severity,
        steps=steps,
        outcome="output-error" if feedback else "insufficient-evidence",
    )
