from __future__ import annotations

import pytest

from reviewforge.engine.coverage_ledger import CoverageCell, CoverageDimension, CoverageLedger, CoverageStatus
from reviewforge.engine.orchestrator import Orchestrator
from reviewforge.engine.run_health import RunHealth, StageResult


def test_stage_result_rejects_negative_failure_counts():
    with pytest.raises(ValueError, match="cannot be negative"):
        StageResult(name="tasks", failures=-1)


def test_run_health_keeps_success_summary_shape_unchanged():
    summary = {"tasks_failed": 0, "confirmed": 2}

    result = RunHealth.build().apply_to_summary(summary)

    assert result == {"tasks_failed": 0, "confirmed": 2}


def test_failed_tasks_make_run_retryable_and_operationally_incomplete():
    health = RunHealth.build(tasks_failed=2)
    summary = health.apply_to_summary({"tasks_failed": 2})

    assert health.operationally_incomplete is True
    assert health.retryable is True
    assert summary == {"tasks_failed": 2, "status": "partial", "retryable": True}
    assert health.failures_payload() == {
        "tasks_failed": 2,
        "planner": 0,
        "publication": 0,
        "delivery": 0,
        "operationally_incomplete": True,
    }
    assert health.errors == ["tasks incomplete (2 failure(s))"]


def test_run_health_aggregates_stage_errors_without_losing_counts():
    health = RunHealth.build(
        planner_errors=("planner unavailable",),
        publication_failures=3,
        publication_errors=("provider limited",),
        publication_retryable=True,
        delivery_failures=2,
        delivery_errors=("github unavailable",),
        delivery_retryable=True,
    )

    assert health.failures_payload() == {
        "tasks_failed": 0,
        "planner": 1,
        "publication": 3,
        "delivery": 2,
        "operationally_incomplete": True,
    }
    assert health.errors == ["planner unavailable", "provider limited", "github unavailable"]


def test_evaluation_coverage_does_not_treat_abstained_as_resolved():
    orchestrator = object.__new__(Orchestrator)
    orchestrator._v3_enabled = True
    orchestrator._v3_coverage_min_risk_score = 0.5
    cell = CoverageCell(
        unit_id="u1",
        path="app.py",
        line=1,
        dimension=CoverageDimension.CORRECTNESS,
        risk=0.9,
        mandatory=True,
        status=CoverageStatus.ABSTAINED,
    )
    orchestrator._v3_ledger = CoverageLedger([cell])

    coverage = orchestrator._evaluation_coverage(available=True)

    assert coverage["high_risk"] == {
        "total": 1,
        "resolved": 0,
        "unresolved": 1,
        "status": "incomplete",
    }
    assert coverage["status"] == "incomplete"
