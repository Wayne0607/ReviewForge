"""Shared work must not outlive its task lease while waiting or committing."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from reviewforge.review_v42.contracts import LeaseError, ReviewScope, Run, Task
from reviewforge.review_v42.shared_investigation import SharedInvestigations
from reviewforge.review_v42.store import ReviewStore


def setup_shared(tmp_path, kind):
    region = {"id": "r", "path": "a.py", "side": "head", "start_line": 1, "end_line": 1, "changed_lines": [1]}
    run = Run(
        "run",
        "org/repo",
        "base",
        "head",
        policy="v42-units-6",
        metadata={
            "workspace_digest": "head",
            "base_workspace_digest": "base",
            "changed_regions": [region],
            "shared_investigation": "shared-1",
        },
    )
    store = ReviewStore(tmp_path / "board.db", run)
    scope = ReviewScope("scope", run.id, "scope", regions=[region])
    store.install_unit_plan([scope], ReviewScope("integration", run.id, "integration", dependencies=[scope.id]))
    store.add(Task("task", run.id, "review", scope.id, 0))
    task = store.acquire_task("task", "worker", 20)
    store.append_turn_audit(task.id, task.lease_token, "turn", "request", [])
    store.append_turn_audit(task.id, task.lease_token, "turn", "response", [{"type": "ai", "data": {"content": ""}}])
    api = SharedInvestigations(store)
    item = None
    if kind != "create":
        item = api.create(task.id, task.lease_token, "question", [scope.id], ["r"], turn_id="turn", tool_call_id="init")
    if kind in {"update", "release"}:
        item = api.claim(task.id, task.lease_token, item["id"], item["revision"], "claim", operation_id="claim")
    return store, api, task, item


def invoke(api, task, item, kind):
    token, tid = task.lease_token, task.id
    if kind == "create":
        return api.create(tid, token, "question", ["scope"], ["r"], turn_id="turn", tool_call_id="create")
    if kind == "claim":
        return api.claim(tid, token, item["id"], item["revision"], "claim", operation_id="new-claim")
    if kind == "update":
        return api.update(
            tid,
            token,
            item["id"],
            item["revision"],
            question="question",
            observations=[],
            open_questions=["open"],
            status="partial",
            reason="update",
            turn_id="turn",
            operation_id="update",
        )
    if kind == "release":
        return api.release(tid, token, item["id"], item["revision"], "release", operation_id="release")
    if kind == "list":
        return api.list(tid, token, limit=10)
    if kind == "list_cases":
        return api.list_cases(tid, token, limit=10)
    if kind == "get":
        return api.get(tid, token, item["id"])
    if kind == "preview":
        return api.preview(tid, token)
    raise AssertionError(kind)


def durable_state(store):
    with store._connect() as db:
        return tuple(
            db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
            for table in (
                "shared_investigations",
                "shared_investigation_operations",
                "shared_investigation_snapshots",
                "events",
            )
        ), tuple(db.execute("SELECT id,revision,payload FROM shared_investigations ORDER BY id"))


@pytest.mark.parametrize("kind", ["create", "claim", "update", "release", "list", "list_cases"])
def test_waiting_for_real_sqlite_write_lock_past_expiry_cannot_write(tmp_path, kind):
    store, api, task, item = setup_shared(tmp_path, kind)
    store.renew_task(task.id, task.lease_token, 0.5)
    before = durable_state(store)
    locked = store._connect()
    locked.execute("BEGIN IMMEDIATE")
    started = threading.Event()

    def run():
        started.set()
        return invoke(api, task, item, kind)

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(run)
            assert started.wait(2)
            time.sleep(0.7)
            locked.commit()
            with pytest.raises(LeaseError):
                result.result(timeout=5)
    finally:
        locked.close()
    assert durable_state(store) == before


@pytest.mark.parametrize("kind", ["create", "claim", "update", "release", "list", "list_cases", "get", "preview"])
def test_slow_in_transaction_read_expiring_lease_rolls_back_or_does_not_deliver(tmp_path, monkeypatch, kind):
    store, api, task, item = setup_shared(tmp_path, kind)
    store.renew_task(task.id, task.lease_token, 0.35)
    before = durable_state(store)
    original = api._task
    first = True

    def slow(db, task_id, token, now):
        nonlocal first
        value = original(db, task_id, token, now)
        if first:
            first = False
            time.sleep(0.45)
        return value

    monkeypatch.setattr(api, "_task", slow)
    with pytest.raises(LeaseError):
        invoke(api, task, item, kind)
    assert durable_state(store) == before


def test_heartbeat_before_lock_extends_real_lease(tmp_path):
    store, api, task, item = setup_shared(tmp_path, "create")
    short = store.renew_task(task.id, task.lease_token, 0.4)
    extended = store.renew_task(task.id, task.lease_token, 3)
    assert extended.lease_expires > short.lease_expires
    locked = store._connect()
    locked.execute("BEGIN IMMEDIATE")
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(invoke, api, task, item, "create")
            time.sleep(0.6)
            assert time.time() > short.lease_expires
            locked.commit()
            assert result.result(timeout=5)["status"] == "open"
    finally:
        locked.close()


@pytest.mark.parametrize("kind", ["claim", "update", "release"])
def test_expiry_after_writes_but_before_commit_rolls_back_everything(tmp_path, monkeypatch, kind):
    store, api, task, item = setup_shared(tmp_path, kind)
    store.renew_task(task.id, task.lease_token, 0.35)
    before = durable_state(store)
    original = api._save_operation

    def slow_save(*args):
        original(*args)
        time.sleep(0.45)

    monkeypatch.setattr(api, "_save_operation", slow_save)
    with pytest.raises(LeaseError):
        invoke(api, task, item, kind)
    assert durable_state(store) == before


def test_explicit_now_remains_a_fixed_test_clock(tmp_path, monkeypatch):
    store, api, task, _ = setup_shared(tmp_path, "create")
    explicit = task.lease_expires - 1
    monkeypatch.setattr("reviewforge.review_v42.shared_investigation.time.time", lambda: task.lease_expires + 10)
    assert (
        api.create(
            task.id, task.lease_token, "question", ["scope"], ["r"], turn_id="turn", tool_call_id="fixed", now=explicit
        )["status"]
        == "open"
    )
