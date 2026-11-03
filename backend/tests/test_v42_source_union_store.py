import copy
import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from reviewforge.review_v42.contracts import BindingError, LeaseError, ReviewScope, Run, Task
from reviewforge.review_v42.store import ReviewStore


@pytest.fixture
def board(tmp_path):
    region = {"id": "r", "path": "a.py", "side": "head", "start_line": 1, "end_line": 4, "changed_lines": [1]}
    run = Run(
        "run",
        "org/repo",
        "base",
        "head",
        policy="v42-units-6",
        metadata={
            "workspace_digest": "workspace",
            "base_workspace_digest": "base-workspace",
            "changed_regions": [region],
            "shared_investigation": "shared-1",
            "source_line_policy": "git-lf-1",
            "frontier_projection_policy": "source-line-union-1",
        },
    )
    store = ReviewStore(tmp_path / "state.db", run)
    store.install_unit_plan(
        [ReviewScope("s", "run", "scope", regions=[region])], ReviewScope("i", "run", "integration", dependencies=["s"])
    )
    store.add(Task("t", "run", "review", "s", 0))
    token = store.acquire_task("t", "worker", 100, now=0).lease_token
    return store, token


def source(store, token, text, first, last, path="a.py", side="head"):
    ref = store.put_evidence(
        text.encode(),
        revision=side,
        side=side,
        kind="source",
        path=path,
        start_line=first,
        end_line=last,
        metadata={
            "workspace_digest": "workspace",
            "tool_name": "read_source",
            "tool_args": {"path": path, "side": side},
        },
    )
    entry = store.pin_frontier_for_task("t", token, ref.id, now=1)
    return ref, entry


def segment(sources, text, first=1, last=4, **changes):
    ref = sources[0][0]
    data = {
        "kind": "source_union",
        "repo": ref.repo,
        "source_revision": ref.revision,
        "side": ref.side,
        "path": ref.path,
        "environment": ref.environment,
        "workspace_digest": "workspace",
        "start_line": first,
        "end_line": last,
        "content": text,
        "content_hash": hashlib.sha256(text.encode()).hexdigest(),
        "mappings": [
            {
                "entry_id": entry.id,
                "ref_id": source.id,
                "start_line": max(first, source.start_line),
                "end_line": min(last, source.end_line),
            }
            for source, entry in sources
        ],
    }
    data.update(changes)
    return data


def turn(store, token, label, entries, *, response=True):
    request = [
        {
            "type": "human",
            "data": {"content": json.dumps({"frontier_revision": 1, "entries": entries}, ensure_ascii=False)},
        }
    ]
    store.append_turn_audit("t", token, label, "request", request, now=2)
    if response:
        store.append_turn_audit("t", token, label, "response", [{"type": "ai", "data": {"content": "ok"}}], now=2)


def delivered(store, token, turn_id, refs):
    with store._connect() as db:
        return store._require_delivered(db, "t", token, turn_id, set(refs))


def test_overlap_preserves_git_lines_and_exact_per_ref_coverage(board):
    store, token = board
    a = source(store, token, "one\r\ntwo\rX\u2028\v\nthree\n", 1, 3)
    b = source(store, token, "two\rX\u2028\v\nthree\nfour", 2, 4)
    block = segment([a, b], "one\r\ntwo\rX\u2028\v\nthree\nfour")
    turn(store, token, "ok", [block])
    assert delivered(store, token, "ok", [a[0].id, b[0].id]) == {a[0].id: [(1, 3)], b[0].id: [(2, 4)]}
    # A partial visible segment never implies either full ref was delivered.
    part = segment([a, b], "two\rX\u2028\v\nthree\n", 2, 3)
    turn(store, token, "part", [part])
    assert delivered(store, token, "part", [a[0].id, b[0].id]) == {a[0].id: [(2, 3)], b[0].id: [(2, 3)]}


@pytest.mark.parametrize("mutation", ["text", "hash", "range", "entry", "ref", "side", "workspace", "gap", "duplicate"])
def test_forged_union_never_counts_as_delivery(board, mutation):
    store, token = board
    a = source(store, token, "one\ntwo\nthree\n", 1, 3)
    b = source(store, token, "two\nthree\nfour\n", 2, 4)
    block = segment([a, b], "one\ntwo\nthree\nfour\n")
    if mutation == "text":
        block["content"] = block["content"].replace("two", "TWO")
        block["content_hash"] = hashlib.sha256(block["content"].encode()).hexdigest()
    elif mutation == "hash":
        block["content_hash"] = "0" * 64
    elif mutation == "range":
        block["mappings"][1]["start_line"] = 1
    elif mutation == "entry":
        block["mappings"][0]["entry_id"] = "frontier_fake"
    elif mutation == "ref":
        block["mappings"][0]["ref_id"] = b[0].id
    elif mutation == "side":
        block["side"] = "base"
    elif mutation == "workspace":
        block["workspace_digest"] = "other"
    elif mutation == "gap":
        block["mappings"] = [block["mappings"][0]]
    else:
        block["mappings"].append(copy.deepcopy(block["mappings"][0]))
    turn(store, token, mutation, [block])
    with pytest.raises(BindingError):
        delivered(store, token, mutation, [a[0].id, b[0].id])


def test_missing_response_wrong_lease_and_unpinned_alias_fail(board):
    store, token = board
    a = source(store, token, "one\ntwo\nthree\n", 1, 3)
    b = source(store, token, "two\nthree\nfour\n", 2, 4)
    block = segment([a, b], "one\ntwo\nthree\nfour\n")
    turn(store, token, "failed", [block], response=False)
    with pytest.raises(BindingError):
        delivered(store, token, "failed", [a[0].id])
    turn(store, token, "successful", [block])
    with pytest.raises(LeaseError):
        delivered(store, "wrong-token", "successful", [a[0].id])
    block["mappings"][0]["ref_id"] = "never-pinned"
    turn(store, token, "unbound", [block])
    with pytest.raises(BindingError):
        delivered(store, token, "unbound", [a[0].id])


def test_invalid_policy_fails_before_filesystem_creation_and_old_run_remains_legacy(tmp_path):
    base = Run(
        "run",
        "repo",
        "base",
        "head",
        policy="v42-units-6",
        metadata={"shared_investigation": "shared-1", "source_line_policy": "git-lf-1"},
    )
    for index, value in enumerate((None, "", False, "unknown")):
        path = tmp_path / f"invalid-{index}" / "state.db"
        with pytest.raises(BindingError):
            ReviewStore(path, replace(base, metadata={**base.metadata, "frontier_projection_policy": value}))
        assert not path.parent.exists()
    for key, value in (("shared_investigation", ""), ("source_line_policy", None)):
        path = tmp_path / key / "state.db"
        with pytest.raises(BindingError):
            ReviewStore(
                path,
                replace(
                    base, metadata={**base.metadata, key: value, "frontier_projection_policy": "source-line-union-1"}
                ),
            )
        assert not path.parent.exists()
    old = ReviewStore(tmp_path / "old" / "state.db", base)
    assert ReviewStore(old.path, base).run == base


def test_alias_added_later_does_not_retroactively_deliver_and_release_does_not_erase_audit(board):
    store, token = board
    a = source(store, token, "one\ntwo\nthree\n", 1, 3)
    b = source(store, token, "two\nthree\nfour\n", 2, 4)
    block = segment([a, b], "one\ntwo\nthree\nfour\n")
    turn(store, token, "old-request", [block])
    alias = store.put_evidence(
        b"one\ntwo\nthree\n",
        revision="head",
        side="head",
        kind="source",
        path="a.py",
        start_line=1,
        end_line=3,
        metadata={
            "workspace_digest": "workspace",
            "tool_name": "read_source",
            "tool_args": {"path": "a.py", "side": "head"},
            "request_marker": "another-alias",
        },
    )
    alias_entry = store.pin_frontier_for_task("t", token, alias.id, now=3)
    assert alias_entry.id == a[1].id and alias.id != a[0].id
    with pytest.raises(BindingError):
        delivered(store, token, "old-request", [alias.id])
    store.release_frontier_for_task(
        "t", token, [a[1].id], reason="processed", expected_revision=store.frontier_revision("t"), now=3
    )
    reopened = ReviewStore(store.path, store.run)
    assert delivered(reopened, token, "old-request", [a[0].id, b[0].id]) == {a[0].id: [(1, 3)], b[0].id: [(2, 4)]}
    # An explicitly shown alias can be counted after it is read under this task.
    store.pin_frontier_for_task("t", token, alias.id, now=4)
    with_alias = segment([a, b, (alias, alias_entry)], "one\ntwo\nthree\nfour\n")
    turn(store, token, "new-request", [with_alias])
    assert delivered(store, token, "new-request", [alias.id]) == {alias.id: [(1, 3)]}


def test_legacy_run_ignores_unrecognized_union_instead_of_changing_checkpoint(tmp_path):
    region = {"id": "r", "path": "a.py", "side": "head", "start_line": 1, "end_line": 2, "changed_lines": [1]}
    run = Run(
        "old",
        "repo",
        "base",
        "head",
        policy="v42-units-6",
        metadata={
            "workspace_digest": "workspace",
            "base_workspace_digest": "base-workspace",
            "changed_regions": [region],
            "shared_investigation": "shared-1",
            "source_line_policy": "git-lf-1",
        },
    )
    store = ReviewStore(tmp_path / "old.sqlite", run)
    store.install_unit_plan(
        [ReviewScope("s", "old", "scope", regions=[region])], ReviewScope("i", "old", "i", dependencies=["s"])
    )
    store.add(Task("t", "old", "review", "s", 0))
    token = store.acquire_task("t", "worker", 100, now=0).lease_token
    turn(store, token, "legacy", [{"kind": "source_union", "content": "not a valid union"}])
    assert delivered(store, token, "legacy", []) == {}
    with pytest.raises(BindingError):
        delivered(store, token, "legacy", ["not-a-ref"])


def test_conflicting_originals_do_not_allow_overlapping_source_delivery(board):
    store, token = board
    a = source(store, token, "one\ntwo\nthree\n", 1, 3)
    b = source(store, token, "DIFFERENT\nthree\nfour\n", 2, 4)
    block = segment([a, b], "one\ntwo\nthree\nfour\n")
    turn(store, token, "conflict", [block])
    with pytest.raises(BindingError):
        delivered(store, token, "conflict", [a[0].id, b[0].id])


def test_task_source_union_cannot_borrow_another_task_entry(board):
    store, token = board
    a = source(store, token, "one\ntwo\nthree\n", 1, 3)
    b = source(store, token, "two\nthree\nfour\n", 2, 4)
    store.add(Task("other", "run", "review", "i", 0))
    other_token = store.acquire_task("other", "worker", 100, now=0).lease_token
    other_entry = store.pin_frontier_for_task("other", other_token, a[0].id, now=1)
    block = segment([a, b], "one\ntwo\nthree\nfour\n")
    block["mappings"][0]["entry_id"] = other_entry.id
    turn(store, token, "other-task", [block])
    with pytest.raises(BindingError):
        delivered(store, token, "other-task", [a[0].id, b[0].id])


def test_actual_unit_decision_consumes_only_successfully_delivered_ranges(board):
    store, token = board
    a = source(store, token, "one\ntwo\nthree\n", 1, 3)
    b = source(store, token, "two\nthree\nfour\n", 2, 4)
    block = segment([a, b], "one\ntwo\nthree\nfour\n")
    turn(store, token, "decision", [block])
    outcome = store.commit_unit_decision(
        "t",
        token,
        0,
        "decision-one",
        checked_region_ids=["r"],
        observations=[{"statement": "source checked", "evidence_refs": [a[0].id, b[0].id], "region_ids": ["r"]}],
        questions=[],
        cases=[],
        unresolved=[],
        turn_id="decision",
        now=3,
    )
    assert outcome["scope"].checked_regions


def test_reacquired_lease_cannot_reuse_old_turn_delivery(board):
    store, token = board
    a = source(store, token, "one\ntwo\nthree\n", 1, 3)
    b = source(store, token, "two\nthree\nfour\n", 2, 4)
    block = segment([a, b], "one\ntwo\nthree\nfour\n")
    turn(store, token, "old-lease", [block])
    new_token = store.acquire_task("t", "new-worker", 100, now=101).lease_token
    observations = [{"statement": "read", "evidence_refs": [a[0].id, b[0].id], "region_ids": ["r"]}]
    with pytest.raises(LeaseError):
        store.commit_unit_decision(
            "t",
            token,
            0,
            "old",
            checked_region_ids=["r"],
            observations=observations,
            questions=[],
            cases=[],
            unresolved=[],
            turn_id="old-lease",
            now=102,
        )
    with pytest.raises(LeaseError):
        store.commit_unit_decision(
            "t",
            new_token,
            0,
            "wrong-owner",
            checked_region_ids=["r"],
            observations=observations,
            questions=[],
            cases=[],
            unresolved=[],
            turn_id="old-lease",
            now=102,
        )
    request = [
        {
            "type": "human",
            "data": {"content": json.dumps({"frontier_revision": store.frontier_revision("t"), "entries": [block]})},
        }
    ]
    store.append_turn_audit("t", new_token, "new-lease", "request", request, now=102)
    store.append_turn_audit(
        "t", new_token, "new-lease", "response", [{"type": "ai", "data": {"content": "ok"}}], now=102
    )
    result = store.commit_unit_decision(
        "t",
        new_token,
        0,
        "new",
        checked_region_ids=["r"],
        observations=observations,
        questions=[],
        cases=[],
        unresolved=[],
        turn_id="new-lease",
        now=103,
    )
    assert result["scope"].checked_regions


@pytest.mark.parametrize("timing", ["before_validation", "between_full_reads"])
def test_source_union_rejects_corruption_outside_visible_page(board, monkeypatch, timing):
    store, token = board
    original = b"one\ntwo\nthree\nfour\n"
    corrupt = b"one\ntwo\nthree\nFAIL\n"
    ref = store.put_evidence(
        original,
        revision="head",
        side="head",
        kind="source",
        path="a.py",
        start_line=1,
        end_line=4,
        metadata={
            "workspace_digest": "workspace",
            "tool_name": "read_source",
            "tool_args": {"path": "a.py", "side": "head"},
        },
    )
    entry = store.pin_frontier_for_task("t", token, ref.id, start_line=1, end_line=2, now=1)
    block = segment([(ref, entry)], "one\ntwo\n", first=1, last=2)
    turn(store, token, "corrupt-full-ref", [block])
    blob = store.blob_dir / ref.content_hash
    assert hashlib.sha256(original).hexdigest() == ref.content_hash
    assert corrupt.startswith(b"one\ntwo\n") and len(corrupt) == len(original)
    if timing == "before_validation":
        blob.write_bytes(corrupt)
    else:
        read_bytes = Path.read_bytes
        reads = []

        def changed_after_check(path):
            value = read_bytes(path)
            if path == blob:
                reads.append(1)
                if len(reads) == 2:
                    return corrupt
            return value

        monkeypatch.setattr(Path, "read_bytes", changed_after_check)
    with pytest.raises(BindingError) as failure:
        delivered(store, token, "corrupt-full-ref", [ref.id])
    assert "blob hash mismatch" in str(failure.value.__cause__)
    if timing == "between_full_reads":
        assert len(reads) == 2
