from __future__ import annotations

import pytest

from reviewforge.eval.ledger_recall import _matching, candidate_claims, evaluate_pr


def _items(*pairs: tuple[str, str]) -> list[dict]:
    return [{"claim": claim, "status": status} for status, claim in pairs]


def test_candidate_claims_filters_by_status() -> None:
    items = _items(("confirmed", "a"), ("open", "b"), ("refuted", "c"), ("unknown", "d"), ("open", ""))
    assert candidate_claims(items, ("confirmed", "open", "unknown")) == ["a", "b", "d"]
    assert candidate_claims(items, ("confirmed",)) == ["a"]
    assert candidate_claims(items, ("refuted",)) == ["c"]


def test_matching_ignores_below_confidence_and_out_of_range() -> None:
    goldens = [{"comment": "g0"}]
    candidates = ["c0", "c1"]
    matches = [
        {"golden_index": 0, "candidate_index": 0, "confidence": 0.9},
        {"golden_index": 0, "candidate_index": 1, "confidence": 0.3},  # too weak
        {"golden_index": 5, "candidate_index": 0, "confidence": 0.9},  # out of range
    ]
    tp, matched = _matching(goldens, candidates, matches)
    assert tp == 1
    assert matched[0]["candidate_index"] == 0


class _FakeJudge:
    def __init__(self, responses: dict):
        self._responses = responses
        self.calls: list[tuple[list, list]] = []

    async def match_all(self, goldens: list[str], claims: list[str]) -> dict:
        self.calls.append((goldens, claims))
        if not claims:
            return {"matches": []}
        # Return a match for the first candidate to the first golden.
        return {"matches": [{"golden_index": 0, "candidate_index": 0, "confidence": 0.9}]}


@pytest.mark.asyncio
async def test_evaluate_pr_reports_recall_and_refuted_goldens() -> None:
    goldens = [{"comment": "golden issue"}]
    items = _items(
        ("confirmed", "confirmed claim"),
        ("refuted", "refuted claim"),
        ("open", "open claim"),
        ("unknown", "unknown claim"),
    )
    judge = _FakeJudge({})

    result = await evaluate_pr(judge, goldens, items)

    assert result["total_golden"] == 1
    assert result["ledger_candidates"] == 3  # confirmed + open + unknown
    assert result["ledger_tp"] == 1
    assert result["ledger_recall"] == 1.0
    assert result["confirmed_tp"] == 1
    assert result["refuted_goldens"] == 1  # the refuted claim also matched → investigation误杀
