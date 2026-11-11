"""Region receipts survive sibling gaps, restarts, and exact missing-line repair."""

import asyncio
import json

import pytest
from langchain_core.messages import AIMessage
from test_v42_candidate_draft_reviewer import Model
from test_v42_decision_failure_batch import PagedGateway
from test_v42_decision_loop import from_board, step
from test_v42_stateful_reviewer import call

from reviewforge.review_v42.contracts import BindingError, ReviewScope, Run, Task
from reviewforge.review_v42.coverage_delivery import COVERAGE_DELIVERY_POLICY, CoverageDelivery, missing_ranges
from reviewforge.review_v42.decision_loop import DecisionReviewer
from reviewforge.review_v42.store import ReviewStore
from reviewforge.review_v42.tools import EvidenceGateway


def board(tmp_path):
    regions = [
        {"id": name, "path": name + ".py", "side": "head", "start_line": 1, "end_line": 3, "changed_lines": [1]}
        for name in ("a", "b", "c")
    ]
    run = Run(
        "run",
        "repo",
        "base",
        "head",
        policy="v42-units-6",
        metadata={
            "changed_regions": regions,
            "workspace_digest": "head-snapshot",
            "base_workspace_digest": "base-snapshot",
            "decision_cycle_policy": "decision-cycle-1",
            "shared_investigation": "shared-1",
            "candidate_draft_policy": "candidate-draft-1",
            "source_line_policy": "git-lf-1",
            "frontier_projection_policy": "source-line-union-1",
            "coverage_delivery_policy": COVERAGE_DELIVERY_POLICY,
        },
    )
    store = ReviewStore(tmp_path / "board.sqlite", run)
    store.install_unit_plan(
        [ReviewScope("primary", run.id, "primary", regions=regions)],
        ReviewScope("integration", run.id, "integration", dependencies=["primary"]),
    )
    task = store.add(Task("review", run.id, "review", "primary", 0))
    return store, store.acquire_task(task.id, "worker", 120)


def native(args, name="review_step"):
    return AIMessage(content="", tool_calls=[call(name, args, name)])


def completion(messages, observations, ids):
    scope = json.loads(messages[1].content)["scope"]
    return native(
        {
            "scope_id": scope["id"],
            "expected_scope_revision": scope["revision"],
            "payload": {"checked_region_ids": ids, "observations": observations, "finish": True},
        },
        "complete_scope",
    )


def observe(rid, refs):
    return {"statement": "Checked the fixed implementation", "region_ids": [rid], "evidence_refs": refs}


@pytest.mark.parametrize("restart", [False, True])
async def test_good_region_survives_missing_reference_and_missing_lines(tmp_path, restart):
    store, task = board(tmp_path)
    gateway = EvidenceGateway(store, PagedGateway())
    refs = {}

    def read(_messages):
        return native(
            step(
                "request_evidence",
                {
                    "gap": "Inspect the regions",
                    "relevance": "Check behavior",
                    "alternatives": "Confirm or refute",
                    "operations": [
                        {"name": "read_source", "args": {"path": name + ".py", "start_line": first, "end_line": 3}}
                        for name, first in (("a", 1), ("b", 1), ("c", 2))
                    ],
                },
            )
        )

    def close(messages):
        item = from_board(messages)
        results = json.loads(messages[-1].content)["results"]
        refs.update({name: entry["result"]["evidence_ref"] for name, entry in zip(("a", "b", "c"), results)})
        return native(
            step(
                "close_question",
                {
                    "question_id": item["question_id"],
                    "answer": "Checked all available originals",
                },
                item,
                list(refs.values()),
            )
        )

    def mixed_receipt(messages):
        return completion(
            messages,
            [
                observe("a", [refs["a"]]),
                observe("b", [refs["a"]]),
                observe("c", [refs["c"]]),
            ],
            ["a", "b", "c"],
        )

    def inspect_receipt(messages):
        feedback = json.loads(messages[-1].content)
        assert feedback["settled_region_ids"] == ["a"]
        assert feedback["remaining_region_ids"] == ["b", "c"]
        assert [r["id"] for r in store.get(ReviewScope, "primary").checked_regions] == ["a"]
        gaps = {row["region_id"]: row for row in feedback["errors"]}
        assert gaps["b"]["reason"] == "missing_region_reference"
        assert gaps["b"]["available_source_refs"] == [refs["b"]]
        assert gaps["c"]["reason"] == "missing_source_range"
        assert gaps["c"]["read_source"] == [{"path": "c.py", "side": "head", "start_line": 1, "end_line": 1}]
        with store._connect() as db:
            attempt = json.loads(
                db.execute(
                    "SELECT payload FROM decision_attempts WHERE json_extract(payload,'$.args.scope_id')='primary'"
                ).fetchone()[0]
            )
        again = CoverageDelivery(store).record(task.id, task.lease_token, attempt["turn_id"], attempt["call_id"])
        assert again == {key: value for key, value in feedback.items() if key not in {"step_id", "scope_id"}}
        if restart:
            raise asyncio.CancelledError()
        return missing_source(messages)

    def missing_source(messages):
        assert json.loads(messages[1].content)["current_region_receipt"]["region_id"] == "b"
        return native(
            step(
                "request_evidence",
                {
                    "gap": "The first handler line has not been read",
                    "relevance": "Complete region c",
                    "alternatives": "Inspect the missing line",
                    "operations": [{"name": "read_source", "args": {"path": "c.py", "start_line": 1, "end_line": 1}}],
                },
                from_board(messages),
                [refs["c"]],
            )
        )

    def repair(messages):
        refs["missing"] = json.loads(messages[-1].content)["results"][0]["result"]["evidence_ref"]
        return completion(messages, [observe("b", [refs["b"]]), observe("c", [refs["c"], refs["missing"]])], ["b", "c"])

    def finish(messages):
        receipt = json.loads(messages[-1].content)
        assert receipt["remaining_region_ids"] == []
        return completion(messages, [], [])

    model = Model([read, close, mixed_receipt, inspect_receipt, repair, finish])
    reviewer = DecisionReviewer(store, model, gateway, task_id=task.id, lease_token=task.lease_token)
    if restart:
        with pytest.raises(asyncio.CancelledError):
            await reviewer.run("primary")
        store.release_task(task.id, task.lease_token)
        task = store.acquire_task(task.id, "resumed", 120)
        reviewer = DecisionReviewer(
            store, Model([missing_source, repair, finish]), gateway, task_id=task.id, lease_token=task.lease_token
        )
    assert await reviewer.run("primary") == "reviewed", reviewer.error
    assert [r["id"] for r in store.get(ReviewScope, "primary").checked_regions] == ["a", "b", "c"]
    assert len(store.history("UnitDecision")) == 4


def test_missing_ranges_merge_overlaps_and_keep_inclusive_edges():
    assert missing_ranges(1, 12, [(3, 5), (4, 8), (11, 11)]) == [
        {"start_line": 1, "end_line": 2},
        {"start_line": 9, "end_line": 10},
        {"start_line": 12, "end_line": 12},
    ]


async def test_source_failure_rolls_back_the_whole_submission(tmp_path, monkeypatch):
    store, task = board(tmp_path)
    gateway = EvidenceGateway(store, PagedGateway())
    refs = {}
    commit = store.commit_unit_decision
    attempts = []

    def fail_second(*args, **kwargs):
        attempts.append(kwargs["checked_region_ids"])
        if len(attempts) == 2:
            raise BindingError("source integrity failed")
        return commit(*args, **kwargs)

    monkeypatch.setattr(store, "commit_unit_decision", fail_second)

    def read(_messages):
        return native(
            step(
                "request_evidence",
                {
                    "gap": "Review",
                    "relevance": "Changed implementation",
                    "alternatives": "Check originals",
                    "operations": [
                        {"name": "read_source", "args": {"path": name + ".py", "start_line": 1, "end_line": 3}}
                        for name in ("a", "b")
                    ],
                },
            )
        )

    def close(messages):
        refs.update(
            {
                name: row["result"]["evidence_ref"]
                for name, row in zip(("a", "b"), json.loads(messages[-1].content)["results"])
            }
        )
        item = from_board(messages)
        return native(
            step(
                "close_question",
                {"question_id": item["question_id"], "answer": "Checked"},
                item,
                list(refs.values()),
            )
        )

    def submit(messages):
        return completion(messages, [observe("a", [refs["a"]]), observe("b", [refs["b"]])], ["a", "b"])

    reviewer = DecisionReviewer(
        store, Model([read, close, submit]), gateway, task_id=task.id, lease_token=task.lease_token
    )
    assert await reviewer.run("primary") == "partial"
    assert reviewer.error == "candidate_or_source_unavailable"
    assert attempts == [["a"], ["b"]]
    assert store.get(ReviewScope, "primary").checked_regions == []
    assert not store.history("UnitDecision")
