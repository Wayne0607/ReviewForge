"""Durable task recovery and terminal-event ordering contracts."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from reviewforge.core.database import Database
from reviewforge.core.evaluation_telemetry import parse_evaluation_telemetry
from reviewforge.core.events import EventBus
from reviewforge.core.specs import build_registry
from reviewforge.core.state import Finding, ReviewTask, StateStore
from reviewforge.engine.mock_llm import MockChatLLM
from reviewforge.engine.orchestrator import CommentDeliveryResult, Orchestrator
from reviewforge.engine.run_health import RunHealth
from reviewforge.tools.gateway import ToolGateway
from reviewforge.tools.mock_github import MockGitHubClient


def _orchestrator(*, db=None, events: EventBus | None = None) -> Orchestrator:
    registry = build_registry()
    return Orchestrator(
        registry=registry,
        gateway=ToolGateway(registry, MockGitHubClient()),
        event_bus=events or EventBus(),
        planner_llm=MockChatLLM(),
        reviewer_llm=MockChatLLM(),
        calibrator_llm=MockChatLLM(),
        db=db,
    )


class _NoTaskPlanner:
    async def plan(self, state, notes=None):
        return []


async def test_rehydrate_preserves_multi_chunk_task_identity_and_attempts(tmp_path):
    db = Database(tmp_path / "chunks.db")
    await db.connect()
    await db.create_run("run-chunks", "owner/repo", 1, "head")

    await db.checkpoint_task_round(
        run_id="run-chunks",
        round_id="planner-round-a",
        tasks=[
            {
                "task_id": "task-a",
                "reviewer_name": "correctness_reviewer",
                "files": ["a.py"],
            },
            {
                "task_id": "task-b",
                "reviewer_name": "correctness_reviewer",
                "files": ["b.py"],
            },
            {
                "task_id": "task-c",
                "reviewer_name": "correctness_reviewer",
                "files": ["c.py"],
            },
        ],
    )
    await db.upsert_task_checkpoint(
        run_id="run-chunks",
        task_id="task-a",
        reviewer_name="correctness_reviewer",
        files=["a.py"],
        status="claimed",
    )
    await db.upsert_task_checkpoint(
        run_id="run-chunks",
        task_id="task-a",
        reviewer_name="correctness_reviewer",
        files=["a.py"],
        status="completed",
    )
    await db.upsert_task_checkpoint(
        run_id="run-chunks",
        task_id="task-b",
        reviewer_name="correctness_reviewer",
        files=["b.py"],
        status="claimed",
    )
    # This failure has no reviewer_metrics row.  It must still be retryable.
    await db.upsert_task_checkpoint(
        run_id="run-chunks",
        task_id="task-b",
        reviewer_name="correctness_reviewer",
        files=["b.py"],
        status="failed",
        error="provider timeout",
    )
    await db.upsert_task_checkpoint(
        run_id="run-chunks",
        task_id="task-b",
        reviewer_name="correctness_reviewer",
        files=["b.py"],
        status="claimed",
    )
    # Re-claiming an orphaned claimed task opens a distinct attempt.
    await db.upsert_task_checkpoint(
        run_id="run-chunks",
        task_id="task-c",
        reviewer_name="correctness_reviewer",
        files=["c.py"],
        status="claimed",
    )
    await db.upsert_task_checkpoint(
        run_id="run-chunks",
        task_id="task-c",
        reviewer_name="correctness_reviewer",
        files=["c.py"],
        status="claimed",
    )
    await db.upsert_task_checkpoint(
        run_id="run-chunks",
        task_id="task-c",
        reviewer_name="correctness_reviewer",
        files=["c.py"],
        status="completed",
    )

    with pytest.raises(ValueError, match="identity drift"):
        await db.upsert_task_checkpoint(
            run_id="run-chunks",
            task_id="task-a",
            reviewer_name="correctness_reviewer",
            files=["different.py"],
            status="failed",
        )
    with pytest.raises(ValueError, match="completed task checkpoint is immutable"):
        await db.upsert_task_checkpoint(
            run_id="run-chunks",
            task_id="task-a",
            reviewer_name="correctness_reviewer",
            files=["a.py"],
            status="failed",
        )

    history = await db.get_task_checkpoints("run-chunks", latest_only=False)
    assert [(row["task_id"], row["attempt"], row["status"]) for row in history] == [
        ("task-a", 1, "completed"),
        ("task-b", 1, "failed"),
        ("task-b", 2, "claimed"),
        ("task-c", 1, "claimed"),
        ("task-c", 2, "completed"),
    ]

    state = StateStore(pr_number=1, repo="owner/repo", head_sha="head", files_changed=["a.py", "b.py"])
    recovery = await _orchestrator(db=db)._rehydrate(state, "run-chunks", checkpoint_version=2)
    restored = {task.id: task for task in state.list_tasks()}
    assert restored["task-a"].files == ["a.py"]
    assert restored["task-a"].status == "completed"
    assert restored["task-b"].files == ["b.py"]
    assert restored["task-b"].status == "pending"
    assert restored["task-c"].files == ["c.py"]
    assert restored["task-c"].status == "completed"
    assert recovery.incomplete_task_ids == frozenset({"task-b"})
    assert recovery.runnable_task_ids == frozenset({"task-b"})
    assert recovery.publication_only_safe is False
    await db.close()


async def test_v3_metric_namespace_is_not_used_as_task_recovery_evidence(tmp_path):
    db = Database(tmp_path / "v3.db")
    await db.connect()
    await db.create_run("run-v3", "owner/repo", 1, "head")
    await db.upsert_task_checkpoint(
        run_id="run-v3",
        task_id="task-v3",
        reviewer_name="security_reviewer",
        files=["auth.py"],
        status="failed",
        error="tool unavailable",
        rationale="v3 targeted closure: security for unit-auth",
        task_kind="v3_closure",
    )
    await db.insert_metric(
        "run-v3",
        "v3_closure_security_reviewer",
        status="completed",
    )

    state = StateStore(pr_number=1, repo="owner/repo", head_sha="head", files_changed=["auth.py"])
    orchestrator = _orchestrator(db=db)
    with patch.object(db, "get_metrics", side_effect=AssertionError("metrics are not checkpoints")):
        recovery = await orchestrator._rehydrate(state, "run-v3", checkpoint_version=2)

    assert state.list_tasks() == [], "specialized coverage context must not be faked during recovery"
    assert recovery.incomplete_task_ids == frozenset({"task-v3"})
    assert recovery.runnable_task_ids == frozenset()
    assert recovery.publication_only_safe is False
    await db.close()


async def test_unsealed_partial_planner_round_is_ignored_and_forces_replan(tmp_path):
    db = Database(tmp_path / "partial-round.db")
    await db.connect()
    await db.create_run("run-partial", "owner/repo", 1, "head")
    expected_tasks = [
        {
            "task_id": "task-partial-a",
            "reviewer_name": "correctness_reviewer",
            "files": ["a.py"],
        },
        {
            "task_id": "task-missing-b",
            "reviewer_name": "correctness_reviewer",
            "files": ["b.py"],
        },
    ]
    now = "2026-01-01T00:00:00+00:00"
    await db._db.execute(
        "INSERT INTO review_task_rounds "
        "(run_id, round_id, task_count, task_hash, sealed, created_at) "
        "VALUES (?, ?, ?, ?, 0, ?)",
        (
            "run-partial",
            "planner-crashed",
            2,
            db.task_round_hash(expected_tasks),
            now,
        ),
    )
    await db._db.execute(
        "INSERT INTO review_task_checkpoints "
        "(run_id, task_id, attempt, round_id, reviewer_name, files_json, task_signature, "
        "task_kind, rationale, status, error, created_at, updated_at) "
        "VALUES (?, ?, 1, ?, ?, ?, ?, 'reviewer', '', 'completed', '', ?, ?)",
        (
            "run-partial",
            "task-partial-a",
            "planner-crashed",
            "correctness_reviewer",
            json.dumps(["a.py"]),
            db.task_signature("correctness_reviewer", ["a.py"]),
            now,
            now,
        ),
    )
    await db._db.commit()

    assert await db.get_task_checkpoints("run-partial") == []
    state = StateStore(pr_number=1, repo="owner/repo", head_sha="head", files_changed=["a.py", "b.py"])
    recovery = await _orchestrator(db=db)._rehydrate(state, "run-partial", checkpoint_version=2)

    assert state.list_tasks() == [], "a partial reviewer must not suppress re-planning its missing sibling"
    assert recovery.planner_rounds_complete is False
    assert recovery.publication_only_safe is False
    await db.close()


async def test_legacy_run_never_uses_metrics_to_enable_publication_only(tmp_path):
    db = Database(tmp_path / "legacy.db")
    await db.connect()
    await db.create_run("legacy-run", "owner/repo", 1, "head")
    await db._db.execute(
        "UPDATE review_runs SET task_checkpoint_version=0 WHERE run_id=?",
        ("legacy-run",),
    )
    await db._db.commit()
    await db.insert_metric("legacy-run", "security_reviewer", status="completed")

    state = StateStore(pr_number=1, repo="owner/repo", head_sha="head", files_changed=["auth.py"])
    recovery = await _orchestrator(db=db)._rehydrate(state, "legacy-run", checkpoint_version=0)

    assert state.list_tasks() == []
    assert recovery.publication_only_safe is False
    await db.close()


async def test_missing_specialized_owning_phase_remains_in_final_run_health(tmp_path):
    db = Database(tmp_path / "missing-phase.db")
    await db.connect()
    await db.create_run("run-missing-phase", "owner/repo", 1, "head")
    await db.upsert_task_checkpoint(
        run_id="run-missing-phase",
        task_id="task-v3-unresolved",
        reviewer_name="security_reviewer",
        files=["auth.py"],
        status="failed",
        error="provider unavailable",
        rationale="v3 targeted closure: security for unit-auth",
        task_kind="v3_closure",
    )
    await db.fail_run("run-missing-phase", "crashed before V3 retry")

    orchestrator = _orchestrator(db=db)
    orchestrator._planner = _NoTaskPlanner()
    state = StateStore(
        pr_number=1,
        repo="owner/repo",
        head_sha="head",
        files_changed=[],
        diff_summary="",
    )
    summary = await orchestrator.run(state)

    assert summary["tasks_failed"] == 1
    assert summary["status"] == "partial"
    assert summary["retryable"] is True
    assert (await db.get_run("run-missing-phase"))["status"] == "failed"
    await db.close()


async def test_completed_checkpoint_is_flushed_only_after_findings_are_durable(tmp_path):
    db = Database(tmp_path / "ordering.db")
    await db.connect()
    await db.create_run("run-ordering", "owner/repo", 1, "head")
    orchestrator = _orchestrator(db=db)
    state = StateStore(pr_number=1, repo="owner/repo", head_sha="head", files_changed=["a.py"])
    task = ReviewTask(
        id="task-ordering",
        reviewer="correctness_reviewer",
        files=["a.py"],
    )
    await orchestrator._add_planner_round_checkpointed(state, "run-ordering", [task])
    await orchestrator._update_task_checkpointed(
        state,
        "run-ordering",
        task.id,
        status="claimed",
    )

    state.add_finding(
        Finding(
            id="finding-ordering",
            file="a.py",
            line=1,
            severity="error",
            category="correctness",
            message="observable failure",
            reviewer="correctness_reviewer",
        )
    )
    state.update_task(task.id, status="completed")

    before = await db.get_task_checkpoints("run-ordering")
    assert before[0]["status"] == "claimed", "a crash here must retry the task"
    for finding in state.list_findings():
        await db.insert_finding("run-ordering", finding.to_dict())
    await orchestrator._flush_task_checkpoints(state, "run-ordering")

    after = await db.get_task_checkpoints("run-ordering")
    assert after[0]["status"] == "completed"
    assert [row["id"] for row in await db.get_findings(run_id="run-ordering")] == ["finding-ordering"]
    await db.close()


class _RecordingTerminalDB:
    def __init__(self, order: list[str], *, fail_complete: bool = False) -> None:
        self.order = order
        self.fail_complete = fail_complete

    async def complete_run(self, run_id, summary):
        self.order.append("db.complete")
        if self.fail_complete:
            raise RuntimeError("commit failed")

    async def fail_run(self, run_id, error, summary=None):
        self.order.append("db.fail")


async def test_terminal_database_commit_precedes_events_and_emits_valid_telemetry():
    order: list[str] = []
    events = EventBus()
    seen = []
    events.subscribe(lambda event: (order.append(f"event.{event.event_type}"), seen.append(event)))
    orchestrator = _orchestrator(db=_RecordingTerminalDB(order), events=events)
    state = StateStore(pr_number=1, repo="owner/repo", head_sha="head")

    await orchestrator._finalize_run(
        run_id="run-final",
        state=state,
        summary={
            "total_findings": 0,
            "confirmed": 0,
            "false_positives": 0,
            "tasks_completed": 0,
            "tasks_failed": 0,
        },
        health=RunHealth.build(),
        publication=None,
        delivery=CommentDeliveryResult(),
        coverage_available=False,
        failure_prefix="incomplete: ",
    )

    assert order == [
        "db.complete",
        "event.evaluation.telemetry",
        "event.review.completed",
    ]
    telemetry = next(event.data for event in seen if event.event_type == "evaluation.telemetry")
    parsed = parse_evaluation_telemetry(telemetry)
    assert parsed.coverage["threshold"] == orchestrator._v3_coverage_min_risk_score


async def test_commit_failure_emits_only_finalization_failed_after_reliable_fallback():
    order: list[str] = []
    events = EventBus()
    seen = []
    events.subscribe(lambda event: (order.append(f"event.{event.event_type}"), seen.append(event)))
    orchestrator = _orchestrator(db=_RecordingTerminalDB(order, fail_complete=True), events=events)
    state = StateStore(pr_number=1, repo="owner/repo", head_sha="head")

    with pytest.raises(RuntimeError, match="could not finalize"):
        await orchestrator._finalize_run(
            run_id="run-final-failed",
            state=state,
            summary={
                "total_findings": 0,
                "confirmed": 0,
                "false_positives": 0,
                "tasks_completed": 0,
                "tasks_failed": 0,
            },
            health=RunHealth.build(),
            publication=None,
            delivery=CommentDeliveryResult(),
            coverage_available=False,
            failure_prefix="incomplete: ",
        )

    assert order == [
        "db.complete",
        "db.fail",
        "event.review.finalization_failed",
    ]
    assert [event.event_type for event in seen] == ["review.finalization_failed"]
    assert seen[0].data["persisted_status"] == "failed"
