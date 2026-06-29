"""Verifier — pure-logic auditor: merges duplicate findings and drops low-confidence noise.

This is the documented Verifier stage (去误报 / 合并重复). It runs BEFORE the LLM
Calibrator: deterministically merging duplicates across reviewers and dropping
sub-floor findings means the (token-costly) calibrator only judges distinct,
plausible candidates. No LLM call — pure reasoning over the candidate set.
"""

from __future__ import annotations

import logging

from reviewforge.core.state import Finding

logger = logging.getLogger(__name__)


class Verifier:
    """Deterministic de-duplication + confidence-floor filtering of candidate findings."""

    def __init__(self, confidence_floor: float = 0.0) -> None:
        self._floor = confidence_floor

    def verify(self, findings: list[Finding]) -> tuple[list[Finding], list[str]]:
        """Return (survivors, dropped_ids).

        - Findings below the confidence floor are dropped.
        - Findings sharing (file, line, category) are merged into one survivor:
          the highest-confidence finding wins, and the reviewers of the merged
          duplicates are unioned onto it (so "found by N reviewers" is preserved).
        """
        survivors: dict[tuple, Finding] = {}
        dropped: list[str] = []

        for f in findings:
            if f.confidence < self._floor:
                dropped.append(f.id)
                continue
            key = (f.file, f.line, f.category)
            existing = survivors.get(key)
            if existing is None:
                survivors[key] = f
                continue
            # Duplicate: keep the higher-confidence one, union reviewer attribution.
            winner, loser = (f, existing) if f.confidence > existing.confidence else (existing, f)
            winner.reviewer = self._union_reviewers(winner.reviewer, loser.reviewer)
            survivors[key] = winner
            dropped.append(loser.id)

        out = list(survivors.values())
        if dropped:
            logger.info(f"Verifier: {len(out)} kept, {len(dropped)} merged/dropped as duplicate/low-confidence")
        return out, dropped

    @staticmethod
    def _union_reviewers(a: str, b: str) -> str:
        names = sorted({n for n in (a, b) if n})
        return ",".join(names)
