"""Models can copy each work-item and draft version from native receipts."""

import json

from langchain_core.messages import AIMessage, ToolMessage
from test_v42_candidate_draft_reviewer import Model
from test_v42_decision_loop import decision_store, from_board, step
from test_v42_fair_runtime import PinnedGateway
from test_v42_stateful_reviewer import call

from reviewforge.review_v42.contracts import Case
from reviewforge.review_v42.decision_loop import DecisionReviewer
from reviewforge.review_v42.decision_state import DecisionState
from reviewforge.review_v42.tools import EvidenceGateway


def receipt(messages):
    return json.loads(next(m.content for m in reversed(messages) if isinstance(m, ToolMessage)))


def item_from_receipt(messages, store, task):
    value = receipt(messages)
    item = DecisionState(store).current(task.id)
    assert value["work_item_id"] == item["id"]
    assert value["work_item_revision"] == item["revision"]
    assert "revision" not in value
    return {"id": value["work_item_id"], "revision": value["work_item_revision"], "question": item["question"]}


async def test_copy_receipts_through_candidate_correction_and_completion(tmp_path):
    store, task = decision_store(tmp_path)
    accepted = []
    saved = {}
    first = AIMessage(
        content="",
        tool_calls=[
            call(
                "review_step",
                step(
                    "request_evidence",
                    {
                        "gap": "source",
                        "relevance": "behavior",
                        "alternatives": "confirm or refute",
                        "operations": [{"name": "read_source", "args": {"path": "a.py"}}],
                    },
                ),
                "read",
            )
        ],
    )

    def reread(messages):
        item = item_from_receipt(messages, store, task)
        payload = {
            "gap": "comparison",
            "relevance": "behavior",
            "alternatives": "confirm or refute",
            "operations": [{"name": "read_source", "args": {"path": "a.py"}}],
        }
        return AIMessage(
            content="", tool_calls=[call("review_step", step("request_evidence", payload, item), "reread")]
        )

    def invalid_candidate(messages):
        item = item_from_receipt(messages, store, task)
        saved["ref"] = receipt(messages)["results"][0]["result"]["evidence_ref"]
        payload = {
            "candidate": {
                "trigger": "trigger",
                "behavior": "mechanism",
                "impact": "impact",
                "evidence_refs": ["missing"],
            }
        }
        return AIMessage(
            content="", tool_calls=[call("review_step", step("offer_candidate", payload, item, [saved["ref"]]), "bad")]
        )

    def corrected_candidate(messages):
        item = item_from_receipt(messages, store, task)
        value = receipt(messages)
        assert value["status"] == "repair_needed"
        assert value["draft_revision"] != value["work_item_revision"]
        payload = {
            "candidate": {
                "trigger": "trigger",
                "behavior": "mechanism",
                "impact": "impact",
                "evidence_refs": [saved["ref"]],
                "draft_id": value["draft_id"],
                "expected_revision": value["draft_revision"],
            }
        }
        return AIMessage(
            content="",
            tool_calls=[call("review_step", step("offer_candidate", payload, item, [saved["ref"]]), "correct")],
        )

    def close(messages):
        item = item_from_receipt(messages, store, task)
        value = receipt(messages)
        assert value["status"] == "promoted" and value["case_id"]
        assert len(accepted) == len(store.list(Case)) == 1
        qid = from_board(messages)["question_id"]
        payload = {"question_id": qid, "answer": "Checked source", "evidence_refs": [saved["ref"]]}
        return AIMessage(
            content="", tool_calls=[call("review_step", step("close_question", payload, item, [saved["ref"]]), "close")]
        )

    def complete(messages):
        item = item_from_receipt(messages, store, task)
        assert receipt(messages)["status"] == "answered"
        payload = {
            "checked_region_ids": ["a"],
            "observations": [{"statement": "Checked source", "evidence_refs": [saved["ref"]], "region_ids": ["a"]}],
            "unresolved_refs": [],
            "finish": True,
        }
        return AIMessage(
            content="",
            tool_calls=[call("review_step", step("complete_review", payload, item, [saved["ref"]]), "complete")],
        )

    reviewer = DecisionReviewer(
        store,
        Model([first, reread, invalid_candidate, corrected_candidate, close, complete]),
        EvidenceGateway(store, PinnedGateway()),
        on_case=accepted.append,
        task_id=task.id,
        lease_token=task.lease_token,
    )
    assert await reviewer.run("a") == "reviewed", reviewer.error
    exchange = [row for row in store.read_turn_audit(task.id) if row["phase"].startswith("tool:")][-1]
    final_receipt = json.loads(exchange["messages"][0]["data"]["content"])
    assert final_receipt["work_item_revision"] == DecisionState(store).current(task.id)["revision"]
    assert final_receipt["status"] == "reviewed"


async def test_retraction_receipt_keeps_distinct_versions(tmp_path):
    store, task = decision_store(tmp_path)
    candidate = {
        "candidate": {"trigger": "trigger", "behavior": "behavior", "impact": "impact", "evidence_refs": ["missing"]}
    }
    payload = {
        "gap": "source",
        "relevance": "behavior",
        "alternatives": "confirm or refute",
        "operations": [{"name": "read_source", "args": {"path": "a.py"}}],
    }
    first = AIMessage(content="", tool_calls=[call("review_step", step("request_evidence", payload), "read")])

    def invalid_candidate(messages):
        item = item_from_receipt(messages, store, task)
        return AIMessage(content="", tool_calls=[call("review_step", step("offer_candidate", candidate, item), "bad")])

    def retract(messages):
        item, value = item_from_receipt(messages, store, task), receipt(messages)
        payload = {
            "retract_draft": {
                "draft_id": value["draft_id"],
                "expected_revision": value["draft_revision"],
                "reason": "mistaken candidate",
            }
        }
        return AIMessage(
            content="", tool_calls=[call("review_step", step("offer_candidate", payload, item), "retract")]
        )

    def stop(messages):
        item = item_from_receipt(messages, store, task)
        assert receipt(messages)["status"] == "retracted"
        payload = {"missing_fact": "contract", "attempted": "candidate", "recovery_condition": "contract available"}
        return AIMessage(content="", tool_calls=[call("review_step", step("unresolved", payload, item), "stop")])

    reviewer = DecisionReviewer(
        store,
        Model([first, invalid_candidate, retract, stop]),
        EvidenceGateway(store, PinnedGateway()),
        task_id=task.id,
        lease_token=task.lease_token,
    )
    assert await reviewer.run("a") == "partial", reviewer.error
    assert not store.pending_candidate_drafts("a")
