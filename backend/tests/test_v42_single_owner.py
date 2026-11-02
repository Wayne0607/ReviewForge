"""Opt-in ownership changes organization, not proof or integration gates."""

from collections import defaultdict
from dataclasses import replace

import pytest
from test_v42_integration import NativeModel, product, tool
from test_v42_units_product import FixedBaseGitHub, TwoFileWorkspace, last_receipt, two_file_state, unit_context
from test_v42_units_state_new import task, turn

from reviewforge.review_v42.contracts import BindingError, ReviewScope, Run
from reviewforge.review_v42.runtime import ReviewRuntime
from reviewforge.review_v42.scope import compile_scopes
from reviewforge.review_v42.store import ReviewStore


def manifest():
    return [
        {
            "id": "a1",
            "path": "a.py",
            "side": "head",
            "start_line": 1,
            "end_line": 1,
            "base_start_line": 1,
            "base_end_line": 1,
            "changed_lines": [1],
            "patch": "@@ first",
        },
        {
            "id": "a2",
            "path": "a.py",
            "side": "head",
            "start_line": 3,
            "end_line": 3,
            "base_start_line": 3,
            "base_end_line": 3,
            "changed_lines": [3],
            "patch": "@@ second",
        },
        {
            "id": "deleted",
            "path": "old.py",
            "side": "head",
            "start_line": 0,
            "end_line": 0,
            "base_start_line": 1,
            "base_end_line": 1,
            "changed_lines": [],
            "change": "deleted",
            "patch": "@@ delete",
        },
        {"id": "binary", "path": "image.bin", "side": "head", "unsupported": True, "patch": ""},
    ]


def run_for(regions, *, plan=None, policy="v42-units-6"):
    metadata = {
        "changed_regions": regions,
        "workspace_digest": "head-snapshot",
        "base_workspace_digest": "base-snapshot",
    }
    if plan is not None:
        metadata["unit_plan"] = plan
    return Run("r", "org/repo", "base", "head", policy=policy, metadata=metadata)


def test_single_owner_preserves_complete_manifest_and_existing_grouping():
    regions = manifest()
    single = run_for(regions, plan="single-owner-1")
    scopes = compile_scopes(single, regions)
    assert len(scopes) == 1
    assert {r["id"]: r for r in scopes[0].regions} == {r["id"]: r for r in regions}
    assert scopes[0].regions == compile_scopes(single, list(reversed(regions)))[0].regions
    assert scopes[0].id == compile_scopes(single, list(reversed(regions)))[0].id
    assert len(compile_scopes(run_for(regions), regions)) == 3
    assert len(compile_scopes(run_for(regions, policy="v42-frontier-5"), regions)) == 1
    with pytest.raises(ValueError, match="duplicate region"):
        compile_scopes(single, [regions[0], regions[0]])
    with pytest.raises(ValueError, match="transaction_id"):
        compile_scopes(single, [{**regions[0], "transaction_id": ""}])
    with pytest.raises(ValueError, match="unsupported v4.2 unit plan"):
        compile_scopes(run_for([], plan="unknown"), [])


def test_single_owner_cannot_mark_unavailable_region_and_requires_integration(tmp_path):
    regions = manifest()
    run = run_for(regions, plan="single-owner-1")
    store = ReviewStore(tmp_path / "board.db", run)
    runtime = ReviewRuntime(store, None, None, None)
    scope = runtime.prepare(regions)[0]
    plan = store.get_unit_plan()
    assert plan.primary_scope_ids == [scope.id]
    assert len(store.list_integration_obligations()) >= 2
    assert store.get(ReviewScope, plan.integration_scope_id).dependencies == [scope.id]
    assert runtime.prepare(regions)[0].id == scope.id
    with pytest.raises(BindingError):
        ReviewStore(store.path, replace(run, metadata={**run.metadata, "unit_plan": "different"}))
    tid, token = task(store, scope.id)
    deleted_ref = store.put_evidence(
        b"old line\n",
        revision="base",
        side="base",
        kind="source",
        path="old.py",
        start_line=1,
        end_line=1,
        metadata={"workspace_digest": "head-snapshot"},
    )
    store.pin_frontier_for_task(tid, token, deleted_ref.id, now=1)
    turn(store, tid, token, "received", [deleted_ref])
    observation = {
        "statement": "Inspected deleted source.",
        "evidence_refs": [deleted_ref.id],
        "region_ids": ["deleted"],
    }
    kwargs = dict(observations=[observation], questions=[], cases=[], unresolved=[], turn_id="received", now=2)
    result = store.commit_unit_decision(
        tid, token, scope.revision, "delete-decision", checked_region_ids=["deleted"], finish=True, **kwargs
    )
    assert result["scope"].status == "partial"
    assert [r["id"] for r in result["scope"].checked_regions] == ["deleted"]
    with pytest.raises(BindingError, match="available original source"):
        store.commit_unit_decision(
            tid, token, result["scope"].revision, "bad", checked_region_ids=["binary"], finish=True, **kwargs
        )


@pytest.mark.asyncio
async def test_product_single_owner_full_delivery_integration_and_run_identity(tmp_path, monkeypatch):
    monkeypatch.setenv("REVIEWFORGE_V42_UNIT_PLAN", "single-owner-1")
    monkeypatch.delenv("REVIEWFORGE_V42_UNIT_SCHEDULER", raising=False)
    monkeypatch.delenv("REVIEWFORGE_V42_SHARED_INVESTIGATION", raising=False)
    phases, refs = defaultdict(int), defaultdict(dict)
    primary_paths, integration_calls = [], []

    def review(messages, _turn):
        state = unit_context(messages)
        sid, role = state["scope"]["id"], state["unit_role"]
        phase = phases[sid]
        phases[sid] += 1
        if phase in (0, 1):
            if phase:
                refs[sid]["a.py"] = last_receipt(messages)["evidence_ref"]
            return tool("read_source", {"path": ("a.py", "b.py")[phase], "side": "head"})
        if phase == 2:
            refs[sid]["b.py"] = last_receipt(messages)["evidence_ref"]
        if role == "primary":
            regions = state["scope"]["regions"]
            primary_paths.append({r["path"] for r in regions})
            observations = [
                {
                    "statement": "Inspected fixed source.",
                    "evidence_refs": [refs[sid][r["path"]]],
                    "region_ids": [r["id"]],
                }
                for r in regions
            ]
            return tool(
                "submit_unit_decision",
                {
                    "checked_region_ids": [r["id"] for r in regions],
                    "observations": observations,
                    "unresolved_refs": [],
                    "finish": True,
                },
            )
        outstanding = [o for o in state["integration_obligations"] if o["status"] != "checked"]
        if outstanding:
            integration_calls.append(outstanding[0]["id"])
            return tool(
                "record_integration_receipt",
                {
                    "obligation_id": outstanding[0]["id"],
                    "disposition": "checked",
                    "evidence_refs": list(refs[sid].values()),
                    "reason": "Inspected the change and its consumer.",
                },
            )
        return tool("finish_integration", {})

    reviewer = NativeModel(review)
    verifier = NativeModel(lambda *_: pytest.fail("no candidate requires verification"))
    obj, _ = product(tmp_path, monkeypatch, reviewer, verifier, TwoFileWorkspace(), policy=None)
    obj._gateway._github = FixedBaseGitHub()
    result = await obj.run(two_file_state())
    assert result["status"] == "completed", result
    assert primary_paths == [{"a.py", "b.py"}]
    assert len(integration_calls) >= 2
    report = result["review_v42_report"]
    assert report["run"]["metadata"]["unit_plan"] == "single-owner-1"
    assert report["metrics"]["coverage"]["checked_regions"] == 2
    calls = len(reviewer._inputs)
    resumed = await obj.run(two_file_state())
    assert resumed["run_id"] == result["run_id"] and len(reviewer._inputs) == calls
    monkeypatch.delenv("REVIEWFORGE_V42_UNIT_PLAN")
    other = await obj.run(two_file_state())
    assert other["run_id"] != result["run_id"]
    assert "unit_plan" not in other["review_v42_report"]["run"]["metadata"]
    assert "shared_investigation" not in other["review_v42_report"]["run"]["metadata"]
    monkeypatch.setenv("REVIEWFORGE_V42_SHARED_INVESTIGATION", "shared-1")
    shared = await obj.run(two_file_state())
    assert shared["run_id"] != other["run_id"]
    assert shared["review_v42_report"]["run"]["metadata"]["shared_investigation"] == "shared-1"
    calls = len(reviewer._inputs)
    assert (await obj.run(two_file_state()))["run_id"] == shared["run_id"]
    assert len(reviewer._inputs) == calls


@pytest.mark.asyncio
async def test_product_rejects_unknown_plan_before_any_model_call(tmp_path, monkeypatch):
    monkeypatch.setenv("REVIEWFORGE_V42_UNIT_PLAN", "unknown")
    model = NativeModel(lambda *_: pytest.fail("invalid plan must not call the model"))
    obj, _ = product(tmp_path, monkeypatch, model, model, policy=None)
    with pytest.raises(ValueError, match="unsupported v4.2 unit plan"):
        await obj.run(two_file_state())
    assert not model._inputs


@pytest.mark.asyncio
@pytest.mark.parametrize("policy, value", [("v42-units-6", "bad"), ("v42-frontier-5", "shared-1")])
async def test_product_rejects_invalid_shared_investigation(tmp_path, monkeypatch, policy, value):
    monkeypatch.delenv("REVIEWFORGE_V42_UNIT_PLAN", raising=False)
    monkeypatch.setenv("REVIEWFORGE_V42_SHARED_INVESTIGATION", value)
    model = NativeModel(lambda *_: pytest.fail("invalid option must not call the model"))
    obj, _ = product(tmp_path, monkeypatch, model, model, policy=policy)
    with pytest.raises(ValueError, match="unsupported v4.2 shared investigation"):
        await obj.run(two_file_state())
    assert not model._inputs
