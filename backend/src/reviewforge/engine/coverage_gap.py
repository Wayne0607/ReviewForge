"""Selective coverage-gap analysis for high-risk changed symbols.

The first review pass is intentionally broad and inexpensive.  This module
finds important changed symbols that did not receive a candidate finding and
builds small, provenance-carrying evidence cards for one bounded second pass.
It is pure logic: deciding whether a second pass is useful costs no tokens.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from typing import Any

from reviewforge.core.state import Finding

_NON_ACTIONABLE_CATEGORIES = {
    "documentation",
    "missing-doc",
    "missing-test",
    "style",
    "test-coverage",
}


@dataclass(frozen=True)
class EvidenceCard:
    """Bounded evidence for one changed symbol that still needs scrutiny."""

    file: str
    symbol: str
    symbol_type: str
    start_line: int
    end_line: int
    added_lines: tuple[int, ...]
    risk_score: int
    risk_reasons: tuple[str, ...]
    coverage_gaps: tuple[str, ...]
    references: tuple[str, ...] = ()
    candidate_tests: tuple[str, ...] = ()
    historical_evidence: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    wiki_facts: tuple[dict[str, Any], ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        for key in (
            "added_lines",
            "risk_reasons",
            "coverage_gaps",
            "references",
            "candidate_tests",
            "historical_evidence",
            "wiki_facts",
        ):
            payload[key] = list(payload[key])
        return payload


def build_evidence_cards(
    manifest: dict[str, Any] | None,
    findings: Iterable[Finding],
    *,
    min_risk_score: int = 4,
    max_cards: int = 3,
) -> list[EvidenceCard]:
    """Return the highest-risk changed symbols not covered by a finding.

    Risk is derived only from retrieved facts (sensitive names, live
    references, calls, and historical graph rows).  Missing tests or history
    are coverage gaps, not evidence that a defect exists.
    """

    if not manifest or max_cards <= 0:
        return []

    all_findings = list(findings)
    references = {
        str(item.get("symbol", "")): tuple(str(path) for path in item.get("paths", []) if path)
        for item in manifest.get("references", [])
    }
    candidate_tests = tuple(str(path) for path in manifest.get("candidate_tests", []) if path)
    risk_signals = list(manifest.get("risk_signals", []))
    graph_rows = list(manifest.get("historical_graph", []))
    wiki_pages = list(manifest.get("wiki_pages", []))

    cards: list[EvidenceCard] = []
    for file_item in manifest.get("files", []):
        path = str(file_item.get("path", ""))
        file_added_lines = tuple(int(line) for line in file_item.get("added_lines", []) if isinstance(line, int))
        calls = list(file_item.get("calls", []))
        for symbol_item in file_item.get("changed_symbols", []):
            symbol = str(symbol_item.get("name", ""))
            if not path or not symbol:
                continue
            start = _as_positive_int(symbol_item.get("start_line") or symbol_item.get("line"))
            end = max(start, _as_positive_int(symbol_item.get("end_line") or start))
            symbol_added_lines = tuple(
                int(line) for line in symbol_item.get("added_lines", []) if isinstance(line, int)
            )
            added_lines = symbol_added_lines or tuple(line for line in file_added_lines if start <= line <= end)
            if not added_lines or _symbol_has_finding(path, symbol, start, end, all_findings):
                continue

            symbol_refs = references.get(symbol, ())
            symbol_graph = tuple(row for row in graph_rows if _row_matches(row, path, symbol))[:3]
            symbol_facts = tuple(_wiki_facts_for_symbol(wiki_pages, path, symbol))[:5]
            symbol_calls = [
                call
                for call in calls
                if str(call.get("caller", "")) == symbol or int(call.get("line", 0) or 0) in added_lines
            ]

            risk_score = 0
            reasons: list[str] = []
            if not symbol.startswith("_"):
                risk_score += 1
                reasons.append("changed-public-symbol")
            if _has_signal(risk_signals, "security-sensitive-symbol", path, symbol):
                risk_score += 3
                reasons.append("security-sensitive-symbol")
            if symbol_refs:
                reference_weight = 2 if len(symbol_refs) >= 2 else 1
                risk_score += reference_weight
                reasons.append(f"live-references:{len(symbol_refs)}")
            if symbol_calls:
                risk_score += 1
                reasons.append(f"changed-call-sites:{len(symbol_calls)}")
            if symbol_graph:
                risk_score += 2
                reasons.append(f"historical-relations:{len(symbol_graph)}")

            if risk_score < min_risk_score:
                continue

            gaps: list[str] = []
            if not symbol_refs:
                gaps.append("live-references")
            if not symbol_graph:
                gaps.append("historical-context")
            matching_tests = tuple(path for path in candidate_tests if path in symbol_refs)
            if not matching_tests:
                gaps.append("test-evidence")
            if not symbol_facts:
                gaps.append("wiki-contract")

            cards.append(
                EvidenceCard(
                    file=path,
                    symbol=symbol,
                    symbol_type=str(symbol_item.get("type", "")),
                    start_line=start,
                    end_line=end,
                    added_lines=added_lines[:12],
                    risk_score=risk_score,
                    risk_reasons=tuple(reasons),
                    coverage_gaps=tuple(gaps),
                    references=symbol_refs[:8],
                    candidate_tests=matching_tests[:4],
                    historical_evidence=symbol_graph,
                    wiki_facts=symbol_facts,
                )
            )

    cards.sort(key=lambda card: (-card.risk_score, card.file, card.start_line, card.symbol))
    return cards[:max_cards]


def filter_gap_findings(
    findings: Iterable[Finding],
    cards: Iterable[EvidenceCard],
    *,
    min_confidence: float = 0.65,
) -> tuple[list[Finding], list[Finding]]:
    """Require second-pass findings to anchor to a card's changed lines."""

    card_list = list(cards)
    accepted: list[Finding] = []
    rejected: list[Finding] = []
    for finding in findings:
        category = finding.category.strip().lower()
        anchored = any(finding.file == card.file and finding.line in card.added_lines for card in card_list)
        if anchored and finding.confidence >= min_confidence and category not in _NON_ACTIONABLE_CATEGORIES:
            finding.reviewer = "coverage_gap_reviewer"
            accepted.append(finding)
        else:
            rejected.append(finding)
    return accepted, rejected


def _symbol_has_finding(
    path: str,
    symbol: str,
    start: int,
    end: int,
    findings: list[Finding],
) -> bool:
    symbol_lower = symbol.lower()
    for finding in findings:
        if finding.file != path or finding.status == "false_positive":
            continue
        if start <= finding.line <= end:
            return True
        if symbol_lower in f"{finding.message} {finding.suggestion}".lower():
            return True
    return False


def _has_signal(signals: list[dict[str, Any]], signal_type: str, path: str, symbol: str) -> bool:
    return any(
        str(item.get("type", "")) == signal_type
        and str(item.get("file", "")) == path
        and str(item.get("symbol", "")) == symbol
        for item in signals
    )


def _row_matches(row: dict[str, Any], path: str, symbol: str) -> bool:
    if str(row.get("symbol", "")) == symbol:
        return True
    paths = {str(row.get(key, "")) for key in ("file", "source_file", "target_file")}
    symbols = {str(row.get(key, "")) for key in ("symbol", "source_symbol", "target_symbol")}
    return path in paths and symbol in symbols


def _wiki_facts_for_symbol(pages: list[dict[str, Any]], path: str, symbol: str) -> list[dict[str, Any]]:
    facts: list[dict[str, Any]] = []
    for page in pages:
        source = page.get("source", {})
        source_path = str(source.get("path", page.get("source_path", "")))
        title = str(page.get("title", ""))
        if title:
            if title != symbol:
                continue
        elif source_path != path:
            continue
        for fact in page.get("facts", []):
            if isinstance(fact, dict):
                facts.append(dict(fact))
    return facts


def _as_positive_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0
