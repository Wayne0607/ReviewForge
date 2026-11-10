"""Question disposition and native work-item operations have separate lifecycles."""

import json

import pytest
from langchain_core.messages import AIMessage
from test_v42_decision_failure_batch import make_reviewer, read
from test_v42_decision_loop import decision_store, from_board, last_ref, step
from test_v42_decision_state import audit, initial
from test_v42_stateful_reviewer import call

from reviewforge.review_v42.contracts import Case, Question, ReviewScope
from reviewforge.review_v42.decision_state import DecisionState


def native(name, args):
    return AIMessage(content="", tool_calls=[call("review_step", args, name)])


def close(messages):
    item, ref = from_board(messages), last_ref(messages)
    payload = {"question_id": item["question_id"], "answer": "Checked the fixed source", "evidence_refs": [ref]}
    return native("close", step("close_question", payload, item, [ref]))


def finish(item, refs):
    payload = {
        "checked_region_ids": ["a"],
        "observations": [{"statement": "Checked the fixed source", "evidence_refs": refs, "region_ids": ["a"]}],
        "finish": True,
    }
    return step("complete_review", payload, item, refs)


@pytest.mark.parametrize("repair_attempt", [False, True])
async def test_answered_question_survives_native_candidate_and_completion(tmp_path, repair_attempt):
    store, task = decision_store(tmp_path)
    accepted = []
    state = DecisionState(store)

    def submit_offer(messages, call_id, **fields):
        item = from_board(messages)
        question = store.get(Question, item["question_id"])
        assert question.status == item["status"] == "answered"
        payload = {
            "candidate": {
                "trigger": "Read the changed value",
                "behavior": "The new value changes the result",
                "impact": "A caller gets a different result",
                "evidence_refs": question.evidence_refs,
                **fields,
            }
        }
        return native(call_id, step("offer_candidate", payload, item, question.evidence_refs))

    def offer(messages):
        return submit_offer(messages, "offer")

    def invalid_offer(messages):
        return submit_offer(messages, "invalid-offer", draft_id="unknown-draft", expected_revision=0)

    def withdraw(messages):
        receipt = json.loads(messages[-1].content)
        assert receipt["status"] == "repair_needed" and receipt["requires_resolution"] is True
        item = from_board(messages)
        assert item["status"] == state.current(task.id)["status"] == "answered"
        payload = {
            "retract_draft": {
                "draft_id": receipt["draft_id"],
                "expected_revision": receipt["draft_revision"],
                "reason": "Invalid draft target",
            }
        }
        return native("withdraw", step("offer_candidate", payload, item))

    def complete(messages):
        receipt = json.loads(messages[-1].content)
        item = from_board(messages)
        question = store.get(Question, item["question_id"])
        assert receipt["status"] == "promoted"
        assert question.status == item["status"] == state.current(task.id)["status"] == "answered"
        assert item["case_ids"] == [receipt["case_id"]]
        assert "case_ids" not in state.current(task.id)
        return native("complete", finish(item, question.evidence_refs))

    replies = [native("read", read(1)), close]
    if repair_attempt:
        replies.extend([invalid_offer, withdraw])
    replies.extend([offer, complete])
    reviewer = make_reviewer(store, task, replies, accepted=accepted)
    assert await reviewer.run("a") == "reviewed", reviewer.error
    assert store.get(ReviewScope, "a").status == "reviewed"
    assert len(accepted) == len(store.list(Case)) == 1


async def test_answered_question_cannot_finish_before_same_batch_read_is_consumed(tmp_path):
    store, task = decision_store(tmp_path)
    state = DecisionState(store)

    def read_and_finish(messages):
        item = from_board(messages)
        question = store.get(Question, item["question_id"])
        next_item = {**item, "revision": item["revision"] + 1}
        return AIMessage(
            content="",
            tool_calls=[
                call("review_step", read(2, item), "supplemental"),
                call("review_step", finish(next_item, question.evidence_refs), "premature"),
            ],
        )

    def complete(messages):
        item = from_board(messages)
        question = store.get(Question, item["question_id"])
        assert item["status"] == question.status == "answered"
        [pending] = state.pending(task.id)
        assert pending["call_id"] == "supplemental"
        assert pending["operations"][0]["status"] == "completed"
        assert json.loads(messages[-1].content)["errors"][0]["reason"] == "unconsumed_source_operations"
        assert "line-2" in messages[-2].content
        return native("complete", finish(item, question.evidence_refs))

    reviewer = make_reviewer(store, task, [native("read", read(1)), close, read_and_finish, complete])
    assert await reviewer.run("a") == "reviewed", reviewer.error
    assert state.pending(task.id) == []


@pytest.mark.parametrize(
    ("changes", "status"),
    [
        ({"status": "open"}, "judging"),
        ({"status": "disputed"}, "unresolved"),
        ({"answer_status": "retracted", "change_reason": "Counterevidence invalidates the answer"}, "unresolved"),
    ],
)
async def test_invalidated_question_blocks_native_completion(tmp_path, changes, status):
    store, task = decision_store(tmp_path)
    state = DecisionState(store)

    def complete(messages):
        item = from_board(messages)
        question = store.get(Question, item["question_id"])
        assert item["status"] == question.status == "answered"
        store.update(Question, question.id, question.revision, **changes)
        assert state.list_items(task.id)[0]["status"] == status
        return native("complete", finish(item, question.evidence_refs))

    reviewer = make_reviewer(store, task, [native("read", read(1)), close, complete])
    assert await reviewer.run("a") == "partial"
    assert state.current(task.id)["status"] == status
    assert store.get(ReviewScope, "a").status == "partial"


def test_unresolved_question_survives_candidate_and_retraction_staging(tmp_path):
    store, task = decision_store(tmp_path)
    token = task.lease_token
    state = DecisionState(store)
    unresolved = {
        **initial(),
        "action": "unresolved",
        "payload": {"missing_fact": "contract", "attempted": "source", "recovery_condition": "contract available"},
    }
    audit(store, token, "gap", [call("review_step", unresolved, "gap")])
    state.stage("review", token, "gap", "gap", unresolved, now=2)
    item = state.current("review")
    question = store.get(Question, item["question_id"])
    for call_id, payload in (
        ("offer", {"candidate": {"trigger": "t", "behavior": "b", "impact": "i", "evidence_refs": ["ref"]}}),
        ("withdraw", {"retract_draft": {"draft_id": "draft", "expected_revision": 0, "reason": "mistaken"}}),
    ):
        args = step("offer_candidate", payload, item)
        audit(store, token, call_id, [call("review_step", args, call_id)])
        state.stage("review", token, call_id, call_id, args, now=2)
        item = state.current("review")
        assert item["status"] == question.status == "unresolved"
        assert store.get(Question, question.id) == question
