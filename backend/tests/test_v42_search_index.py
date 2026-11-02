import json
from dataclasses import replace

import pytest

from reviewforge.review_v42.context import ContextBuilder
from reviewforge.review_v42.contracts import BindingError, ReviewScope, Run, Task
from reviewforge.review_v42.store import ReviewStore


@pytest.fixture
def board(tmp_path):
    run = Run("r", "org/repo", "base", "head", policy="v42-frontier-5", metadata={"workspace_digest": "snapshot"})
    store = ReviewStore(tmp_path / "board.db", run)
    store.add(ReviewScope("scope", "r", "scope"))
    store.add(Task("task", "r", "review", "scope", 0))
    token = store.acquire_task("task", "worker", 100, now=0).lease_token
    return store, token


def search(store, token, query="needle", glob="", offset=0, total=3, skipped=None, tool="search_code"):
    skipped = skipped if skipped is not None else []
    count = min(100, max(0, total - offset))
    matches = [
        {"path": f"file{i}.py", "line": i + 1, "text": f"原文 original {i}"} for i in range(offset, offset + count)
    ]
    following = offset + 100 if offset + 100 < total else None
    doc = {
        "query": query,
        "glob": glob,
        "offset": offset,
        "next_offset": following,
        "total_matches": total,
        "matches": matches,
        "skipped_paths": skipped,
        "aliases_resolved": False,
    }
    args = {("query" if tool == "search_code" else "symbol"): query, "glob": glob, "offset": offset}
    content = json.dumps(doc, ensure_ascii=False).encode("utf-8")
    ref = store.put_evidence(
        content,
        revision="head",
        side="head",
        kind="search",
        metadata={
            "tool_name": tool,
            "tool_args": args,
            "status": "available",
            "workspace_digest": "snapshot",
            "complete": not skipped and following is None,
            "navigation_only": True,
        },
    )
    entry = store.pin_frontier_for_task("task", token, ref.id, now=1)
    return ref, entry, doc, content


def test_six_314_skips_index_and_original_recovery(board):
    store, token = board
    builder = ContextBuilder(store)
    skipped = [f"unavailable/{i}.py" for i in range(314)]
    entries = [search(store, token, query=f"query{i}", total=i, skipped=skipped) for i in range(6)]
    for ref, entry, doc, raw in entries:
        projection = builder.search_index(entry)
        assert projection["kind"] == "search_index"
        item = projection["sources"][0]
        assert item["ref"] == ref.id and item["tool_args"]["query"] == doc["query"]
        assert item["skipped_count"] == 314 and not item["complete"] and item["next_offset"] is None
        assert item["matches"] == [{"path": m["path"], "line": m["line"]} for m in doc["matches"]]
        assert all("text" not in match for match in item["matches"]) and "unavailable/" not in str(projection)
        assert store.read_evidence(ref.id) == raw
        first = builder.search_section(ref.id, "skipped_paths", offset=0, limit=200)
        second = builder.search_section(ref.id, "skipped_paths", offset=first["next_cursor"], limit=200)
        assert first["items"] + second["items"] == skipped
        assert second["next_cursor"] is None and not second["complete"]
    restored = ReviewStore(store.path, store.run)
    assert ContextBuilder(restored).search_index(restored.list_frontier("task")[0]) == builder.search_index(
        entries[0][1]
    )
    assert len({entry.id for _, entry, _, _ in entries}) == 6


def test_page_identity_and_full_match_paging(board):
    store, token = board
    builder = ContextBuilder(store)
    first = search(store, token, total=103)
    next_page = search(store, token, offset=100, total=103)
    different_glob = search(store, token, glob="**/*.py", total=103)
    references = search(store, token, tool="find_references", total=103)
    assert len({entry.id for _, entry, _, _ in (first, next_page, different_glob, references)}) == 4
    index = builder.search_index(first[1])["sources"][0]
    assert index["next_offset"] == 100 and index["total_matches"] == 103 and not index["complete"]
    p1 = builder.search_section(first[0].id, "matches", offset=0, limit=76)
    p2 = builder.search_section(first[0].id, "matches", offset=p1["next_cursor"], limit=76)
    assert p1["items"] + p2["items"] == first[2]["matches"]
    assert p2["search_next_offset"] == 100 and p2["tool_args"]["query"] == "needle"
    assert builder.search_section(first[0].id, "matches", offset=100, limit=1)["items"] == []
    for offset, limit in ((101, 1), (-1, 1), (0, 0), (True, 1)):
        with pytest.raises(ValueError):
            builder.search_section(first[0].id, "matches", offset=offset, limit=limit)


def test_same_search_alias_preserves_each_reference(board):
    store, token = board
    ref, entry, _, raw = search(store, token)
    alias = store.put_evidence(
        raw, revision="head", side="head", kind="search", metadata={**ref.metadata, "provenance_note": "second"}
    )
    merged = store.pin_frontier_for_task("task", token, alias.id, now=1)
    assert merged.id == entry.id
    index = ContextBuilder(store).search_index(merged)
    assert [source["ref"] for source in index["sources"]] == [ref.id, alias.id]


def test_unknown_and_non_search_fallback_and_corrupt_blob_fails_closed(board):
    store, token = board
    builder = ContextBuilder(store)
    ref, entry, _, _ = search(store, token)
    for malformed in (b"not-json", b'{"query":"x","query":"y"}', b'{"unexpected":"shape"}'):
        bad = store.put_evidence(malformed, revision="head", side="head", kind="search", metadata=ref.metadata)
        item = store.pin_frontier_for_task("task", token, bad.id, now=1)
        assert builder.search_index(item) is None
        assert store.read_frontier_entry("task", item.id) == malformed
        with pytest.raises(ValueError):
            builder.search_section(bad.id, "matches", offset=0, limit=1)
    diff = store.put_evidence(b"+diff\n", revision="head", side="head", kind="diff", metadata={"navigation_only": True})
    diff_entry = store.pin_frontier_for_task("task", token, diff.id, now=1)
    assert builder.search_index(diff_entry) is None and store.read_frontier_entry("task", diff_entry.id) == b"+diff\n"
    assert builder.search_index(replace(entry, aliases=["fake"])) is None
    (store.blob_dir / ref.content_hash).write_bytes(b"corrupted")
    with pytest.raises(BindingError):
        builder.search_index(entry)


def test_old_policy_unmodified_and_navigation_inherited(board, tmp_path):
    store, token = board
    state = {"question": "focus", "next_step": "next", "observations": [], "open_questions": ["question"]}
    result = store.merge_working_states("task", token, 0, [state], now=1)
    assert result["items"][0].text == "question"
    assert ContextBuilder(store).reviewer("scope")["navigation"][0]["text"] == "question"
    store.append_turn_audit("task", token, "turn", "request", [{"type": "human", "data": {"content": "x"}}], now=2)
    assert store.read_turn_audit("task")[0]["messages"][0]["data"]["content"] == "x"
    old_run = replace(store.run, id="old", policy="v42-frontier-4")
    old = ReviewStore(tmp_path / "old" / "board.db", old_run)
    old.add(ReviewScope("scope", "old", "scope"))
    old.add(Task("task", "old", "review", "scope", 0))
    old_token = old.acquire_task("task", "worker", 100, now=0).lease_token
    ref, entry, _, raw = search(old, old_token)
    assert ContextBuilder(old).search_index(entry) is None
    assert old.read_frontier_entry("task", entry.id) == raw == old.read_evidence(ref.id)
