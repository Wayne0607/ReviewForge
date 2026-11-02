"""Shared work cannot silently turn an incomplete unit or review into complete."""

import json

import pytest
from test_v42_case_counterevidence import board as board
from test_v42_case_counterevidence import deliver

from reviewforge.review_v42.contracts import LeaseError, ReviewScope, RevisionConflict, Run, Task
from reviewforge.review_v42.shared_investigation import SharedInvestigations
from reviewforge.review_v42.store import ReviewStore


def test_shared_store_missing_is_a_completion_blocker(board):
    store, task, _, _, _, counter = board
    scope = store.get(ReviewScope, "a")
    result = store.commit_unit_decision(
        task.id,
        task.lease_token,
        scope.revision,
        "no-shared-store",
        checked_region_ids=["a"],
        observations=[{"statement": "inspected", "evidence_refs": [counter.id], "region_ids": ["a"]}],
        questions=[],
        cases=[],
        unresolved=[],
        finish=True,
        turn_id="source-turn",
        now=2,
    )
    assert result["scope"].status == "partial"
    assert not store.validate_units_completion()["valid"]


def test_creator_can_claim_resolve_and_explicitly_finish_while_other_task_fenced(board):
    store, task, other, case, _, counter = board
    shared = SharedInvestigations(store)
    previous_verdict = store.get_verdict(case.id, "claim", "v42-independent-1")
    item = shared.create(
        task.id,
        task.lease_token,
        "How do the units interact?",
        ["a", "b"],
        ["a", "b"],
        turn_id="source-turn",
        tool_call_id="create",
        now=2,
    )
    current = store.get(Task, task.id)
    assert current.lease_token == task.lease_token
    assert current.input_revision == store.get(ReviewScope, "a").revision
    assert store.get_verdict(case.id, "claim", "v42-independent-1") == previous_verdict
    with pytest.raises((LeaseError, RevisionConflict)):
        with store._connect() as db:
            store._guard_review_task(db, other.id, other.lease_token, "b", 3)
    claimed = shared.claim(
        task.id, task.lease_token, item["id"], item["revision"], "investigate", operation_id="claim", now=3
    )
    result = store.commit_unit_decision(
        task.id,
        task.lease_token,
        current.input_revision,
        "pending",
        checked_region_ids=["a"],
        observations=[{"statement": "inspected", "evidence_refs": [counter.id], "region_ids": ["a"]}],
        questions=[],
        cases=[],
        unresolved=[],
        finish=True,
        turn_id="source-turn",
        now=4,
    )
    assert result["scope"].status == "partial"
    with store._connect() as db:
        assert item["id"] in store._pending_shared_investigations(db, "integration")
    resolved = shared.update(
        task.id,
        task.lease_token,
        item["id"],
        claimed["revision"],
        question="How do the units interact?",
        observations=[{"statement": "source resolves the question", "evidence_refs": [counter.id]}],
        open_questions=[],
        status="resolved",
        reason="resolved by investigation",
        turn_id="source-turn",
        operation_id="resolve",
        now=5,
    )
    assert resolved["status"] == "resolved"
    assert store.get(ReviewScope, "a").status == "pending"
    assert store.get(ReviewScope, "a").checked_regions == []
    assert {entry["scope_id"] for entry in store.pending_unit_sources()} == {"a", "b"}
    final = store.commit_unit_decision(
        task.id,
        task.lease_token,
        store.get(ReviewScope, "a").revision,
        "after-resolution",
        checked_region_ids=["a"],
        observations=[{"statement": "rechecked after resolution", "evidence_refs": [counter.id], "region_ids": ["a"]}],
        questions=[],
        cases=[],
        unresolved=[],
        finish=True,
        turn_id="source-turn",
        now=6,
    )
    assert final["scope"].status == "reviewed"
    with store._connect() as db:
        assert not store._pending_shared_investigations(db, "integration")


def test_retracted_investigation_rechecks_only_named_region_without_fake_sources(tmp_path):
    regions = [
        {"id": "first", "path": "a.py", "side": "head", "start_line": 1, "end_line": 1, "changed_lines": [1]},
        {"id": "second", "path": "a.py", "side": "head", "start_line": 2, "end_line": 2, "changed_lines": [2]},
    ]
    run = Run(
        "run",
        "repo",
        "base",
        "head",
        policy="v42-units-6",
        metadata={
            "shared_investigation": "shared-1",
            "workspace_digest": "ws",
            "base_workspace_digest": "base-ws",
            "changed_regions": regions,
        },
    )
    store = ReviewStore(tmp_path / "selective.sqlite", run)
    store.install_unit_plan(
        [ReviewScope("a", "run", "a", regions=regions)],
        ReviewScope("integration", "run", "integration", dependencies=["a"]),
    )
    shared = SharedInvestigations(store)
    ref = store.put_evidence(
        b"one\ntwo\n",
        revision="head",
        side="head",
        kind="source",
        path="a.py",
        start_line=1,
        end_line=2,
        metadata={"workspace_digest": "ws"},
    )
    store.add(Task("task-a", "run", "review", "a", 0))
    task = store.acquire_task("task-a", "reviewer", 100, now=0)
    deliver(store, task, "source", ref)
    complete = store.commit_unit_decision(
        task.id,
        task.lease_token,
        0,
        "both",
        checked_region_ids=["first", "second"],
        observations=[{"statement": "inspected", "evidence_refs": [ref.id], "region_ids": ["first", "second"]}],
        questions=[],
        cases=[],
        unresolved=[],
        finish=True,
        turn_id="source",
        now=2,
    )
    assert complete["scope"].status == "reviewed"
    item = shared.create(
        task.id,
        task.lease_token,
        "Does first matter?",
        ["a"],
        ["first"],
        turn_id="source",
        tool_call_id="create",
        now=3,
    )
    assert [r["id"] for r in store.get(ReviewScope, "a").checked_regions] == ["second"]
    claimed = shared.claim(
        task.id, task.lease_token, item["id"], item["revision"], "investigate", operation_id="claim", now=4
    )
    shared.update(
        task.id,
        task.lease_token,
        item["id"],
        claimed["revision"],
        question="Does first matter?",
        observations=[],
        open_questions=[],
        status="retracted",
        reason="question withdrawn",
        turn_id="source",
        operation_id="retract",
        now=5,
    )
    scope = store.get(ReviewScope, "a")
    assert scope.status == "pending" and [r["id"] for r in scope.checked_regions] == ["second"]
    assert store.pending_unit_sources() == []


@pytest.mark.parametrize("partial", [False, True])
def test_resolved_source_and_extra_document_preserve_exact_delivery(board, partial):
    store, task, _, _, _, source = board
    shared = SharedInvestigations(store)
    item = shared.create(
        task.id, task.lease_token, "What happens?", ["a"], ["a"], turn_id="source-turn", tool_call_id="create", now=2
    )
    claimed = shared.claim(
        task.id, task.lease_token, item["id"], item["revision"], "investigate", operation_id="claim", now=3
    )
    doc = store.put_evidence(
        b"contract\n",
        revision="head",
        side="head",
        kind="documentation",
        path="contract.md",
        start_line=1,
        end_line=1,
        metadata={"workspace_digest": "ws"},
    )
    store.pin_frontier_for_task(task.id, task.lease_token, doc.id, now=3)
    source_content = "counter one\n" if partial else "counter one\ncounter two\n"
    receipts = [
        {"evidence_ref": source.id, "content": source_content, "start_line": 1, "end_line": 1 if partial else 2},
        {"evidence_ref": doc.id, "content": "contract\n", "start_line": 1, "end_line": 1},
    ]
    store.append_turn_audit(
        task.id,
        task.lease_token,
        "combined",
        "request",
        [{"type": "tool", "data": {"content": json.dumps(receipt)}} for receipt in receipts],
        now=3,
    )
    store.append_turn_audit(
        task.id, task.lease_token, "combined", "response", [{"type": "ai", "data": {"content": "ok"}}], now=3
    )

    def close():
        return shared.update(
            task.id,
            task.lease_token,
            item["id"],
            claimed["revision"],
            question="What happens?",
            observations=[{"statement": "checked source and document", "evidence_refs": [source.id, doc.id]}],
            open_questions=[],
            status="resolved",
            reason="resolved",
            turn_id="combined",
            operation_id="resolve",
            now=4,
        )

    if partial:
        with pytest.raises(ValueError, match="not fully delivered"):
            close()
        assert shared.get(task.id, task.lease_token, item["id"], now=4)["status"] == "open"
        assert store.pending_unit_sources() == []
    else:
        assert close()["status"] == "resolved"
        assert {entry["ref_id"] for entry in store.pending_unit_sources()} == {source.id}
