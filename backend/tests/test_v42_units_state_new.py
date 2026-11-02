import json
from dataclasses import replace

import pytest

from reviewforge.review_v42.context import ContextBuilder
from reviewforge.review_v42.contracts import BindingError, Case, LeaseError, ReviewScope, RevisionConflict, Run, Task
from reviewforge.review_v42.store import ReviewStore


@pytest.fixture
def board(tmp_path):
    regions = [
        {
            "id": "a",
            "path": "a.py",
            "side": "head",
            "start_line": 1,
            "end_line": 2,
            "base_start_line": 1,
            "base_end_line": 2,
            "changed_lines": [1],
            "patch": "@@ a",
        },
        {
            "id": "b",
            "path": "b.py",
            "side": "head",
            "start_line": 1,
            "end_line": 2,
            "base_start_line": 1,
            "base_end_line": 2,
            "changed_lines": [2],
            "patch": "@@ b",
        },
    ]
    run = Run(
        "r",
        "org/repo",
        "base",
        "head",
        policy="v42-units-6",
        metadata={
            "workspace_digest": "head-snapshot",
            "base_workspace_digest": "base-snapshot",
            "changed_regions": regions,
        },
    )
    store = ReviewStore(tmp_path / "board.db", run)
    units = [ReviewScope("u1", "r", "one", regions=[regions[0]]), ReviewScope("u2", "r", "two", regions=[regions[1]])]
    integration = ReviewScope("integrate", "r", "integration", dependencies=["u1", "u2"])
    store.install_unit_plan(units, integration)
    return store, units, integration


def task(store, sid):
    scope = store.get(ReviewScope, sid)
    tid = "task-" + sid
    store.add(Task(tid, "r", "review", sid, scope.revision))
    token = store.acquire_task(tid, "worker", 100, now=0).lease_token
    return tid, token


def src(store, path):
    return store.put_evidence(
        b"line one\nline two\n",
        revision="head",
        side="head",
        kind="source",
        path=path,
        start_line=1,
        end_line=2,
        metadata={"workspace_digest": "head-snapshot"},
    )


def turn(store, tid, token, turn_id, refs, *, response=True, pages=None):
    msgs = []
    for ref in refs:
        content = store.read_evidence(ref.id).decode()
        start, end = (pages or {}).get(ref.id, (ref.start_line, ref.end_line))
        if start != ref.start_line or end != ref.end_line:
            content = "".join(content.splitlines(keepends=True)[start - ref.start_line : end - ref.start_line + 1])
        receipt = {"evidence_ref": ref.id, "content": content, "start_line": start, "end_line": end}
        msgs.append({"type": "tool", "data": {"content": json.dumps(receipt)}})
    store.append_turn_audit(tid, token, turn_id, "request", msgs, now=1)
    if response:
        store.append_turn_audit(tid, token, turn_id, "response", [{"type": "ai", "data": {"content": "ok"}}], now=1)


def decide(store, tid, token, sid, ref, turn_id="turn", finish=True):
    return store.commit_unit_decision(
        tid,
        token,
        store.get(ReviewScope, sid).revision,
        "decision-" + sid,
        checked_region_ids=[{"u1": "a", "u2": "b"}[sid]],
        observations=[
            {"statement": "inspected", "evidence_refs": [ref.id], "region_ids": [{"u1": "a", "u2": "b"}[sid]]}
        ],
        questions=[],
        cases=[],
        unresolved=[],
        finish=finish,
        turn_id=turn_id,
        now=2,
    )


def test_plan_coverage_recovery_and_frontier_isolation(board):
    store, units, integration = board
    assert store.install_unit_plan(units, integration) == store.get_unit_plan()
    with pytest.raises(BindingError):
        store.install_unit_plan([units[0]], replace(integration, dependencies=["u1"]))
    t1, token = task(store, "u1")
    ref = src(store, "a.py")
    store.pin_frontier_for_task(t1, token, ref.id, now=1)
    assert [x["id"] for x in ContextBuilder(store).reviewer("u1")["evidence"]] == [ref.id]
    assert ContextBuilder(store).reviewer("u2")["evidence"] == []
    before = store.query_shared(limit=1)
    again = ReviewStore(store.path, store.run)
    assert again.get_unit_plan() == store.get_unit_plan()
    assert again.query_shared(limit=1)["items"] == before["items"]


def test_delivery_audit_cas_page_and_failed_request(board):
    store, _, _ = board
    tid, token = task(store, "u1")
    ref = src(store, "a.py")
    store.pin_frontier_for_task(tid, token, ref.id, now=1)
    turn(store, tid, token, "failure", [ref], response=False)
    with pytest.raises(BindingError):
        decide(store, tid, token, "u1", ref, "failure")
    turn(store, tid, token, "short", [ref], pages={ref.id: (1, 1)})
    with pytest.raises(BindingError):
        decide(store, tid, token, "u1", ref, "short")
    turn(store, tid, token, "good", [ref])
    result = decide(store, tid, token, "u1", ref, "good")
    assert result["scope"].status == "reviewed"
    assert decide(store, tid, token, "u1", ref, "good") == result
    with pytest.raises(RevisionConflict):
        store.commit_unit_decision(
            tid,
            token,
            result["scope"].revision,
            "decision-u1",
            checked_region_ids=[],
            observations=[],
            questions=[],
            cases=[],
            unresolved=[],
            turn_id="good",
            now=2,
        )
    with pytest.raises(BindingError):
        store.update_scope_for_task(tid, token, result["scope"].revision, checked_regions=[])


def test_full_integration_requires_receipts_and_actual_delivery(board):
    store, _, _ = board
    refs = []
    for sid, path in [("u1", "a.py"), ("u2", "b.py")]:
        tid, token = task(store, sid)
        ref = src(store, path)
        refs.append(ref)
        store.pin_frontier_for_task(tid, token, ref.id, now=1)
        turn(store, tid, token, "primary-" + sid, [ref])
        if sid == "u1":
            discovered = store.register_unit_dependency(
                tid, token, "u2", "a affects b", [ref.id], turn_id="primary-u1", now=2
            )
            repeated = store.register_unit_dependency(
                tid, token, "u2", "a affects b", [ref.id], turn_id="primary-u1", now=2
            )
            assert repeated["id"] == discovered["id"] and discovered["kind"] == "discovered"
        decide(store, tid, token, sid, ref, "primary-" + sid)
        store.complete_task(tid, token, {}, now=3)
    tid, token = task(store, "integrate")
    for ref in refs:
        store.pin_frontier_for_task(tid, token, ref.id, now=1)
    with pytest.raises(BindingError):
        store.finish_integration(tid, token, 0, now=2)
    turn(store, tid, token, "integrate-turn", refs)
    obligations = store.list_integration_obligations()
    for obligation in obligations:
        if obligation["kind"] == "discovered":
            continue
        involved = [refs[0] if sid == "u1" else refs[1] for sid in obligation["unit_ids"]]
        saved = store.record_integration_receipt(
            tid,
            token,
            obligation["id"],
            obligation["revision"],
            obligation["input_digest"],
            disposition="checked",
            evidence_refs=[r.id for r in involved],
            reason="inspected",
            turn_id="integrate-turn",
            now=2,
        )
        assert saved["valid"]
    with pytest.raises(BindingError):
        store.finish_integration(tid, token, 0, now=2)
    pending = next(item for item in store.list_integration_obligations() if item["kind"] == "discovered")
    store.record_integration_receipt(
        tid,
        token,
        pending["id"],
        pending["revision"],
        pending["input_digest"],
        disposition="checked",
        evidence_refs=[ref.id for ref in refs],
        reason="checked the related units",
        turn_id="integrate-turn",
        now=2,
    )
    assert store.finish_integration(tid, token, 0, now=3).status == "reviewed"
    store.complete_task(tid, token, {}, now=4)
    view = store.validate_units_completion()
    assert view["valid"], view["reasons"]
    assert view["owners"] == {"a": "u1", "b": "u2"}
    newer = store.put_evidence(
        b"changed source\n",
        revision="head",
        side="head",
        kind="source",
        path="a.py",
        start_line=1,
        end_line=1,
        metadata={"workspace_digest": "head-snapshot"},
    )
    current = store.get(ReviewScope, "u1")
    reopened = store.reopen_unit_for_evidence("u1", current.revision, [newer.id], "new source")
    assert reopened.status == "pending" and reopened.checked_regions == []
    assert store.get(ReviewScope, "integrate").status == "pending"
    assert not store.validate_units_completion()["valid"]
    obligations = store.list_integration_obligations()
    assert next(x for x in obligations if x["kind"] == "unit" and x["unit_ids"] == ["u1"])["status"] == "open"
    assert next(x for x in obligations if x["kind"] == "unit" and x["unit_ids"] == ["u2"])["status"] == "checked"


def test_cursor_snapshot_and_controller_tool_failure(board):
    store, _, _ = board
    tid, token = task(store, "u1")
    for name in ("a.py", "other.py"):
        ref = src(store, name)
        store.pin_frontier_for_task(tid, token, ref.id, now=1)
    first = store.query_shared(limit=1)
    third = src(store, "third.py")
    store.pin_frontier_for_task(tid, token, third.id, now=1)
    second = store.query_shared(cursor=first["next_cursor"], limit=10)
    assert len(first["items"] + second["items"]) == 2
    assert len(store.query_shared(limit=10)["items"]) == 3
    args = {"path": "a.py", "side": "head"}
    failure = store.record_tool_failure_for_task(tid, token, "read_source", args, now=1)
    assert failure.status == "unresolved"
    available = store.put_evidence(
        b"line one\nline two\n",
        revision="head",
        side="head",
        kind="source",
        path="a.py",
        start_line=1,
        end_line=2,
        metadata={"workspace_digest": "head-snapshot", "tool_name": "read_source", "tool_args": args},
    )
    with pytest.raises(BindingError):
        store.resolve_tool_failure_for_task(
            tid, token, failure.id, available.id, name="read_source", args={"path": "other.py"}, now=2
        )
    resolved = store.resolve_tool_failure_for_task(
        tid, token, failure.id, available.id, name="read_source", args=args, now=2
    )
    assert resolved.status == "answered" and resolved.evidence_refs == [available.id]


def test_verifier_new_source_fences_inflight_primary(board):
    store, _, _ = board
    tid, token = task(store, "u1")
    first = src(store, "a.py")
    store.pin_frontier_for_task(tid, token, first.id, now=1)
    turn(store, tid, token, "candidate", [first])
    case = Case("case", "r", scope_id="u1", trigger="trigger", evidence_refs=[first.id])
    store.commit_unit_decision(
        tid,
        token,
        0,
        "candidate",
        checked_region_ids=[],
        observations=[],
        questions=[],
        cases=[case],
        unresolved=[],
        turn_id="candidate",
        now=2,
    )
    stale_revision = store.get(ReviewScope, "u1").revision
    store.add(Task("verify", "r", "verify", "case", 0))
    verify = store.acquire_task("verify", "worker", 100, now=2)
    second = store.put_evidence(
        b"new source\n",
        revision="head",
        side="head",
        kind="source",
        path="a.py",
        start_line=1,
        end_line=1,
        metadata={"workspace_digest": "head-snapshot"},
    )
    store.record_verification_observations("verify", verify.lease_token, 0, [], [first.id, second.id], now=3)
    assert store.get(ReviewScope, "u1").revision == stale_revision + 1
    assert store.pending_unit_cases() == [{"case_id": "case", "scope_id": "u1", "obligation_id": ""}]
    assert store.pending_unit_sources() == [{"scope_id": "u1", "region_id": "a", "ref_id": second.id}]
    with pytest.raises(RevisionConflict):
        store.commit_unit_decision(
            tid,
            token,
            stale_revision,
            "stale",
            checked_region_ids=[],
            observations=[],
            questions=[],
            cases=[],
            unresolved=[],
            turn_id="candidate",
            now=4,
        )


def test_reopened_unit_ignores_unreplayed_history_but_requires_current_delivery(board):
    store, _, _ = board
    old_task, old_token = task(store, "u1")
    first = src(store, "a.py")
    store.pin_frontier_for_task(old_task, old_token, first.id, now=1)
    turn(store, old_task, old_token, "old-turn", [first])
    decide(store, old_task, old_token, "u1", first, "old-turn")
    store.complete_task(old_task, old_token, {}, now=3)
    alias = store.put_evidence(
        store.read_evidence(first.id),
        revision="head",
        side="head",
        kind="source",
        path="a.py",
        start_line=1,
        end_line=2,
        metadata={"workspace_digest": "head-snapshot", "timestamp": "later"},
    )
    with pytest.raises(BindingError):
        store.reopen_unit_for_evidence("u1", store.get(ReviewScope, "u1").revision, [alias.id], "same source")
    newer = store.put_evidence(
        b"new one\nnew two\n",
        revision="head",
        side="head",
        kind="source",
        path="a.py",
        start_line=1,
        end_line=2,
        metadata={"workspace_digest": "head-snapshot"},
    )
    reopened = store.reopen_unit_for_evidence("u1", store.get(ReviewScope, "u1").revision, [newer.id], "new source")
    store.add(Task("task-u1-new", "r", "review", "u1", reopened.revision))
    new_token = store.acquire_task("task-u1-new", "worker-new", 100, now=0).lease_token
    store.pin_frontier_for_task("task-u1-new", new_token, first.id, now=1)
    turn(store, "task-u1-new", new_token, "old-again", [first])
    with pytest.raises(BindingError):
        store.commit_unit_decision(
            "task-u1-new",
            new_token,
            reopened.revision,
            "missing-new-source",
            checked_region_ids=["a"],
            observations=[],
            questions=[],
            cases=[],
            unresolved=[],
            turn_id="old-again",
            finish=True,
            now=2,
        )
    store.pin_frontier_for_task("task-u1-new", new_token, newer.id, now=1)
    turn(store, "task-u1-new", new_token, "new-turn", [newer])
    with pytest.raises(BindingError):
        store.commit_unit_decision(
            "task-u1-new",
            new_token,
            reopened.revision,
            "cannot-use-old",
            checked_region_ids=["a"],
            observations=[],
            questions=[],
            cases=[],
            unresolved=[],
            turn_id="new-turn",
            finish=True,
            now=2,
        )
    result = store.commit_unit_decision(
        "task-u1-new",
        new_token,
        reopened.revision,
        "new-decision",
        checked_region_ids=["a"],
        observations=[{"statement": "newly inspected", "evidence_refs": [newer.id], "region_ids": ["a"]}],
        questions=[],
        cases=[],
        unresolved=[],
        turn_id="new-turn",
        finish=True,
        now=2,
    )
    assert result["scope"].status == "reviewed"
    assert store.pending_unit_sources() == []
    assert store.history("UnitDecision", "decision-u1")[0]["payload"]["observations"][0]["evidence_refs"] == [first.id]
    with pytest.raises((BindingError, LeaseError, RevisionConflict)):
        decide(store, old_task, old_token, "u1", first, "old-turn")
