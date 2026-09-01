from __future__ import annotations

import copy

import pytest

from reviewforge.core.evaluation_telemetry import (
    EvaluationTelemetryError,
    build_evaluation_telemetry,
    parse_evaluation_telemetry,
)
from reviewforge.core.state import Finding, StateStore
from reviewforge.engine.orchestrator import Orchestrator


def _payload() -> dict:
    return {
        "schema_version": 1,
        "resume_mode": "normal",
        "failures": {
            "tasks_failed": 0,
            "planner": 0,
            "publication": 0,
            "delivery": 0,
            "operationally_incomplete": False,
        },
        "coverage": {
            "available": True,
            "threshold": 0.7,
            "status": "complete",
            "high_risk": {"total": 2, "resolved": 2, "unresolved": 0, "status": "complete"},
        },
        "funnel": {
            "findings_detected": 3,
            "findings_confirmed": 2,
            "findings_filtered": 1,
            "findings_reported": 2,
            "publication_candidates": 2,
            "delivery_attempted": 2,
            "delivery_reported": 2,
            "tasks_completed": 1,
            "tasks_failed": 0,
        },
        "validation_funnel": [
            {
                "stage": "finding_status",
                "input": 3,
                "added": 0,
                "kept": 2,
                "filtered": 1,
                "merged": 0,
                "inconclusive": 0,
                "failed": 0,
            }
        ],
    }


def test_builder_and_parser_share_one_normalized_v1_contract():
    payload = _payload()

    parsed = parse_evaluation_telemetry(payload)
    built = build_evaluation_telemetry(
        resume_mode=payload["resume_mode"],
        failures=payload["failures"],
        coverage=payload["coverage"],
        funnel=payload["funnel"],
        validation_funnel=payload["validation_funnel"],
    )

    assert parsed.to_dict() == built
    assert built["coverage"]["threshold"] == 0.7


@pytest.mark.parametrize(
    ("counter", "incomplete"),
    [("tasks_failed", False), (None, True)],
)
def test_failure_incomplete_flag_must_match_all_failure_counters(counter: str | None, incomplete: bool):
    payload = _payload()
    if counter is not None:
        payload["failures"][counter] = 1
    payload["failures"]["operationally_incomplete"] = incomplete

    with pytest.raises(EvaluationTelemetryError, match="must equal whether any failure counter"):
        parse_evaluation_telemetry(payload)


def test_unavailable_coverage_requires_zero_counters_and_unavailable_status():
    payload = _payload()
    payload["coverage"].update({"available": False, "status": "unavailable"})
    payload["coverage"]["high_risk"].update({"resolved": 0, "unresolved": 2, "status": "unavailable"})

    with pytest.raises(EvaluationTelemetryError, match="counters must all be zero"):
        parse_evaluation_telemetry(payload)

    payload["coverage"]["high_risk"].update({"total": 0, "unresolved": 0})
    assert parse_evaluation_telemetry(payload).coverage["status"] == "unavailable"


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda value: value["coverage"]["high_risk"].update(unresolved=1), "must conserve"),
        (lambda value: value["coverage"].update(threshold=1.1), "between 0 and 1"),
        (lambda value: value["coverage"].update(status="incomplete"), "must match availability"),
    ],
)
def test_coverage_conservation_threshold_and_status_are_strict(mutate, message: str):
    payload = _payload()
    mutate(payload)
    with pytest.raises(EvaluationTelemetryError, match=message):
        parse_evaluation_telemetry(payload)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda value: value["funnel"].update(findings_reported=3), "cannot exceed findings_confirmed"),
        (lambda value: value["funnel"].update(findings_filtered=2), "cannot exceed findings_detected"),
        (lambda value: value["funnel"].update(delivery_reported=3), "cannot exceed delivery_attempted"),
        (lambda value: value["funnel"].update(tasks_failed=-1), "non-negative integer"),
    ],
)
def test_flat_funnel_final_counters_are_conservative(mutate, message: str):
    payload = _payload()
    mutate(payload)
    with pytest.raises(EvaluationTelemetryError, match=message):
        parse_evaluation_telemetry(payload)


def test_parser_rejects_unexpected_fields_instead_of_silently_dropping_them():
    payload = copy.deepcopy(_payload())
    payload["coverage"]["legacy_total"] = 2

    with pytest.raises(EvaluationTelemetryError, match="unexpected fields"):
        parse_evaluation_telemetry(payload)


def test_validation_funnel_is_required_and_must_match_flat_final_status_counters():
    missing = _payload()
    missing.pop("validation_funnel")
    with pytest.raises(EvaluationTelemetryError, match="missing required fields: validation_funnel"):
        parse_evaluation_telemetry(missing)

    inconsistent = _payload()
    inconsistent["validation_funnel"][0]["inconclusive"] = 1
    inconsistent["validation_funnel"][0]["input"] = 4
    with pytest.raises(EvaluationTelemetryError, match="finding_status.input must equal 3"):
        parse_evaluation_telemetry(inconsistent)


def test_runtime_finding_status_partition_builds_strict_telemetry_and_fails_closed():
    state = StateStore()
    for index, status in enumerate(("candidate", "confirmed", "false_positive", "reported")):
        state.add_finding(Finding(id=f"finding-{index}", file="app.py", message="finding", status=status))

    validation = Orchestrator._evaluation_validation_funnel(state)
    payload = _payload()
    payload["funnel"].update(
        findings_detected=4,
        findings_confirmed=2,
        findings_filtered=1,
        findings_reported=1,
    )
    built = build_evaluation_telemetry(
        resume_mode=payload["resume_mode"],
        failures=payload["failures"],
        coverage=payload["coverage"],
        funnel=payload["funnel"],
        validation_funnel=validation,
    )
    assert built["validation_funnel"][0] == {
        "stage": "finding_status",
        "input": 4,
        "added": 0,
        "kept": 2,
        "filtered": 1,
        "merged": 0,
        "inconclusive": 1,
        "failed": 0,
    }

    state.findings["finding-0"].status = "unknown"
    with pytest.raises(ValueError, match="unsupported final finding status"):
        Orchestrator._evaluation_validation_funnel(state)


def test_task_failure_counter_must_match_between_health_and_funnel():
    payload = _payload()
    payload["failures"].update(tasks_failed=1, operationally_incomplete=True)

    with pytest.raises(EvaluationTelemetryError, match="funnel.tasks_failed must equal"):
        parse_evaluation_telemetry(payload)


def test_resume_mode_is_a_closed_v1_enum():
    payload = _payload()
    payload["resume_mode"] = "banana"

    with pytest.raises(EvaluationTelemetryError, match="normal or publication-only"):
        parse_evaluation_telemetry(payload)
