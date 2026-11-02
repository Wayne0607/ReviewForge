"""Shared-mode native batches fail closed after an interrupted action."""

import json
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage
from test_v42_fair_runtime import PinnedGateway, finish_unit
from test_v42_stateful_reviewer import call

from reviewforge.review_v42.contracts import ReviewScope, Run, Task
from reviewforge.review_v42.reviewer import Reviewer, _ambiguous_native_ids
from reviewforge.review_v42.shared_investigation import SharedInvestigations
from reviewforge.review_v42.store import ReviewStore
from reviewforge.review_v42.tools import EvidenceGateway


@pytest.fixture
def board(tmp_path):
    regions = [{"id": "a", "path": "a.py", "side": "head", "start_line": 1, "end_line": 1, "changed_lines": [1]}]
    run = Run(
        "run",
        "a/b",
        "base",
        "head",
        policy="v42-units-6",
        metadata={
            "changed_regions": regions,
            "workspace_digest": "head-snapshot",
            "base_workspace_digest": "base-snapshot",
            "shared_investigation": "shared-1",
        },
    )
    store = ReviewStore(tmp_path / "board.sqlite", run)
    store.install_unit_plan(
        [ReviewScope("a", run.id, "a", regions=regions)],
        ReviewScope("integration", run.id, "integration", dependencies=["a"]),
    )
    SharedInvestigations(store)
    store.add(Task("task", run.id, "review", "a", 0))
    task = store.acquire_task("task", "tester", 120)
    return store, task


def create_call(ident, question="why?"):
    return call("create_shared_investigation", {"question": question, "scope_ids": ["a"], "region_ids": ["a"]}, ident)


class Crash(BaseException):
    pass


def test_raw_and_invalid_call_ids_are_checked_even_when_parsed_calls_are_unique():
    parsed = [call("read_source", {"path": "a.py"}, "unique")]
    assert _ambiguous_native_ids(
        SimpleNamespace(
            tool_calls=parsed, invalid_tool_calls=[], additional_kwargs={"tool_calls": [{"id": "x"}, {"id": "x"}]}
        )
    )
    assert _ambiguous_native_ids(
        SimpleNamespace(tool_calls=parsed, invalid_tool_calls=[{"id": "unique"}], additional_kwargs={})
    )


@pytest.mark.parametrize("after_receipt", [False, True])
@pytest.mark.parametrize("different_task", [False, True])
async def test_interrupted_action_blocks_recovery_and_new_lease(board, monkeypatch, after_receipt, different_task):
    store, task = board

    class Model:
        def bind_tools(self, *args, **kwargs):
            return self

        async def ainvoke(self, messages):
            return AIMessage(content="", tool_calls=[create_call("first"), create_call("second", "other?")])

    original = store.append_turn_audit

    def interrupt(task_id, token, turn_id, phase, messages, **kwargs):
        if phase.startswith("tool:0:"):
            if after_receipt:
                original(task_id, token, turn_id, phase, messages, **kwargs)
            raise Crash()
        return original(task_id, token, turn_id, phase, messages, **kwargs)

    monkeypatch.setattr(store, "append_turn_audit", interrupt)
    reviewer = Reviewer(
        store, Model(), EvidenceGateway(store, PinnedGateway()), task_id=task.id, lease_token=task.lease_token
    )
    with pytest.raises(Crash):
        await reviewer.run("a")
    monkeypatch.setattr(store, "append_turn_audit", original)
    pending = store.pending_shared_batches("a")
    assert pending and "first" in json.dumps(pending) and "second" in json.dumps(pending)
    outcomes = {item["id"]: item["receipt"] for item in pending[0]["calls"]}
    assert outcomes == {"first": "recorded" if after_receipt else "outcome_unknown", "second": "outcome_unknown"}
    assert SharedInvestigations(store).preview(task.id, task.lease_token)["total"] == 1
    store.release_task(task.id, task.lease_token)
    next_task = task.id
    if different_task:
        next_task = "replacement-task"
        scope = store.get(ReviewScope, "a")
        store.add(Task(next_task, store.run.id, "review", "a", scope.revision))
    new = store.acquire_task(next_task, "new-owner", 120)
    assert new.lease_token != task.lease_token

    class MustNotCall:
        def bind_tools(self, *args, **kwargs):
            raise AssertionError("pending batch must be handled before binding")

    resumed = Reviewer(
        store, MustNotCall(), EvidenceGateway(store, PinnedGateway()), task_id=new.id, lease_token=new.lease_token
    )
    assert await resumed.run("a") == "partial"
    assert "incomplete_native_batches" in resumed.error and "second" in resumed.error
    assert SharedInvestigations(store).preview(new.id, new.lease_token)["total"] == 1
    assert store.pending_shared_batches("a") == pending


@pytest.mark.parametrize("bad_ids", [("same", "same"), ("", "valid")])
@pytest.mark.parametrize("same_input", [False, True])
async def test_ambiguous_ids_reject_whole_batch_and_allow_correction(board, bad_ids, same_input):
    store, task = board

    class Model:
        def __init__(self):
            self.count = 0

        def bind_tools(self, *args, **kwargs):
            return self

        async def ainvoke(self, messages):
            self.count += 1
            if self.count == 1:
                return AIMessage(
                    content="",
                    tool_calls=[
                        create_call(bad_ids[0]),
                        create_call(bad_ids[1], "why?" if same_input else "other?"),
                        call("read_source", {"path": "a.py"}, "valid-sibling"),
                    ],
                )
            if self.count == 2:
                assert "No tool was executed" in messages[-1].content
                assert SharedInvestigations(store).preview(task.id, task.lease_token)["total"] == 0
                return AIMessage(content="", tool_calls=[create_call("corrected")])
            return AIMessage(content="")

    gateway = PinnedGateway()
    reviewer = Reviewer(store, Model(), EvidenceGateway(store, gateway), task_id=task.id, lease_token=task.lease_token)
    await reviewer.run("a")
    assert gateway.calls == []
    assert SharedInvestigations(store).preview(task.id, task.lease_token)["total"] == 1
    assert not store.pending_shared_batches("a")


async def test_finish_committed_without_exchange_remains_partial_after_new_lease(board, monkeypatch):
    store, task = board

    class Model:
        def __init__(self):
            self.step = 0

        def bind_tools(self, *args, **kwargs):
            return self

        async def ainvoke(self, messages):
            self.step += 1
            if self.step == 1:
                return AIMessage(content="", tool_calls=[call("read_source", {"path": "a.py"}, "read")])
            ref = next(json.loads(message.content)["evidence_ref"] for message in messages if message.type == "tool")
            return AIMessage(content="", tool_calls=[finish_unit("a", ref)])

    original = store.append_turn_audit

    def interrupt(task_id, token, turn_id, phase, messages, **kwargs):
        if phase.endswith(":finish-a"):
            raise Crash()
        return original(task_id, token, turn_id, phase, messages, **kwargs)

    monkeypatch.setattr(store, "append_turn_audit", interrupt)
    reviewer = Reviewer(
        store, Model(), EvidenceGateway(store, PinnedGateway()), task_id=task.id, lease_token=task.lease_token
    )
    with pytest.raises(Crash):
        await reviewer.run("a")
    monkeypatch.setattr(store, "append_turn_audit", original)
    assert store.get(ReviewScope, "a").status == "reviewed"
    assert store.pending_shared_batches("a")
    assert store.validate_units_completion()["valid"] is False
    store.release_task(task.id, task.lease_token)
    new = store.acquire_task(task.id, "replacement", 120)
    resumed = Reviewer(
        store, object(), EvidenceGateway(store, PinnedGateway()), task_id=new.id, lease_token=new.lease_token
    )
    assert await resumed.run("a") == "partial"
    assert store.get(ReviewScope, "a").status == "partial"
    assert "finish-a" in resumed.error
