"""Line-aware scoring for end-to-end pull-request benchmarks."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any

from reviewforge.engine.security_categories import normalize_category


def score_live_benchmark(
    manifest: dict[str, Any],
    findings_payload: list[dict[str, Any]] | dict[str, Any],
    tokens_payload: Any = None,
    *,
    line_tolerance: int = 2,
) -> dict[str, Any]:
    """Score delivered/confirmed findings against planted issues one-to-one.

    The manifest contains ``prs`` with ``pr_number``, ``changed_lines``, optional
    ``clean`` and an ``issues`` list. Each issue needs ``file``, ``category`` and
    either ``line`` or ``line_start``/``line_end``. ``accepted_categories`` may
    list intentional aliases. Findings may be a bare list or ``{"findings": [...]}``.
    """

    tolerance = max(0, int(line_tolerance))
    prs = {int(item["pr_number"]): item for item in manifest.get("prs", [])}
    expected = _expand_expected(prs)
    actual = _normalize_actual(findings_payload, prs)

    expected_by_pr: dict[int, list[dict[str, Any]]] = defaultdict(list)
    actual_by_pr: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for item in expected:
        expected_by_pr[item["pr_number"]].append(item)
    for item in actual:
        actual_by_pr[item["pr_number"]].append(item)

    matched_pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    missed: list[dict[str, Any]] = []
    false_positives: list[dict[str, Any]] = []

    all_prs = sorted(set(prs) | set(actual_by_pr))
    per_pr: dict[str, dict[str, Any]] = {}
    for pr_number in all_prs:
        exp = expected_by_pr.get(pr_number, [])
        act = actual_by_pr.get(pr_number, [])
        pairs, unmatched_exp, unmatched_act = _match_one_to_one(exp, act, tolerance)
        matched_pairs.extend(pairs)
        missed.extend(unmatched_exp)
        false_positives.extend(unmatched_act)
        per_pr[str(pr_number)] = _metric_block(len(pairs), len(unmatched_act), len(unmatched_exp))
        per_pr[str(pr_number)].update(
            {
                "name": prs.get(pr_number, {}).get("name", ""),
                "clean": bool(prs.get(pr_number, {}).get("clean", False)),
                "changed_lines": int(prs.get(pr_number, {}).get("changed_lines", 0) or 0),
            }
        )

    overall = _metric_block(len(matched_pairs), len(false_positives), len(missed))
    token_total, tokens_by_pr = _token_totals(tokens_payload)
    clean_prs = {number for number, item in prs.items() if item.get("clean")}
    clean_fp = sum(1 for item in false_positives if item["pr_number"] in clean_prs)
    clean_lines = sum(int(prs[number].get("changed_lines", 0) or 0) for number in clean_prs)

    result: dict[str, Any] = {
        "metadata": manifest.get("metadata", {}),
        "line_tolerance": tolerance,
        **overall,
        "expected_total": len(expected),
        "actual_total": len(actual),
        "token_total": token_total,
        "tokens_per_true_positive": round(token_total / len(matched_pairs), 2) if matched_pairs else None,
        "clean_false_positives": clean_fp,
        "clean_changed_lines": clean_lines,
        "clean_fp_per_100_changed_lines": round(clean_fp * 100 / clean_lines, 4) if clean_lines else 0.0,
        "per_pr": per_pr,
        "per_language": _group_metrics(expected, actual, matched_pairs, missed, false_positives, "language"),
        "per_category": _group_metrics(expected, actual, matched_pairs, missed, false_positives, "category"),
        "tokens_by_pr": {str(key): value for key, value in sorted(tokens_by_pr.items())},
        "matched": [{"expected": _public_issue(exp), "finding": _public_finding(act)} for exp, act in matched_pairs],
        "missed": [_public_issue(item) for item in missed],
        "false_positive_details": [_public_finding(item) for item in false_positives],
    }
    return result


def _expand_expected(prs: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
    expanded: list[dict[str, Any]] = []
    for pr_number, pr in prs.items():
        for raw in pr.get("issues", []):
            count = max(1, int(raw.get("count", 1)))
            for ordinal in range(count):
                item = dict(raw)
                item["pr_number"] = pr_number
                item["id"] = str(raw.get("id", f"pr{pr_number}-issue-{len(expanded) + 1}"))
                if count > 1:
                    item["id"] = f"{item['id']}-{ordinal + 1}"
                item["line_start"] = int(raw.get("line_start", raw.get("line", 0)) or 0)
                item["line_end"] = int(raw.get("line_end", raw.get("line", item["line_start"])) or 0)
                if item["line_end"] < item["line_start"]:
                    item["line_start"], item["line_end"] = item["line_end"], item["line_start"]
                item["category"] = normalize_category(str(raw.get("category", "")))
                accepted = raw.get("accepted_categories", [])
                item["accepted_categories"] = {
                    normalize_category(str(category)) for category in [item["category"], *accepted]
                }
                item["language"] = str(raw.get("language") or _language_for_file(str(raw.get("file", ""))))
                expanded.append(item)
    return expanded


def _normalize_actual(
    payload: list[dict[str, Any]] | dict[str, Any], prs: dict[int, dict[str, Any]]
) -> list[dict[str, Any]]:
    rows = payload.get("findings", []) if isinstance(payload, dict) else payload
    file_languages: dict[tuple[int, str], str] = {}
    for pr_number, pr in prs.items():
        for issue in pr.get("issues", []):
            file_path = str(issue.get("file", ""))
            file_languages[(pr_number, file_path)] = str(issue.get("language") or _language_for_file(file_path))

    actual: list[dict[str, Any]] = []
    for raw in rows or []:
        status = str(raw.get("status", "confirmed"))
        if status not in {"", "confirmed", "reported", "delivered"}:
            continue
        pr_number = int(raw.get("pr_number", raw.get("pr", 0)) or 0)
        file_path = str(raw.get("file", raw.get("path", "")))
        item = dict(raw)
        item.update(
            {
                "pr_number": pr_number,
                "file": file_path,
                "line": int(raw.get("line", raw.get("new_line", 0)) or 0),
                "category": normalize_category(str(raw.get("category", ""))),
                "language": str(
                    raw.get("language") or file_languages.get((pr_number, file_path)) or _language_for_file(file_path)
                ),
            }
        )
        actual.append(item)
    return actual


def _match_one_to_one(
    expected: list[dict[str, Any]], actual: list[dict[str, Any]], tolerance: int
) -> tuple[list[tuple[dict[str, Any], dict[str, Any]]], list[dict[str, Any]], list[dict[str, Any]]]:
    candidates: list[list[int]] = []
    for exp in expected:
        matching = [idx for idx, act in enumerate(actual) if _matches(exp, act, tolerance)]
        matching.sort(key=lambda idx: _line_distance(exp, actual[idx]))
        candidates.append(matching)

    actual_to_expected: dict[int, int] = {}

    def assign(exp_idx: int, seen: set[int]) -> bool:
        for act_idx in candidates[exp_idx]:
            if act_idx in seen:
                continue
            seen.add(act_idx)
            previous = actual_to_expected.get(act_idx)
            if previous is None or assign(previous, seen):
                actual_to_expected[act_idx] = exp_idx
                return True
        return False

    for exp_idx in sorted(range(len(expected)), key=lambda idx: len(candidates[idx])):
        assign(exp_idx, set())

    expected_to_actual = {exp_idx: act_idx for act_idx, exp_idx in actual_to_expected.items()}
    pairs = [(expected[idx], actual[expected_to_actual[idx]]) for idx in sorted(expected_to_actual)]
    missed = [item for idx, item in enumerate(expected) if idx not in expected_to_actual]
    false_positives = [item for idx, item in enumerate(actual) if idx not in actual_to_expected]
    return pairs, missed, false_positives


def _matches(expected: dict[str, Any], actual: dict[str, Any], tolerance: int) -> bool:
    if expected["file"] != actual["file"]:
        return False
    if actual["category"] not in expected["accepted_categories"]:
        return False
    line = int(actual.get("line", 0))
    return expected["line_start"] - tolerance <= line <= expected["line_end"] + tolerance


def _line_distance(expected: dict[str, Any], actual: dict[str, Any]) -> int:
    line = int(actual.get("line", 0))
    if expected["line_start"] <= line <= expected["line_end"]:
        return 0
    return min(abs(line - expected["line_start"]), abs(line - expected["line_end"]))


def _metric_block(tp: int, fp: int, fn: int) -> dict[str, Any]:
    precision = tp / (tp + fp) if tp + fp else 1.0
    recall = tp / (tp + fn) if tp + fn else 1.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "true_positives": tp,
        "false_positives": fp,
        "false_negatives": fn,
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
    }


def _group_metrics(
    expected: list[dict[str, Any]],
    actual: list[dict[str, Any]],
    pairs: list[tuple[dict[str, Any], dict[str, Any]]],
    missed: list[dict[str, Any]],
    false_positives: list[dict[str, Any]],
    key: str,
) -> dict[str, dict[str, Any]]:
    keys = {str(item.get(key, "unknown") or "unknown") for item in [*expected, *actual]}
    output: dict[str, dict[str, Any]] = {}
    for value in sorted(keys):
        tp = sum(1 for exp, _act in pairs if str(exp.get(key, "unknown") or "unknown") == value)
        fn = sum(1 for item in missed if str(item.get(key, "unknown") or "unknown") == value)
        fp = sum(1 for item in false_positives if str(item.get(key, "unknown") or "unknown") == value)
        output[value] = _metric_block(tp, fp, fn)
    return output


def _token_totals(payload: Any) -> tuple[int, dict[int, int]]:
    if payload is None:
        return 0, {}
    by_pr: dict[int, int] = defaultdict(int)
    if isinstance(payload, (int, float)):
        return int(payload), {}
    if isinstance(payload, dict) and "tokens" in payload:
        payload = payload["tokens"]
    if isinstance(payload, dict):
        for key, value in payload.items():
            if isinstance(value, dict):
                value = value.get("total_tokens", value.get("total", 0))
            by_pr[int(key)] += int(value or 0)
    elif isinstance(payload, list):
        for row in payload:
            by_pr[int(row.get("pr_number", row.get("pr", 0)) or 0)] += int(
                row.get("total_tokens", row.get("total", 0)) or 0
            )
    return sum(by_pr.values()), dict(by_pr)


def _language_for_file(file_path: str) -> str:
    suffix = Path(file_path).suffix.lower()
    return {
        ".py": "python",
        ".go": "go",
        ".java": "java",
        ".rb": "ruby",
        ".rs": "rust",
        ".js": "javascript",
        ".jsx": "javascript",
        ".ts": "typescript",
        ".tsx": "typescript",
        ".vue": "vue",
        ".svelte": "svelte",
        ".yaml": "yaml",
        ".yml": "yaml",
        ".toml": "toml",
        ".xml": "xml",
        ".json": "json",
    }.get(suffix, "other")


def _public_issue(item: dict[str, Any]) -> dict[str, Any]:
    return {
        key: item[key]
        for key in ("id", "pr_number", "file", "line_start", "line_end", "category", "language")
        if key in item
    }


def _public_finding(item: dict[str, Any]) -> dict[str, Any]:
    return {
        key: item[key]
        for key in ("id", "pr_number", "file", "line", "category", "language", "reviewer", "status")
        if key in item
    }
