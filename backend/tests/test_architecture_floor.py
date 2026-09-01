from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

from reviewforge.eval.architecture_floor import ArchitectureFloorError, build_architecture_floor


def _fingerprint(*, judge_code: str = "judge-code-a") -> dict:
    return {
        "model": "judge-v1",
        "temp": 0,
        "prompt_sha256": "prompt-a",
        "judge_code_sha256": judge_code,
        "workload_sha256": "workload-a",
    }


def _observation(
    pr_id: str,
    *,
    tp: int,
    fp: int,
    fn: int,
    repeat: int = 1,
    judge_repeat: int = 1,
    incomplete: bool = False,
    high_risk_total: int = 2,
    high_risk_resolved: int = 2,
    config: str = "config-a",
) -> dict:
    coverage_status = "complete" if high_risk_resolved == high_risk_total else "incomplete"
    return {
        "pr_id": pr_id,
        "system_repeat_id": repeat,
        "judge_repeat_id": judge_repeat,
        "config_fingerprint": config,
        "judge_fingerprint": _fingerprint(),
        "candidate_artifact_sha256": f"candidate-{config}-{repeat}",
        "judgment_artifact_sha256": f"judgment-{config}-{repeat}-{judge_repeat}",
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "telemetry": {
            "schema_version": 1,
            "resume_mode": "normal",
            "failures": {
                "tasks_failed": int(incomplete),
                "planner": 0,
                "publication": 0,
                "delivery": 0,
                "operationally_incomplete": incomplete,
            },
            "coverage": {
                "available": True,
                "threshold": 0.7,
                "status": coverage_status,
                "high_risk": {
                    "total": high_risk_total,
                    "resolved": high_risk_resolved,
                    "unresolved": high_risk_total - high_risk_resolved,
                    "status": coverage_status,
                },
            },
            "funnel": {
                "findings_detected": tp + fp,
                "findings_confirmed": tp,
                "findings_filtered": fp,
                "findings_reported": tp,
                "publication_candidates": tp,
                "delivery_attempted": tp,
                "delivery_reported": tp,
                "tasks_completed": 1,
                "tasks_failed": int(incomplete),
            },
            "validation_funnel": [
                {
                    "stage": "finding_status",
                    "input": tp + fp,
                    "added": 0,
                    "kept": tp,
                    "filtered": fp,
                    "merged": 0,
                    "inconclusive": 0,
                    "failed": 0,
                }
            ],
        },
    }


def _manifest(observations: list[dict]) -> dict:
    expected_runs: dict[tuple[str, int, int], dict] = {}
    for observation in observations:
        key = (
            observation["config_fingerprint"],
            observation["system_repeat_id"],
            observation["judge_repeat_id"],
        )
        expected_runs[key] = {
            "config_fingerprint": key[0],
            "system_repeat_id": key[1],
            "judge_repeat_id": key[2],
            "judge_fingerprint": copy.deepcopy(observation["judge_fingerprint"]),
        }
    return {
        "coverage_threshold": 0.7,
        "expected_pr_ids": sorted({observation["pr_id"] for observation in observations}),
        "expected_runs": [expected_runs[key] for key in sorted(expected_runs)],
    }


def _experiment() -> dict:
    baseline = [
        _observation("repo#1", tp=1, fp=1, fn=1),
        _observation("repo#2", tp=0, fp=1, fn=1, incomplete=True, high_risk_resolved=1),
    ]
    candidate = [
        _observation("repo#1", tp=2, fp=0, fn=0),
        _observation("repo#2", tp=1, fp=1, fn=0),
    ]
    return {
        "schema_version": "reviewforge.architecture-floor-input.v1",
        "experiment_id": "floor-smoke",
        **_manifest(candidate),
        "systems": [
            {"system_id": "candidate", "observations": candidate},
            {"system_id": "baseline", "observations": baseline},
        ],
        "comparisons": [{"baseline": "baseline", "candidate": "candidate"}],
    }


def test_builds_micro_tail_adjusted_and_strict_paired_metrics_without_mutating_input():
    experiment = _experiment()
    original = copy.deepcopy(experiment)

    result = build_architecture_floor(experiment)

    assert experiment == original
    baseline = result["systems"]["baseline"]
    assert baseline["true_positives"] == 1
    assert baseline["false_positives"] == 2
    assert baseline["false_negatives"] == 2
    assert baseline["raw_f1"] == 0.333333
    assert baseline["p10_f1"] == 0.333333
    assert baseline["worst_f1"] == 0.333333
    assert baseline["adjusted_p10_f1"] == 0.125
    assert baseline["adjusted_worst_f1"] == 0.125
    assert baseline["sample_size"] == 1
    assert baseline["run_sample_size"] == 1
    assert baseline["observation_count"] == 2
    assert baseline["operational_completion_ratio"] == 0.5
    assert baseline["high_risk_resolution_ratio"] == 0.75
    assert baseline["adjusted_f1"] == 0.125
    assert baseline["small_n"] is True
    assert "descriptive only" in baseline["small_n_note"]

    comparison = result["comparisons"][0]
    assert comparison["valid"] is True
    assert comparison["paired_sample_size"] == 1
    assert comparison["raw_f1_delta"] == 0.52381
    assert comparison["adjusted_f1_delta"] == 0.732143
    assert comparison["p10_f1_delta"] == 0.52381
    assert comparison["worst_f1_delta"] == 0.52381
    assert comparison["adjusted_p10_f1_delta"] == 0.732143
    assert comparison["adjusted_worst_f1_delta"] == 0.732143
    assert comparison["raw_win_tie_loss"] == {"wins": 1, "ties": 0, "losses": 0}
    assert comparison["adjusted_win_tie_loss"] == {"wins": 1, "ties": 0, "losses": 0}


def test_missing_telemetry_keeps_raw_metrics_but_invalidates_adjusted_score():
    experiment = _experiment()
    del experiment["systems"][0]["observations"][0]["telemetry"]

    result = build_architecture_floor(experiment)

    candidate = result["systems"]["candidate"]
    assert candidate["raw_f1"] == 0.857143
    assert candidate["valid"] is False
    assert candidate["adjusted_f1"] is None
    assert candidate["operational_completion_ratio"] is None
    assert any("telemetry must be an object" in reason for reason in candidate["invalid_reasons"])
    assert result["comparisons"][0]["valid"] is False


def test_unavailable_or_legacy_coverage_cannot_vacuously_improve_adjusted_score():
    experiment = _experiment()
    coverage = experiment["systems"][0]["observations"][0]["telemetry"]["coverage"]
    coverage["available"] = False
    coverage["status"] = "unavailable"
    coverage["high_risk"] = {"total": 0, "resolved": 0, "unresolved": 0, "status": "unavailable"}

    candidate = build_architecture_floor(experiment)["systems"]["candidate"]

    assert candidate["raw_f1"] == 0.857143
    assert candidate["valid"] is False
    assert candidate["adjusted_f1"] is None
    assert any("coverage is unavailable" in reason for reason in candidate["invalid_reasons"])


def test_runtime_telemetry_is_deduplicated_and_must_match_across_judge_repeats():
    experiment = _experiment()
    candidate = experiment["systems"][0]["observations"]
    candidate_repeats = copy.deepcopy(candidate)
    for repeated in candidate_repeats:
        repeated["judge_repeat_id"] = 2
    candidate.extend(candidate_repeats)

    baseline = experiment["systems"][1]["observations"]
    baseline_repeats = copy.deepcopy(baseline)
    for repeated in baseline_repeats:
        repeated["judge_repeat_id"] = 2
    baseline.extend(baseline_repeats)
    experiment.update(_manifest(candidate))

    candidate_repeats[0]["judgment_artifact_sha256"] = "different-judgment-output"
    result = build_architecture_floor(experiment)
    candidate_result = result["systems"]["candidate"]
    assert candidate_result["sample_size"] == 2
    assert candidate_result["observation_count"] == 4
    assert candidate_result["operational_total"] == 2
    assert candidate_result["high_risk_total"] == 4

    candidate_repeats[0]["candidate_artifact_sha256"] = "different-candidate-input"
    artifact_invalid = build_architecture_floor(experiment)["systems"]["candidate"]
    assert artifact_invalid["valid"] is False
    assert any("candidate artifact mismatch" in reason for reason in artifact_invalid["invalid_reasons"])
    candidate_repeats[0]["candidate_artifact_sha256"] = candidate[0]["candidate_artifact_sha256"]

    candidate_repeats[0]["telemetry"]["coverage"]["high_risk"]["resolved"] = 1
    invalid_result = build_architecture_floor(experiment)["systems"]["candidate"]
    assert invalid_result["valid"] is False
    assert invalid_result["adjusted_f1"] is None
    assert any("telemetry mismatch across judge repeats" in reason for reason in invalid_result["invalid_reasons"])


def test_tail_metrics_use_complete_run_scores_not_individual_pr_difficulty():
    observations = [
        _observation("easy", tp=9, fp=0, fn=0, repeat=1),
        _observation("hard", tp=0, fp=0, fn=1, repeat=1),
        _observation("easy", tp=1, fp=0, fn=0, repeat=2),
        _observation("hard", tp=0, fp=0, fn=9, repeat=2),
    ]
    experiment = {
        "schema_version": "reviewforge.architecture-floor-input.v1",
        **_manifest(observations),
        "systems": [{"system_id": "architecture-a", "observations": observations}],
    }

    result = build_architecture_floor(experiment)["systems"]["architecture-a"]

    assert result["observation_count"] == 4
    assert result["run_sample_size"] == 2
    assert result["raw_f1"] == 0.666667
    assert result["p10_f1"] == 0.181818
    assert result["worst_f1"] == 0.181818


def test_multiple_configs_with_same_repeat_ids_are_distinct_complete_runs():
    observations = [
        _observation("pr-1", tp=1, fp=0, fn=0, config="config-a"),
        _observation("pr-2", tp=1, fp=0, fn=0, config="config-a"),
        _observation("pr-1", tp=1, fp=1, fn=0, config="config-b"),
        _observation("pr-2", tp=0, fp=0, fn=1, config="config-b"),
    ]
    experiment = {
        "schema_version": "reviewforge.architecture-floor-input.v1",
        **_manifest(observations),
        "systems": [{"system_id": "architecture-a", "observations": observations}],
    }

    result = build_architecture_floor(experiment)["systems"]["architecture-a"]

    assert result["valid"] is True
    assert result["config_fingerprints"] == ["config-a", "config-b"]
    assert result["sample_size"] == 2
    assert result["p10_f1"] == 0.5
    assert result["worst_f1"] == 0.5


def test_incomplete_run_pr_set_is_invalid_but_raw_score_remains_available():
    observations = [
        _observation("pr-1", tp=1, fp=0, fn=0, repeat=1),
        _observation("pr-2", tp=1, fp=0, fn=0, repeat=1),
        _observation("pr-1", tp=1, fp=0, fn=0, repeat=2),
    ]
    manifest_observations = [*observations, _observation("pr-2", tp=0, fp=0, fn=0, repeat=2)]
    experiment = {
        "schema_version": "reviewforge.architecture-floor-input.v1",
        **_manifest(manifest_observations),
        "systems": [{"system_id": "architecture-a", "observations": observations}],
    }

    result = build_architecture_floor(experiment)["systems"]["architecture-a"]

    assert result["raw_f1"] == 1.0
    assert result["valid"] is False
    assert result["excluded"] is True
    assert result["p10_f1"] is None
    assert result["adjusted_p10_f1"] is None
    assert result["adjusted_f1"] is None
    assert any("missing 1 expected" in reason for reason in result["invalid_reasons"])


def test_zero_observation_system_is_excluded_instead_of_aborting_the_experiment():
    experiment = _experiment()
    experiment["systems"][0]["observations"] = []

    result = build_architecture_floor(experiment)
    candidate = result["systems"]["candidate"]

    assert candidate["excluded"] is True
    assert candidate["observation_count"] == 0
    assert candidate["raw_f1"] == 0.0
    assert candidate["p10_f1"] is None
    assert candidate["worst_f1"] is None
    assert candidate["adjusted_p10_f1"] is None
    assert result["comparisons"][0]["valid"] is False


def test_nearest_rank_p10_uses_second_run_for_eleven_samples_and_zero_counts_score_zero():
    observations = [_observation("pr-1", tp=0, fp=0, fn=0, repeat=1)]
    observations.append(_observation("pr-1", tp=1, fp=1, fn=1, repeat=2))
    observations.extend(_observation("pr-1", tp=1, fp=0, fn=0, repeat=repeat) for repeat in range(3, 12))
    experiment = {
        "schema_version": "reviewforge.architecture-floor-input.v1",
        **_manifest(observations),
        "systems": [{"system_id": "architecture-a", "observations": observations}],
    }

    result = build_architecture_floor(experiment)["systems"]["architecture-a"]

    assert result["run_sample_size"] == 11
    assert result["small_n"] is False
    assert result["worst_f1"] == 0.0
    assert result["p10_f1"] == 0.5


def test_comparison_delta_rounds_once_from_exact_scores():
    baseline = _observation("pr-1", tp=1, fp=2, fn=2)
    candidate = _observation("pr-1", tp=2, fp=1, fn=1)
    experiment = {
        "schema_version": "reviewforge.architecture-floor-input.v1",
        **_manifest([baseline]),
        "systems": [
            {"system_id": "baseline", "observations": [baseline]},
            {"system_id": "candidate", "observations": [candidate]},
        ],
        "comparisons": [{"baseline": "baseline", "candidate": "candidate"}],
    }

    comparison = build_architecture_floor(experiment)["comparisons"][0]

    assert comparison["raw_f1_delta"] == 0.333333
    assert comparison["adjusted_f1_delta"] == 0.333333


def test_telemetry_schema_version_requires_integer_one():
    experiment = _experiment()
    experiment["systems"][0]["observations"][0]["telemetry"]["schema_version"] = True

    candidate = build_architecture_floor(experiment)["systems"]["candidate"]

    assert candidate["valid"] is False
    assert candidate["adjusted_f1"] is None
    assert any("schema_version must be integer 1" in reason for reason in candidate["invalid_reasons"])


def test_observed_coverage_threshold_must_match_predeclared_experiment_policy():
    experiment = _experiment()
    experiment["systems"][0]["observations"][0]["telemetry"]["coverage"]["threshold"] = 0.2

    candidate = build_architecture_floor(experiment)["systems"]["candidate"]

    assert candidate["valid"] is False
    assert candidate["adjusted_f1"] is None
    assert any("coverage threshold" in reason for reason in candidate["invalid_reasons"])


def test_pairing_rejects_repeat_and_judge_fingerprint_mismatches():
    experiment = _experiment()
    candidate = experiment["systems"][0]["observations"]
    candidate[0]["system_repeat_id"] = 2
    candidate[1]["judge_fingerprint"] = _fingerprint(judge_code="judge-code-b")

    comparison = build_architecture_floor(experiment)["comparisons"][0]

    assert comparison["valid"] is False
    assert comparison["raw_f1_delta"] is None
    assert any("run set mismatch" in reason for reason in comparison["invalid_reasons"])
    assert any("judge fingerprint mismatch" in reason for reason in comparison["invalid_reasons"])


def test_system_specific_candidate_and_judgment_artifacts_do_not_break_pairing():
    experiment = _experiment()
    for observation in experiment["systems"][0]["observations"]:
        observation["candidate_artifact_sha256"] = "candidate-system-a"
        observation["judgment_artifact_sha256"] = "judgment-system-a"
    for observation in experiment["systems"][1]["observations"]:
        observation["candidate_artifact_sha256"] = "candidate-system-b"
        observation["judgment_artifact_sha256"] = "judgment-system-b"

    result = build_architecture_floor(experiment)

    assert result["comparisons"][0]["valid"] is True
    assert result["systems"]["candidate"]["artifact_provenance"][0]["candidate_artifact_sha256"] == (
        "candidate-system-a"
    )


def test_funnel_conservation_allows_added_and_rejects_loss():
    valid = _experiment()
    assert build_architecture_floor(valid)["systems"]["candidate"]["valid"] is True

    invalid = _experiment()
    invalid["systems"][0]["observations"][0]["telemetry"]["validation_funnel"][0]["kept"] = 1

    result = build_architecture_floor(invalid)["systems"]["candidate"]
    assert result["valid"] is False
    assert any("violates input + added" in reason for reason in result["invalid_reasons"])


def test_rejects_legacy_or_unknown_input_schema():
    with pytest.raises(ArchitectureFloorError, match="schema_version"):
        build_architecture_floor({"systems": []})
    with pytest.raises(ArchitectureFloorError, match="expected_pr_ids"):
        build_architecture_floor(
            {
                "schema_version": "reviewforge.architecture-floor-input.v1",
                "coverage_threshold": 0.7,
                "systems": [{"system_id": "candidate", "observations": [_observation("pr-1", tp=1, fp=0, fn=0)]}],
            }
        )


def test_cli_writes_deterministic_new_artifact_and_refuses_source_overwrite(tmp_path: Path):
    input_path = tmp_path / "legacy-input.json"
    output_path = tmp_path / "architecture-floor.json"
    source_text = json.dumps(_experiment(), ensure_ascii=False)
    input_path.write_text(source_text, encoding="utf-8")
    script = Path(__file__).resolve().parents[1] / "scripts" / "eval_architecture_floor.py"

    command = [sys.executable, str(script), "--input", str(input_path), "--out", str(output_path)]
    first = subprocess.run(command, capture_output=True, check=True, text=True)
    first_output = output_path.read_text(encoding="utf-8")
    second = subprocess.run(command, capture_output=True, check=True, text=True)

    assert first.stdout == second.stdout
    assert first_output == output_path.read_text(encoding="utf-8")
    assert input_path.read_text(encoding="utf-8") == source_text
    assert json.loads(first_output)["schema_version"] == "reviewforge.architecture-floor.v1"
    assert not list(tmp_path.glob(".architecture-floor.json.*.tmp"))

    overwrite = subprocess.run(
        [sys.executable, str(script), "--input", str(input_path), "--out", str(input_path)],
        capture_output=True,
        check=False,
        text=True,
    )
    assert overwrite.returncode != 0
    assert "never overwritten" in overwrite.stderr
    assert input_path.read_text(encoding="utf-8") == source_text

    write_failure = subprocess.run(
        [sys.executable, str(script), "--input", str(input_path), "--out", str(tmp_path)],
        capture_output=True,
        check=False,
        text=True,
    )
    assert write_failure.returncode != 0
    assert write_failure.stdout == ""
    assert "failed to write" in write_failure.stderr
    assert not list(tmp_path.parent.glob(f".{tmp_path.name}.*.tmp"))
