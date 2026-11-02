"""Pending native protocol must not hide cross-unit effects from publication."""

import json

import pytest
from langchain_core.messages import AIMessage
from test_v42_case_counterevidence import board as board
from test_v42_fair_runtime import PinnedGateway
from test_v42_stateful_reviewer import call

from reviewforge.review_v42.publication import PublicationOutbox
from reviewforge.review_v42.reviewer import Reviewer
from reviewforge.review_v42.shared_investigation import SharedInvestigations
from reviewforge.review_v42.tools import EvidenceGateway


class Crash(BaseException):
    pass


async def test_filtered_invalid_cross_unit_call_blocks_frozen_comment_before_feedback(board, monkeypatch):
    store, old, _, case, _, counter = board
    SharedInvestigations(store)
    task = store.acquire_task(old.id, "current", 120)
    outbox = PublicationOutbox(store)
    original = outbox.freeze()
    assert len(original) == 1 and original[0]["case_id"] == case.id

    class Model:
        def bind_tools(self, *args, **kwargs):
            return self

        async def ainvoke(self, messages):
            return AIMessage(
                content="",
                tool_calls=[call("read_source", {"path": "a.py"}, "read")],
                invalid_tool_calls=[
                    {
                        "id": "counter",
                        "name": "register_case_counterevidence",
                        "args": json.dumps({"case_id": case.id, "evidence_refs": [counter.id]})[:-1],
                        "error": "truncated JSON",
                    }
                ],
            )

    append = store.append_turn_audit

    def interrupt(task_id, token, turn_id, phase, messages, **kwargs):
        result = append(task_id, token, turn_id, phase, messages, **kwargs)
        if phase == "response":
            raise Crash()
        return result

    monkeypatch.setattr(store, "append_turn_audit", interrupt)
    gateway = PinnedGateway()
    with pytest.raises(Crash):
        await Reviewer(
            store, Model(), EvidenceGateway(store, gateway), task_id=task.id, lease_token=task.lease_token
        ).run("a")
    assert gateway.calls == []
    pending = store.pending_shared_batches("a")
    assert len(pending) == 1 and "invalid native" in pending[0]["reason"]
    assert outbox.freeze() == []
    with pytest.raises(ValueError, match="permission"):
        outbox.prepare(original[0]["id"])


@pytest.mark.parametrize("problem", ["count", "duplicate_id", "mismatch", "duplicate_argument", "broken_blob"])
def test_pending_raw_identity_ambiguity_blocks_all_comments(board, problem):
    store, task, _, _, _, _ = board
    SharedInvestigations(store)
    outbox = PublicationOutbox(store)
    original = outbox.freeze()
    assert original
    calls = [call("read_source", {"path": "a.py"}, "read")]
    raw = [{"id": "read", "function": {"name": "read_source", "arguments": '{"path":"a.py"}'}}]
    if problem == "count":
        raw.append({"id": "other", "function": {"name": "register_case_counterevidence", "arguments": "{}"}})
    elif problem == "duplicate_id":
        raw[0]["id"] = "different"
    elif problem == "mismatch":
        raw[0]["function"]["name"] = "register_case_counterevidence"
    elif problem == "duplicate_argument":
        raw[0]["function"]["arguments"] = '{"path":"a.py","path":"a.py"}'
    native = [{"raw_tool_calls": raw, "invalid_tool_calls": []}]
    store.append_turn_audit(task.id, task.lease_token, "pending", "request", [], now=1)
    entry = store.append_turn_audit(task.id, task.lease_token, "pending", "native", native, now=1)
    store.append_turn_audit(
        task.id,
        task.lease_token,
        "pending",
        "response",
        [{"type": "ai", "data": {"content": "", "tool_calls": calls}}],
        now=1,
    )
    if problem == "broken_blob":
        (store.audit_dir / entry["content_hash"]).write_bytes(b"corrupt")
    pending = store.pending_shared_batches("a")
    assert len(pending) == 1 and pending[0]["reason"] != "native exchange not fully persisted"
    assert outbox.freeze() == []
    with pytest.raises(ValueError, match="permission"):
        outbox.prepare(original[0]["id"])
