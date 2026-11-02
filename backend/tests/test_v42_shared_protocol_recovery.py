"""Protocol failures cannot lose the correction across a reviewer interruption."""

import pytest
from langchain_core.messages import AIMessage
from test_v42_fair_runtime import PinnedGateway
from test_v42_shared_batch_recovery import Crash, create_call
from test_v42_shared_batch_recovery import board as board
from test_v42_stateful_reviewer import call

from reviewforge.review_v42.contracts import ReviewScope, Task
from reviewforge.review_v42.reviewer import Reviewer
from reviewforge.review_v42.shared_investigation import SharedInvestigations
from reviewforge.review_v42.tools import EvidenceGateway


def malformed(kind):
    if kind == "mixed":
        return AIMessage(
            content="",
            tool_calls=[create_call("valid")],
            invalid_tool_calls=[{"id": "invalid", "name": "read_source", "args": "{", "error": "invalid JSON"}],
        )
    ids = ("duplicate", "duplicate") if kind == "duplicate" else ("", "valid")
    return AIMessage(content="", tool_calls=[create_call(ids[0]), call("read_source", {"path": "a.py"}, ids[1])])


@pytest.mark.parametrize("kind", ["mixed", "duplicate", "empty"])
@pytest.mark.parametrize("point", ["response", "feedback", "exchange"])
@pytest.mark.parametrize("fenced", [False, True])
async def test_interrupted_protocol_rejection_is_partial_or_recovers_feedback(board, monkeypatch, kind, point, fenced):
    store, task = board
    gateway = PinnedGateway()

    class FirstModel:
        def bind_tools(self, *args, **kwargs):
            return self

        async def ainvoke(self, messages):
            return malformed(kind)

    original_audit = store.append_turn_audit
    original_save = store.save_task_session

    def audit(task_id, token, turn, phase, messages, **kwargs):
        value = original_audit(task_id, token, turn, phase, messages, **kwargs)
        if phase == point:
            raise Crash()
        return value

    def save(task_id, token, messages, **kwargs):
        value = original_save(task_id, token, messages, **kwargs)
        if point == "feedback":
            raise Crash()
        return value

    monkeypatch.setattr(store, "append_turn_audit", audit)
    monkeypatch.setattr(store, "save_task_session", save)
    reviewer = Reviewer(
        store, FirstModel(), EvidenceGateway(store, gateway), task_id=task.id, lease_token=task.lease_token
    )
    with pytest.raises(Crash):
        await reviewer.run("a")
    monkeypatch.setattr(store, "append_turn_audit", original_audit)
    monkeypatch.setattr(store, "save_task_session", original_save)
    assert SharedInvestigations(store).preview(task.id, task.lease_token)["total"] == 0
    assert gateway.calls == []
    pending = store.pending_shared_batches("a")
    assert bool(pending) == (point != "exchange")
    if fenced:
        scope = store.get(ReviewScope, "a")
        store.update_scope_for_task(task.id, task.lease_token, scope.revision, status="partial")
    store.release_task(task.id, task.lease_token)
    new_id = task.id
    if fenced:
        new_id = "replacement"
        scope = store.get(ReviewScope, "a")
        store.add(Task(new_id, store.run.id, "review", "a", scope.revision))
    new = store.acquire_task(new_id, "replacement", 120)

    class RecoveryModel:
        def __init__(self):
            self.seen = False

        def bind_tools(self, *args, **kwargs):
            assert point == "exchange", "a pending batch must prevent a new model request"
            return self

        async def ainvoke(self, messages):
            if not self.seen:
                self.seen = True
                assert "No tool was executed" in messages[-1].content
                return AIMessage(content="", tool_calls=[create_call("corrected")])
            return AIMessage(content="")

    model = RecoveryModel()
    resumed = Reviewer(store, model, EvidenceGateway(store, gateway), task_id=new.id, lease_token=new.lease_token)
    result = await resumed.run("a")
    if point == "exchange":
        assert model.seen
        assert SharedInvestigations(store).preview(new.id, new.lease_token)["total"] == 1
    else:
        assert result == "partial" and "incomplete_native_batches" in resumed.error
        assert SharedInvestigations(store).preview(new.id, new.lease_token)["total"] == 0
