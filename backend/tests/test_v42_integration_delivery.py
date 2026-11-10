"""Regression for the integration stages that returned unregistered shell calls."""

import asyncio
import json

import pytest
from langchain_core.messages import AIMessage
from test_v42_candidate_draft_reviewer import Model
from test_v42_decision_loop import decision_store, finish_primary, from_board, step
from test_v42_fair_runtime import PinnedGateway
from test_v42_stateful_reviewer import call

from reviewforge.review_v42.contracts import Question, Task
from reviewforge.review_v42.decision_loop import DecisionReviewer
from reviewforge.review_v42.integration_delivery import INTEGRATION_DELIVERY_POLICY
from reviewforge.review_v42.tools import EvidenceGateway


class TextModel(Model):
    def bind_tools(self, *_args, **_kwargs):
        pytest.fail("integration delivery must use the ordinary message channel")


def submission(action):
    return AIMessage(content=json.dumps(action))


def shell():
    return AIMessage(content="", tool_calls=[call("shell", {}, "shell-a"), call("shell", {}, "shell-b")])


async def integration_store(tmp_path):
    store, primary = decision_store(tmp_path, integration_delivery_policy=INTEGRATION_DELIVERY_POLICY)
    await finish_primary(store, primary)
    task = store.add(Task("integration-task", store.run.id, "review", "integration", 0))
    return store, store.acquire_task(task.id, "integrator", 120)


def reviewer(store, task, gateway, replies):
    return DecisionReviewer(
        store, TextModel(replies), EvidenceGateway(store, gateway), task_id=task.id, lease_token=task.lease_token
    )


def scope_submission(messages, action, **fields):
    scope = json.loads(messages[1].content)["scope"]
    return submission(
        {"action": action, "scope_id": scope["id"], "expected_scope_revision": scope["revision"], **fields}
    )


async def test_shell_rejection_source_delivery_and_receipt_resume(tmp_path):
    store, task = await integration_store(tmp_path)
    gateway = PinnedGateway()
    received = {}

    def read(messages):
        feedback = json.loads(messages[-1].content)
        assert feedback["obligation_id"] == json.loads(messages[1].content)["current_integration_obligation"]["id"]
        assert "without tool calls" in feedback["error"]
        return submission(
            step(
                "request_evidence",
                {
                    "gap": "Check the changed implementation",
                    "relevance": "Changed unit",
                    "alternatives": "Compare source behavior",
                    "operations": [{"name": "read_source", "args": {"path": "a.py"}}],
                },
            )
        )

    def close(messages):
        item = from_board(messages)
        received["ref"] = json.loads(messages[-1].content)["results"][0]["result"]["evidence_ref"]
        payload = {
            "question_id": item["question_id"],
            "answer": "Checked fixed source",
            "evidence_refs": [received["ref"]],
        }
        return submission(step("close_question", payload, item, [received["ref"]]))

    def receipt(messages):
        current = json.loads(messages[1].content)["current_integration_obligation"]
        return scope_submission(
            messages,
            "submit_receipt",
            receipt={
                "obligation_id": current["id"],
                "disposition": "checked",
                "reason": "The fixed implementation supports the result",
                "evidence_refs": [received["ref"]],
            },
        )

    def interrupt(_messages):
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await reviewer(store, task, gateway, [shell(), read, close, receipt, interrupt]).run("integration")
    checked = {o["id"]: o["revision"] for o in store.list_integration_obligations() if o["valid"]}
    assert len(checked) == 1
    store.release_task(task.id, task.lease_token)
    task = store.acquire_task(task.id, "resumed", 120)

    def finish(messages):
        assert json.loads(messages[1].content)["current_integration_obligation"] is None
        return scope_submission(messages, "finish_integration")

    remaining = [receipt] * sum(not o["valid"] for o in store.list_integration_obligations())
    resumed = reviewer(store, task, gateway, [*remaining, finish])
    assert await resumed.run("integration") == "reviewed", resumed.error
    assert [name for name, _ in gateway.calls] == ["read_source"]
    assert all(o["valid"] and o["revision"] == checked.get(o["id"], 1) for o in store.list_integration_obligations())
    audit = store.read_turn_audit(task.id)
    actual = [m["data"] for a in audit if a["phase"] == "response" for m in a["messages"]]
    assert actual[0]["tool_calls"][0]["name"] == "shell"
    assert all(not m["tool_calls"] for m in actual[1:])
    assert json.loads(actual[-1]["content"])["action"] == "finish_integration"


@pytest.mark.parametrize("repeated_shell", [False, True])
async def test_unfinished_integration_keeps_a_concrete_gap_or_protocol_failure(tmp_path, repeated_shell):
    store, task = await integration_store(tmp_path)
    gateway = PinnedGateway()

    def gap(messages):
        oid = json.loads(messages[1].content)["current_integration_obligation"]["id"]
        payload = {
            "missing_fact": "The approved shipping threshold",
            "attempted": "Checked the PR source and repository documents",
            "recovery_condition": "Obtain the current deployment's approved policy",
        }
        return submission({**step("unresolved", payload), "question": f"{oid}: what is the approved threshold?"})

    model = reviewer(store, task, gateway, [shell(), shell() if repeated_shell else gap])
    assert await model.run("integration") == "partial"
    assert not gateway.calls
    assert not any(o["valid"] for o in store.list_integration_obligations())
    if repeated_shell:
        assert "without tool calls" in model.error
        assert any(o["id"] in model.error for o in store.list_integration_obligations())
    else:
        assert model.error is None
        unresolved = [q for q in store.list(Question) if q.status == "unresolved"]
        assert len(unresolved) == 1
        gap = json.loads(unresolved[0].change_reason)
        assert gap["missing_fact"] == "The approved shipping threshold"
        assert gap["recovery_condition"] == "Obtain the current deployment's approved policy"
