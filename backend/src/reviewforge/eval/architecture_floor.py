"""Deterministic, offline aggregation for the ReviewForge architecture floor.

Input schema (``reviewforge.architecture-floor-input.v1``)::

    {
      "schema_version": "reviewforge.architecture-floor-input.v1",
      "experiment_id": "holdout-2026-08",
      "coverage_threshold": 0.7,
      "expected_pr_ids": ["owner/repo#123"],
      "expected_runs": [{
        "config_fingerprint": "sha256:...",
        "system_repeat_id": 1,
        "judge_repeat_id": 1,
        "judge_fingerprint": {
          "model": "judge-model", "temp": 0,
          "prompt_sha256": "...", "judge_code_sha256": "...",
          "workload_sha256": "..."
        }
      }],
      "systems": [
        {
          "system_id": "candidate",
          "observations": [{
            "pr_id": "owner/repo#123",
            "system_repeat_id": 1,
            "judge_repeat_id": 1,
            "config_fingerprint": "sha256:...",
            "judge_fingerprint": {
              "model": "judge-model",
              "temp": 0,
              "prompt_sha256": "...",
              "judge_code_sha256": "...",
              "workload_sha256": "..."
            },
            "candidate_artifact_sha256": "...",
            "judgment_artifact_sha256": "...",
            "tp": 2, "fp": 1, "fn": 1,
            "telemetry": {
              "schema_version": 1,
              "resume_mode": "normal",
              "failures": {
                "tasks_failed": 0, "planner": 0, "publication": 0,
                "delivery": 0, "operationally_incomplete": false
              },
              "coverage": {
                "available": true,
                "threshold": 0.7,
                "status": "complete",
                "high_risk": {
                  "total": 3, "resolved": 3, "unresolved": 0,
                  "status": "complete"
                }
              },
              "funnel": {
                "findings_detected": 3, "findings_confirmed": 2,
                "findings_filtered": 1, "findings_reported": 2,
                "publication_candidates": 2, "delivery_attempted": 2,
                "delivery_reported": 2, "tasks_completed": 1,
                "tasks_failed": 0
              },
              "validation_funnel": [{
                "stage": "finding_status", "input": 3, "added": 0,
                "kept": 2, "filtered": 1, "merged": 0,
                "inconclusive": 0, "failed": 0
              }]
            }
          }]
        }
      ],
      "comparisons": [{"baseline": "baseline", "candidate": "candidate"}]
    }

``validation_funnel`` is required.  Every stage must conserve items as
``input + added == kept + filtered + merged + inconclusive + failed`` and the
``finding_status`` stage must exactly partition the flat final counters.
Telemetry needed for the adjusted score is not optional: missing completion or
coverage data leaves the raw score available but marks the system invalid and
sets ``adjusted_f1`` to ``None``.

The manifest is authoritative.  Every system must contain exactly the Cartesian
product of ``expected_pr_ids`` and ``expected_runs``.  Incomplete systems are
reported as excluded and cannot contribute raw or adjusted tail statistics.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from typing import Any

from reviewforge.core.evaluation_telemetry import (
    EvaluationTelemetryError,
    parse_evaluation_telemetry,
)

INPUT_SCHEMA_VERSION = "reviewforge.architecture-floor-input.v1"
OUTPUT_SCHEMA_VERSION = "reviewforge.architecture-floor.v1"
SMALL_N_THRESHOLD = 10


class ArchitectureFloorError(ValueError):
    """Raised when an architecture-floor artifact is structurally unsafe to score."""


def build_architecture_floor(experiment: dict[str, Any]) -> dict[str, Any]:
    """Build a new architecture-floor artifact without mutating ``experiment``.

    Counts are micro-aggregated.  P10 uses nearest-rank over complete-run F1
    values (rank ``ceil(0.10 * n)``), and ``worst_f1`` is their minimum.  A run
    is one config/system-repeat/judge-repeat over the system's complete PR set.
    The adjusted score is::

        raw_f1 * operational_completion_ratio * high_risk_resolution_ratio

    Paired comparisons are emitted only after strict PR, repeat, and judge
    fingerprint checks.  Invalid comparisons retain reasons and null deltas.
    """

    root = _mapping(experiment, "experiment")
    version = root.get("schema_version")
    if version != INPUT_SCHEMA_VERSION:
        raise ArchitectureFloorError(f"experiment.schema_version must be {INPUT_SCHEMA_VERSION!r}; got {version!r}")

    raw_systems = root.get("systems")
    if not isinstance(raw_systems, list) or not raw_systems:
        raise ArchitectureFloorError("experiment.systems must be a non-empty list")
    expected_pr_ids, expected_runs, coverage_threshold = _normalize_manifest(root)

    systems: dict[str, dict[str, Any]] = {}
    observation_indexes: dict[str, dict[tuple[str, str, int, int], dict[str, Any]]] = {}
    for index, raw_system in enumerate(raw_systems):
        path = f"experiment.systems[{index}]"
        system = _mapping(raw_system, path)
        system_id = _non_empty_string(system.get("system_id"), f"{path}.system_id")
        if system_id in systems:
            raise ArchitectureFloorError(f"{path}.system_id duplicates {system_id!r}")
        result, observation_index = _aggregate_system(
            system,
            path,
            expected_pr_ids=expected_pr_ids,
            expected_runs=expected_runs,
            coverage_threshold=coverage_threshold,
        )
        systems[system_id] = result
        observation_indexes[system_id] = observation_index

    comparisons = _build_comparisons(root.get("comparisons", []), systems, observation_indexes)
    experiment_id = root.get("experiment_id")
    if experiment_id is not None:
        experiment_id = _non_empty_string(experiment_id, "experiment.experiment_id")

    return {
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "source_schema_version": INPUT_SCHEMA_VERSION,
        "experiment_id": experiment_id,
        "coverage_threshold": coverage_threshold,
        "expected_pr_ids": list(expected_pr_ids),
        "expected_runs": [
            {
                "config_fingerprint": key[0],
                "system_repeat_id": key[1],
                "judge_repeat_id": key[2],
                "judge_fingerprint": _judge_fingerprint_dict(fingerprint),
            }
            for key, fingerprint in sorted(expected_runs.items())
        ],
        "methodology": {
            "aggregation": "micro",
            "adjusted_f1_formula": ("raw_f1 * operational_completion_ratio * high_risk_resolution_ratio"),
            "p10_method": "nearest-rank over complete-run micro F1",
            "small_n_threshold": SMALL_N_THRESHOLD,
            "pairing": ("exact config, PR, system_repeat_id, judge_repeat_id, and judge fingerprint"),
        },
        "systems": {key: _public_system(systems[key]) for key in sorted(systems)},
        "comparisons": comparisons,
    }


def _normalize_manifest(
    root: Mapping[str, Any],
) -> tuple[tuple[str, ...], dict[tuple[str, int, int], tuple[Any, ...]], float]:
    coverage_threshold = _bounded_probability(root.get("coverage_threshold"), "experiment.coverage_threshold")
    raw_pr_ids = root.get("expected_pr_ids")
    if not isinstance(raw_pr_ids, list) or not raw_pr_ids:
        raise ArchitectureFloorError("experiment.expected_pr_ids must be a non-empty list")
    pr_ids = [
        _non_empty_string(value, f"experiment.expected_pr_ids[{index}]") for index, value in enumerate(raw_pr_ids)
    ]
    if len(set(pr_ids)) != len(pr_ids):
        raise ArchitectureFloorError("experiment.expected_pr_ids must not contain duplicates")

    raw_runs = root.get("expected_runs")
    if not isinstance(raw_runs, list) or not raw_runs:
        raise ArchitectureFloorError("experiment.expected_runs must be a non-empty list")
    runs: dict[tuple[str, int, int], tuple[Any, ...]] = {}
    required = {"config_fingerprint", "system_repeat_id", "judge_repeat_id", "judge_fingerprint"}
    for index, raw_run in enumerate(raw_runs):
        path = f"experiment.expected_runs[{index}]"
        run = _mapping(raw_run, path)
        missing = sorted(required - set(run))
        extras = sorted(set(run) - required)
        if missing:
            raise ArchitectureFloorError(f"{path} is missing required fields: {', '.join(missing)}")
        if extras:
            raise ArchitectureFloorError(f"{path} has unexpected fields: {', '.join(extras)}")
        key = (
            _non_empty_string(run.get("config_fingerprint"), f"{path}.config_fingerprint"),
            _positive_int(run.get("system_repeat_id"), f"{path}.system_repeat_id"),
            _positive_int(run.get("judge_repeat_id"), f"{path}.judge_repeat_id"),
        )
        if key in runs:
            raise ArchitectureFloorError(f"{path} duplicates expected run {key!r}")
        runs[key] = _normalize_judge_fingerprint(run.get("judge_fingerprint"), f"{path}.judge_fingerprint")
    return tuple(sorted(pr_ids)), runs, coverage_threshold


def _aggregate_system(
    system: Mapping[str, Any],
    path: str,
    *,
    expected_pr_ids: tuple[str, ...],
    expected_runs: Mapping[tuple[str, int, int], tuple[Any, ...]],
    coverage_threshold: float,
) -> tuple[dict[str, Any], dict[tuple[str, str, int, int], dict[str, Any]]]:
    raw_observations = system.get("observations")
    if not isinstance(raw_observations, list):
        raise ArchitectureFloorError(f"{path}.observations must be a list")

    observations: list[dict[str, Any]] = []
    observation_index: dict[tuple[str, str, int, int], dict[str, Any]] = {}
    invalid_reasons: list[str] = []
    for index, raw_observation in enumerate(raw_observations):
        observation_path = f"{path}.observations[{index}]"
        observation = _normalize_observation(raw_observation, observation_path, invalid_reasons)
        key = (
            observation["config_fingerprint"],
            observation["pr_id"],
            observation["system_repeat_id"],
            observation["judge_repeat_id"],
        )
        if key in observation_index:
            raise ArchitectureFloorError(
                f"{observation_path} duplicates observation key "
                f"(config_fingerprint={key[0]!r}, pr_id={key[1]!r}, "
                f"system_repeat_id={key[2]}, judge_repeat_id={key[3]})"
            )
        observation_index[key] = observation
        observations.append(observation)

    expected_keys = {
        (config_fingerprint, pr_id, system_repeat_id, judge_repeat_id)
        for config_fingerprint, system_repeat_id, judge_repeat_id in expected_runs
        for pr_id in expected_pr_ids
    }
    actual_keys = set(observation_index)
    missing_keys = sorted(expected_keys - actual_keys)
    unexpected_keys = sorted(actual_keys - expected_keys)
    fingerprint_mismatches = sorted(
        key
        for key in expected_keys & actual_keys
        if observation_index[key]["judge_fingerprint"] != expected_runs[(key[0], key[2], key[3])]
    )
    coverage_threshold_mismatches = sorted(
        key
        for key in expected_keys & actual_keys
        if observation_index[key]["coverage_threshold"] is not None
        and not math.isclose(
            observation_index[key]["coverage_threshold"],
            coverage_threshold,
            rel_tol=0.0,
            abs_tol=1e-12,
        )
    )
    exclusion_reasons: list[str] = []
    if missing_keys:
        exclusion_reasons.append(f"missing {len(missing_keys)} expected PR/run observations")
    if unexpected_keys:
        exclusion_reasons.append(f"found {len(unexpected_keys)} observations outside the manifest")
    if fingerprint_mismatches:
        exclusion_reasons.append(
            f"found {len(fingerprint_mismatches)} observations with a judge fingerprint outside the manifest"
        )
    if coverage_threshold_mismatches:
        invalid_reasons.append(
            f"found {len(coverage_threshold_mismatches)} observations whose coverage threshold "
            "does not match experiment.coverage_threshold"
        )
    manifest_complete = not exclusion_reasons
    invalid_reasons.extend(exclusion_reasons)

    tp = sum(item["tp"] for item in observations)
    fp = sum(item["fp"] for item in observations)
    fn = sum(item["fn"] for item in observations)
    precision, recall, raw_f1 = _metrics(tp, fp, fn)
    run_counts = _run_counts(observations)
    run_score_map = {key: _metrics(*counts)[2] for key, counts in run_counts.items()}
    run_f1 = sorted(run_score_map.values())

    runtime_observations: dict[tuple[str, str, int], dict[str, Any]] = {}
    inconsistent_runtime_keys: set[tuple[str, str, int]] = set()
    inconsistent_candidate_artifact_keys: set[tuple[str, str, int]] = set()
    for item in observations:
        runtime_key = (item["config_fingerprint"], item["pr_id"], item["system_repeat_id"])
        existing = runtime_observations.get(runtime_key)
        if existing is None:
            runtime_observations[runtime_key] = item
        else:
            if existing["telemetry_canonical"] != item["telemetry_canonical"]:
                inconsistent_runtime_keys.add(runtime_key)
            if existing["candidate_artifact_sha256"] != item["candidate_artifact_sha256"]:
                inconsistent_candidate_artifact_keys.add(runtime_key)
    for config_fingerprint, pr_id, system_repeat_id in sorted(inconsistent_runtime_keys):
        invalid_reasons.append(
            "telemetry mismatch across judge repeats for "
            f"(config_fingerprint={config_fingerprint!r}, pr_id={pr_id!r}, "
            f"system_repeat_id={system_repeat_id})"
        )
    for config_fingerprint, pr_id, system_repeat_id in sorted(inconsistent_candidate_artifact_keys):
        invalid_reasons.append(
            "candidate artifact mismatch across judge repeats for "
            f"(config_fingerprint={config_fingerprint!r}, pr_id={pr_id!r}, "
            f"system_repeat_id={system_repeat_id})"
        )

    runtime_values = list(runtime_observations.values())
    completion_values = [item["operationally_incomplete"] for item in runtime_values]
    coverage_values = [item["coverage"] for item in runtime_values]
    telemetry_valid = (
        bool(runtime_values)
        and all(item["telemetry_valid"] for item in runtime_values)
        and all(value is not None for value in completion_values)
        and all(value is not None for value in coverage_values)
        and not inconsistent_runtime_keys
        and not inconsistent_candidate_artifact_keys
        and not coverage_threshold_mismatches
    )
    system_valid = telemetry_valid and manifest_complete

    if telemetry_valid:
        completed = sum(value is False for value in completion_values)
        operational_completion_ratio = completed / len(runtime_values)
        high_risk_total = sum(value[0] for value in coverage_values if value is not None)
        high_risk_resolved = sum(value[1] for value in coverage_values if value is not None)
        high_risk_resolution_ratio = high_risk_resolved / high_risk_total if high_risk_total else 1.0
        adjusted_f1 = raw_f1 * operational_completion_ratio * high_risk_resolution_ratio if system_valid else None
    else:
        completed = None
        operational_completion_ratio = None
        high_risk_total = None
        high_risk_resolved = None
        high_risk_resolution_ratio = None
        adjusted_f1 = None

    adjusted_run_score_map: dict[tuple[str, int, int], float] = {}
    if system_valid:
        for run_key in sorted(expected_runs):
            config_fingerprint, system_repeat_id, _judge_repeat_id = run_key
            run_runtime = [
                runtime_observations[(config_fingerprint, pr_id, system_repeat_id)] for pr_id in expected_pr_ids
            ]
            run_completion = sum(not item["operationally_incomplete"] for item in run_runtime) / len(run_runtime)
            run_high_risk_total = sum(item["coverage"][0] for item in run_runtime)
            run_high_risk_resolved = sum(item["coverage"][1] for item in run_runtime)
            run_resolution = run_high_risk_resolved / run_high_risk_total if run_high_risk_total else 1.0
            adjusted_run_score_map[run_key] = run_score_map[run_key] * run_completion * run_resolution
    adjusted_run_f1 = sorted(adjusted_run_score_map.values())

    config_fingerprints = sorted({key[0] for key in expected_runs})
    run_n = len(expected_runs)
    return (
        {
            "valid": system_valid,
            "invalid_reasons": sorted(set(invalid_reasons)),
            "excluded": not manifest_complete,
            "exclusion_reasons": exclusion_reasons,
            "missing_observation_count": len(missing_keys),
            "unexpected_observation_count": len(unexpected_keys),
            "fingerprint_mismatch_count": len(fingerprint_mismatches),
            "sample_size": run_n,
            "run_sample_size": run_n,
            "observation_count": len(observations),
            "small_n": run_n < SMALL_N_THRESHOLD,
            "small_n_note": (
                f"run n={run_n} is below the predeclared threshold {SMALL_N_THRESHOLD}; "
                "tail statistics are descriptive only."
                if run_n < SMALL_N_THRESHOLD
                else None
            ),
            "config_fingerprints": config_fingerprints,
            "true_positives": tp,
            "false_positives": fp,
            "false_negatives": fn,
            "micro_precision": _rounded(precision),
            "micro_recall": _rounded(recall),
            "raw_f1": _rounded(raw_f1),
            "p10_f1": _rounded(_nearest_rank(run_f1, 0.10)) if manifest_complete else None,
            "worst_f1": _rounded(run_f1[0]) if manifest_complete else None,
            "adjusted_p10_f1": _rounded(_nearest_rank(adjusted_run_f1, 0.10)) if system_valid else None,
            "adjusted_worst_f1": _rounded(adjusted_run_f1[0]) if system_valid else None,
            "operational_completed": completed,
            "operational_total": len(runtime_values) if telemetry_valid else None,
            "operational_completion_ratio": _rounded(operational_completion_ratio),
            "high_risk_resolved": high_risk_resolved,
            "high_risk_total": high_risk_total,
            "high_risk_resolution_ratio": _rounded(high_risk_resolution_ratio),
            "adjusted_f1": _rounded(adjusted_f1),
            "artifact_provenance": [
                {
                    "config_fingerprint": item["config_fingerprint"],
                    "pr_id": item["pr_id"],
                    "system_repeat_id": item["system_repeat_id"],
                    "judge_repeat_id": item["judge_repeat_id"],
                    "candidate_artifact_sha256": item["candidate_artifact_sha256"],
                    "judgment_artifact_sha256": item["judgment_artifact_sha256"],
                }
                for item in sorted(
                    observations,
                    key=lambda value: (
                        value["config_fingerprint"],
                        value["pr_id"],
                        value["system_repeat_id"],
                        value["judge_repeat_id"],
                    ),
                )
            ],
            "_raw_f1_exact": raw_f1,
            "_adjusted_f1_exact": adjusted_f1,
            "_run_scores_exact": run_score_map,
            "_adjusted_run_scores_exact": adjusted_run_score_map,
        },
        observation_index,
    )


def _normalize_observation(raw_observation: Any, path: str, invalid_reasons: list[str]) -> dict[str, Any]:
    observation = _mapping(raw_observation, path)
    pr_id = _non_empty_string(observation.get("pr_id"), f"{path}.pr_id")
    system_repeat_id = _positive_int(observation.get("system_repeat_id"), f"{path}.system_repeat_id")
    judge_repeat_id = _positive_int(observation.get("judge_repeat_id"), f"{path}.judge_repeat_id")
    config_fingerprint = _non_empty_string(observation.get("config_fingerprint"), f"{path}.config_fingerprint")
    judge_fingerprint = _normalize_judge_fingerprint(observation.get("judge_fingerprint"), f"{path}.judge_fingerprint")
    tp = _non_negative_int(observation.get("tp"), f"{path}.tp")
    fp = _non_negative_int(observation.get("fp"), f"{path}.fp")
    fn = _non_negative_int(observation.get("fn"), f"{path}.fn")
    candidate_artifact_sha256 = _non_empty_string(
        observation.get("candidate_artifact_sha256"),
        f"{path}.candidate_artifact_sha256",
    )
    judgment_artifact_sha256 = _non_empty_string(
        observation.get("judgment_artifact_sha256"),
        f"{path}.judgment_artifact_sha256",
    )

    operationally_incomplete: bool | None = None
    coverage: tuple[int, int] | None = None
    coverage_threshold: float | None = None
    telemetry_valid = False
    normalized_telemetry: dict[str, Any] | None = None
    try:
        parsed_telemetry = parse_evaluation_telemetry(observation.get("telemetry"))
    except EvaluationTelemetryError as exc:
        invalid_reasons.append(f"{path}: {exc}")
    else:
        normalized_telemetry = parsed_telemetry.to_dict()
        operationally_incomplete = bool(parsed_telemetry.failures["operationally_incomplete"])
        high_risk = parsed_telemetry.coverage["high_risk"]
        coverage = (int(high_risk["total"]), int(high_risk["resolved"]))
        coverage_threshold = float(parsed_telemetry.coverage["threshold"])
        telemetry_valid = parsed_telemetry.coverage["available"] is True
        if not telemetry_valid:
            invalid_reasons.append(f"{path}.telemetry.coverage is unavailable")

    telemetry_canonical = (
        _canonical_json(normalized_telemetry, f"{path}.telemetry") if normalized_telemetry is not None else None
    )

    return {
        "pr_id": pr_id,
        "system_repeat_id": system_repeat_id,
        "judge_repeat_id": judge_repeat_id,
        "config_fingerprint": config_fingerprint,
        "judge_fingerprint": judge_fingerprint,
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "candidate_artifact_sha256": candidate_artifact_sha256,
        "judgment_artifact_sha256": judgment_artifact_sha256,
        "operationally_incomplete": operationally_incomplete,
        "coverage": coverage,
        "coverage_threshold": coverage_threshold,
        "telemetry_valid": telemetry_valid,
        "telemetry_canonical": telemetry_canonical,
    }


def _normalize_judge_fingerprint(raw: Any, path: str) -> tuple[Any, ...]:
    fingerprint = _mapping(raw, path)
    model = _non_empty_string(fingerprint.get("model"), f"{path}.model")
    temperature = fingerprint.get("temp")
    if isinstance(temperature, bool) or not isinstance(temperature, (int, float)):
        raise ArchitectureFloorError(f"{path}.temp must be a finite number")
    if not math.isfinite(float(temperature)):
        raise ArchitectureFloorError(f"{path}.temp must be a finite number")
    prompt_sha256 = _non_empty_string(fingerprint.get("prompt_sha256"), f"{path}.prompt_sha256")
    judge_code_sha256 = _non_empty_string(
        fingerprint.get("judge_code_sha256"),
        f"{path}.judge_code_sha256",
    )
    workload_sha256 = _non_empty_string(fingerprint.get("workload_sha256"), f"{path}.workload_sha256")
    return (model, float(temperature), prompt_sha256, judge_code_sha256, workload_sha256)


def _build_comparisons(
    raw_comparisons: Any,
    systems: Mapping[str, dict[str, Any]],
    observation_indexes: Mapping[str, dict[tuple[str, str, int, int], dict[str, Any]]],
) -> list[dict[str, Any]]:
    if not isinstance(raw_comparisons, list):
        raise ArchitectureFloorError("experiment.comparisons must be a list")
    normalized: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for index, raw_comparison in enumerate(raw_comparisons):
        path = f"experiment.comparisons[{index}]"
        comparison = _mapping(raw_comparison, path)
        baseline = _non_empty_string(comparison.get("baseline"), f"{path}.baseline")
        candidate = _non_empty_string(comparison.get("candidate"), f"{path}.candidate")
        if baseline == candidate:
            raise ArchitectureFloorError(f"{path} must compare two different systems")
        for role, system_id in (("baseline", baseline), ("candidate", candidate)):
            if system_id not in systems:
                raise ArchitectureFloorError(f"{path}.{role} references unknown system {system_id!r}")
        key = (baseline, candidate)
        if key in seen:
            raise ArchitectureFloorError(f"{path} duplicates comparison {baseline!r} -> {candidate!r}")
        seen.add(key)
        normalized.append(key)

    output = []
    for baseline, candidate in sorted(normalized):
        baseline_index = observation_indexes[baseline]
        candidate_index = observation_indexes[candidate]
        reasons = _pairing_errors(baseline, candidate, baseline_index, candidate_index)
        if not systems[baseline]["valid"]:
            reasons.append(f"baseline system {baseline!r} has an invalid architecture-floor sample")
        if not systems[candidate]["valid"]:
            reasons.append(f"candidate system {candidate!r} has an invalid architecture-floor sample")

        valid = not reasons
        pair_deltas: list[float] = []
        adjusted_pair_deltas: list[float] = []
        if valid:
            baseline_runs = systems[baseline]["_run_scores_exact"]
            candidate_runs = systems[candidate]["_run_scores_exact"]
            pair_deltas = [candidate_runs[key] - baseline_runs[key] for key in sorted(baseline_runs)]
            baseline_adjusted = systems[baseline]["_adjusted_run_scores_exact"]
            candidate_adjusted = systems[candidate]["_adjusted_run_scores_exact"]
            adjusted_pair_deltas = [
                candidate_adjusted[key] - baseline_adjusted[key] for key in sorted(baseline_adjusted)
            ]

        n = len(pair_deltas) if valid else 0
        output.append(
            {
                "baseline": baseline,
                "candidate": candidate,
                "valid": valid,
                "invalid_reasons": reasons,
                "paired_sample_size": n if valid else None,
                "small_n": n < SMALL_N_THRESHOLD if valid else None,
                "raw_f1_delta": _delta(systems[candidate]["_raw_f1_exact"], systems[baseline]["_raw_f1_exact"])
                if valid
                else None,
                "adjusted_f1_delta": _delta(
                    systems[candidate]["_adjusted_f1_exact"], systems[baseline]["_adjusted_f1_exact"]
                )
                if valid
                else None,
                "p10_f1_delta": _rounded(_nearest_rank(sorted(pair_deltas), 0.10)) if valid else None,
                "worst_f1_delta": _rounded(min(pair_deltas)) if valid else None,
                "adjusted_p10_f1_delta": (
                    _rounded(_nearest_rank(sorted(adjusted_pair_deltas), 0.10)) if valid else None
                ),
                "adjusted_worst_f1_delta": _rounded(min(adjusted_pair_deltas)) if valid else None,
                "raw_win_tie_loss": _win_tie_loss(pair_deltas) if valid else None,
                "adjusted_win_tie_loss": _win_tie_loss(adjusted_pair_deltas) if valid else None,
            }
        )
    return output


def _pairing_errors(
    baseline: str,
    candidate: str,
    baseline_index: Mapping[tuple[str, str, int, int], dict[str, Any]],
    candidate_index: Mapping[tuple[str, str, int, int], dict[str, Any]],
) -> list[str]:
    reasons: list[str] = []
    baseline_configs = {key[0] for key in baseline_index}
    candidate_configs = {key[0] for key in candidate_index}
    if baseline_configs != candidate_configs:
        reasons.append(
            f"config set mismatch: {baseline!r}={sorted(baseline_configs)!r}, "
            f"{candidate!r}={sorted(candidate_configs)!r}"
        )

    baseline_runs = {(key[0], key[2], key[3]) for key in baseline_index}
    candidate_runs = {(key[0], key[2], key[3]) for key in candidate_index}
    if baseline_runs != candidate_runs:
        reasons.append(
            f"run set mismatch: {baseline!r}={sorted(baseline_runs)!r}, {candidate!r}={sorted(candidate_runs)!r}"
        )

    for run_key in sorted(baseline_runs & candidate_runs):
        baseline_prs = {key[1] for key in baseline_index if (key[0], key[2], key[3]) == run_key}
        candidate_prs = {key[1] for key in candidate_index if (key[0], key[2], key[3]) == run_key}
        if baseline_prs != candidate_prs:
            reasons.append(
                f"PR set mismatch for run {run_key!r}: {baseline!r}={sorted(baseline_prs)!r}, "
                f"{candidate!r}={sorted(candidate_prs)!r}"
            )

    for key in sorted(set(baseline_index) & set(candidate_index)):
        if baseline_index[key]["judge_fingerprint"] != candidate_index[key]["judge_fingerprint"]:
            reasons.append(
                "judge fingerprint mismatch for "
                f"(config_fingerprint={key[0]!r}, pr_id={key[1]!r}, "
                f"system_repeat_id={key[2]}, judge_repeat_id={key[3]})"
            )
    return reasons


def _run_scores(observations: Any) -> dict[tuple[str, int, int], float]:
    return {key: _metrics(*counts)[2] for key, counts in _run_counts(observations).items()}


def _run_counts(observations: Any) -> dict[tuple[str, int, int], list[int]]:
    run_counts: dict[tuple[str, int, int], list[int]] = {}
    for item in observations:
        key = (item["config_fingerprint"], item["system_repeat_id"], item["judge_repeat_id"])
        counts = run_counts.setdefault(key, [0, 0, 0])
        counts[0] += item["tp"]
        counts[1] += item["fp"]
        counts[2] += item["fn"]
    return run_counts


def _public_system(system: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in system.items() if not key.startswith("_")}


def _win_tie_loss(deltas: list[float]) -> dict[str, int]:
    epsilon = 1e-12
    return {
        "wins": sum(delta > epsilon for delta in deltas),
        "ties": sum(abs(delta) <= epsilon for delta in deltas),
        "losses": sum(delta < -epsilon for delta in deltas),
    }


def _judge_fingerprint_dict(fingerprint: tuple[Any, ...]) -> dict[str, Any]:
    return {
        "model": fingerprint[0],
        "temp": fingerprint[1],
        "prompt_sha256": fingerprint[2],
        "judge_code_sha256": fingerprint[3],
        "workload_sha256": fingerprint[4],
    }


def _metrics(tp: int, fp: int, fn: int) -> tuple[float, float, float]:
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return precision, recall, f1


def _nearest_rank(sorted_values: list[float], percentile: float) -> float:
    rank = max(1, math.ceil(percentile * len(sorted_values)))
    return sorted_values[rank - 1]


def _delta(candidate: float | None, baseline: float | None) -> float | None:
    if candidate is None or baseline is None:
        return None
    return _rounded(candidate - baseline)


def _rounded(value: float | None) -> float | None:
    return round(value, 6) if value is not None else None


def _canonical_json(value: Any, path: str) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ArchitectureFloorError(f"{path} must contain only finite JSON values: {exc}") from exc


def _mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ArchitectureFloorError(f"{path} must be an object")
    return value


def _non_empty_string(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ArchitectureFloorError(f"{path} must be a non-empty string")
    return value


def _positive_int(value: Any, path: str) -> int:
    number = _non_negative_int(value, path)
    if number < 1:
        raise ArchitectureFloorError(f"{path} must be at least 1")
    return number


def _non_negative_int(value: Any, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ArchitectureFloorError(f"{path} must be a non-negative integer")
    return value


def _bounded_probability(value: Any, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise ArchitectureFloorError(f"{path} must be a finite number between 0 and 1")
    normalized = float(value)
    if not 0.0 <= normalized <= 1.0:
        raise ArchitectureFloorError(f"{path} must be a finite number between 0 and 1")
    return normalized
