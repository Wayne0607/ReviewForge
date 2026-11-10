"""Correctable native input errors identify the specific envelope or payload field."""

import json

import pytest
from langchain_core.messages import AIMessage
from pydantic import ValidationError
from test_v42_decision_failure_batch import make_reviewer, read
from test_v42_decision_loop import decision_store, from_board, last_ref, step
from test_v42_stateful_reviewer import call

from reviewforge.review_v42.contracts import Question, ReviewScope
from reviewforge.review_v42.decision_loop import ReviewStep, _validation_issues


@pytest.mark.parametrize(
    ("action", "payload", "field", "reason"),
    [
        (
            "request_evidence",
            {"gap": "g", "relevance": "r", "alternatives": "a", "operations": [{"name": 1, "args": {}}]},
            "payload.operations.0.name",
            "string_type",
        ),
        (
            "offer_candidate",
            {"candidate": {"behavior": "b", "impact": "i", "evidence_refs": ["ref"]}},
            "payload.candidate.trigger",
            "missing",
        ),
        ("offer_candidate", {}, "payload", "value_error"),
        ("close_question", {"question_id": "q", "answer": "a"}, "payload.evidence_refs", "missing"),
        (
            "complete_review",
            {"observations": [{"statement": "s", "evidence_refs": ["ref"]}]},
            "payload.observations.0.region_ids",
            "missing",
        ),
        ("unresolved", {"missing_fact": "m", "attempted": "a"}, "payload.recovery_condition", "missing"),
    ],
)
def test_payload_schema_errors_include_full_native_path(action, payload, field, reason):
    with pytest.raises(ValidationError) as exc:
        ReviewStep.model_validate(step(action, payload, refs=["ref"]))
    assert [(issue["field"], issue["reason"]) for issue in _validation_issues(exc.value)] == [(field, reason)]


def test_outer_schema_errors_keep_their_own_path():
    args = step("close_question", {"question_id": "q", "answer": "a"})
    del args["evidence_refs"]
    with pytest.raises(ValidationError) as exc:
        ReviewStep.model_validate(args)
    assert [(issue["field"], issue["reason"]) for issue in _validation_issues(exc.value)] == [
        ("evidence_refs", "missing"),
        ("payload.evidence_refs", "missing"),
    ]


@pytest.mark.parametrize("missing_finish_item", [False, True])
async def test_model_uses_nested_field_receipt_to_close_question(tmp_path, missing_finish_item):
    store, task = decision_store(tmp_path)
    first = AIMessage(content="", tool_calls=[call("review_step", read(1), "read")])

    def missing_inner_refs(messages):
        item, ref = from_board(messages), last_ref(messages)
        payload = {"question_id": item["question_id"], "answer": "Checked the fixed source"}
        return AIMessage(
            content="", tool_calls=[call("review_step", step("close_question", payload, item, [ref]), "close-bad")]
        )

    def correct_from_receipt(messages):
        receipt = json.loads(messages[-1].content)
        assert receipt["errors"] == [
            {"field": "payload.evidence_refs", "reason": "missing", "message": "Field required"}
        ]
        submitted = messages[-2].tool_calls[0]["args"]
        assert store.get(Question, submitted["payload"]["question_id"]).status == "open"
        parent, field = receipt["errors"][0]["field"].split(".")
        corrected = {**submitted, parent: {**submitted[parent], field: submitted["evidence_refs"]}}
        return AIMessage(content="", tool_calls=[call("review_step", corrected, "close-corrected")])

    def finish(messages):
        assert json.loads(messages[-1].content)["status"] == "answered"
        item = from_board(messages)
        question = store.get(Question, item["question_id"])
        assert question.status == "answered" and question.answer == "Checked the fixed source"
        payload = {
            "checked_region_ids": ["a"],
            "observations": [{"statement": "Checked", "evidence_refs": question.evidence_refs, "region_ids": ["a"]}],
        }
        args = step("complete_review", payload, item, question.evidence_refs)
        if missing_finish_item:
            args.update(work_item_id="", expected_revision=0, question="A new phrasing of the answered question")
            return AIMessage(content="", tool_calls=[call("review_step", args, "finish-bad")])
        return AIMessage(content="", tool_calls=[call("review_step", args, "finish")])

    def finish_existing(messages):
        receipt = json.loads(messages[-1].content)
        assert receipt["errors"][0]["field"] == "work_item_id"
        assert receipt["errors"][0]["reason"] == "existing_work_item_required"
        assert len(store.list(Question)) == 1
        submitted = messages[-2].tool_calls[0]["args"]
        corrected = step("complete_review", submitted["payload"], from_board(messages), submitted["evidence_refs"])
        return AIMessage(content="", tool_calls=[call("review_step", corrected, "finish")])

    responses = [first, missing_inner_refs, correct_from_receipt, finish]
    if missing_finish_item:
        responses.append(finish_existing)
    reviewer = make_reviewer(store, task, responses)
    assert await reviewer.run("a") == "reviewed", reviewer.error
    assert store.get(ReviewScope, "a").status == "reviewed"
    attempts = {event["payload"]["call_id"]: event["payload"]["args"] for event in store.history("DecisionAttempt")}
    original, corrected = attempts["close-bad"], attempts["close-corrected"]
    assert original["evidence_refs"] and "evidence_refs" not in original["payload"]
    assert corrected == {**original, "payload": {**original["payload"], "evidence_refs": original["evidence_refs"]}}
