"""Source projections preserve exact originals and prove each alias independently."""

import hashlib
import json

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from reviewforge.review_v42.contracts import ReviewScope, Run, Task
from reviewforge.review_v42.reviewer import FrontierUnavailableError, Reviewer
from reviewforge.review_v42.shared_investigation import SharedInvestigations
from reviewforge.review_v42.store import ReviewStore


def board(tmp_path, *, enabled=True):
    region = {"id": "a", "path": "a.py", "side": "head", "start_line": 1, "end_line": 1, "changed_lines": [1]}
    metadata = {
        "changed_regions": [region],
        "workspace_digest": "snapshot",
        "base_workspace_digest": "base-snapshot",
        "shared_investigation": "shared-1",
        "source_line_policy": "git-lf-1",
    }
    if enabled:
        metadata["frontier_projection_policy"] = "source-line-union-1"
    run = Run("run", "org/repo", "base", "head", policy="v42-units-6", metadata=metadata)
    store = ReviewStore(tmp_path / "board.sqlite", run)
    store.install_unit_plan(
        [ReviewScope("a", run.id, "a", regions=[region])],
        ReviewScope("integration", run.id, "integration", dependencies=["a"]),
    )
    SharedInvestigations(store)
    task = store.add(Task("task", run.id, "review", "a", 0))
    task = store.acquire_task(task.id, "worker", 120)
    reviewer = Reviewer(store, object(), object(), task_id=task.id, lease_token=task.lease_token)
    return store, task, reviewer


def add(store, task, text, start, end, *, side="head", path="a.py", environment="", marker=""):
    ref = store.put_evidence(
        text.encode(),
        revision=side,
        side=side,
        kind="source",
        path=path,
        start_line=start,
        end_line=end,
        environment=environment,
        metadata={
            "workspace_digest": "snapshot",
            "status": "available",
            "tool_name": "read_source",
            "tool_args": {"path": path, "side": side},
            "marker": marker,
        },
    )
    entry = store.pin_frontier_for_task(task.id, task.lease_token, ref.id)
    return ref, entry


def entries(reviewer, recent=None):
    return json.loads(reviewer._context("a", recent=recent)[2].content)["entries"]


def test_overlaps_and_aliases_preserve_git_line_bytes(tmp_path):
    store, task, reviewer = board(tmp_path)
    first, first_entry = add(store, task, "α\r\nb\rc\u2028d\v\nlast", 1, 3)
    second, second_entry = add(store, task, "b\rc\u2028d\v\nlast", 2, 3)
    alias, alias_entry = add(store, task, "b\rc\u2028d\v\nlast", 2, 3, marker="alias")
    assert second_entry.id == alias_entry.id
    union = entries(reviewer)
    assert len(union) == 1 and union[0]["kind"] == "source_union"
    item = union[0]
    assert item["content"] == "α\r\nb\rc\u2028d\v\nlast"
    assert item["content_hash"] == hashlib.sha256(item["content"].encode()).hexdigest()
    assert {(m["entry_id"], m["ref_id"], m["start_line"], m["end_line"]) for m in item["mappings"]} == {
        (first_entry.id, first.id, 1, 3),
        (second_entry.id, second.id, 2, 3),
        (second_entry.id, alias.id, 2, 3),
    }


def test_gaps_and_identity_boundaries_are_not_joined(tmp_path):
    store, task, reviewer = board(tmp_path)
    add(store, task, "a\nb\n", 1, 2)
    add(store, task, "b\nc\n", 2, 3)
    add(store, task, "g\nh\n", 7, 8)
    add(store, task, "a\nb\n", 1, 2, side="base")
    add(store, task, "b\nc\n", 2, 3, side="base")
    add(store, task, "b\nc\n", 2, 3, environment="other")
    result = entries(reviewer)
    assert [(e["side"], e["start_line"], e["end_line"]) for e in result if e["kind"] == "source_union"] == [
        ("head", 1, 3),
        ("base", 1, 3),
    ]
    assert any(e["kind"] == "source" and e["start_line"] == 7 for e in result)
    assert any(e["kind"] == "source" and e["environment"] == "other" for e in result)


def test_explicit_page_maps_only_its_actual_intersection(tmp_path):
    store, task, reviewer = board(tmp_path)
    full, whole = add(store, task, "a\nb\nc\nd\n", 1, 4)
    page = store.pin_frontier_for_task(task.id, task.lease_token, full.id, start_line=2, end_line=3)
    item = entries(reviewer)[0]
    assert item["content"] == "a\nb\nc\nd\n"
    assert {(m["entry_id"], m["ref_id"], m["start_line"], m["end_line"]) for m in item["mappings"]} == {
        (whole.id, full.id, 1, 4),
        (page.id, full.id, 2, 3),
    }


def test_adjacent_pages_merge_without_inventing_newlines(tmp_path):
    store, task, reviewer = board(tmp_path)
    add(store, task, "a\r\nb\n", 1, 2)
    add(store, task, "c\rd", 3, 3)
    item = entries(reviewer)[0]
    assert item["kind"] == "source_union" and item["content"] == "a\r\nb\nc\rd"


def test_adjacent_inconsistent_unterminated_line_falls_back(tmp_path):
    store, task, reviewer = board(tmp_path)
    add(store, task, "a", 1, 1)
    add(store, task, "b\n", 2, 2)
    assert [item["content"] for item in entries(reviewer)] == ["a", "b\n"]


def test_whole_file_without_explicit_range_can_overlap_a_page(tmp_path):
    store, task, reviewer = board(tmp_path)
    ref = store.put_evidence(
        b"a\nb\n",
        revision="head",
        side="head",
        kind="source",
        path="a.py",
        metadata={
            "workspace_digest": "snapshot",
            "tool_name": "read_source",
            "tool_args": {"path": "a.py", "side": "head"},
        },
    )
    whole = store.pin_frontier_for_task(task.id, task.lease_token, ref.id)
    page = store.pin_frontier_for_task(task.id, task.lease_token, ref.id, start_line=2, end_line=2)
    item = entries(reviewer)[0]
    assert item["content"] == "a\nb\n"
    assert {(m["entry_id"], m["ref_id"], m["start_line"], m["end_line"]) for m in item["mappings"]} == {
        (whole.id, ref.id, 1, 2),
        (page.id, ref.id, 2, 2),
    }


def test_invalid_source_provenance_falls_back_to_original_entries(tmp_path):
    store, task, reviewer = board(tmp_path)
    add(store, task, "a\nb\n", 1, 2)
    untrusted = store.put_evidence(
        b"b\nc\n",
        revision="head",
        side="head",
        kind="source",
        path="a.py",
        start_line=2,
        end_line=3,
        metadata={"workspace_digest": "snapshot", "navigation_only": True},
    )
    store.pin_frontier_for_task(task.id, task.lease_token, untrusted.id)
    result = entries(reviewer)
    assert [item["kind"] for item in result] == ["source", "source"]


def test_conflicting_overlap_falls_back_without_losing_source(tmp_path):
    store, task, reviewer = board(tmp_path)
    add(store, task, "a\nb\n", 1, 2)
    add(store, task, "WRONG\nc\n", 2, 3)
    result = entries(reviewer)
    assert [item["kind"] for item in result] == ["source", "source"]
    assert [item["content"] for item in result] == ["a\nb\n", "WRONG\nc\n"]


def test_latest_tool_exchange_is_not_replaced_or_deduplicated(tmp_path):
    store, task, reviewer = board(tmp_path)
    ref, entry = add(store, task, "a\nb\n", 1, 2)
    add(store, task, "b\nc\n", 2, 3)
    tool = ToolMessage(
        content=json.dumps({"frontier_entry_id": entry.id, "evidence_ref": ref.id, "content": "a\nb\n"}),
        tool_call_id="read",
    )
    response = AIMessage(content="", tool_calls=[{"name": "read_source", "args": {}, "id": "read"}])
    projected = reviewer._checkpoint_context("a", [response, tool])
    assert projected[-1] == tool
    assert all(item["kind"] != "source_union" for item in json.loads(projected[2].content)["entries"])


def test_corrupt_blob_fails_closed_and_legacy_is_unchanged(tmp_path):
    store, task, reviewer = board(tmp_path / "new")
    ref, _ = add(store, task, "a\nb\n", 1, 2)
    add(store, task, "b\nc\n", 2, 3)
    (store.blob_dir / ref.content_hash).write_bytes(b"corrupt")
    with pytest.raises(FrontierUnavailableError):
        entries(reviewer)
    old, old_task, old_reviewer = board(tmp_path / "old", enabled=False)
    add(old, old_task, "a\nb\n", 1, 2)
    add(old, old_task, "b\nc\n", 2, 3)
    assert [item["kind"] for item in entries(old_reviewer)] == ["source", "source"]
