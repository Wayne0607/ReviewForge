"""Incremental, lease-bound navigation for the workspace-3 policy."""

import pytest

from reviewforge.review_v42.context import ContextBuilder
from reviewforge.review_v42.context_recovery import reviewer_context
from reviewforge.review_v42.contracts import (
    BindingError,
    Case,
    EvidenceRef,
    LeaseError,
    NavigationItem,
    ReviewScope,
    RevisionConflict,
    Run,
    Task,
)
from reviewforge.review_v42.store import ReviewStore


@pytest.fixture
def board(tmp_path):
    store = ReviewStore(tmp_path / "board.db", Run("r", "org/repo", "base", "head", policy="v42-workspace-3"))
    store.add(ReviewScope("scope", "r", "Scope"))
    store.add(Task("task", "r", "review", "scope", 0))
    lease = store.acquire_task("task", "worker", 100, now=0)
    ref = store.put_evidence(
        b"actual source\n" * 20000,
        revision="head",
        side="head",
        kind="source",
        path="a.py",
        metadata={"secret": "never include in board"},
    )
    return store, lease, ref


def state(question, next_step, observations=(), open_questions=()):
    return {
        "question": question,
        "next_step": next_step,
        "observations": list(observations),
        "open_questions": list(open_questions),
    }


def test_real_trace_empty_next_focus_keeps_three_questions_and_observation(board):
    store, lease, ref = board
    old = state(
        "Which handler defects are real?",
        "Inspect authorization helpers",
        [{"statement": "handler filters then emails raw inputs", "evidence_refs": [ref.id]}],
        ["Do roles imply each other?", "Which credentials are used?", "Is raw input intentional?"],
    )
    first = store.merge_working_states("task", lease.lease_token, 0, [old], now=1)
    assert first["errors"] == [] and len(first["items"]) == 4
    later = state("Does MultiEmail introduce defects?", "Read MultiEmail")
    second = store.merge_working_states("task", lease.lease_token, first["scope"].revision, [later], now=2)
    view = ContextBuilder(store).reviewer("scope")
    assert second["scope"].working_state == later
    assert len(view["navigation"]) == 4
    assert {v["text"] for v in view["navigation"]} == {
        "handler filters then emails raw inputs",
        "Do roles imply each other?",
        "Which credentials are used?",
        "Is raw input intentional?",
    }
    assert store.read_evidence(ref.id) == b"actual source\n" * 20000
    assert store.get(Task, "task").input_revision == second["scope"].revision
    assert store.list(EvidenceRef) == [ref]


def test_exact_idempotence_closed_status_and_explicit_correction_are_auditable(board):
    store, lease, ref = board
    initial = state("focus", "next", [{"statement": "seen", "evidence_refs": [ref.id, ref.id]}], ["question"])
    first = store.merge_working_states("task", lease.lease_token, 0, [initial], now=1)
    again = store.merge_working_states("task", lease.lease_token, first["scope"].revision, [initial], now=2)
    assert again["scope"].revision == first["scope"].revision
    assert {x.id for x in again["items"]} == {x.id for x in first["items"]}
    question = next(item for item in first["items"] if item.kind == "question")
    closed = store.update_navigation_for_task(
        "task", lease.lease_token, question.id, 0, status="resolved", reason="source answers it", now=3
    )
    assert closed.status == "resolved" and closed.revision == 1
    current = store.get(ReviewScope, "scope")
    replay = store.merge_working_states("task", lease.lease_token, current.revision, [initial], now=4)
    assert next(x for x in replay["items"] if x.id == question.id).status == "resolved"
    assert question.id not in {x["id"] for x in ContextBuilder(store).reviewer("scope")["navigation"]}
    revised = store.update_navigation_for_task(
        "task",
        lease.lease_token,
        question.id,
        1,
        text="corrected question",
        status="open",
        reason="prior premise wrong",
        now=5,
    )
    current = store.get(ReviewScope, "scope")
    stale = store.merge_working_states("task", lease.lease_token, current.revision, [initial], now=6)
    assert any("corrected" in e["error"] for e in stale["errors"])
    assert store.get(NavigationItem, revised.id).text == "corrected question"
    events = store.history("NavigationItem", question.id)
    assert [event["action"] for event in events] == ["add", "update", "update"]
    assert events[-1]["payload"]["reason"] == "prior premise wrong"


def test_invalid_sibling_not_erase_valid_and_no_cross_run_refs(board, tmp_path):
    store, lease, ref = board
    other = ReviewStore(tmp_path / "other.db", Run("other", "org/repo", "base", "head", policy="v42-workspace-3"))
    foreign = other.put_evidence(b"other", revision="head", side="head", kind="source")
    valid = state("q", "next", [{"statement": "fact", "evidence_refs": [ref.id]}], ["open"])
    bad = state("bad", "next", [{"statement": "foreign", "evidence_refs": [foreign.id]}])
    incomplete = state("q2", "next2", [{"statement": "unsupported", "evidence_refs": []}])
    result = store.merge_working_states("task", lease.lease_token, 0, [valid, bad, incomplete], now=1)
    assert {x["index"] for x in result["errors"]} == {1, 2}
    assert {item.text for item in store.list(NavigationItem)} == {"fact", "open"}
    assert result["scope"].working_state["question"] == "q2"
    with pytest.raises(BindingError):
        store.update_navigation_for_task(
            "task", lease.lease_token, result["items"][0].id, 0, evidence_refs=[foreign.id], reason="bad", now=2
        )


def test_lease_revision_and_navigation_do_not_change_case_verdict(board):
    store, lease, ref = board
    store.add(Case("case", "r", scope_id="scope", trigger="trigger", claims=[{"id": "claim"}], evidence_refs=[ref.id]))
    verdict = store.save_verdict("case", "claim", 0, "p", "unresolved", [ref.id], {"reason": "gap"})
    initial = state("q", "next", [{"statement": "source seen", "evidence_refs": [ref.id]}])
    result = store.merge_working_states("task", lease.lease_token, 0, [initial], now=1)
    with pytest.raises(RevisionConflict):
        store.merge_working_states("task", lease.lease_token, 0, [initial], now=2)
    item = result["items"][0]
    with pytest.raises(RevisionConflict):
        store.update_navigation_for_task(
            "task", lease.lease_token, item.id, 1, status="resolved", reason="reason", now=2
        )
    with pytest.raises(LeaseError):
        store.merge_working_states("task", lease.lease_token, 1, [initial], now=100)
    store.update_navigation_for_task(
        "task", lease.lease_token, item.id, 0, status="retracted", reason="incorrect", now=2
    )
    assert store.get(Case, "case").revision == 0
    assert store.get_verdict("case", "claim", "p") == verdict
    assert "source seen" not in str(ContextBuilder(store).verifier("case"))


def test_restart_audit_legacy_policy_and_no_history_in_prompt(tmp_path, board):
    store, lease, ref = board
    first = store.merge_working_states("task", lease.lease_token, 0, [state("q", "n", open_questions=["open"])], now=1)
    before = reviewer_context(store, "scope")
    with store._connect() as db:
        db.execute("BEGIN")
        db.executemany(
            "INSERT INTO events(kind,object_id,action,payload) VALUES ('Noise','other','noise','{}')", [()] * 10000
        )
        db.commit()
    assert reviewer_context(store, "scope") == before
    restored = ReviewStore(store.path, store.run)
    assert restored.list(NavigationItem) == first["items"]
    assert restored.read_evidence(ref.id).startswith(b"actual source")
    restored.append_turn_audit(
        "task", lease.lease_token, "turn", "request", [{"type": "human", "data": {"content": "x"}}], now=2
    )
    assert restored.read_turn_audit("task")[0]["messages"][0]["data"]["content"] == "x"
    assert all(
        set(e) == {"id", "path", "side", "revision", "start_line", "end_line", "kind"} for e in before["evidence"]
    )
    old = ReviewStore(tmp_path / "old.db", Run("old", "org/repo", "base", "head", policy="v42-stateful-2"))
    old.add(ReviewScope("oldscope", "old", "Scope"))
    assert "navigation" not in ContextBuilder(old).reviewer("oldscope")
    with pytest.raises(ValueError):
        old.merge_working_states("missing", "token", 0, [], now=1)


def test_explicit_pages_have_continuations_without_truncating_or_metadata(board):
    store, lease, ref = board
    store.put_evidence(
        b"other source", revision="head", side="head", kind="source", path="b.py", metadata={"secret": "never expose"}
    )
    store.merge_working_states(
        "task", lease.lease_token, 0, [state("focus", "next", open_questions=["first", "second", "third"])], now=1
    )
    builder = ContextBuilder(store)
    first = builder.navigation_page("scope", limit=1)
    assert len(first["items"]) == 1 and first["next_cursor"]
    second = builder.navigation_page("scope", cursor=first["next_cursor"], limit=2)
    assert len(second["items"]) == 2 and second["next_cursor"] is None
    assert {i["id"] for i in first["items"] + second["items"]} == {
        i["id"] for i in builder.reviewer("scope")["navigation"]
    }
    refs = builder.evidence_page(limit=1)
    assert refs["next_cursor"] and "metadata" not in refs["items"][0]
    last = builder.evidence_page(cursor=refs["next_cursor"], limit=1)
    assert last["next_cursor"] is None and "secret" not in str(refs) + str(last)
    assert {item["id"] for item in refs["items"] + last["items"]} == {
        item["id"] for item in builder.reviewer("scope")["evidence"]
    }
    with pytest.raises(ValueError):
        builder.navigation_page("scope", limit=0)


def test_navigation_wrong_scope_old_lease_and_concurrent_revision_conflict(board):
    from concurrent.futures import ThreadPoolExecutor

    store, lease, ref = board
    store.add(ReviewScope("other", "r", "other"))
    store.add(Task("other-task", "r", "review", "other", 0))
    other = store.acquire_task("other-task", "other worker", 100, now=0)

    def merge(question):
        try:
            return store.merge_working_states(
                "task", lease.lease_token, 0, [state("focus", "next", open_questions=[question])], now=1
            )
        except RevisionConflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(merge, ["first", "second"]))
    assert sum(result is None for result in results) == 1
    item = next(result["items"][0] for result in results if result)
    with pytest.raises(BindingError):
        store.update_navigation_for_task(
            "other-task", other.lease_token, item.id, 0, status="resolved", reason="wrong scope", now=2
        )
    replacement = store.acquire_task("task", "replacement", 100, now=101)
    with pytest.raises(LeaseError):
        store.update_navigation_for_task(
            "task", lease.lease_token, item.id, 0, status="resolved", reason="stale", now=102
        )
    result = store.update_navigation_for_task(
        "task", replacement.lease_token, item.id, 0, status="resolved", reason="inspected", now=102
    )
    assert result.status == "resolved"
    assert ContextBuilder(store).reviewer("other")["navigation"] == []


def test_multiple_valid_states_merge_all_and_keep_last_focus(board):
    store, lease, ref = board
    one = state(
        "first focus",
        "first action",
        [{"statement": "first observation", "evidence_refs": [ref.id]}],
        ["first question"],
    )
    two = state(
        "second focus",
        "second action",
        [{"statement": "second observation", "evidence_refs": [ref.id]}],
        ["second question"],
    )
    merged = store.merge_working_states("task", lease.lease_token, 0, [one, two], now=1)
    assert merged["errors"] == []
    assert len(merged["items"]) == 4
    assert merged["scope"].working_state["question"] == "second focus"
    assert {item.text for item in store.list(NavigationItem)} == {
        "first observation",
        "first question",
        "second observation",
        "second question",
    }
