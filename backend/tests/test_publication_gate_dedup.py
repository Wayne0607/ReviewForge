"""Tests for the publication_gate dedup pass (perf/gate-dedup-20260729)."""

from __future__ import annotations

from types import SimpleNamespace

from reviewforge.engine.orchestrator import Orchestrator


def _finding(fid: str, file: str, line: int, category: str, confidence: float) -> SimpleNamespace:
    return SimpleNamespace(
        id=fid,
        file=file,
        line=line,
        category=category,
        confidence=confidence,
    )


def _orchestrator_with_dedup(enabled: bool) -> Orchestrator:
    """Test instance: only the dedup fields are exercised."""
    return Orchestrator.__new__(Orchestrator)  # bypass __init__


def test_dedup_collapses_same_root_cause_keeps_highest_confidence():
    orch = _orchestrator_with_dedup(True)
    orch._publication_gate_dedup = True
    candidates = [
        _finding("a", "x.py", 10, "sql-injection", 0.80),
        _finding("b", "x.py", 10, "sql-injection", 0.95),  # higher confidence
        _finding("c", "x.py", 10, "sql-injection", 0.70),
    ]
    kept, stats = orch._dedup_by_root_cause(candidates)
    assert [f.id for f in kept] == ["b"]
    assert stats.collapsed == 2
    assert stats.buckets == 1


def test_dedup_disabled_returns_input_unchanged():
    orch = _orchestrator_with_dedup(False)
    orch._publication_gate_dedup = False
    candidates = [
        _finding("a", "x.py", 10, "sql-injection", 0.80),
        _finding("b", "x.py", 10, "sql-injection", 0.95),
    ]
    kept, stats = orch._dedup_by_root_cause(candidates)
    assert [f.id for f in kept] == ["a", "b"]
    assert stats.collapsed == 0


def test_dedup_keeps_distinct_anchor_and_category():
    orch = _orchestrator_with_dedup(True)
    orch._publication_gate_dedup = True
    candidates = [
        _finding("a", "x.py", 10, "sql-injection", 0.80),
        _finding("b", "x.py", 11, "sql-injection", 0.80),  # different line
        _finding("c", "y.py", 10, "sql-injection", 0.80),  # different file
        _finding("d", "x.py", 10, "xss", 0.80),  # different category
    ]
    kept, stats = orch._dedup_by_root_cause(candidates)
    assert [f.id for f in kept] == ["a", "b", "c", "d"]
    assert stats.collapsed == 0
    assert stats.buckets == 4


def test_dedup_keeps_unkeyed_findings():
    orch = _orchestrator_with_dedup(True)
    orch._publication_gate_dedup = True
    candidates = [
        _finding("a", "", 10, "sql-injection", 0.80),  # missing file
        _finding("b", "x.py", 0, "sql-injection", 0.80),  # missing line
        _finding("c", "x.py", 10, "sql-injection", 0.80),  # normal
    ]
    kept, stats = orch._dedup_by_root_cause(candidates)
    assert len(kept) == 3
    assert stats.collapsed == 0


def test_dedup_key_normalizes_category():
    orch = _orchestrator_with_dedup(True)
    assert orch._dedup_key(_finding("a", "x.py", 10, "sql_injection", 0.8)) == (
        "x.py",
        10,
        "sql-injection",
    )
    assert orch._dedup_key(_finding("a", "x.py", 10, "SQL_INJECTION", 0.8)) == (
        "x.py",
        10,
        "sql-injection",
    )
    assert orch._dedup_key(_finding("a", "", 10, "x", 0.8)) is None
    assert orch._dedup_key(_finding("a", "x.py", 0, "x", 0.8)) is None
