"""Fail-closed unit coverage and integration publication checks."""

import json
import sqlite3

import pytest

from reviewforge.review_v42.contracts import ReviewScope, Run, Task
from reviewforge.review_v42.publication import Publication, PublicationOutbox
from reviewforge.review_v42.store import ReviewStore


def region(identity, path, **kwargs):
    return {
        "id": identity,
        "path": path,
        "side": "head",
        "start_line": 1,
        "end_line": 1,
        "changed_lines": [1],
        **kwargs,
    }


def scope(identity, regions, checked=None):
    return ReviewScope(
        identity,
        "run",
        identity,
        regions=regions,
        checked_regions=regions if checked is None else checked,
        status="reviewed",
    )


def report(tmp_path, regions, scopes):
    store = ReviewStore(
        tmp_path / "unit.sqlite",
        Run("run", "repo", "base", "head", policy="v42-units-6", metadata={"changed_regions": regions}),
    )
    for item in scopes:
        store.add(item)
    return Publication(store).local_report()


def deliver(store, task, refs, turn):
    messages = []
    for ref in refs:
        store.pin_frontier_for_task(task.id, task.lease_token, ref.id)
        content = {
            "evidence_ref": ref.id,
            "content": store.read_evidence(ref.id).decode(),
            "start_line": ref.start_line,
            "end_line": ref.end_line,
        }
        messages.append({"type": "tool", "data": {"content": json.dumps(content)}})
    store.append_turn_audit(task.id, task.lease_token, turn, "request", messages)
    store.append_turn_audit(task.id, task.lease_token, turn, "response", [{"type": "ai", "data": {"content": "done"}}])


def test_empty_integration_scope_and_empty_case_cannot_complete(tmp_path):
    one, two = region("a", "a.py"), region("b", "b.py")
    result = report(tmp_path, [one, two], [scope("one", [one]), scope("two", [two]), scope("fake-integration", [])])
    assert result["status"] == "partial"
    assert any(item.get("kind") == "integration" for item in result["unresolved"])
    assert result["candidates"] == 0 and result["comments"] == []


@pytest.mark.parametrize("scenario", ["missing", "duplicate", "forged_checked", "unsupported", "empty_manifest"])
def test_unit_manifest_defects_never_report_complete(tmp_path, scenario):
    one, two = region("a", "a.py"), region("b", "b.py")
    manifest = [one, two]
    scopes = [scope("one", [one]), scope("two", [two])]
    if scenario == "missing":
        scopes.pop()
    elif scenario == "duplicate":
        scopes.append(scope("extra", [one]))
    elif scenario == "forged_checked":
        scopes[0] = scope("one", [one], checked=[{**one, "path": "elsewhere.py"}])
    elif scenario == "unsupported":
        one = {**one, "unsupported": True}
        manifest[0] = one
        scopes[0] = scope("one", [one])
    elif scenario == "empty_manifest":
        manifest = []
    result = report(tmp_path, manifest, scopes)
    assert result["status"] == "partial"
    assert any(item.get("kind") == "unit_coverage" for item in result["unresolved"])


@pytest.fixture
def completed_units(tmp_path, request):
    register_dependency = getattr(request, "param", False)
    one, two = region("a", "a.py"), region("b", "b.py")
    store = ReviewStore(
        tmp_path / "lifecycle.sqlite",
        Run(
            "run",
            "repo",
            "base",
            "head",
            policy="v42-units-6",
            metadata={
                "changed_regions": [one, two],
                "workspace_digest": "workspace",
                "base_workspace_digest": "base-workspace",
            },
        ),
    )
    store.install_unit_plan(
        [ReviewScope("one", "run", "one", regions=[one]), ReviewScope("two", "run", "two", regions=[two])],
        ReviewScope("integration", "run", "integration", dependencies=["one", "two"]),
    )
    refs = {}
    for identity, assigned in (("one", one), ("two", two)):
        refs[identity] = store.put_evidence(
            (identity + " original source\n").encode(),
            revision="head",
            side="head",
            kind="source",
            path=assigned["path"],
            start_line=1,
            end_line=1,
            metadata={"workspace_digest": "workspace"},
        )
        task_id = "task-" + identity
        store.add(Task(task_id, "run", "review", identity, 0))
        task = store.acquire_task(task_id, "reviewer", 300)
        deliver(store, task, [refs[identity]], "turn-" + identity)
        if register_dependency and identity == "one":
            store.register_unit_dependency(
                task.id, task.lease_token, "two", "caller depends on consumer", [refs[identity].id], turn_id="turn-one"
            )
        store.commit_unit_decision(
            task_id,
            task.lease_token,
            0,
            "decision-" + identity,
            checked_region_ids=[assigned["id"]],
            observations=[
                {
                    "statement": "inspected original source",
                    "evidence_refs": [refs[identity].id],
                    "region_ids": [assigned["id"]],
                }
            ],
            questions=[],
            cases=[],
            unresolved=[],
            finish=True,
            turn_id="turn-" + identity,
        )
        store.complete_task(task.id, task.lease_token, {})
    store.add(Task("task-integration", "run", "review", "integration", 0))
    task = store.acquire_task("task-integration", "integrator", 300)
    deliver(store, task, list(refs.values()), "turn-integration")
    for obligation in store.list_integration_obligations():
        if register_dependency and obligation["kind"] == "discovered":
            continue
        store.record_integration_receipt(
            task.id,
            task.lease_token,
            obligation["id"],
            obligation["revision"],
            obligation["input_digest"],
            disposition="checked",
            evidence_refs=[refs[identity].id for identity in obligation["unit_ids"]],
            reason="inspected the source and interaction",
            turn_id="turn-integration",
        )
    return store, task, refs


@pytest.mark.parametrize("completed_units", [True], indirect=True)
def test_registered_dependency_requires_its_own_current_integration(completed_units):
    store, task, refs = completed_units
    discovered = next(o for o in store.list_integration_obligations() if o["kind"] == "discovered")
    assert not discovered["valid"]
    assert all(o["valid"] for o in store.list_integration_obligations() if o["kind"] != "discovered")
    with pytest.raises(ValueError, match="obligations"):
        store.finish_integration(task.id, task.lease_token, 0)
    assert Publication(store).local_report()["status"] == "partial"
    store.record_integration_receipt(
        task.id,
        task.lease_token,
        discovered["id"],
        discovered["revision"],
        discovered["input_digest"],
        disposition="checked",
        evidence_refs=[refs["one"].id, refs["two"].id],
        reason="reviewed the registered cross-unit dependency",
        turn_id="turn-integration",
    )
    store.finish_integration(task.id, task.lease_token, 0)
    store.complete_task(task.id, task.lease_token, {})
    assert Publication(store).local_report()["status"] == "complete"


def test_full_current_independent_integration_can_complete(completed_units):
    store, task, _ = completed_units
    store.finish_integration(task.id, task.lease_token, 0)
    store.complete_task(task.id, task.lease_token, {})
    assert Publication(store).local_report()["status"] == "complete"


def test_discovered_cross_unit_obligation_blocks_completion(completed_units):
    store, task, refs = completed_units
    store.add_integration_obligation(
        task.id,
        task.lease_token,
        ["one", "two"],
        "potential interaction",
        [refs["one"].id, refs["two"].id],
        turn_id="turn-integration",
    )
    report = Publication(store).local_report()
    assert report["status"] == "partial"
    assert any(item.get("kind") == "integration" for item in report["unresolved"])


@pytest.mark.parametrize(
    "defect",
    ["missing_unit", "missing_cross", "stale", "wrong_review", "wrong_source", "wrong_task", "open_discovered"],
)
def test_publication_rechecks_integrator_view(completed_units, monkeypatch, defect):
    store, task, refs = completed_units
    store.finish_integration(task.id, task.lease_token, 0)
    store.complete_task(task.id, task.lease_token, {})
    validate = store.validate_units_completion

    def forged(*, db=None):
        if db is None:
            with store._connect() as connection:
                connection.execute("BEGIN")
                return forged(db=connection)
        view = validate(db=db)
        assert view["valid"]
        if defect == "missing_unit":
            view["obligations"] = [
                x for x in view["obligations"] if not (x["kind"] == "unit" and x["unit_ids"] == ["one"])
            ]
        elif defect == "missing_cross":
            view["obligations"] = [x for x in view["obligations"] if x["kind"] != "cross_unit"]
        elif defect == "stale":
            view["obligations"][0]["receipt_digest"] = "old"
        elif defect == "wrong_review":
            view["integration_review"]["scope_id"] = "one"
        elif defect == "wrong_task":
            view["integration_tasks"] = []
        elif defect == "open_discovered":
            discovered = dict(next(x for x in view["obligations"] if x["kind"] == "cross_unit"))
            discovered.update(id="new-relationship", kind="discovered", valid=False, status="open")
            view["obligations"].append(discovered)
        else:
            cross = next(x for x in view["obligations"] if x["kind"] == "cross_unit")
            cross["evidence_refs"] = [refs["one"].id]
        return view

    monkeypatch.setattr(store, "validate_units_completion", forged)
    report = Publication(store).local_report()
    assert report["status"] == "partial"
    assert any(item.get("kind") == "integration" for item in report["unresolved"])


def test_outbox_freeze_rechecks_in_same_write_transaction(completed_units, monkeypatch):
    store, task, _ = completed_units
    store.finish_integration(task.id, task.lease_token, 0)
    store.complete_task(task.id, task.lease_token, {})
    validate = store.validate_units_completion
    visited = []

    def check_locked(*, db=None):
        assert db is not None and db.in_transaction
        with sqlite3.connect(store.path, timeout=0) as other:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                other.execute("BEGIN IMMEDIATE")
        visited.append(True)
        return validate(db=db)

    monkeypatch.setattr(store, "validate_units_completion", check_locked)
    frozen = PublicationOutbox(store).freeze_batch()
    assert visited and frozen["report"]["status"] == "complete"
