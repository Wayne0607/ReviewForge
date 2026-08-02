from pathlib import Path

from reviewforge.eval.gauntlet import load_golden, run_scanner_eval


def test_default_gauntlet_has_92_expected_findings():
    repo_root = Path(__file__).resolve().parents[2]
    golden = load_golden(repo_root / "backend" / "eval" / "golden_expected_findings.json")

    expected_total = sum(int(item.get("count", 1)) for case in golden["cases"] for item in case["expected"])

    assert expected_total == 92
    assert golden["metadata"]["baseline_detected_hint"] == 62


def test_scanner_eval_reports_security_and_supply_chain_metrics():
    repo_root = Path(__file__).resolve().parents[2]
    golden = load_golden(repo_root / "backend" / "eval" / "golden_expected_findings.json")

    result = run_scanner_eval(golden, repo_root)

    assert result["expected_total"] == 92
    assert "recall" in result
    assert result["security"]["expected"] > 0
    assert result["supply_chain"]["expected"] > 0
    assert result["token_total"] == 0


def test_issue118_planted_notifier_sample_is_fully_detected():
    """Issue #118 smoke: the planted notifier sample must be caught by the
    deterministic scanners, including the multi-line f-string SQL injection."""
    repo_root = Path(__file__).resolve().parents[2]
    golden = load_golden(repo_root / "backend" / "eval" / "golden_expected_findings.json")
    planted = {
        "metadata": golden["metadata"],
        "cases": [case for case in golden["cases"] if case["name"] == "planted-issues-20260731-r2-notifier"],
        "cross_pr_cases": [],
    }
    assert len(planted["cases"]) == 1

    result = run_scanner_eval(planted, repo_root)

    assert result["expected_total"] == 7
    assert result["detected_total"] == 7
    assert result["false_positive_total"] == 0
    assert result["missed_total"] == 0
