"""Golden expected-finding evaluation for detector and PR review outputs."""

from __future__ import annotations

import json
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

from reviewforge.core.database import Database
from reviewforge.core.state import Finding, StateStore
from reviewforge.engine.cross_pr_analyzer import CrossPRAnalyzer
from reviewforge.engine.detectors import detect_dependency_findings, detect_security_findings
from reviewforge.engine.security_categories import is_security_category, normalize_category

SUPPLY_CHAIN_CATEGORIES = {
    "ci-security",
    "dependency-deprecated",
    "dependency-version-range",
    "dependency-vulnerability",
    "supply-chain-risk",
    "vulnerable-dependency",
}


def load_golden(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def run_scanner_eval(golden: dict[str, Any], repo_root: str | Path) -> dict[str, Any]:
    """Run zero-token deterministic scanners against golden fixtures."""

    repo_root = Path(repo_root)
    diffs: dict[str, str] = {}
    for case in golden.get("cases", []):
        for file_path in case.get("files", []):
            full_path = repo_root / file_path
            if full_path.exists():
                diffs[file_path] = _as_added_diff(full_path.read_text(encoding="utf-8"))
        for file_path, content in case.get("inline_files", {}).items():
            diffs[file_path] = _as_added_diff(str(content))

    actual = [*_finding_dicts(detect_security_findings(diffs)), *_finding_dicts(detect_dependency_findings(diffs))]
    return score_findings(golden, actual, token_total=0, mode="scanner")


async def run_full_eval(golden: dict[str, Any], repo_root: str | Path) -> dict[str, Any]:
    """Run scanner and structural cross-PR evals against the full gauntlet."""

    repo_root = Path(repo_root)
    diffs: dict[str, str] = {}
    for case in golden.get("cases", []):
        for file_path in case.get("files", []):
            full_path = repo_root / file_path
            if full_path.exists():
                diffs[file_path] = _as_added_diff(full_path.read_text(encoding="utf-8"))
        for file_path, content in case.get("inline_files", {}).items():
            diffs[file_path] = _as_added_diff(str(content))

    actual = [*_finding_dicts(detect_security_findings(diffs)), *_finding_dicts(detect_dependency_findings(diffs))]
    actual.extend(await _run_cross_pr_cases(golden))
    combined = _with_cross_pr_expected(golden)
    return score_findings(combined, actual, token_total=0, mode="full")


def score_findings(
    golden: dict[str, Any],
    actual_findings: list[dict[str, Any]],
    token_total: int = 0,
    mode: str = "external",
) -> dict[str, Any]:
    """Score actual findings against expected finding counts.

    Matching is intentionally category/file/count based. This keeps the metric stable
    across line-number drift in generated PR diffs while still counting every planted
    issue through the optional expected `count` field.
    """

    expected = _expected_counter(golden)
    actual = _actual_counter(actual_findings)
    matched = expected & actual
    false_positive = actual - expected
    missed = expected - actual

    summary = _summarize(expected, actual, matched, false_positive, missed)
    summary.update(
        {
            "mode": mode,
            "token_total": token_total,
            "metadata": golden.get("metadata", {}),
            "expected_total": sum(expected.values()),
            "detected_total": sum(matched.values()),
            "actual_security_total": sum(actual.values()),
            "false_positive_total": sum(false_positive.values()),
            "missed_total": sum(missed.values()),
            "missed": _counter_detail(missed),
            "false_positives": _counter_detail(false_positive),
        }
    )
    return summary


def _expected_counter(golden: dict[str, Any]) -> Counter[tuple[str, str, str]]:
    counter: Counter[tuple[str, str, str]] = Counter()
    for case in golden.get("cases", []):
        default_kind = case.get("kind", "security")
        for item in case.get("expected", []):
            category = normalize_category(item.get("category", ""))
            if not _counts_for_security_metric(category, default_kind):
                continue
            count = int(item.get("count", 1))
            file_path = item.get("file", "")
            kind = item.get("kind", default_kind)
            counter[(kind, file_path, category)] += max(1, count)
    return counter


def _with_cross_pr_expected(golden: dict[str, Any]) -> dict[str, Any]:
    combined = dict(golden)
    cases = list(golden.get("cases", []))
    cross_expected = []
    for case in golden.get("cross_pr_cases", []):
        cross_expected.extend(case.get("expected", []))
    if cross_expected:
        cases.append({"name": "cross-pr-expected", "kind": "cross-pr", "expected": cross_expected})
    combined["cases"] = cases
    return combined


async def _run_cross_pr_cases(golden: dict[str, Any]) -> list[dict[str, Any]]:
    actual: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "cross_pr.db")
        await db.connect()
        analyzer = CrossPRAnalyzer(db, llm=None)
        for idx, case in enumerate(golden.get("cross_pr_cases", []), start=1):
            seed = case["seed"]
            seed_file = seed["file"]
            seed_state = StateStore(
                repo="gauntlet/cross-pr",
                pr_number=idx * 10,
                head_sha=f"seed-{idx}",
                files_changed=[seed_file],
                diff_summary=_case_diff(seed_file, seed["content"]),
            )
            seed_findings = [
                Finding(
                    file=seed_file,
                    line=item.get("line", 1),
                    severity=item.get("severity", "error"),
                    category=item["category"],
                    message=item.get("message", item["category"]),
                    confidence=item.get("confidence", 0.95),
                    reviewer="security_reviewer",
                    status="confirmed",
                )
                for item in seed.get("findings", [])
            ]
            await analyzer.analyze(f"seed-{idx}", seed_state, seed_findings)

            consumer = case["consumer"]
            consumer_file = consumer["file"]
            consumer_state = StateStore(
                repo="gauntlet/cross-pr",
                pr_number=idx * 10 + 1,
                head_sha=f"consumer-{idx}",
                files_changed=[consumer_file],
                diff_summary=_case_diff(consumer_file, consumer["content"]),
            )
            findings = await analyzer.analyze(f"consumer-{idx}", consumer_state, [])
            actual.extend(f.to_dict() for f in findings)
        await db.close()
    return actual


def _actual_counter(findings: list[dict[str, Any]]) -> Counter[tuple[str, str, str]]:
    counter: Counter[tuple[str, str, str]] = Counter()
    for item in findings:
        category = normalize_category(item.get("category", ""))
        if not is_security_category(category) and not category.startswith("cross-pr-"):
            continue
        kind = _kind_for_category(category)
        counter[(kind, item.get("file", ""), category)] += 1
    return counter


def _summarize(
    expected: Counter[tuple[str, str, str]],
    actual: Counter[tuple[str, str, str]],
    matched: Counter[tuple[str, str, str]],
    false_positive: Counter[tuple[str, str, str]],
    missed: Counter[tuple[str, str, str]],
) -> dict[str, Any]:
    tp = sum(matched.values())
    fp = sum(false_positive.values())
    fn = sum(missed.values())
    precision = tp / (tp + fp) if tp + fp else 1.0
    recall = tp / (tp + fn) if tp + fn else 1.0
    false_positive_rate = fp / (tp + fp) if tp + fp else 0.0

    return {
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "false_positive_rate": round(false_positive_rate, 4),
        "security": _category_group_summary(expected, matched, missed, "security"),
        "cross_pr": _category_group_summary(expected, matched, missed, "cross-pr"),
        "supply_chain": _category_group_summary(expected, matched, missed, "dependency"),
    }


def _category_group_summary(
    expected: Counter[tuple[str, str, str]],
    matched: Counter[tuple[str, str, str]],
    missed: Counter[tuple[str, str, str]],
    kind: str,
) -> dict[str, Any]:
    expected_n = sum(v for (k, _file, _cat), v in expected.items() if k == kind)
    matched_n = sum(v for (k, _file, _cat), v in matched.items() if k == kind)
    missed_n = sum(v for (k, _file, _cat), v in missed.items() if k == kind)
    return {
        "expected": expected_n,
        "detected": matched_n,
        "missed": missed_n,
        "recall": round(matched_n / expected_n, 4) if expected_n else 1.0,
    }


def _kind_for_category(category: str) -> str:
    if category.startswith("cross-pr-"):
        return "cross-pr"
    if category in SUPPLY_CHAIN_CATEGORIES:
        return "dependency"
    return "security"


def _counts_for_security_metric(category: str, kind: str) -> bool:
    if kind in {"security", "dependency", "cross-pr"}:
        return True
    return is_security_category(category)


def _counter_detail(counter: Counter[tuple[str, str, str]]) -> list[dict[str, Any]]:
    return [
        {"kind": kind, "file": file_path, "category": category, "count": count}
        for (kind, file_path, category), count in sorted(counter.items())
        if count
    ]


def _finding_dicts(findings: list[Any]) -> list[dict[str, Any]]:
    return [
        {
            "file": item.file,
            "line": item.line,
            "category": item.category,
            "severity": item.severity,
            "confidence": item.confidence,
        }
        for item in findings
    ]


def _as_added_diff(content: str) -> str:
    return "@@ golden @@\n" + "\n".join(f"+{line}" for line in content.splitlines())


def _case_diff(file_path: str, content: str) -> str:
    return f"--- {file_path} (+{len(content.splitlines())} -0)\n" + "\n".join(
        f"+{line}" for line in content.splitlines()
    )
