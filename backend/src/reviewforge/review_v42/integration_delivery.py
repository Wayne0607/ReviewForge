"""Text submission transport for the v4.2 integration role."""

from __future__ import annotations

import json
from typing import Any

from reviewforge.engine.native_submission import _invalid_constant, _unique_object, inspect_tool_calls

from .response_protocol import JSON_CONSTRAINT
from .response_protocol import JSON_REQUEST_OPTIONS as JSON_REQUEST_OPTIONS

INTEGRATION_DELIVERY_POLICY = "integration-delivery-1"
INTEGRATION_JSON_CONSTRAINT = JSON_CONSTRAINT

PROMPT = """
You are the independent cross-unit integration reviewer. Return exactly one JSON object in the
message body. Do not emit a tool call. Start with the board's current_integration_obligation.
Read the fixed originals for every listed unit and include any origin_evidence_refs in the receipt.
When the originals support a decision, submit it promptly. Do not repeat settled obligations.

For a receipt return {"action":"submit_receipt","scope_id":"...","expected_scope_revision":0,
"receipt":{"obligation_id":"...","disposition":"checked","evidence_refs":["..."],"reason":"..."}}.
For a new cross-unit relationship return {"action":"register_relation","scope_id":"...",
"expected_scope_revision":0,"relation":{"unit_ids":["...","..."],"description":"...","evidence_refs":["..."]}}.
After checking the obligations and looking for relevant cross-unit interactions, return
{"action":"finish_integration","scope_id":"...","expected_scope_revision":0}.
Use the current scope.id and scope.revision from the board for these three actions.

For investigation use {"action":"request_evidence","work_item_id":"","expected_revision":0,
"question":"...","judgment":"...","evidence_refs":[],
"payload":{"gap":"...","relevance":"...","alternatives":"...","operations":[{"name":"read_source",
"args":{"side":"head","path":"...","start_line":1,"end_line":40}}],"navigation_refs":[]}}.
The controller performs source operations from source_operation_schemas and returns their results.
Explain exactly what is missing. Review the returned sources and return to the same obligation.
Navigation IDs may help locate originals. Cite only original evidence IDs in conclusions.
Use the returned work_item_id and work_item_revision for a continuing question.
With the same outer work-item fields you can use action "offer_candidate".
After resolving a source question, close it with this exact flat object:
{"action":"close_question","work_item_id":"...","expected_revision":0,
"answer":"...","evidence_refs":["..."]}.
The controller finds the existing question from that work item. Then submit the receipt for the
current obligation. A checked receipt does not close a question.
If an authoritative rule or environment is absent, use action "unresolved" with payload
{"missing_fact":"...","attempted":"...","recovery_condition":"..."}.
If part of an answer is supported, use close_question with the same gap in top-level evidence_gap.
Use the current obligation in the question and describe the specific fact that prevents settlement.
Pending candidates and open questions must be handled before finish_integration.
"""


def integration_action(response: Any, turn_id: str, board: dict[str, Any] | None = None) -> dict[str, Any]:
    """Derive a controller action from the actual text, without constructing a model tool call."""
    calls, errors = inspect_tool_calls(response)
    metadata = getattr(response, "response_metadata", None) or {}
    if calls or errors or metadata.get("integration_native_errors"):
        raise ValueError("Return one JSON object in the message body without tool calls.")
    if metadata.get("finish_reason") == "length":
        raise ValueError("The JSON response was truncated.")
    content = response.content
    if not isinstance(content, str):
        raise ValueError("Return one JSON object in the message body.")
    content = content.strip()
    if content.startswith("```json\n") and content.endswith("\n```"):
        content = content[8:-4]
    try:
        args = json.loads(content, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
    except ValueError as exc:
        raise ValueError("Return one complete JSON object with unique keys.") from exc
    if not isinstance(args, dict):
        raise ValueError("Return one JSON object.")
    action = args.get("action")
    if action == "close_question":
        required = {"action", "work_item_id", "expected_revision", "answer", "evidence_refs"}
        if not required <= args.keys() <= required | {"evidence_gap"}:
            raise ValueError(
                "Use the flat close_question object with work_item_id, expected_revision, answer and evidence_refs."
            )
        work_item = next(
            (item for item in (board or {}).get("decision_items", []) if item["id"] == args["work_item_id"]),
            None,
        )
        if work_item is None:
            raise ValueError("Use a work_item_id from the current board's decision_items.")
        payload = {
            "question_id": work_item["question_id"],
            "answer": args["answer"],
            "evidence_refs": args["evidence_refs"],
        }
        if "evidence_gap" in args:
            payload["evidence_gap"] = args["evidence_gap"]
        args = {
            "action": action,
            "work_item_id": args["work_item_id"],
            "expected_revision": args["expected_revision"],
            "question": work_item["question"],
            "judgment": args["answer"],
            "evidence_refs": args["evidence_refs"],
            "payload": payload,
        }
    if action in ("request_evidence", "offer_candidate", "close_question", "unresolved"):
        return {"id": f"json-{turn_id}", "name": "review_step", "args": args}

    field = {"submit_receipt": "receipt", "register_relation": "relation", "finish_integration": None}
    if not isinstance(action, str) or action not in field:
        raise ValueError("Unknown integration action. Submit a receipt, request evidence, or report a concrete gap.")
    item = field[action]
    expected = {"action", "scope_id", "expected_scope_revision"}
    if item:
        expected.add(item)
    if set(args) != expected:
        raise ValueError("Return only the fields specified for this integration action.")
    payload: dict[str, Any] = {"finish": item is None}
    if item:
        payload["integration_receipts" if item == "receipt" else "integration_relations"] = [args[item]]
    return {
        "id": f"json-{turn_id}",
        "name": "complete_scope",
        "args": {
            "scope_id": args["scope_id"],
            "expected_scope_revision": args["expected_scope_revision"],
            "payload": payload,
        },
    }
