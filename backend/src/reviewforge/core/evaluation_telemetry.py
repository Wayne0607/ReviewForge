"""Strict shared contract for append-only ``evaluation.telemetry`` v1 payloads."""

from __future__ import annotations

import copy
import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

FAILURE_COUNTERS = ("tasks_failed", "planner", "publication", "delivery")
FUNNEL_COUNTERS = (
    "findings_detected",
    "findings_confirmed",
    "findings_filtered",
    "findings_reported",
    "publication_candidates",
    "delivery_attempted",
    "delivery_reported",
    "tasks_completed",
    "tasks_failed",
)
VALIDATION_OUTPUTS = ("kept", "filtered", "merged", "inconclusive", "failed")


class EvaluationTelemetryError(ValueError):
    """Raised when telemetry is not a valid v1 runtime artifact."""


@dataclass(frozen=True)
class EvaluationTelemetryV1:
    """Validated telemetry with a stable JSON representation."""

    resume_mode: str
    failures: dict[str, int | bool]
    coverage: dict[str, Any]
    funnel: dict[str, int]
    validation_funnel: tuple[dict[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": 1,
            "resume_mode": self.resume_mode,
            "failures": copy.deepcopy(self.failures),
            "coverage": copy.deepcopy(self.coverage),
            "funnel": copy.deepcopy(self.funnel),
        }
        payload["validation_funnel"] = copy.deepcopy(list(self.validation_funnel))
        return payload


def parse_evaluation_telemetry(payload: Any) -> EvaluationTelemetryV1:
    """Parse telemetry v1 and reject missing, extra, inconsistent, or loose-typed data."""

    root = _object(payload, "telemetry")
    allowed = {"schema_version", "resume_mode", "failures", "coverage", "funnel", "validation_funnel"}
    _exact_keys(root, allowed, allowed, "telemetry")
    version = root.get("schema_version")
    if type(version) is not int or version != 1:
        raise EvaluationTelemetryError("telemetry.schema_version must be integer 1")
    resume_mode = _string(root.get("resume_mode"), "telemetry.resume_mode")
    if resume_mode not in {"normal", "publication-only"}:
        raise EvaluationTelemetryError("telemetry.resume_mode must be normal or publication-only")
    failures = _parse_failures(root.get("failures"))
    coverage = _parse_coverage(root.get("coverage"))
    funnel = _parse_funnel(root.get("funnel"))
    if funnel["tasks_failed"] != failures["tasks_failed"]:
        raise EvaluationTelemetryError("telemetry.funnel.tasks_failed must equal telemetry.failures.tasks_failed")
    validation_funnel = _parse_validation_funnel(root.get("validation_funnel"))
    _validate_finding_status_stage(funnel, validation_funnel)
    return EvaluationTelemetryV1(resume_mode, failures, coverage, funnel, validation_funnel)


def build_evaluation_telemetry(
    *,
    resume_mode: str,
    failures: Mapping[str, Any],
    coverage: Mapping[str, Any],
    funnel: Mapping[str, Any],
    validation_funnel: list[Mapping[str, Any]],
) -> dict[str, Any]:
    """Build telemetry through the same strict parser used by offline evaluation."""

    payload: dict[str, Any] = {
        "schema_version": 1,
        "resume_mode": resume_mode,
        "failures": dict(failures),
        "coverage": copy.deepcopy(dict(coverage)),
        "funnel": dict(funnel),
    }
    payload["validation_funnel"] = copy.deepcopy(validation_funnel)
    return parse_evaluation_telemetry(payload).to_dict()


def _parse_failures(raw: Any) -> dict[str, int | bool]:
    failures = _object(raw, "telemetry.failures")
    required = {*FAILURE_COUNTERS, "operationally_incomplete"}
    _exact_keys(failures, required, required, "telemetry.failures")
    normalized: dict[str, int | bool] = {
        key: _non_negative_int(failures.get(key), f"telemetry.failures.{key}") for key in FAILURE_COUNTERS
    }
    incomplete = failures.get("operationally_incomplete")
    if type(incomplete) is not bool:
        raise EvaluationTelemetryError("telemetry.failures.operationally_incomplete must be a boolean")
    expected_incomplete = any(int(normalized[key]) > 0 for key in FAILURE_COUNTERS)
    if incomplete != expected_incomplete:
        raise EvaluationTelemetryError(
            "telemetry.failures.operationally_incomplete must equal whether any failure counter is non-zero"
        )
    normalized["operationally_incomplete"] = incomplete
    return normalized


def _parse_coverage(raw: Any) -> dict[str, Any]:
    coverage = _object(raw, "telemetry.coverage")
    required = {"available", "threshold", "status", "high_risk"}
    _exact_keys(coverage, required, required, "telemetry.coverage")
    available = coverage.get("available")
    if type(available) is not bool:
        raise EvaluationTelemetryError("telemetry.coverage.available must be a boolean")
    threshold = coverage.get("threshold")
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or not math.isfinite(float(threshold)):
        raise EvaluationTelemetryError("telemetry.coverage.threshold must be a finite number")
    threshold = float(threshold)
    if not 0.0 <= threshold <= 1.0:
        raise EvaluationTelemetryError("telemetry.coverage.threshold must be between 0 and 1")
    status = _status(coverage.get("status"), "telemetry.coverage.status")

    high_risk = _object(coverage.get("high_risk"), "telemetry.coverage.high_risk")
    high_risk_required = {"total", "resolved", "unresolved", "status"}
    _exact_keys(high_risk, high_risk_required, high_risk_required, "telemetry.coverage.high_risk")
    total = _non_negative_int(high_risk.get("total"), "telemetry.coverage.high_risk.total")
    resolved = _non_negative_int(high_risk.get("resolved"), "telemetry.coverage.high_risk.resolved")
    unresolved = _non_negative_int(high_risk.get("unresolved"), "telemetry.coverage.high_risk.unresolved")
    high_risk_status = _status(high_risk.get("status"), "telemetry.coverage.high_risk.status")
    if total != resolved + unresolved:
        raise EvaluationTelemetryError("telemetry.coverage.high_risk must conserve total = resolved + unresolved")

    expected_status = "unavailable" if not available else ("complete" if unresolved == 0 else "incomplete")
    if status != expected_status or high_risk_status != expected_status:
        raise EvaluationTelemetryError(
            "telemetry.coverage status and high_risk.status must match availability and unresolved count"
        )
    if not available and (total != 0 or resolved != 0 or unresolved != 0):
        raise EvaluationTelemetryError("telemetry.coverage unavailable high_risk counters must all be zero")
    return {
        "available": available,
        "threshold": threshold,
        "status": status,
        "high_risk": {
            "total": total,
            "resolved": resolved,
            "unresolved": unresolved,
            "status": high_risk_status,
        },
    }


def _parse_funnel(raw: Any) -> dict[str, int]:
    funnel = _object(raw, "telemetry.funnel")
    required = set(FUNNEL_COUNTERS)
    _exact_keys(funnel, required, required, "telemetry.funnel")
    normalized = {key: _non_negative_int(funnel.get(key), f"telemetry.funnel.{key}") for key in FUNNEL_COUNTERS}
    if normalized["findings_confirmed"] > normalized["findings_detected"]:
        raise EvaluationTelemetryError("telemetry.funnel.findings_confirmed cannot exceed findings_detected")
    if normalized["findings_reported"] > normalized["findings_confirmed"]:
        raise EvaluationTelemetryError("telemetry.funnel.findings_reported cannot exceed findings_confirmed")
    if normalized["findings_confirmed"] + normalized["findings_filtered"] > normalized["findings_detected"]:
        raise EvaluationTelemetryError(
            "telemetry.funnel findings_confirmed + findings_filtered cannot exceed findings_detected"
        )
    if normalized["delivery_reported"] > normalized["delivery_attempted"]:
        raise EvaluationTelemetryError("telemetry.funnel.delivery_reported cannot exceed delivery_attempted")
    return normalized


def _parse_validation_funnel(raw: Any) -> tuple[dict[str, Any], ...]:
    if not isinstance(raw, list):
        raise EvaluationTelemetryError("telemetry.validation_funnel must be a list")
    stages: list[dict[str, Any]] = []
    seen: set[str] = set()
    required = {"stage", "input", "added", *VALIDATION_OUTPUTS}
    for index, raw_stage in enumerate(raw):
        path = f"telemetry.validation_funnel[{index}]"
        stage = _object(raw_stage, path)
        _exact_keys(stage, required, required, path)
        name = _string(stage.get("stage"), f"{path}.stage")
        if name in seen:
            raise EvaluationTelemetryError(f"{path}.stage duplicates {name!r}")
        seen.add(name)
        normalized: dict[str, Any] = {"stage": name}
        for key in ("input", "added", *VALIDATION_OUTPUTS):
            normalized[key] = _non_negative_int(stage.get(key), f"{path}.{key}")
        outputs = sum(normalized[key] for key in VALIDATION_OUTPUTS)
        if normalized["input"] + normalized["added"] != outputs:
            raise EvaluationTelemetryError(
                f"{path} violates input + added = kept + filtered + merged + inconclusive + failed"
            )
        stages.append(normalized)
    return tuple(stages)


def _validate_finding_status_stage(funnel: Mapping[str, int], validation_funnel: tuple[dict[str, Any], ...]) -> None:
    stages = [stage for stage in validation_funnel if stage["stage"] == "finding_status"]
    if len(stages) != 1:
        raise EvaluationTelemetryError("telemetry.validation_funnel must contain exactly one finding_status stage")
    stage = stages[0]
    expected = {
        "input": funnel["findings_detected"],
        "added": 0,
        "kept": funnel["findings_confirmed"],
        "filtered": funnel["findings_filtered"],
        "merged": 0,
        "inconclusive": (funnel["findings_detected"] - funnel["findings_confirmed"] - funnel["findings_filtered"]),
        "failed": 0,
    }
    for key, expected_value in expected.items():
        if stage[key] != expected_value:
            raise EvaluationTelemetryError(
                f"telemetry.validation_funnel finding_status.{key} must equal {expected_value}"
            )


def _exact_keys(value: Mapping[str, Any], allowed: set[str], required: set[str], path: str) -> None:
    extras = sorted(set(value) - allowed)
    missing = sorted(required - set(value))
    if missing:
        raise EvaluationTelemetryError(f"{path} is missing required fields: {', '.join(missing)}")
    if extras:
        raise EvaluationTelemetryError(f"{path} has unexpected fields: {', '.join(extras)}")


def _object(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise EvaluationTelemetryError(f"{path} must be an object")
    if not all(isinstance(key, str) for key in value):
        raise EvaluationTelemetryError(f"{path} keys must be strings")
    return value


def _string(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EvaluationTelemetryError(f"{path} must be a non-empty string")
    return value


def _status(value: Any, path: str) -> str:
    status = _string(value, path)
    if status not in {"complete", "incomplete", "unavailable"}:
        raise EvaluationTelemetryError(f"{path} must be complete, incomplete, or unavailable")
    return status


def _non_negative_int(value: Any, path: str) -> int:
    if type(value) is not int or value < 0:
        raise EvaluationTelemetryError(f"{path} must be a non-negative integer")
    return value
