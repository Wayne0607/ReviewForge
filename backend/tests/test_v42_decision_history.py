"""Decision history stays durable and is explicitly readable as navigation."""

import json

import pytest
from langchain_core.messages import AIMessage
from test_v42_candidate_draft_reviewer import Model
from test_v42_decision_loop import decision_store, step
from test_v42_fair_runtime import PinnedGateway
from test_v42_stateful_reviewer import call

from reviewforge.review_v42.contracts import EvidenceRef, Task
from reviewforge.review_v42.decision_loop import DecisionReviewer
from reviewforge.review_v42.decision_state import DecisionState
from reviewforge.review_v42.tools import EvidenceGateway, InvalidEvidenceRequestError


def read(item=None, *, judgment="judgment"):
    args = step(
        "request_evidence",
        {
            "gap": "source",
            "relevance": "behavior",
            "alternatives": "confirm or refute",
            "operations": [{"name": "read_source", "args": {"path": "a.py"}}],
        },
        item,
    )
    args["judgment"] = judgment
    return args


def history(item, target, *, offset=0, limit=None):
    args = {"work_item_id": target, "offset": offset}
    if limit is not None:
        args["limit"] = limit
    return step(
        "request_evidence",
        {
            "gap": "prior judgment",
            "relevance": "compare",
            "alternatives": "retain or correct",
            "operations": [{"name": "read_decision_history", "args": args}],
        },
        item,
    )


def decision_items(messages):
    return json.loads(messages[1].content)["decision_items"]


async def test_old_judgments_are_paged_on_request_without_automatic_injection(tmp_path):
    store, task = decision_store(tmp_path)
    first = AIMessage(content="", tool_calls=[call("review_step", read(judgment="older-first"), "one")])

    def second(messages):
        return AIMessage(
            content="",
            tool_calls=[call("review_step", read(decision_items(messages)[0], judgment="older-current"), "two")],
        )

    def switch(messages):
        arg = read(judgment="new-current")
        arg["question"] = "Another question?"
        return AIMessage(content="", tool_calls=[call("review_step", arg, "switch")])

    def request_first_page(messages):
        board = json.loads(messages[1].content)
        items = board["decision_items"]
        assert len(items) == 2
        assert items[0]["history_count"] == 2 and items[0]["history_available_via"] == "read_decision_history"
        assert [j["text"] for j in items[0]["judgments"]] == ["older-current"]
        assert "older-first" not in messages[1].content
        assert any(s["name"] == "read_decision_history" for s in board["source_operation_schemas"] if "name" in s)
        return AIMessage(
            content="", tool_calls=[call("review_step", history(items[1], items[0]["id"], limit=1), "history-1")]
        )

    def request_second_page(messages):
        result = json.loads(messages[-1].content)["results"][0]["result"]
        assert result["navigation_only"] and result["items"][0]["text"] == "older-first"
        assert result["next_offset"] == 1 and result["complete"] is False
        assert "evidence_ref" not in result
        items = decision_items(messages)
        return AIMessage(
            content="",
            tool_calls=[
                call(
                    "review_step", history(items[1], items[0]["id"], offset=result["next_offset"], limit=1), "history-2"
                )
            ],
        )

    def finish(messages):
        result = json.loads(messages[-1].content)["results"][0]["result"]
        assert result["items"][0]["text"] == "older-current"
        assert result["complete"] is True and result["next_offset"] is None
        item = decision_items(messages)[1]
        payload = {"missing_fact": "contract", "attempted": "history", "recovery_condition": "contract available"}
        return AIMessage(content="", tool_calls=[call("review_step", step("unresolved", payload, item), "stop")])

    reviewer = DecisionReviewer(
        store,
        Model([first, second, switch, request_first_page, request_second_page, finish]),
        EvidenceGateway(store, PinnedGateway()),
        task_id=task.id,
        lease_token=task.lease_token,
    )
    assert await reviewer.run("a") == "partial", reviewer.error
    items = DecisionState(store).list_items(task.id)
    assert len(items[0]["judgments"]) == 2
    assert all(ref.kind != "decision_history" for ref in store.list(EvidenceRef))


async def test_history_rejects_another_scope_and_invalid_pagination(tmp_path):
    store, task = decision_store(tmp_path)
    payload = {"missing_fact": "contract", "attempted": "review", "recovery_condition": "contract available"}
    reviewer = DecisionReviewer(
        store,
        Model([AIMessage(content="", tool_calls=[call("review_step", step("unresolved", payload), "stop")])]),
        EvidenceGateway(store, PinnedGateway()),
        task_id=task.id,
        lease_token=task.lease_token,
    )
    assert await reviewer.run("a") == "partial"
    item = DecisionState(store).current(task.id)
    other = store.add(Task("integration-task", store.run.id, "review", "integration", 0))
    other = store.acquire_task(other.id, "integrator", 120)
    integration = DecisionReviewer(
        store, None, EvidenceGateway(store, PinnedGateway()), task_id=other.id, lease_token=other.lease_token
    )
    full = await reviewer._read_operation(
        {"name": "read_decision_history", "args": {"work_item_id": item["id"]}}, reviewer._operations()
    )
    assert full["complete"] is True and full["next_offset"] is None
    assert full["items"] == item["judgments"] and full["navigation_only"] is True
    with pytest.raises(InvalidEvidenceRequestError):
        await integration._read_operation(
            {"name": "read_decision_history", "args": {"work_item_id": item["id"]}}, integration._operations()
        )
    with pytest.raises(InvalidEvidenceRequestError):
        await reviewer._read_operation(
            {"name": "read_decision_history", "args": {"work_item_id": item["id"], "offset": 100}},
            reviewer._operations(),
        )
    assert not store.list(EvidenceRef)
