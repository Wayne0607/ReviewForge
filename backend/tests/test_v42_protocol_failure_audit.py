"""Visible malformed calls remain distinct and auditable without provider raw calls."""

import json
from types import SimpleNamespace

from langchain_core.messages import AIMessage
from test_v42_fair_runtime import PinnedGateway
from test_v42_shared_batch_recovery import Crash
from test_v42_shared_batch_recovery import board as board
from test_v42_stateful_reviewer import call

from reviewforge.review_v42.publication import _incomplete_batch_impact
from reviewforge.review_v42.reviewer import Reviewer, _protocol_failure
from reviewforge.review_v42.tools import EvidenceGateway


def malformed(path, ident, *, state=None, sibling=False):
    # Provider supplied a visible call whose name is invalid, but no raw or invalid list.
    calls = [{"id": ident, "name": None, "args": {"path": path, "review_state": state}}]
    if sibling:
        calls.append(call("read_source", {"path": "valid.py"}, "good"))
    return AIMessage.model_construct(content="", tool_calls=calls, invalid_tool_calls=[], additional_kwargs={})


def test_parsed_failure_signature_tracks_actions_not_ids_or_working_notes():
    errors = ["invalid-native-tool-call"]
    a = malformed("first.py", "one", state={"question": "first"})
    same = malformed("first.py", "two", state={"question": "different"})
    changed = malformed("second.py", "three", state={"question": "different"})
    assert _protocol_failure(a, errors) == _protocol_failure(same, errors)
    assert _protocol_failure(a, errors) != _protocol_failure(changed, errors)
    changed_name = SimpleNamespace(
        tool_calls=[{"id": "one", "name": 12, "args": {"path": "first.py"}}],
        invalid_tool_calls=[],
        additional_kwargs={},
    )
    assert _protocol_failure(a, errors) != _protocol_failure(changed_name, errors)


async def test_different_malformed_calls_allow_correction_same_failure_remains_partial(board):
    store, task = board

    class Model:
        def __init__(self):
            self.count = 0

        def bind_tools(self, *args, **kwargs):
            return self

        async def ainvoke(self, messages):
            self.count += 1
            path = "first.py" if self.count == 1 else "second.py"
            return malformed(path, f"call-{self.count}", state={"note": self.count})

    model, gateway = Model(), PinnedGateway()
    reviewer = Reviewer(store, model, EvidenceGateway(store, gateway), task_id=task.id, lease_token=task.lease_token)
    assert await reviewer.run("a") == "partial"
    assert reviewer.error == "repeated_failed_batch" and model.count == 3
    assert gateway.calls == []
    audit = store.read_turn_audit(task.id)
    native = [entry for entry in audit if entry["phase"] == "native"]
    assert len(native) == 3
    assert all(entry["messages"][0]["protocol_errors"] == ["invalid-native-tool-call"] for entry in native)
    assert all(entry["messages"][0]["visible_tool_calls"][0]["name"] is None for entry in native)


async def test_mixed_native_batch_crash_keeps_global_protocol_uncertainty(board, monkeypatch):
    store, task = board

    class Model:
        def bind_tools(self, *args, **kwargs):
            return self

        async def ainvoke(self, messages):
            return malformed("invalid.py", "bad", sibling=True)

    original = store.append_turn_audit

    def crash_after_response(task_id, token, turn_id, phase, messages, **kwargs):
        result = original(task_id, token, turn_id, phase, messages, **kwargs)
        if phase == "response":
            raise Crash()
        return result

    monkeypatch.setattr(store, "append_turn_audit", crash_after_response)
    gateway = PinnedGateway()
    reviewer = Reviewer(store, Model(), EvidenceGateway(store, gateway), task_id=task.id, lease_token=task.lease_token)
    try:
        await reviewer.run("a")
        raise AssertionError("the simulated crash did not occur")
    except Crash:
        pass
    assert gateway.calls == []
    audit = store.read_turn_audit(task.id)
    native = next(entry for entry in audit if entry["phase"] == "native")
    response = next(entry for entry in audit if entry["phase"] == "response")
    assert native["seq"] < response["seq"]
    assert native["messages"][0]["protocol_errors"] == ["invalid-native-tool-call"]
    assert len(native["messages"][0]["visible_tool_calls"]) == 2
    pending = store.pending_shared_batches("a")
    assert pending and "invalid native" in pending[0]["reason"]
    assert _incomplete_batch_impact({"a": pending}, {"a", "integration"}, set())[2] is True
    assert "valid.py" in json.dumps(native["messages"])
