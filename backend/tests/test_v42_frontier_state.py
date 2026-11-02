import pytest

from reviewforge.review_v42.context import ContextBuilder
from reviewforge.review_v42.contracts import BindingError, Case, LeaseError, ReviewScope, RevisionConflict, Run, Task
from reviewforge.review_v42.store import ReviewStore


@pytest.fixture
def board(tmp_path):
    store = ReviewStore(tmp_path / "board.db", Run("run", "org/repo", "base", "head", policy="v42-frontier-4"))
    store.add(ReviewScope("scope", "run", "Scope"))
    store.add(Task("task", "run", "review", "scope", 0))
    lease = store.acquire_task("task", "worker", 100, now=0)
    return store, lease.lease_token


def source(store, path="a.py", side="head", content=b"one\r\ntwo\r\nthree\r\n", **kwargs):
    return store.put_evidence(
        content, revision={"head": "head", "base": "base"}[side], side=side, kind="source", path=path, **kwargs
    )


def test_batches_aliases_ranges_and_complete_raw(board):
    store, token = board
    a = source(store, metadata={"tool_args": {"limit": 9}})
    alias = source(store, metadata={"tool_args": {"limit": 10}})
    b = source(store, path="b.py")
    base = source(store, side="base")
    first = store.pin_frontier_for_task("task", token, a.id, now=1)
    merged = store.pin_frontier_for_task("task", token, alias.id, now=1)
    second = store.pin_frontier_for_task("task", token, b.id, now=1)
    third = store.pin_frontier_for_task("task", token, base.id, now=1)
    page = store.pin_frontier_for_task("task", token, a.id, start_line=2, end_line=2, now=1)
    assert first.id == merged.id and set(merged.aliases) == {a.id, alias.id}
    assert len({first.id, second.id, third.id, page.id}) == 4
    assert store.read_frontier_entry("task", first.id) == b"one\r\ntwo\r\nthree\r\n"
    assert store.read_frontier_entry("task", page.id) == b"two\r\n"
    assert store.read_evidence(a.id) == b"one\r\ntwo\r\nthree\r\n"
    assert len(store.list_frontier("task")) == 4
    first_page = store.list_frontier_page("task", limit=2)
    second_page = store.list_frontier_page("task", cursor=first_page["next_cursor"], limit=2)
    assert len(first_page["items"] + second_page["items"]) == 4 and second_page["next_cursor"] is None
    body_page = store.read_frontier_page("task", first.id, start_line=2, end_line=2)
    assert body_page["content"] == b"two\r\n" and body_page["next_line"] == 3
    with pytest.raises(ValueError):
        store.read_frontier_page("task", page.id, start_line=1, end_line=1)
    with pytest.raises(ValueError):
        store.pin_frontier_for_task("task", token, a.id, start_line=2, end_line=20, now=1)


def test_page_from_bounded_source_and_same_actual_range_dedup(board):
    store, token = board
    bounded = source(store, content=b"two\r\nthree\r\n", start_line=2, end_line=3)
    entire = source(store)
    full = store.pin_frontier_for_task("task", token, bounded.id, now=1)
    page = store.pin_frontier_for_task("task", token, entire.id, start_line=2, end_line=3, now=1)
    assert full.id == page.id and set(page.aliases) == {bounded.id, entire.id}
    assert store.read_frontier_entry("task", page.id) == b"two\r\nthree\r\n"


def test_release_recovery_cas_and_lease(board):
    store, token = board
    ref = source(store)
    entry = store.pin_frontier_for_task("task", token, ref.id, expected_revision=0, now=1)
    with pytest.raises(RevisionConflict):
        store.release_frontier_for_task("task", token, [entry.id], "done", expected_revision=0, now=2)
    with pytest.raises(LeaseError):
        store.release_frontier_for_task("task", token, [entry.id], "done", expected_revision=1, now=100)
    released = store.release_frontier_for_task("task", token, [entry.id], "investigated", expected_revision=1, now=2)[0]
    assert released.status == "released" and store.list_frontier("task") == []
    assert store.read_evidence(ref.id) == b"one\r\ntwo\r\nthree\r\n"
    restored = ReviewStore(store.path, store.run)
    assert restored.frontier_revision("task") == 2
    resumed = restored.pin_frontier_for_task("task", token, ref.id, expected_revision=2, now=3)
    assert resumed.id == entry.id and resumed.item_revision == 2
    assert [event["action"] for event in store.history("FrontierEntry", entry.id)] == ["pin", "release", "pin"]
    assert store.get(Task, "task").input_revision == 0
    assert store.get(ReviewScope, "scope").revision == 0


def test_foreign_binding_query_identity_and_verdict_stability(board, tmp_path):
    store, token = board
    ref = source(store)
    foreign_store = ReviewStore(tmp_path / "other.db", Run("other", "org/repo", "base", "head"))
    foreign = source(foreign_store)
    with pytest.raises(BindingError):
        store.pin_frontier_for_task("task", token, foreign.id, now=1)
    store.add(
        Case("case", "run", scope_id="scope", trigger="trigger", claims=[{"id": "claim"}], evidence_refs=[ref.id])
    )
    verdict = store.save_verdict("case", "claim", 0, "p", "unresolved", [ref.id], {"reason": "gap"})
    one = store.put_evidence(b"none", revision="head", side="head", kind="search", metadata={"tool_args": {"q": "a"}})
    two = store.put_evidence(b"none", revision="head", side="head", kind="search", metadata={"tool_args": {"q": "b"}})
    e1 = store.pin_frontier_for_task("task", token, one.id, now=1)
    e2 = store.pin_frontier_for_task("task", token, two.id, now=1)
    assert e1.id != e2.id
    with pytest.raises(BindingError):
        store.add(Case("bad", "run", evidence_refs=[e1.id]))
    store.release_frontier_for_task("task", token, [e1.id], "done", expected_revision=2, now=2)
    assert store.get(Case, "case").revision == 0
    assert store.get_verdict("case", "claim", "p") == verdict
    assert "frontier_" not in str(ContextBuilder(store).verifier("case"))


def test_policy_three_compatibility_and_audit(board):
    store, token = board
    result = store.merge_working_states(
        "task",
        token,
        0,
        [{"question": "q", "next_step": "n", "observations": [], "open_questions": ["still open"]}],
        now=1,
    )
    assert result["items"][0].text == "still open"
    assert ContextBuilder(store).reviewer("scope")["navigation"][0]["text"] == "still open"
    before = ContextBuilder(store).reviewer("scope")
    with store._connect() as db:
        db.execute("BEGIN")
        db.executemany(
            "INSERT INTO events(kind,object_id,action,payload) VALUES ('Noise','other','noise','{}')", [()] * 10000
        )
        db.commit()
    assert ContextBuilder(store).reviewer("scope") == before
    store.append_turn_audit("task", token, "turn", "request", [{"type": "human", "data": {"content": "x"}}], now=2)
    assert store.read_turn_audit("task")[0]["messages"][0]["data"]["content"] == "x"
