"""Ledger recall — measure hypothesis-generation coverage against goldens.

Reuses the Martian judge's matching口径 (same ``match_all`` contract, 0.7
confidence floor, and bipartite maximum matching) so the numbers are directly
comparable to the published-review recall.  The judge client is injected, so
the candidate-selection and matching logic is unit-testable without an LLM.

Candidates are ledger hypothesis claims (not published comment bodies), by
status pool:

- ``ledger_recall``:       CONFIRMED + OPEN + UNKNOWN claims.
- ``confirmed_recall``:    CONFIRMED claims only.
- ``refuted_goldens``:     goldens matched by REFUTED claims (investigation
  falsely killed a real issue).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

_MATCH_CONFIDENCE = 0.7
_ALL_STATUSES = ("confirmed", "open", "unknown")


def candidate_claims(items: list[dict[str, Any]], statuses: tuple[str, ...] | list[str]) -> list[str]:
    """Return non-empty hypothesis claims for the given status pool."""

    wanted = set(statuses)
    claims: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("status") not in wanted:
            continue
        claim = str(item.get("claim", "")).strip()
        if claim:
            claims.append(claim)
    return claims


def _matching(
    goldens: list[dict[str, Any]], candidates: list[str], matches: list[dict[str, Any]]
) -> tuple[int, list[dict[str, Any]]]:
    """Deterministic bipartite matching; identical to martian_judge.evaluate."""

    eligible_pairs: dict[tuple[int, int], dict[str, Any]] = {}
    for result in matches:
        try:
            golden_index = int(result["golden_index"])
            candidate_index = int(result["candidate_index"])
            confidence = float(result.get("confidence", 0))
            goldens[golden_index]
            candidates[candidate_index]
        except (KeyError, TypeError, ValueError, IndexError):
            continue
        if confidence < _MATCH_CONFIDENCE:
            continue
        pair = (golden_index, candidate_index)
        previous = eligible_pairs.get(pair)
        if previous is None or confidence > float(previous["confidence"]):
            eligible_pairs[pair] = {
                "golden_index": golden_index,
                "candidate_index": candidate_index,
                "confidence": confidence,
            }

    adjacency: dict[int, list[int]] = {}
    for golden_index, candidate_index in eligible_pairs:
        adjacency.setdefault(golden_index, []).append(candidate_index)
    for golden_index, candidate_indices in adjacency.items():
        candidate_indices.sort(
            key=lambda candidate_index: (
                -float(eligible_pairs[(golden_index, candidate_index)]["confidence"]),
                candidate_index,
            )
        )

    candidate_to_golden: dict[int, int] = {}

    def augment(golden_index: int, seen: set[int]) -> bool:
        for candidate_index in adjacency.get(golden_index, []):
            if candidate_index in seen:
                continue
            seen.add(candidate_index)
            current = candidate_to_golden.get(candidate_index)
            if current is None or augment(current, seen):
                candidate_to_golden[candidate_index] = golden_index
                return True
        return False

    golden_order = sorted(
        adjacency,
        key=lambda golden_index: (
            -max(
                float(eligible_pairs[(golden_index, candidate_index)]["confidence"])
                for candidate_index in adjacency[golden_index]
            ),
            golden_index,
        ),
    )
    for golden_index in golden_order:
        augment(golden_index, set())

    matched = {
        golden_index: eligible_pairs[(golden_index, candidate_index)]
        for candidate_index, golden_index in candidate_to_golden.items()
    }
    return len(matched), list(matched.values())


def _recall(tp: int, golden_count: int) -> float:
    return tp / golden_count if golden_count else 0.0


async def evaluate_pr(judge: Any, goldens: list[dict[str, Any]], items: list[dict[str, Any]]) -> dict[str, Any]:
    """Judge one PR's ledger against its goldens."""

    golden_count = len(goldens)
    recall_claims = candidate_claims(items, _ALL_STATUSES)
    confirmed_claims = candidate_claims(items, ("confirmed",))
    refuted_claims = candidate_claims(items, ("refuted",))

    async def _judge(claims: list[str]) -> dict:
        if not claims:
            return {"matches": []}
        result = await judge.match_all([str(golden["comment"]) for golden in goldens], claims)
        if isinstance(result, dict) and result.get("error"):
            return {"matches": []}
        return result if isinstance(result, dict) else {"matches": []}

    overall = await _judge(recall_claims)
    confirmed = await _judge(confirmed_claims)
    refuted = await _judge(refuted_claims)

    overall_tp, _ = _matching(goldens, recall_claims, overall.get("matches", []))
    confirmed_tp, _ = _matching(goldens, confirmed_claims, confirmed.get("matches", []))
    refuted_tp, matched_refuted = _matching(goldens, refuted_claims, refuted.get("matches", []))

    return {
        "total_golden": golden_count,
        "ledger_candidates": len(recall_claims),
        "ledger_recall": _recall(overall_tp, golden_count),
        "ledger_tp": overall_tp,
        "confirmed_recall": _recall(confirmed_tp, golden_count),
        "confirmed_tp": confirmed_tp,
        "refuted_goldens": refuted_tp,
        "refuted_matches": matched_refuted,
    }


def _load_judge(concurrency: int) -> Any:
    root = Path(__file__).resolve()
    for parent in (root, *root.parents):
        candidate = parent / ".reviewforge" / "benchmarks"
        if (candidate / "martian_judge.py").exists():
            sys.path.insert(0, str(candidate))
            break
    import martian_judge  # noqa: PLC0415

    return martian_judge.Judge(concurrency)


async def main_async(args: argparse.Namespace) -> None:
    workload = json.loads(Path(args.workload).read_text(encoding="utf-8"))
    ledgers = json.loads(Path(args.ledgers).read_text(encoding="utf-8"))
    judge = _load_judge(args.concurrency)
    try:
        rows: list[dict[str, Any]] = []
        for item in workload:
            url = item["golden_url"]
            items = ledgers.get(url)
            if items is None:
                continue
            result = await evaluate_pr(judge, item["golden_comments"], items)
            result["golden_url"] = url
            rows.append(result)
        summary = {
            "reviews": len(rows),
            "total_golden": sum(row["total_golden"] for row in rows),
            "ledger_tp": sum(row["ledger_tp"] for row in rows),
            "confirmed_tp": sum(row["confirmed_tp"] for row in rows),
            "refuted_goldens": sum(row["refuted_goldens"] for row in rows),
        }
        summary["ledger_recall"] = _recall(summary["ledger_tp"], summary["total_golden"])
        summary["confirmed_recall"] = _recall(summary["confirmed_tp"], summary["total_golden"])
        payload = {"summary": summary, "per_pr": rows}
        Path(args.output).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(summary, ensure_ascii=False), flush=True)
    finally:
        await judge.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workload", required=True)
    parser.add_argument("--ledgers", required=True, help="JSON {golden_url: [hypothesis, ...]}")
    parser.add_argument("--output", required=True)
    parser.add_argument("--concurrency", type=int, default=20)
    asyncio.run(main_async(parser.parse_args()))


if __name__ == "__main__":
    main()
