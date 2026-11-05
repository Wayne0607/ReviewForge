"""A failed sibling must not prevent a successful decision or source delivery."""

import json

import pytest
from langchain_core.messages import AIMessage
from test_v42_candidate_draft_reviewer import Model
from test_v42_decision_loop import decision_store, from_board, last_ref, step
from test_v42_fair_runtime import PinnedGateway
from test_v42_stateful_reviewer import call

from reviewforge.review_v42.contracts import Case, Question
from reviewforge.review_v42.decision_loop import DecisionReviewer
from reviewforge.review_v42.tools import EvidenceGateway


class PagedGateway(PinnedGateway):
    async def __call__(self, name, args):
        self.calls.append((name, args.copy()))
        first, last = args.get("start_line", 1), args.get("end_line", 1)
        return {
            "content": "".join(f"line-{number}\n" for number in range(first, last + 1)),
            "revision": "head",
            "side": "head",
            "kind": "source",
            "path": args["path"],
            "start_line": first,
            "end_line": last,
            "metadata": {"workspace_digest": "head-snapshot", "status": "available"},
        }


def read(page, item=None, *, include_bad_operation=False):
    operations = [{"name": "read_source", "args": {"path": "a.py", "start_line": page, "end_line": page}}]
    if include_bad_operation:
        operations.append({"name": "unknown_source", "args": {}})
    payload = {"gap": "compare", "relevance": "behavior", "alternatives": "confirm or refute", "operations": operations}
    return step("request_evidence", payload, item)


def bad():
    return call("review_step", {"action": "invalid"}, "bad")


def unresolved(item, refs=None):
    payload = {"missing_fact": "contract", "attempted": "sources", "recovery_condition": "contract available"}
    return call("review_step", step("unresolved", payload, item, refs), "unresolved")


def make_reviewer(store, task, replies, gateway=None, accepted=None):
    return DecisionReviewer(
        store,
        Model(replies),
        EvidenceGateway(store, gateway or PagedGateway()),
        task_id=task.id,
        lease_token=task.lease_token,
        on_case=(accepted.append if accepted is not None else None),
    )


def sibling_read(page, item=None):
    return AIMessage(content="", tool_calls=[bad(), call("review_step", read(page, item), f"page-{page}")])


async def test_new_page_with_same_failing_sibling_is_delivered(tmp_path):
    store, task = decision_store(tmp_path)

    def next_page(messages):
        return sibling_read(2, from_board(messages))

    def inspect(messages):
        assert "line-1" in messages[2].content
        ref = last_ref(messages)
        assert "line-2" in messages[-1].content
        return AIMessage(content="", tool_calls=[unresolved(from_board(messages), [ref])])

    reviewer = make_reviewer(store, task, [sibling_read(1), next_page, inspect])
    assert await reviewer.run("a") == "partial"
    assert reviewer.error is None
    assert any(q.status == "unresolved" for q in store.list(Question))


async def test_successful_rereads_with_same_failing_sibling_do_not_stop(tmp_path):
    store, task = decision_store(tmp_path)

    def reread(messages):
        return sibling_read(1, from_board(messages))

    def inspect(messages):
        return AIMessage(content="", tool_calls=[unresolved(from_board(messages), [last_ref(messages)])])

    gateway = PagedGateway()
    reviewer = make_reviewer(store, task, [sibling_read(1), reread, reread, inspect], gateway)
    assert await reviewer.run("a") == "partial"
    assert reviewer.error is None
    assert len(gateway.calls) == 3


async def test_identical_entirely_failed_batches_still_stop(tmp_path):
    store, task = decision_store(tmp_path)
    response = AIMessage(content="", tool_calls=[bad()])
    reviewer = make_reviewer(store, task, [response, response])
    assert await reviewer.run("a") == "partial"
    assert reviewer.error == "repeated_failed_batch"


@pytest.mark.parametrize("action", ["close_question", "offer_candidate", "unresolved"])
async def test_successful_decisions_survive_repeated_failing_sibling(tmp_path, action):
    store, task = decision_store(tmp_path)
    accepted = []

    def decide(messages):
        item, ref = from_board(messages), last_ref(messages)
        if action == "close_question":
            payload = {"question_id": item["question_id"], "answer": "Checked source", "evidence_refs": [ref]}
        elif action == "offer_candidate":
            payload = {
                "candidate": {"trigger": "trigger", "behavior": "behavior", "impact": "impact", "evidence_refs": [ref]}
            }
        else:
            payload = {"missing_fact": "contract", "attempted": "source", "recovery_condition": "contract available"}
        return AIMessage(
            content="", tool_calls=[bad(), call("review_step", step(action, payload, item, [ref]), "decision")]
        )

    def inspect(messages):
        item = from_board(messages)
        if action == "close_question":
            assert store.get(Question, item["question_id"]).status == "answered"
        else:
            assert len(accepted) == len(store.list(Case)) == 1
        return AIMessage(content="", tool_calls=[unresolved(item)])

    replies = [sibling_read(1), decide] + ([] if action == "unresolved" else [inspect])
    reviewer = make_reviewer(store, task, replies, accepted=accepted)
    assert await reviewer.run("a") == "partial"
    assert reviewer.error is None
    if action == "unresolved":
        assert any(q.status == "unresolved" for q in store.list(Question))


async def test_mixed_read_results_keep_success_and_failure_receipts(tmp_path):
    store, task = decision_store(tmp_path)
    first = AIMessage(content="", tool_calls=[call("review_step", read(1, include_bad_operation=True), "first")])

    def another(messages):
        return AIMessage(
            content="",
            tool_calls=[call("review_step", read(2, from_board(messages), include_bad_operation=True), "second")],
        )

    def inspect(messages):
        receipt = json.loads(messages[-1].content)
        assert len(receipt["results"]) == 2
        assert receipt["results"][0]["result"]["content"] == "line-2\n"
        assert "error" in receipt["results"][1]["result"]
        return AIMessage(content="", tool_calls=[unresolved(from_board(messages), [last_ref(messages)])])

    reviewer = make_reviewer(store, task, [first, another, inspect])
    assert await reviewer.run("a") == "partial"
    assert reviewer.error is None
