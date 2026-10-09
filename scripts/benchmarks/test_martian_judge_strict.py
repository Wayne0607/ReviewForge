"""Minimal offline regression tests for strict one-to-one benchmark scoring."""

from __future__ import annotations

import asyncio
import unittest

from martian_judge import evaluate


class FakeJudge:
    def __init__(self, matches: list[dict[str, object]] | None = None) -> None:
        self.matches = matches

    async def match_all(self, goldens: list[str], candidates: list[str]) -> dict[str, object]:
        self.seen_candidates = candidates
        return {
            "matches": self.matches
            or [
                {
                    "golden_index": 0,
                    "candidate_index": 0,
                    "confidence": 0.99,
                    "reasoning": "first copy matches",
                },
                {
                    "golden_index": 0,
                    "candidate_index": 1,
                    "confidence": 0.98,
                    "reasoning": "duplicate copy also matches",
                },
            ]
        }


class StrictEvaluateTest(unittest.TestCase):
    def test_exact_duplicate_copy_counts_as_false_positive(self) -> None:
        judge = FakeJudge()
        result = asyncio.run(
            evaluate(
                judge,  # type: ignore[arg-type]
                [{"comment": "SQL query is injectable", "severity": "high"}],
                ["SQL query is injectable", "SQL query is injectable"],
            )
        )

        self.assertEqual(
            judge.seen_candidates,
            ["SQL query is injectable", "SQL query is injectable"],
        )
        self.assertEqual(result["tp"], 1)
        self.assertEqual(result["fp"], 1)
        self.assertEqual(result["fn"], 0)
        self.assertEqual(result["total_candidates"], 2)
        self.assertEqual(result["false_positives"], ["SQL query is injectable"])
        self.assertEqual(result["true_positives"][0]["golden_index"], 0)
        self.assertIn(result["true_positives"][0]["candidate_index"], {0, 1})

    def test_matching_is_one_to_one_without_greedy_undercount(self) -> None:
        judge = FakeJudge(
            [
                {"golden_index": 0, "candidate_index": 0, "confidence": 0.99},
                {"golden_index": 0, "candidate_index": 1, "confidence": 0.80},
                {"golden_index": 1, "candidate_index": 0, "confidence": 0.90},
            ]
        )
        result = asyncio.run(
            evaluate(
                judge,  # type: ignore[arg-type]
                [{"comment": "issue A"}, {"comment": "issue B"}],
                ["candidate X", "candidate Y"],
            )
        )

        self.assertEqual(result["tp"], 2)
        self.assertEqual(result["fp"], 0)
        self.assertEqual(result["fn"], 0)
        self.assertEqual(
            len({item["candidate_index"] for item in result["true_positives"]}),
            2,
        )
        self.assertEqual(
            len({item["golden_index"] for item in result["true_positives"]}),
            2,
        )


if __name__ == "__main__":
    unittest.main()
