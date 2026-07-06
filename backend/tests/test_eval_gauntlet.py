from pathlib import Path

from reviewforge.eval.gauntlet import load_golden, run_scanner_eval


def test_default_gauntlet_has_87_expected_findings():
    repo_root = Path(__file__).resolve().parents[2]
    golden = load_golden(repo_root / "backend" / "eval" / "golden_expected_findings.json")

    expected_total = sum(int(item.get("count", 1)) for case in golden["cases"] for item in case["expected"])

    assert expected_total == 87
    assert golden["metadata"]["baseline_detected_hint"] == 62


def test_scanner_eval_reports_security_and_supply_chain_metrics():
    repo_root = Path(__file__).resolve().parents[2]
    golden = load_golden(repo_root / "backend" / "eval" / "golden_expected_findings.json")

    result = run_scanner_eval(golden, repo_root)

    assert result["expected_total"] == 87
    assert "recall" in result
    assert result["security"]["expected"] > 0
    assert result["supply_chain"]["expected"] > 0
    assert result["token_total"] == 0
