"""Incomplete native exchanges must not authorize related comments."""

from copy import deepcopy

import pytest
import test_v42_case_counterevidence as case_fixtures
from langchain_core.messages import AIMessage, messages_to_dict
from test_v42_stateful_reviewer import call

from reviewforge.review_v42.contracts import Case, Task
from reviewforge.review_v42.publication import PublicationOutbox

board = case_fixtures.board


def interrupted_turn(store, task, turn, name="read_source", args=None):
    store.append_turn_audit(task.id, task.lease_token, turn, "request", [], now=1)
    response = AIMessage(content="", tool_calls=[call(name, args or {"path": "b.py"}, "pending-action")])
    store.append_turn_audit(task.id, task.lease_token, turn, "response", messages_to_dict([response]), now=1)


def test_incomplete_batch_blocks_only_its_scope_and_preserves_the_action(board):
    store, _, b, case, source, _ = board
    previous = store.get_verdict(case.id, "claim", "v42-independent-1")["result"]
    other = store.add(
        Case(
            "other-case",
            "run",
            "a",
            trigger="a distinct trigger",
            mechanism=case.mechanism,
            claims=case.claims,
            evidence_refs=[source.id],
        )
    )
    other_result = deepcopy(previous)
    other_result["case_trigger"] = other.trigger
    store.save_verdict(other.id, "claim", 0, "v42-independent-1", "accept", [source.id], other_result)
    outbox = PublicationOutbox(store)
    frozen = {comment["case_id"]: comment for comment in outbox.freeze()}
    assert set(frozen) == {case.id, other.id}

    interrupted_turn(store, b, "interrupted")
    report = outbox.freeze_batch()["report"]
    assert report["status"] == "partial"
    assert [comment["case_id"] for comment in report["comments"]] == [other.id]
    pending = report["incomplete_tool_batches"]
    assert len(pending) == 1 and pending[0]["task_id"] == b.id and pending[0]["turn_id"] == "interrupted"
    assert pending[0]["calls"][0]["name"] == "read_source"
    assert pending[0]["calls"][0]["receipt"] == "outcome_unknown"
    with pytest.raises(ValueError, match="permission"):
        outbox.prepare(frozen[case.id]["id"])
    assert outbox.prepare(frozen[other.id]["id"])["case_id"] == other.id


def test_interrupted_integration_batch_blocks_every_comment(board):
    store, _, _, case, _, _ = board
    outbox = PublicationOutbox(store)
    comment = next(item for item in outbox.freeze() if item["case_id"] == case.id)
    store.add(Task("integration-task", store.run.id, "review", "integration", 0))
    task = store.acquire_task("integration-task", "test", 100, now=0)
    interrupted_turn(store, task, "interrupted-integration")
    report = outbox.freeze_batch()["report"]
    assert report["status"] == "partial" and not report["comments"]
    assert any(item["scope_id"] == "integration" for item in report["incomplete_tool_batches"])
    with pytest.raises(ValueError, match="permission"):
        outbox.prepare(comment["id"])


@pytest.mark.parametrize(
    ("name", "args"),
    [
        ("register_case_counterevidence", {"case_id": "case", "evidence_refs": ["not-yet-delivered"]}),
        ("create_shared_investigation", {"scope_ids": ["a", "b"], "region_ids": ["a", "b"]}),
        ("register_unit_dependency", {"target_scope_id": "b"}),
        ("update_shared_investigation", {"investigation_id": "not-yet-recorded"}),
        ("unrecognized_future_tool", {}),
    ],
)
def test_unfinished_cross_unit_action_blocks_its_possible_target(board, name, args):
    store, a, _, case, _, _ = board
    outbox = PublicationOutbox(store)
    comment = next(item for item in outbox.freeze() if item["case_id"] == case.id)
    interrupted_turn(store, a, "cross-unit-interrupted", name=name, args=args)
    report = outbox.freeze_batch()["report"]
    assert report["status"] == "partial" and not report["comments"]
    assert report["incomplete_tool_batches"][0]["calls"][0]["name"] == name
    with pytest.raises(ValueError, match="permission"):
        outbox.prepare(comment["id"])
