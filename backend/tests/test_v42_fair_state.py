"""Cooperative reviewers share immutable evidence, never task leases or delivery state."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from reviewforge.review_v42.contracts import BindingError, Case, LeaseError, ReviewScope, RevisionConflict, Run, Task
from reviewforge.review_v42.store import ReviewStore


@pytest.fixture
def board(tmp_path):
    regions = [
        {
            "id": name,
            "path": f"{name}.py",
            "side": "head",
            "start_line": 1,
            "end_line": 2,
            "base_start_line": 1,
            "base_end_line": 2,
            "changed_lines": [1],
            "patch": "",
        }
        for name in ("a", "b")
    ]
    run = Run(
        "run",
        "org/repo",
        "base",
        "head",
        policy="v42-units-6",
        metadata={
            "workspace_digest": "snapshot",
            "base_workspace_digest": "base-snapshot",
            "changed_regions": regions,
            "unit_scheduler": "cooperative-1",
        },
    )
    store = ReviewStore(tmp_path / "board.sqlite", run)
    units = [ReviewScope(name, "run", name, regions=[region]) for name, region in zip(("a", "b"), regions)]
    store.install_unit_plan(units, ReviewScope("integration", "run", "integration", dependencies=["a", "b"]))
    for name in ("a", "b"):
        store.add(Task("task-" + name, "run", "review", name, 0))
    leases = {name: store.acquire_task("task-" + name, "worker-" + name, 100, now=0).lease_token for name in ("a", "b")}
    return store, leases


def source(store, path, text=b"first\nsecond\n"):
    return store.put_evidence(
        text,
        revision="head",
        side="head",
        kind="source",
        path=path,
        start_line=1,
        end_line=2,
        metadata={"workspace_digest": "snapshot"},
    )


def audit(store, task_id, token, turn_id, refs, *, now=1):
    messages = []
    for ref in refs:
        receipt = {
            "evidence_ref": ref.id,
            "content": store.read_evidence(ref.id).decode(),
            "start_line": ref.start_line,
            "end_line": ref.end_line,
        }
        messages.append({"type": "tool", "data": {"content": json.dumps(receipt)}})
    store.append_turn_audit(task_id, token, turn_id, "request", messages, now=now)
    store.append_turn_audit(task_id, token, turn_id, "response", [{"type": "ai", "data": {"content": "ok"}}], now=now)


def decision(store, name, token, ref, turn_id, *, now=2):
    tid = "task-" + name
    return store.commit_unit_decision(
        tid,
        token,
        store.get(ReviewScope, name).revision,
        "decision-" + name,
        checked_region_ids=[name],
        observations=[{"statement": "inspected", "evidence_refs": [ref.id], "region_ids": [name]}],
        questions=[],
        cases=[],
        unresolved=[],
        finish=True,
        turn_id=turn_id,
        now=now,
    )


def test_two_live_units_share_blob_but_not_frontier_session_or_audit(board):
    store, leases = board
    gate = threading.Barrier(2)

    def worker(name):
        gate.wait(timeout=10)
        ref = source(store, "shared.py")
        entry = store.pin_frontier_for_task("task-" + name, leases[name], ref.id, now=1)
        session = [{"type": "human", "data": {"content": "private-" + name}}]
        store.save_task_session("task-" + name, leases[name], session, now=1)
        audit(store, "task-" + name, leases[name], "same-turn-id", [ref])
        store.renew_task("task-" + name, leases[name], 100, now=2)
        return ref, entry, session

    with ThreadPoolExecutor(max_workers=2) as pool:
        result = list(pool.map(worker, ("a", "b")))
    assert result[0][0].id == result[1][0].id
    assert result[0][1].id != result[1][1].id
    for idx, name in enumerate(("a", "b")):
        assert store.list_frontier("task-" + name) == [result[idx][1]]
        assert store.load_task_session("task-" + name) == result[idx][2]
        assert [x["phase"] for x in store.read_turn_audit("task-" + name)] == ["request", "response"]
    restarted = ReviewStore(store.path, store.run)
    assert restarted.load_task_session("task-a") == result[0][2]
    assert restarted.read_evidence(result[1][0].id) == b"first\nsecond\n"


def test_shared_directory_is_not_delivery_and_explicit_cross_unit_read_is(board):
    store, leases = board
    ref = source(store, "b.py")
    store.pin_frontier_for_task("task-b", leases["b"], ref.id, now=1)
    shared = store.query_shared(path="b.py", limit=10)
    assert shared["items"][0]["id"] == ref.id
    board_message = [{"type": "human", "data": {"content": json.dumps(shared)}}]
    store.append_turn_audit("task-a", leases["a"], "directory", "request", board_message, now=1)
    store.append_turn_audit(
        "task-a", leases["a"], "directory", "response", [{"type": "ai", "data": {"content": "ok"}}], now=1
    )
    case = Case("case-a", "run", scope_id="a", trigger="cross-unit", evidence_refs=[ref.id])
    kwargs = dict(checked_region_ids=[], observations=[], questions=[], cases=[case], unresolved=[])
    with pytest.raises(BindingError):
        store.commit_unit_decision("task-a", leases["a"], 0, "not-read", **kwargs, turn_id="directory", now=2)
    store.pin_frontier_for_task("task-a", leases["a"], ref.id, now=2)
    audit(store, "task-a", leases["a"], "actually-read", [ref], now=2)
    result = store.commit_unit_decision("task-a", leases["a"], 0, "read", **kwargs, turn_id="actually-read", now=3)
    assert result["case_ids"] == ["case-a"]


def test_verifier_new_source_fences_only_affected_unit_while_other_commits(board):
    store, leases = board
    a, b = source(store, "a.py"), source(store, "b.py")
    for name, ref in (("a", a), ("b", b)):
        store.pin_frontier_for_task("task-" + name, leases[name], ref.id, now=1)
        audit(store, "task-" + name, leases[name], "before", [ref])
    case = Case("case-a", "run", scope_id="a", trigger="trigger", evidence_refs=[a.id])
    store.commit_unit_decision(
        "task-a",
        leases["a"],
        0,
        "candidate",
        checked_region_ids=[],
        observations=[],
        questions=[],
        cases=[case],
        unresolved=[],
        turn_id="before",
        now=2,
    )
    store.add(Task("verify", "run", "verify", case.id, 0))
    verify = store.acquire_task("verify", "verifier", 100, now=2)
    new = source(store, "a.py", b"new one\nnew two\n")
    gate = threading.Barrier(2)

    def verify_new():
        gate.wait(timeout=10)
        return store.record_verification_observations("verify", verify.lease_token, 0, [], [a.id, new.id], now=3)

    def finish_other():
        gate.wait(timeout=10)
        return decision(store, "b", leases["b"], b, "before", now=3)

    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = [pool.submit(verify_new), pool.submit(finish_other)]
        [future.result(timeout=20) for future in pending]
    assert store.get(ReviewScope, "b").status == "reviewed"
    assert store.get(ReviewScope, "a").status == "pending"
    assert store.pending_unit_cases() == [{"case_id": "case-a", "scope_id": "a", "obligation_id": ""}]
    with pytest.raises(RevisionConflict):
        store.update_scope_for_task("task-a", leases["a"], 1, status="running", now=4)


def test_recovery_preserves_exchange_but_old_lease_turn_cannot_commit(board):
    store, leases = board
    ref = source(store, "a.py")
    store.pin_frontier_for_task("task-a", leases["a"], ref.id, now=1)
    audit(store, "task-a", leases["a"], "old-turn", [ref])
    exchange = [
        {"type": "ai", "data": {"tool_calls": [{"name": "read_source"}]}},
        {"type": "tool", "data": {"content": "complete"}},
    ]
    store.save_task_session("task-a", leases["a"], exchange, now=1)
    replacement = store.acquire_task("task-a", "replacement", 100, now=101)
    restored = ReviewStore(store.path, store.run)
    assert restored.load_task_session("task-a") == exchange
    with pytest.raises(LeaseError):
        decision(restored, "a", leases["a"], ref, "old-turn", now=102)
    with pytest.raises(LeaseError):
        decision(restored, "a", replacement.lease_token, ref, "old-turn", now=102)
    audit(restored, "task-a", replacement.lease_token, "new-turn", [ref], now=102)
    assert decision(restored, "a", replacement.lease_token, ref, "new-turn", now=103)["scope"].status == "reviewed"
    assert len(restored.history("UnitDecision", "decision-a")) == 1


def test_scheduler_identity_cannot_silently_change_an_existing_checkpoint(board):
    store, _ = board
    changed = replace(store.run, metadata={**store.run.metadata, "unit_scheduler": "serial"})
    with pytest.raises(BindingError):
        ReviewStore(store.path, changed)
