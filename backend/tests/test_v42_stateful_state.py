"""Stateful review memory and visible audit boundaries."""

from dataclasses import asdict

import pytest

from reviewforge.review_v42.context import ContextBuilder
from reviewforge.review_v42.context_recovery import reviewer_context
from reviewforge.review_v42.contracts import (
    BindingError,
    Case,
    EvidenceRef,
    LeaseError,
    ReviewScope,
    RevisionConflict,
    Run,
    Task,
)
from reviewforge.review_v42.store import ReviewStore


@pytest.fixture
def board(tmp_path):
    store = ReviewStore(tmp_path / "state.db", Run("r", "repo", "base", "head", policy="v42-stateful-2"))
    store.add(ReviewScope("s", "r", "scope", regions=[{"id": "r1", "path": "a", "patch": "large diff"}]))
    store.add(Task("t", "r", "review", "s", 0))
    task = store.acquire_task("t", "reviewer", 100, now=0)
    return store, task


def state(ref):
    return {
        "question": "Does the consumer filter the input?",
        "observations": [{"statement": "The producer passes the original array", "evidence_refs": [ref]}],
        "open_questions": ["What does the consumer do?"],
        "next_step": "Read the consumer function",
    }


def test_working_state_is_atomic_bound_and_does_not_invalidate_frozen_case(board):
    store, task = board
    raw = ("source line\n" * 50000).encode()
    ref = store.put_evidence(raw, revision="head", side="head", kind="source", path="a")
    store.add(
        Case(
            "c", "r", scope_id="s", trigger="input", claims=[{"id": "a", "statement": "claim"}], evidence_refs=[ref.id]
        )
    )
    verdict = store.save_verdict("c", "a", 0, "p", "unresolved", [ref.id], {"reason": "uncertain"})
    scope = store.update_scope_for_task("t", task.lease_token, 0, now=1, working_state=state(ref.id))
    assert scope.revision == 1 and store.get(Task, "t").input_revision == 1
    assert store.get_verdict("c", "a", "p") == verdict
    assert store.get(Case, "c").revision == 0
    assert "consumer filter" not in str(ContextBuilder(store).verifier("c"))
    assert store.read_evidence(ref.id) == raw
    with pytest.raises(RevisionConflict):
        store.update_scope_for_task("t", task.lease_token, 0, now=2, working_state=state(ref.id))
    with pytest.raises(BindingError):
        store.update_scope_for_task("t", task.lease_token, 1, now=2, working_state=state("ev_forged"))
    assert store.get(ReviewScope, "s").working_state == state(ref.id)
    assert store.get(Task, "t").input_revision == 1
    with pytest.raises(LeaseError):
        store.update_scope_for_task("t", task.lease_token, 1, now=101, working_state=state(ref.id))


def test_working_state_schema_rejects_hidden_extra_fields_and_nonempty_legacy(tmp_path, board):
    store, task = board
    ref = store.put_evidence(b"source", revision="head", side="head", kind="source")
    for bad in (
        {**state(ref.id), "reasoning": "hidden"},
        {**state(ref.id), "question": " "},
        {**state(ref.id), "observations": [{"statement": "x", "evidence_refs": "not an array"}]},
    ):
        with pytest.raises(ValueError):
            store.update_scope_for_task("t", task.lease_token, 0, now=1, working_state=bad)
    old = ReviewStore(tmp_path / "legacy.db", Run("old", "repo", "base", "head"))
    with pytest.raises(ValueError):
        old.add(ReviewScope("s", "old", "scope", working_state=state(ref.id)))


def test_stateful_context_has_all_brief_sources_and_no_audit_history(board):
    store, task = board
    ref = store.put_evidence(
        b"full source",
        revision="head",
        side="head",
        kind="source",
        path="a",
        metadata={"tool_args": {"query": "x"}, "secret": "MUST_NOT_APPEAR"},
    )
    unused = store.put_evidence(b"unused but readable", revision="head", side="head", kind="search", path="b")
    store.update_scope_for_task("t", task.lease_token, 0, now=1, working_state=state(ref.id))
    builder = ContextBuilder(store)
    before = builder.reviewer("s")
    assert {r["id"] for r in before["evidence"]} == {ref.id, unused.id}
    assert all(
        set(r) == {"id", "path", "side", "revision", "start_line", "end_line", "kind"} for r in before["evidence"]
    )
    assert "MUST_NOT_APPEAR" not in str(before)
    assert "large diff" not in str(before)
    assert before["scope"]["working_state"] == state(ref.id)
    with store._connect() as db:
        db.execute("BEGIN")
        db.executemany(
            "INSERT INTO events(kind,object_id,action,payload) VALUES ('Noise','other','noise','{}')", [()] * 10000
        )
        db.commit()
    assert builder.reviewer("s") == before
    assert reviewer_context(store, "s")["evidence"] == before["evidence"]
    assert len(store.history("Noise")) == 10000
    assert store.read_evidence(unused.id) == b"unused but readable"


def test_audit_is_idempotent_complete_separate_from_evidence_and_recoverable(tmp_path, board):
    store, task = board
    request = [{"type": "human", "data": {"content": "read x"}}]
    response = [
        {
            "type": "ai",
            "data": {"content": "", "tool_calls": [{"name": "read_source", "args": {"path": "a"}, "id": "call"}]},
        },
        {"type": "tool", "data": {"content": "complete output\n" * 30000, "tool_call_id": "call"}},
    ]
    first = store.append_turn_audit("t", task.lease_token, "turn-1", "request", request, now=1)
    assert store.append_turn_audit("t", task.lease_token, "turn-1", "request", request, now=2) == first
    store.append_turn_audit("t", task.lease_token, "turn-1", "response", response, now=3)
    with pytest.raises(RevisionConflict):
        store.append_turn_audit("t", task.lease_token, "turn-1", "request", [{"different": True}], now=4)
    with pytest.raises(KeyError):
        store.read_evidence(first["content_hash"])
    restored = ReviewStore(store.path, store.run)
    assert [item["messages"] for item in restored.read_turn_audit("t", "turn-1")] == [request, response]
    assert restored.list(EvidenceRef) == []
    with pytest.raises(LeaseError):
        restored.append_turn_audit("t", task.lease_token, "turn-2", "request", request, now=101)
    takeover = restored.acquire_task("t", "new owner", 100, now=101)
    with pytest.raises(LeaseError):
        restored.append_turn_audit("t", task.lease_token, "turn-2", "request", request, now=102)
    restored.append_turn_audit("t", takeover.lease_token, "turn-2", "request", request, now=102)
    with pytest.raises(ValueError, match="reasoning"):
        restored.append_turn_audit(
            "t",
            takeover.lease_token,
            "turn-2",
            "response",
            [{"type": "ai", "data": {"additional_kwargs": {"reasoning_content": "secret"}}}],
            now=103,
        )
    (restored.audit_dir / first["content_hash"]).write_bytes(b"tampered")
    with pytest.raises(BindingError):
        restored.read_turn_audit("t")


def test_legacy_context_keeps_existing_projection(tmp_path):
    store = ReviewStore(tmp_path / "old.db", Run("r", "repo", "base", "head"))
    store.add(ReviewScope("s", "r", "scope", regions=[{"id": "r", "patch": "full diff"}]))
    ref = store.put_evidence(b"source", revision="head", side="head", kind="source", metadata={"key": "value"})
    assert "working_state" not in ContextBuilder(store).reviewer("s")["scope"]
    assert ContextBuilder(store).reviewer("s")["scope"]["regions"][0]["patch"] == "full diff"
    assert reviewer_context(store, "s")["evidence"][0] == asdict(ref)


def test_turn_audit_keeps_business_arguments_and_invalid_calls_with_reasoning_keys(board):
    store, task = board
    messages = [
        {"type": "human", "data": {"content": {"reasoning": "ordinary user input"}}},
        {
            "type": "ai",
            "data": {
                "content": [{"type": "text", "text": "calling tool", "details": {"reasoning": "visible"}}],
                "tool_calls": [
                    {
                        "id": "call-1",
                        "name": "search",
                        "args": {"thinking": "business", "reasoning": {"type": "reasoning", "value": 42}},
                    }
                ],
                "invalid_tool_calls": [
                    {"id": "bad", "name": "search", "args": '{"thinking":', "error": "invalid JSON"}
                ],
                "additional_kwargs": {
                    "tool_calls": [{"function": {"name": "search", "arguments": '{"reasoning":"ordinary"}'}}]
                },
            },
        },
        {
            "type": "tool",
            "data": {"tool_call_id": "call-1", "content": {"type": "reasoning", "thinking": "data returned by tool"}},
        },
    ]
    store.append_turn_audit("t", task.lease_token, "business", "exchange", messages, now=1)
    assert store.read_turn_audit("t", "business")[0]["messages"] == messages


@pytest.mark.parametrize(
    "message",
    [
        {"type": "ai", "data": {"reasoning_content": "private"}},
        {"type": "ai", "data": {"additional_kwargs": {"thinking": "private"}}},
        {"type": "ai", "data": {"response_metadata": {"reasoning": "private"}}},
        {"type": "ai", "data": {"content": [{"type": "thinking", "thinking": "private"}]}},
        {"role": "assistant", "content": [{"type": "reasoning_content", "text": "private"}]},
    ],
)
def test_turn_audit_rejects_actual_assistant_private_protocol_fields(board, message):
    store, task = board
    with pytest.raises(ValueError, match="reasoning"):
        store.append_turn_audit("t", task.lease_token, "private", "response", [message], now=1)
    assert store.read_turn_audit("t") == []
