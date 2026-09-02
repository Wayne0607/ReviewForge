"""Paired per-PR comparison for strict benchmark judgments.

The benchmark judge stores one record per PR under ``completed`` and nests the
selected system (normally ``reviewforge``) below it.  This module deliberately
does not compare aggregate counters from the input: it recomputes each PR's
F1 from ``tp``/``fp``/``fn`` so the paired win/tie/loss and lower-tail metrics
are based on the same unit of observation.
"""

from __future__ import annotations

import argparse
import json
import math
from collections.abc import Mapping
from pathlib import Path
from typing import Any

REPORT_SCHEMA_VERSION = "reviewforge.paired-report.v1"


def _as_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _metric_block(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    """Normalize one judge system result and recompute its derived metrics."""

    raw = raw or {}
    if not any(key in raw for key in ("tp", "fp", "fn", "true_positives", "false_positives", "false_negatives")):
        return {"tp": 0, "fp": 0, "fn": 0, "precision": 0.0, "recall": 0.0, "f1": 0.0}
    tp = _as_int(raw.get("tp", raw.get("true_positives", 0)))
    fp = _as_int(raw.get("fp", raw.get("false_positives", 0)))
    fn = _as_int(raw.get("fn", raw.get("false_negatives", 0)))
    # Match martian_judge._metrics exactly: an empty denominator is a zero
    # score, including the 0/0/0 case.  Treating it as perfect would make a
    # missing/empty review look like a successful paired result.
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
    }


def _system_result(record: Any, system: str) -> Mapping[str, Any]:
    """Return the requested strict-judge system block, without substitutions."""

    if not isinstance(record, Mapping):
        return {}
    selected = record.get(system)
    if isinstance(selected, Mapping):
        return selected
    return {}


def _completed(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    completed = payload.get("completed")
    if isinstance(completed, Mapping):
        return completed
    # Accept a bare ``{pr_id: {system: metrics}}`` mapping for small offline
    # tests and hand-authored comparisons.
    return payload


def _pr_sort_key(value: Any) -> str:
    return str(value)


def _validation_error(source: str, pr_id: Any, category: str) -> ValueError:
    """Build a safe validation error containing only an id and category."""

    return ValueError(f"{source} PR {pr_id}: {category}")


def _validate_strict_artifact(payload: Mapping[str, Any], source: str, system: str) -> Mapping[str, Any]:
    """Validate the strict judge shape before calculating any paired score.

    A paired comparison must never turn missing data or judge failures into a
    zero-score row.  Error details can contain prompt/code content, so only
    the PR id and a stable category are included in raised errors.
    """

    rows = _completed(payload)
    if not isinstance(rows, Mapping):
        raise ValueError(f"{source}: invalid-artifact")
    for raw_pr_id in sorted(rows, key=_pr_sort_key):
        record = rows[raw_pr_id]
        if not isinstance(record, Mapping):
            raise _validation_error(source, raw_pr_id, "invalid-pr-record")
        selected = record.get(system)
        if not isinstance(selected, Mapping):
            raise _validation_error(source, raw_pr_id, "missing-system")
        errors = selected.get("errors")
        if errors:
            raise _validation_error(source, raw_pr_id, "judge-errors")
    return rows


def _p10(values: list[float]) -> float:
    """Return nearest-rank lower-tail P10, matching architecture-floor eval."""

    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, math.ceil(len(ordered) * 0.10))
    return ordered[rank - 1]


def _rounded(value: float) -> float:
    return round(value, 4)


def compare_judged(
    baseline: Mapping[str, Any],
    candidate: Mapping[str, Any],
    *,
    system: str = "reviewforge",
    baseline_label: str = "baseline",
    candidate_label: str = "candidate",
) -> dict[str, Any]:
    """Build a deterministic paired report from two judged artifacts."""

    baseline_rows = _validate_strict_artifact(baseline, baseline_label, system)
    candidate_rows = _validate_strict_artifact(candidate, candidate_label, system)
    baseline_ids = set(baseline_rows)
    candidate_ids = set(candidate_rows)
    if baseline_ids != candidate_ids:
        missing_in_candidate = sorted(baseline_ids - candidate_ids, key=_pr_sort_key)
        missing_in_baseline = sorted(candidate_ids - baseline_ids, key=_pr_sort_key)
        details: list[str] = []
        if missing_in_candidate:
            details.append(f"missing_in_{candidate_label}=[{', '.join(map(str, missing_in_candidate))}]")
        if missing_in_baseline:
            details.append(f"missing_in_{baseline_label}=[{', '.join(map(str, missing_in_baseline))}]")
        raise ValueError("paired inputs: pr-set-mismatch; " + "; ".join(details))
    pr_ids = sorted(baseline_ids, key=_pr_sort_key)
    per_pr: list[dict[str, Any]] = []
    baseline_f1: list[float] = []
    candidate_f1: list[float] = []
    wins = ties = losses = 0

    for raw_pr_id in pr_ids:
        pr_id = str(raw_pr_id)
        before = _metric_block(_system_result(baseline_rows.get(raw_pr_id, {}), system))
        after = _metric_block(_system_result(candidate_rows.get(raw_pr_id, {}), system))
        before_f1 = float(before["f1"])
        after_f1 = float(after["f1"])
        delta = _rounded(after_f1 - before_f1)
        if delta > 0:
            outcome = "win"
            wins += 1
        elif delta < 0:
            outcome = "loss"
            losses += 1
        else:
            outcome = "tie"
            ties += 1
        baseline_f1.append(before_f1)
        candidate_f1.append(after_f1)
        per_pr.append(
            {
                "pr_id": pr_id,
                baseline_label: before,
                candidate_label: after,
                "delta_f1": delta,
                "outcome": outcome,
            }
        )

    baseline_p10 = _rounded(_p10(baseline_f1))
    candidate_p10 = _rounded(_p10(candidate_f1))
    baseline_worst = _rounded(min(baseline_f1, default=0.0))
    candidate_worst = _rounded(min(candidate_f1, default=0.0))
    summary = {
        "paired_prs": len(per_pr),
        "win": wins,
        "tie": ties,
        "loss": losses,
        "p10_f1": {baseline_label: baseline_p10, candidate_label: candidate_p10},
        "worst_f1": {baseline_label: baseline_worst, candidate_label: candidate_worst},
        "delta_p10_f1": _rounded(candidate_p10 - baseline_p10),
        "delta_worst_f1": _rounded(candidate_worst - baseline_worst),
    }
    # Keep explicit aliases convenient for callers that want a flat
    # win/tie/loss or tail metric block while retaining the grouped summary
    # used by the markdown renderer.
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "system": system,
        "baseline": baseline_label,
        "candidate": candidate_label,
        "per_pr": per_pr,
        "summary": summary,
        "win_tie_loss": {"win": wins, "tie": ties, "loss": losses},
        "p10_f1": summary["p10_f1"],
        "worst_f1": summary["worst_f1"],
    }


def render_markdown(report: Mapping[str, Any]) -> str:
    """Render the paired rows and lower-tail summary as a markdown table."""

    baseline = str(report.get("baseline", "baseline"))
    candidate = str(report.get("candidate", "candidate"))
    lines = [
        f"| PR | {baseline} F1 | {candidate} F1 | Δ F1 | Result |",
        "| --- | ---: | ---: | ---: | --- |",
    ]
    for row in report.get("per_pr", []):
        before = row.get(baseline, {})
        after = row.get(candidate, {})
        lines.append(
            f"| {row.get('pr_id', '')} | {float(before.get('f1', 0.0)):.4f} | "
            f"{float(after.get('f1', 0.0)):.4f} | {float(row.get('delta_f1', 0.0)):+.4f} | "
            f"{row.get('outcome', '')} |"
        )
    summary = report.get("summary", {})
    p10 = summary.get("p10_f1", {})
    worst = summary.get("worst_f1", {})
    lines.extend(
        [
            "",
            "| Tail metric | " + baseline + " | " + candidate + " | Δ |",
            "| --- | ---: | ---: | ---: |",
            f"| P10 F1 | {float(p10.get(baseline, 0.0)):.4f} | {float(p10.get(candidate, 0.0)):.4f} | "
            f"{float(summary.get('delta_p10_f1', 0.0)):+.4f} |",
            f"| Worst F1 | {float(worst.get(baseline, 0.0)):.4f} | {float(worst.get(candidate, 0.0)):.4f} | "
            f"{float(summary.get('delta_worst_f1', 0.0)):+.4f} |",
            "",
            f"Win / tie / loss: {summary.get('win', 0)} / {summary.get('tie', 0)} / {summary.get('loss', 0)} "
            f"over {summary.get('paired_prs', 0)} PRs.",
        ]
    )
    return "\n".join(lines)


def build_paired_report(
    baseline: str | Path | Mapping[str, Any],
    candidate: str | Path | Mapping[str, Any],
    *,
    system: str = "reviewforge",
    baseline_label: str = "baseline",
    candidate_label: str = "candidate",
) -> dict[str, Any]:
    """Load paths when needed and return JSON-ready report plus markdown."""

    baseline_payload = _load_payload(baseline)
    candidate_payload = _load_payload(candidate)
    report = compare_judged(
        baseline_payload,
        candidate_payload,
        system=system,
        baseline_label=baseline_label,
        candidate_label=candidate_label,
    )
    report["markdown"] = render_markdown(report)
    return report


def _load_payload(value: str | Path | Mapping[str, Any]) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    return json.loads(Path(value).read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Build a paired per-PR report from two judged-strict.json files.")
    parser.add_argument("--baseline", required=True, help="Baseline judged-strict.json")
    parser.add_argument("--candidate", required=True, help="Candidate judged-strict.json")
    parser.add_argument("--system", default="reviewforge", help="System key under each PR (default: reviewforge)")
    parser.add_argument("--baseline-label", default="baseline")
    parser.add_argument("--candidate-label", default="candidate")
    parser.add_argument("--json-out", default="", help="Optional output path for the JSON report")
    parser.add_argument("--markdown-out", default="", help="Optional output path for the markdown table")
    args = parser.parse_args(argv)

    try:
        report = build_paired_report(
            args.baseline,
            args.candidate,
            system=args.system,
            baseline_label=args.baseline_label,
            candidate_label=args.candidate_label,
        )
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        parser.error(str(exc))

    payload = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.json_out:
        Path(args.json_out).write_text(payload, encoding="utf-8")
    else:
        print(payload, end="")
    markdown = str(report["markdown"]) + "\n"
    if args.markdown_out:
        Path(args.markdown_out).write_text(markdown, encoding="utf-8")
    else:
        print(markdown, end="")


if __name__ == "__main__":
    main()
