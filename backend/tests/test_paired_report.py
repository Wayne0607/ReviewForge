import json
from pathlib import Path

import pytest

from reviewforge.eval.paired_report import build_paired_report, compare_judged, render_markdown


def _artifact(rows):
    return {"completed": {pr_id: {"reviewforge": metrics} for pr_id, metrics in rows.items()}}


def test_compare_judged_builds_per_pr_delta_and_win_tie_loss():
    baseline = _artifact(
        {
            "pr-b": {"tp": 0, "fp": 1, "fn": 1},
            "pr-a": {"tp": 1, "fp": 1, "fn": 1},
        }
    )
    candidate = _artifact(
        {
            "pr-b": {"tp": 0, "fp": 1, "fn": 1},
            "pr-a": {"tp": 1, "fp": 0, "fn": 0},
        }
    )

    report = compare_judged(baseline, candidate)

    assert report["schema_version"] == "reviewforge.paired-report.v1"
    assert [row["pr_id"] for row in report["per_pr"]] == ["pr-a", "pr-b"]
    assert report["summary"]["win"] == 1
    assert report["summary"]["tie"] == 1
    assert report["summary"]["loss"] == 0
    assert report["per_pr"][0]["baseline"]["f1"] == 0.5
    assert report["per_pr"][0]["candidate"]["f1"] == 1.0
    assert report["per_pr"][0]["delta_f1"] == 0.5
    assert report["per_pr"][1]["baseline"]["precision"] == 0.0
    assert report["per_pr"][1]["baseline"]["recall"] == 0.0
    assert report["per_pr"][1]["baseline"]["f1"] == 0.0


def test_zero_zero_zero_matches_strict_judge_zero_metrics():
    artifact = _artifact({"pr-1": {"tp": 0, "fp": 0, "fn": 0}})

    report = compare_judged(artifact, artifact)

    metrics = report["per_pr"][0]["baseline"]
    assert metrics["precision"] == 0.0
    assert metrics["recall"] == 0.0
    assert metrics["f1"] == 0.0


def test_paired_report_fails_closed_when_pr_sets_differ():
    baseline = _artifact({"pr-a": {"tp": 1, "fp": 0, "fn": 0}})
    candidate = _artifact({"pr-b": {"tp": 1, "fp": 0, "fn": 0}})

    with pytest.raises(ValueError, match="pr-set-mismatch") as caught:
        compare_judged(baseline, candidate)

    assert "pr-a" in str(caught.value)
    assert "pr-b" in str(caught.value)


def test_paired_report_fails_closed_for_missing_requested_system_without_fallback():
    baseline = _artifact({"pr-1": {"tp": 1, "fp": 0, "fn": 0}})
    candidate = {"completed": {"pr-1": {"qodo-v2": {"tp": 1, "fp": 0, "fn": 0}}}}

    with pytest.raises(ValueError, match="missing-system") as caught:
        compare_judged(baseline, candidate)

    assert "qodo-v2" not in str(caught.value)


def test_paired_report_fails_closed_for_judge_errors_without_leaking_error_content():
    baseline = {
        "completed": {
            "pr-1": {
                "reviewforge": {
                    "tp": 1,
                    "fp": 0,
                    "fn": 0,
                    "errors": [{"error": "secret prompt text must not leak"}],
                }
            }
        }
    }
    candidate = _artifact({"pr-1": {"tp": 1, "fp": 0, "fn": 0}})

    with pytest.raises(ValueError, match="judge-errors") as caught:
        compare_judged(baseline, candidate)

    assert "secret prompt text" not in str(caught.value)


_REPRESENTATIVE10_URLS = (
    "https://github.com/keycloak/keycloak/pull/37429",
    "https://github.com/keycloak/keycloak/pull/36882",
    "https://github.com/getsentry/sentry/pull/93824",
    "https://github.com/ai-code-review-evaluation/sentry-greptile/pull/1",
    "https://github.com/grafana/grafana/pull/97529",
    "https://github.com/grafana/grafana/pull/90045",
    "https://github.com/ai-code-review-evaluation/discourse-graphite/pull/10",
    "https://github.com/ai-code-review-evaluation/discourse-graphite/pull/4",
    "https://github.com/calcom/cal.com/pull/14740",
    "https://github.com/calcom/cal.com/pull/10967",
)


def test_shipped_workloads_are_fixed_disjoint_dev_and_holdout_split():
    workload_dir = Path(__file__).parents[1] / "src" / "reviewforge" / "eval" / "workloads"
    dev = json.loads((workload_dir / "dev10.json").read_text(encoding="utf-8"))
    holdout = json.loads((workload_dir / "holdout40.json").read_text(encoding="utf-8"))
    dev_urls = [row["golden_url"] for row in dev]
    holdout_urls = [row["golden_url"] for row in holdout]

    assert len(dev) == 10
    assert len(holdout) == 40
    assert dev_urls == list(_REPRESENTATIVE10_URLS)
    assert set(dev_urls).isdisjoint(holdout_urls)
    assert len(set(dev_urls + holdout_urls)) == 50


def test_p10_and_worst_f1_use_lower_tail_nearest_rank():
    baseline_rows = {f"pr-{i:02d}": {"tp": i, "fp": 0, "fn": 10 - i} for i in range(1, 11)}
    candidate_rows = {f"pr-{i:02d}": {"tp": i + 1, "fp": 0, "fn": 9 - i} for i in range(1, 11)}
    report = compare_judged(_artifact(baseline_rows), _artifact(candidate_rows))

    assert report["summary"]["p10_f1"]["baseline"] == 0.1818
    assert report["summary"]["worst_f1"]["baseline"] == 0.1818
    assert report["summary"]["p10_f1"]["candidate"] == 0.3333


def test_build_report_includes_markdown_table():
    report = build_paired_report(_artifact({"pr-1": {"tp": 1, "fp": 0, "fn": 0}}), _artifact({"pr-1": {}}))
    markdown = render_markdown(report)

    assert report["markdown"] == markdown
    assert "| PR | baseline F1 | candidate F1 |" in markdown
    assert "Win / tie / loss:" in markdown
