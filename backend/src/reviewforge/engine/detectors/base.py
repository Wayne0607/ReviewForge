"""Detector primitives shared by deterministic scanners."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from reviewforge.engine.security_categories import normalize_category


@dataclass(frozen=True)
class DetectorFinding:
    """Normalized finding emitted by deterministic scanners."""

    file: str
    line: int
    severity: str
    category: str
    message: str
    suggestion: str
    confidence: float


def normalize_category_for_detector(category: str) -> str:
    """Keep detector category naming stable and lightweight."""

    return normalize_category(category)


def iter_added_lines(diff: str) -> list[tuple[int, str]]:
    """Return `(line_no, line)` for added lines in a unified diff chunk."""

    lines: list[tuple[int, str]] = []
    for idx, raw_line in enumerate((diff or "").splitlines(), start=1):
        if raw_line.startswith("+++"):
            continue
        if raw_line.startswith("+"):
            lines.append((idx, raw_line[1:]))
    return lines


def match_lines(diff: str, pattern: str) -> list[tuple[int, re.Match[str]]]:
    """Return matches only for added lines."""

    out: list[tuple[int, re.Match[str]]] = []
    for line_no, line in iter_added_lines(diff or ""):
        found = re.search(pattern, line, re.IGNORECASE)
        if found:
            out.append((line_no, found))
    return out


def safe_confidence(base: float, hit_count: int) -> float:
    """Slightly increase confidence with repeated matches, capped by 0.97."""

    return min(0.97, base + 0.03 * max(0, hit_count - 1))


def dedupe_findings(findings: list[DetectorFinding]) -> list[DetectorFinding]:
    """Keep strongest duplicate per `(file, line, category)`."""

    deduped: dict[tuple[str, int, str], DetectorFinding] = {}
    for item in findings:
        key = (item.file, item.line, item.category)
        current = deduped.get(key)
        if current is None or item.confidence > current.confidence:
            deduped[key] = item
    return list(deduped.values())


def as_dicts(findings: list[DetectorFinding], reviewer_name: str, reviewer_type: str) -> list[dict[str, Any]]:
    """Serialize detector findings for downstream `Finding` conversion."""

    return [
        {
            "file": item.file,
            "line": max(1, item.line),
            "severity": item.severity,
            "category": item.category,
            "message": item.message,
            "suggestion": item.suggestion,
            "confidence": item.confidence,
            "reviewer": reviewer_name,
            "verified_by": "detector" if reviewer_type in {"security", "dependency"} else "detector",
            "status": "candidate",
        }
        for item in findings
    ]
