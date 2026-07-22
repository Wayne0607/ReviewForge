"""Comprehensive tests for the CoverageLedger — v3 coverage model.

Tests are deterministic and cover:
- Enum completeness
- Cell lifecycle and valid/invalid transitions
- Ledger construction from SemanticChangeSet-shaped dicts
- Mandatory cell policy (correctness always, risk-signal-driven, localization, cross-PR)
- Optional dimension cap
- Prioritized pending queries
- Assignment, finding, no-issue, abstain, failure lifecycle
- Retry after failure
- Terminal/completion rules
- Summary
- Stable JSON round-trip (serialize → deserialize → serialize == original)
- >100 cell stress test
"""

from __future__ import annotations

import json

import pytest

from reviewforge.engine.coverage_ledger import (
    TERMINAL_STATUSES,
    CoverageCell,
    CoverageDimension,
    CoverageLedger,
    CoverageStatus,
    _coerce_float,
    _coerce_int,
    _extract_risk_signals,
    _has_cross_pr_signal,
    _is_localization_path,
)

# ── Helpers ─────────────────────────────────────────────────────────────────


def _make_unit(
    uid: str,
    path: str = "src/app.py",
    line: int = 10,
    risk: int = 0,
    risk_signals: list[str | dict] | None = None,
    metadata: dict | None = None,
    *,
    risk_score: float | None = None,
    start_line: int | None = None,
) -> dict:
    """Build a minimal SemanticUnit-shaped dict.

    Supports both the legacy ``risk``/``line`` keys and the canonical
    ``risk_score``/``start_line`` keys from ``SemanticUnit.to_dict()``.
    When *risk_score* is provided it takes precedence; when *start_line*
    is provided it takes precedence over *line*.
    """
    unit: dict = {"id": uid, "path": path}
    if risk_score is not None:
        unit["risk_score"] = risk_score
    else:
        unit["risk"] = risk
    if start_line is not None:
        unit["start_line"] = start_line
    else:
        unit["line"] = line
    if risk_signals is not None:
        unit["risk_signals"] = risk_signals
    if metadata is not None:
        unit["metadata"] = metadata
    return unit


def _make_semantic_unit(
    uid: str,
    path: str = "src/app.py",
    start_line: int = 10,
    end_line: int = 20,
    risk_score: float = 0.0,
    risk_signals: list[dict] | None = None,
    language: str = "python",
    symbol: str = "func",
) -> dict:
    """Build a dict matching the exact ``SemanticUnit.to_dict()`` shape.

    This is the integration-shaped fixture copied by field shape, not by
    import.  It uses ``risk_score`` (float), ``start_line``/``end_line``,
    and ``risk_signals`` as ``list[dict]`` — exactly what
    ``SemanticUnit.to_dict()`` from the semantic_diff sibling branch
    produces.
    """
    return {
        "id": uid,
        "path": path,
        "language": language,
        "kind": "symbol",
        "symbol": symbol,
        "start_line": start_line,
        "end_line": end_line,
        "added_lines": [],
        "calls": [],
        "imports": [],
        "references": [],
        "candidate_tests": [],
        "risk_signals": risk_signals if risk_signals is not None else [],
        "wiki_facts": [],
        "provenance": {"source": "manifest", "manifest_version": 1, "note": ""},
        "risk_score": risk_score,
        "risk_reasons": [],
    }


def _make_change_set(units: list[dict]) -> dict:
    """Wrap units in a SemanticChangeSet-shaped dict."""
    return {"units": units, "repo": "owner/repo", "pr_number": 1, "head_sha": "abc123"}


# ── Enum completeness ───────────────────────────────────────────────────────


class TestEnumCompleteness:
    def test_dimensions_match_spec(self):
        expected = {
            "correctness",
            "contract",
            "error-handling",
            "security",
            "testing",
            "localization",
            "performance",
            "compatibility",
            "cross-PR",
        }
        actual = {d.value for d in CoverageDimension}
        assert actual == expected

    def test_statuses_match_spec(self):
        expected = {"pending", "assigned", "covered", "no_issue", "abstained", "failed"}
        actual = {s.value for s in CoverageStatus}
        assert actual == expected

    def test_terminal_statuses(self):
        assert CoverageStatus.COVERED in TERMINAL_STATUSES
        assert CoverageStatus.NO_ISSUE in TERMINAL_STATUSES
        assert CoverageStatus.ABSTAINED in TERMINAL_STATUSES
        assert CoverageStatus.FAILED in TERMINAL_STATUSES
        assert CoverageStatus.PENDING not in TERMINAL_STATUSES
        assert CoverageStatus.ASSIGNED not in TERMINAL_STATUSES


# ── CoverageCell lifecycle ───────────────────────────────────────────────────


class TestCellLifecycle:
    def _cell(self, status: CoverageStatus = CoverageStatus.PENDING) -> CoverageCell:
        return CoverageCell(
            unit_id="u1",
            path="src/app.py",
            line=10,
            dimension=CoverageDimension.CORRECTNESS,
            risk=3,
            mandatory=True,
            status=status,
        )

    def test_pending_to_assigned(self):
        cell = self._cell()
        cell.transition(CoverageStatus.ASSIGNED, task_id="task_1")
        assert cell.status == CoverageStatus.ASSIGNED
        assert cell.attempts == 1
        assert "task_1" in cell.assigned_task_ids

    def test_assigned_to_covered(self):
        cell = self._cell(CoverageStatus.ASSIGNED)
        cell.transition(CoverageStatus.COVERED, terminal_reason="finding:f1")
        assert cell.status == CoverageStatus.COVERED
        assert cell.is_terminal()

    def test_assigned_to_no_issue_with_evidence(self):
        cell = self._cell(CoverageStatus.ASSIGNED)
        cell.transition(CoverageStatus.NO_ISSUE, evidence="Reviewed; no defect found.")
        assert cell.status == CoverageStatus.NO_ISSUE
        assert cell.evidence == "Reviewed; no defect found."
        assert cell.is_terminal()

    def test_assigned_to_abstained(self):
        cell = self._cell(CoverageStatus.ASSIGNED)
        cell.transition(CoverageStatus.ABSTAINED, terminal_reason="insufficient context")
        assert cell.status == CoverageStatus.ABSTAINED
        assert cell.is_terminal()

    def test_assigned_to_failed(self):
        cell = self._cell(CoverageStatus.ASSIGNED)
        cell.transition(CoverageStatus.FAILED, terminal_reason="tool error")
        assert cell.status == CoverageStatus.FAILED
        assert cell.attempts == 1

    def test_pending_to_failed(self):
        cell = self._cell()
        cell.transition(CoverageStatus.FAILED, terminal_reason="timeout")
        assert cell.status == CoverageStatus.FAILED

    def test_failed_to_assigned_retry(self):
        cell = self._cell(CoverageStatus.FAILED)
        # attempts=0 because cell was created directly in FAILED, not transitioned
        assert cell.attempts == 0
        cell.transition(CoverageStatus.ASSIGNED, task_id="task_retry")
        assert cell.status == CoverageStatus.ASSIGNED
        assert cell.attempts == 1  # +1 from ASSIGNED transition

    def test_covered_is_terminal(self):
        cell = self._cell(CoverageStatus.COVERED)
        with pytest.raises(ValueError, match="Invalid transition"):
            cell.transition(CoverageStatus.ASSIGNED)

    def test_no_issue_is_terminal(self):
        cell = self._cell(CoverageStatus.NO_ISSUE)
        with pytest.raises(ValueError, match="Invalid transition"):
            cell.transition(CoverageStatus.FAILED)

    def test_abstained_is_terminal(self):
        cell = self._cell(CoverageStatus.ABSTAINED)
        # Abstained is terminal but retryable — COVERED is not allowed
        with pytest.raises(ValueError, match="Invalid transition"):
            cell.transition(CoverageStatus.COVERED)
        # But ASSIGNED (retry) IS allowed
        cell.transition(CoverageStatus.ASSIGNED, task_id="retry_task")
        assert cell.status == CoverageStatus.ASSIGNED

    def test_no_issue_requires_evidence(self):
        cell = self._cell(CoverageStatus.ASSIGNED)
        with pytest.raises(ValueError, match="no_issue closure requires explicit evidence"):
            cell.transition(CoverageStatus.NO_ISSUE, evidence="")
        with pytest.raises(ValueError, match="no_issue closure requires explicit evidence"):
            cell.transition(CoverageStatus.NO_ISSUE, evidence="   ")

    def test_pending_to_covered_invalid(self):
        cell = self._cell()
        with pytest.raises(ValueError, match="Invalid transition"):
            cell.transition(CoverageStatus.COVERED)

    def test_pending_to_no_issue_invalid(self):
        cell = self._cell()
        with pytest.raises(ValueError, match="Invalid transition"):
            cell.transition(CoverageStatus.NO_ISSUE)

    def test_pending_to_abstained_invalid(self):
        cell = self._cell()
        with pytest.raises(ValueError, match="Invalid transition"):
            cell.transition(CoverageStatus.ABSTAINED)

    def test_covered_to_no_issue_invalid(self):
        cell = self._cell(CoverageStatus.COVERED)
        with pytest.raises(ValueError, match="Invalid transition"):
            cell.transition(CoverageStatus.NO_ISSUE)

    def test_add_finding_deduplicates(self):
        cell = self._cell()
        cell.add_finding("f1")
        cell.add_finding("f1")
        cell.add_finding("f2")
        assert cell.finding_ids == ["f1", "f2"]

    def test_add_finding_ignores_empty(self):
        cell = self._cell()
        cell.add_finding("")
        assert cell.finding_ids == []

    def test_task_id_dedup_on_assign(self):
        cell = self._cell()
        cell.transition(CoverageStatus.ASSIGNED, task_id="t1")
        cell.transition(CoverageStatus.FAILED, reason="err")
        cell.transition(CoverageStatus.ASSIGNED, task_id="t1")
        assert cell.assigned_task_ids.count("t1") == 1

    def test_terminal_reason_persists(self):
        cell = self._cell(CoverageStatus.ASSIGNED)
        cell.transition(CoverageStatus.FAILED, terminal_reason="crash")
        assert cell.terminal_reason == "crash"


# ── CoverageCell serialization ──────────────────────────────────────────────


class TestCellSerialization:
    def test_round_trip(self):
        cell = CoverageCell(
            unit_id="u1",
            path="src/app.py",
            line=42,
            dimension=CoverageDimension.SECURITY,
            risk=7,
            mandatory=True,
            status=CoverageStatus.ASSIGNED,
            attempts=2,
            assigned_task_ids=["t1", "t2"],
            finding_ids=["f1"],
            terminal_reason="",
            evidence="",
        )
        d = cell.to_dict()
        restored = CoverageCell.from_dict(d)
        assert restored.to_dict() == d

    def test_from_dict_with_minimal_fields(self):
        d = {"unit_id": "u", "path": "p", "line": 1, "dimension": "correctness"}
        cell = CoverageCell.from_dict(d)
        assert cell.status == CoverageStatus.PENDING
        assert cell.risk == 0
        assert cell.mandatory is False


# ── Ledger construction from change set ─────────────────────────────────────


class TestLedgerConstruction:
    def test_single_unit_gets_correctness(self):
        cs = _make_change_set([_make_unit("u1")])
        ledger = CoverageLedger.from_change_set(cs)
        assert len(ledger.cells) >= 1
        cell = ledger.get_cell("u1", CoverageDimension.CORRECTNESS)
        assert cell is not None
        assert cell.mandatory is True
        assert cell.status == CoverageStatus.PENDING

    def test_security_risk_signal_creates_security_cell(self):
        cs = _make_change_set([_make_unit("u1", risk_signals=["security-sensitive-symbol"])])
        ledger = CoverageLedger.from_change_set(cs)
        sec = ledger.get_cell("u1", CoverageDimension.SECURITY)
        assert sec is not None
        assert sec.mandatory is True

    def test_localization_risk_signal_creates_localization_cell(self):
        cs = _make_change_set([_make_unit("u1", risk_signals=["localization-resource"])])
        ledger = CoverageLedger.from_change_set(cs)
        loc = ledger.get_cell("u1", CoverageDimension.LOCALIZATION)
        assert loc is not None
        assert loc.mandatory is True

    def test_cross_pr_risk_signal_creates_cross_pr_cell(self):
        cs = _make_change_set([_make_unit("u1", risk_signals=["cross-PR"])])
        ledger = CoverageLedger.from_change_set(cs)
        cp = ledger.get_cell("u1", CoverageDimension.CROSS_PR)
        assert cp is not None
        assert cp.mandatory is True

    def test_error_handling_risk_signal(self):
        cs = _make_change_set([_make_unit("u1", risk_signals=["error-handling"])])
        ledger = CoverageLedger.from_change_set(cs)
        eh = ledger.get_cell("u1", CoverageDimension.ERROR_HANDLING)
        assert eh is not None
        assert eh.mandatory is True

    def test_contract_risk_signal(self):
        cs = _make_change_set([_make_unit("u1", risk_signals=["contract-surface"])])
        ledger = CoverageLedger.from_change_set(cs)
        ct = ledger.get_cell("u1", CoverageDimension.CONTRACT)
        assert ct is not None
        assert ct.mandatory is True

    def test_testing_risk_signal(self):
        cs = _make_change_set([_make_unit("u1", risk_signals=["testing-scope"])])
        ledger = CoverageLedger.from_change_set(cs)
        t = ledger.get_cell("u1", CoverageDimension.TESTING)
        assert t is not None
        assert t.mandatory is True

    def test_dict_risk_signals(self):
        cs = _make_change_set([_make_unit("u1", risk_signals=[{"type": "security-sensitive"}])])
        ledger = CoverageLedger.from_change_set(cs)
        sec = ledger.get_cell("u1", CoverageDimension.SECURITY)
        assert sec is not None

    def test_localization_path_heuristic(self):
        cs = _make_change_set([_make_unit("u1", path="src/i18n/messages.properties")])
        ledger = CoverageLedger.from_change_set(cs)
        loc = ledger.get_cell("u1", CoverageDimension.LOCALIZATION)
        assert loc is not None
        assert loc.mandatory is True

    def test_localization_json_in_locale_dir(self):
        cs = _make_change_set([_make_unit("u1", path="src/locales/en.json")])
        ledger = CoverageLedger.from_change_set(cs)
        loc = ledger.get_cell("u1", CoverageDimension.LOCALIZATION)
        assert loc is not None

    def test_cross_pr_metadata_flag(self):
        cs = _make_change_set([_make_unit("u1", metadata={"cross_pr": True})])
        ledger = CoverageLedger.from_change_set(cs)
        cp = ledger.get_cell("u1", CoverageDimension.CROSS_PR)
        assert cp is not None

    def test_no_duplicate_dimensions_per_unit(self):
        cs = _make_change_set([_make_unit("u1", risk_signals=["security-sensitive", "security-sensitive-symbol"])])
        ledger = CoverageLedger.from_change_set(cs)
        sec_cells = [c for c in ledger.cells if c.unit_id == "u1" and c.dimension == CoverageDimension.SECURITY]
        assert len(sec_cells) == 1

    def test_empty_change_set(self):
        cs = _make_change_set([])
        ledger = CoverageLedger.from_change_set(cs)
        assert ledger.cells == []
        assert ledger.is_complete()

    def test_multiple_units(self):
        units = [_make_unit(f"u{i}") for i in range(10)]
        cs = _make_change_set(units)
        ledger = CoverageLedger.from_change_set(cs)
        # At least one correctness cell per unit
        correctness_cells = [c for c in ledger.cells if c.dimension == CoverageDimension.CORRECTNESS]
        assert len(correctness_cells) == 10


# ── Optional cap ────────────────────────────────────────────────────────────


class TestOptionalCap:
    def test_cap_limits_optional_dimensions(self):
        units = [_make_unit(f"u{i}") for i in range(50)]
        cs = _make_change_set(units)
        ledger = CoverageLedger.from_change_set(cs, optional_cap=10)
        optional_cells = [
            c for c in ledger.cells if c.dimension in (CoverageDimension.PERFORMANCE, CoverageDimension.COMPATIBILITY)
        ]
        assert len(optional_cells) <= 10

    def test_cap_does_not_affect_mandatory(self):
        units = [_make_unit(f"u{i}", risk_signals=["security-sensitive"]) for i in range(50)]
        cs = _make_change_set(units)
        ledger = CoverageLedger.from_change_set(cs, optional_cap=5)
        sec_cells = [c for c in ledger.cells if c.dimension == CoverageDimension.SECURITY]
        assert len(sec_cells) == 50  # all mandatory, never dropped

    def test_cap_zero_disables_optional(self):
        units = [_make_unit(f"u{i}") for i in range(5)]
        cs = _make_change_set(units)
        ledger = CoverageLedger.from_change_set(cs, optional_cap=0)
        optional_cells = [
            c for c in ledger.cells if c.dimension in (CoverageDimension.PERFORMANCE, CoverageDimension.COMPATIBILITY)
        ]
        assert len(optional_cells) == 0
        # Correctness still present
        assert len([c for c in ledger.cells if c.dimension == CoverageDimension.CORRECTNESS]) == 5

    def test_cap_none_unlimited(self):
        units = [_make_unit(f"u{i}") for i in range(5)]
        cs = _make_change_set(units)
        ledger = CoverageLedger.from_change_set(cs, optional_cap=None)
        optional_cells = [
            c for c in ledger.cells if c.dimension in (CoverageDimension.PERFORMANCE, CoverageDimension.COMPATIBILITY)
        ]
        assert len(optional_cells) == 10  # 2 per unit


# ── Pending cell queries ────────────────────────────────────────────────────


class TestPendingQueries:
    def test_pending_returns_only_pending(self):
        cs = _make_change_set([_make_unit("u1"), _make_unit("u2")])
        ledger = CoverageLedger.from_change_set(cs)
        ledger.assign("u1", CoverageDimension.CORRECTNESS, "t1")
        pending = ledger.pending_cells(CoverageDimension.CORRECTNESS)
        unit_ids = {c.unit_id for c in pending}
        assert "u1" not in unit_ids
        assert "u2" in unit_ids

    def test_pending_sorted_by_risk_descending(self):
        cs = _make_change_set([_make_unit("u_low", risk=1), _make_unit("u_high", risk=8)])
        ledger = CoverageLedger.from_change_set(cs)
        pending = ledger.pending_cells(CoverageDimension.CORRECTNESS)
        risks = [c.risk for c in pending]
        assert risks == sorted(risks, reverse=True)

    def test_pending_mandatory_before_optional(self):
        cs = _make_change_set([_make_unit("u1", risk=5)])
        ledger = CoverageLedger.from_change_set(cs)
        pending = ledger.pending_cells()
        mandatory_idx = next(i for i, c in enumerate(pending) if c.mandatory)
        optional_idx = next(i for i, c in enumerate(pending) if not c.mandatory)
        assert mandatory_idx < optional_idx

    def test_pending_dimension_filter(self):
        cs = _make_change_set([_make_unit("u1")])
        ledger = CoverageLedger.from_change_set(cs)
        sec_pending = ledger.pending_cells(CoverageDimension.SECURITY)
        # No security cell for plain unit
        assert len(sec_pending) == 0
        correct_pending = ledger.pending_cells(CoverageDimension.CORRECTNESS)
        assert len(correct_pending) == 1


# ── Ledger mutation lifecycle ───────────────────────────────────────────────


class TestLedgerMutation:
    def _ledger(self) -> CoverageLedger:
        cs = _make_change_set([_make_unit("u1")])
        return CoverageLedger.from_change_set(cs)

    def test_assign_and_record_finding(self):
        ledger = self._ledger()
        ledger.assign("u1", CoverageDimension.CORRECTNESS, "task_1")
        cell = ledger.record_finding("u1", CoverageDimension.CORRECTNESS, "finding_1")
        assert cell.status == CoverageStatus.COVERED
        assert "finding_1" in cell.finding_ids

    def test_assign_and_close_no_issue(self):
        ledger = self._ledger()
        ledger.assign("u1", CoverageDimension.CORRECTNESS, "task_1")
        cell = ledger.close_no_issue("u1", CoverageDimension.CORRECTNESS, "Reviewed carefully; no defect.")
        assert cell.status == CoverageStatus.NO_ISSUE
        assert cell.evidence == "Reviewed carefully; no defect."

    def test_close_no_issue_without_evidence_raises(self):
        ledger = self._ledger()
        ledger.assign("u1", CoverageDimension.CORRECTNESS, "task_1")
        with pytest.raises(ValueError, match="no_issue closure requires explicit evidence"):
            ledger.close_no_issue("u1", CoverageDimension.CORRECTNESS, "")

    def test_assign_and_abstain(self):
        ledger = self._ledger()
        ledger.assign("u1", CoverageDimension.CORRECTNESS, "task_1")
        cell = ledger.abstain("u1", CoverageDimension.CORRECTNESS, "insufficient context")
        assert cell.status == CoverageStatus.ABSTAINED

    def test_assign_and_fail_then_retry(self):
        ledger = self._ledger()
        ledger.assign("u1", CoverageDimension.CORRECTNESS, "task_1")
        ledger.fail("u1", CoverageDimension.CORRECTNESS, "tool crash")
        cell = ledger.get_cell("u1", CoverageDimension.CORRECTNESS)
        assert cell.status == CoverageStatus.FAILED
        assert cell.attempts == 2  # +1 for ASSIGNED, +1 for FAILED

        ledger.retry("u1", CoverageDimension.CORRECTNESS, "task_2")
        cell = ledger.get_cell("u1", CoverageDimension.CORRECTNESS)
        assert cell.status == CoverageStatus.ASSIGNED
        assert cell.attempts == 3  # +1 for retry ASSIGNED
        assert "task_2" in cell.assigned_task_ids

    def test_fail_from_pending(self):
        ledger = self._ledger()
        cell = ledger.fail("u1", CoverageDimension.CORRECTNESS, "init error")
        assert cell.status == CoverageStatus.FAILED

    def test_record_finding_on_pending_raises(self):
        ledger = self._ledger()
        with pytest.raises(ValueError, match="must be assigned"):
            ledger.record_finding("u1", CoverageDimension.CORRECTNESS, "f1")

    def test_close_no_issue_on_pending_raises(self):
        ledger = self._ledger()
        with pytest.raises(ValueError, match="must be assigned"):
            ledger.close_no_issue("u1", CoverageDimension.CORRECTNESS, "evidence")

    def test_abstain_on_pending_raises(self):
        ledger = self._ledger()
        with pytest.raises(ValueError, match="must be assigned"):
            ledger.abstain("u1", CoverageDimension.CORRECTNESS, "reason")

    def test_retry_on_non_failed_non_abstained_raises(self):
        ledger = self._ledger()
        with pytest.raises(ValueError, match="must be failed or abstained"):
            ledger.retry("u1", CoverageDimension.CORRECTNESS, "task_x")

    def test_assign_nonexistent_cell_raises(self):
        ledger = self._ledger()
        with pytest.raises(KeyError, match="No coverage cell"):
            ledger.assign("nonexistent", CoverageDimension.CORRECTNESS, "t1")

    def test_assign_wrong_dimension_raises(self):
        ledger = self._ledger()
        with pytest.raises(KeyError, match="No coverage cell"):
            ledger.assign("u1", CoverageDimension.SECURITY, "t1")

    def test_cells_for_unit(self):
        ledger = self._ledger()
        cells = ledger.cells_for_unit("u1")
        assert len(cells) >= 1
        assert all(c.unit_id == "u1" for c in cells)

    def test_cells_by_dimension(self):
        cs = _make_change_set([_make_unit("u1"), _make_unit("u2")])
        ledger = CoverageLedger.from_change_set(cs)
        correct_cells = ledger.cells_by_dimension(CoverageDimension.CORRECTNESS)
        assert len(correct_cells) == 2

    def test_non_terminal_cells(self):
        ledger = self._ledger()
        ledger.assign("u1", CoverageDimension.CORRECTNESS, "t1")
        non_term = ledger.non_terminal_cells()
        # Assigned is non-terminal, so the correctness cell should be in the list
        correct = [c for c in non_term if c.dimension == CoverageDimension.CORRECTNESS]
        assert len(correct) == 1
        assert correct[0].status == CoverageStatus.ASSIGNED


# ── Completion rules ────────────────────────────────────────────────────────


class TestCompletion:
    def test_empty_ledger_is_complete(self):
        ledger = CoverageLedger()
        assert ledger.is_complete()
        assert ledger.mandatory_complete()

    def test_not_complete_with_pending_cells(self):
        cs = _make_change_set([_make_unit("u1")])
        ledger = CoverageLedger.from_change_set(cs)
        assert not ledger.is_complete()
        assert not ledger.mandatory_complete()

    def test_complete_when_all_terminal(self):
        cs = _make_change_set([_make_unit("u1")])
        ledger = CoverageLedger.from_change_set(cs)
        for cell in ledger.cells:
            cell.status = CoverageStatus.COVERED
        assert ledger.is_complete()
        assert ledger.mandatory_complete()

    def test_mandatory_complete_when_optional_pending(self):
        cs = _make_change_set([_make_unit("u1")])
        ledger = CoverageLedger.from_change_set(cs)
        for cell in ledger.cells:
            if cell.mandatory:
                cell.status = CoverageStatus.COVERED
        assert ledger.mandatory_complete()
        # Optional cells still pending
        optional_pending = [c for c in ledger.cells if not c.mandatory and c.status == CoverageStatus.PENDING]
        if optional_pending:
            assert not ledger.is_complete()

    def test_summary_structure(self):
        cs = _make_change_set([_make_unit("u1"), _make_unit("u2")])
        ledger = CoverageLedger.from_change_set(cs)
        ledger.assign("u1", CoverageDimension.CORRECTNESS, "t1")
        ledger.record_finding("u1", CoverageDimension.CORRECTNESS, "f1")
        summary = ledger.completion_summary()

        assert summary["total"] > 0
        assert "by_status" in summary
        assert "by_dimension" in summary
        assert summary["mandatory_total"] >= 2  # at least correctness for each unit
        assert summary["mandatory_resolved"] >= 1
        assert isinstance(summary["complete"], bool)
        assert isinstance(summary["mandatory_complete"], bool)

    def test_summary_by_dimension_counts(self):
        cs = _make_change_set([_make_unit("u1")])
        ledger = CoverageLedger.from_change_set(cs)
        summary = ledger.completion_summary()
        dim_summary = summary["by_dimension"]
        assert "correctness" in dim_summary
        assert dim_summary["correctness"]["pending"] >= 1


# ── JSON round-trip ─────────────────────────────────────────────────────────


class TestJsonRoundTrip:
    def test_serialize_deserialize_preserves_cells(self):
        cs = _make_change_set(
            [
                _make_unit("u1", risk=5, risk_signals=["security-sensitive"]),
                _make_unit("u2", path="src/i18n/messages.properties"),
            ]
        )
        ledger = CoverageLedger.from_change_set(cs)
        ledger.assign("u1", CoverageDimension.CORRECTNESS, "t1")
        ledger.record_finding("u1", CoverageDimension.CORRECTNESS, "f1")
        ledger.assign("u2", CoverageDimension.LOCALIZATION, "t2")
        ledger.close_no_issue("u2", CoverageDimension.LOCALIZATION, "No i18n issues.")

        d = ledger.to_dict()
        json_str = json.dumps(d, sort_keys=True)
        restored = CoverageLedger.from_dict(json.loads(json_str))

        assert len(restored.cells) == len(ledger.cells)
        for orig, rest in zip(ledger.cells, restored.cells):
            assert orig.to_dict() == rest.to_dict()

    def test_double_round_trip_stable(self):
        cs = _make_change_set([_make_unit("u1")])
        ledger = CoverageLedger.from_change_set(cs)
        d1 = ledger.to_dict()
        ledger2 = CoverageLedger.from_dict(d1)
        d2 = ledger2.to_dict()
        assert d1 == d2

    def test_round_trip_preserves_optional_cap(self):
        cs = _make_change_set([_make_unit("u1")])
        ledger = CoverageLedger.from_change_set(cs, optional_cap=42)
        restored = CoverageLedger.from_dict(ledger.to_dict())
        assert restored._optional_cap == 42

    def test_round_trip_with_all_terminal_states(self):
        cs = _make_change_set([_make_unit(f"u{i}") for i in range(4)])
        ledger = CoverageLedger.from_change_set(cs)

        # Assign and resolve each correctness cell differently
        ledger.assign("u0", CoverageDimension.CORRECTNESS, "t0")
        ledger.record_finding("u0", CoverageDimension.CORRECTNESS, "f0")

        ledger.assign("u1", CoverageDimension.CORRECTNESS, "t1")
        ledger.close_no_issue("u1", CoverageDimension.CORRECTNESS, "No issue.")

        ledger.assign("u2", CoverageDimension.CORRECTNESS, "t2")
        ledger.abstain("u2", CoverageDimension.CORRECTNESS, "Cannot determine.")

        ledger.assign("u3", CoverageDimension.CORRECTNESS, "t3")
        ledger.fail("u3", CoverageDimension.CORRECTNESS, "Tool error.")

        d = ledger.to_dict()
        restored = CoverageLedger.from_dict(d)
        assert restored.get_cell("u0", CoverageDimension.CORRECTNESS).status == CoverageStatus.COVERED
        assert restored.get_cell("u1", CoverageDimension.CORRECTNESS).status == CoverageStatus.NO_ISSUE
        assert restored.get_cell("u2", CoverageDimension.CORRECTNESS).status == CoverageStatus.ABSTAINED
        assert restored.get_cell("u3", CoverageDimension.CORRECTNESS).status == CoverageStatus.FAILED


# ── >100 cells stress test ──────────────────────────────────────────────────


class TestStressCells:
    def test_100_units_create_100_plus_cells(self):
        units = [_make_unit(f"u{i}", risk=i % 10) for i in range(100)]
        cs = _make_change_set(units)
        ledger = CoverageLedger.from_change_set(cs)

        assert len(ledger.cells) >= 100
        # Every unit has at least a correctness cell
        for i in range(100):
            cell = ledger.get_cell(f"u{i}", CoverageDimension.CORRECTNESS)
            assert cell is not None
            assert cell.mandatory is True

    def test_100_units_with_security_signals(self):
        units = [_make_unit(f"u{i}", risk=5 + (i % 5), risk_signals=["security-sensitive-symbol"]) for i in range(100)]
        cs = _make_change_set(units)
        ledger = CoverageLedger.from_change_set(cs)

        sec_cells = [c for c in ledger.cells if c.dimension == CoverageDimension.SECURITY]
        assert len(sec_cells) == 100
        assert all(c.mandatory for c in sec_cells)

    def test_stress_full_lifecycle(self):
        """Assign, find, and close 100+ cells to verify no state corruption."""
        units = [_make_unit(f"u{i}", risk=i % 10) for i in range(50)]
        cs = _make_change_set(units)
        ledger = CoverageLedger.from_change_set(cs)

        # Assign all correctness cells
        for i in range(50):
            ledger.assign(f"u{i}", CoverageDimension.CORRECTNESS, f"task_{i}")

        # Half get findings, half get no_issue
        for i in range(25):
            ledger.record_finding(f"u{i}", CoverageDimension.CORRECTNESS, f"finding_{i}")
        for i in range(25, 50):
            ledger.close_no_issue(f"u{i}", CoverageDimension.CORRECTNESS, f"Reviewed unit u{i}.")

        summary = ledger.completion_summary()
        assert summary["by_status"].get("covered", 0) >= 25
        assert summary["by_status"].get("no_issue", 0) >= 25

    def test_200_units_with_mixed_risk_signals(self):
        """200 units with various risk signals — no duplicates, no missing mandatory."""
        signals = [
            ["security-sensitive-symbol"],
            ["localization-resource"],
            ["cross-PR"],
            ["error-handling"],
            ["contract-surface"],
            ["testing-scope"],
            [],  # no signals — only correctness mandatory
        ]
        units = [_make_unit(f"u{i}", risk=i % 10, risk_signals=signals[i % len(signals)]) for i in range(200)]
        cs = _make_change_set(units)
        ledger = CoverageLedger.from_change_set(cs, optional_cap=50)

        assert len(ledger.cells) >= 200  # at least 200 correctness cells

        # Verify no duplicate (unit_id, dimension) pairs
        seen = set()
        for cell in ledger.cells:
            key = (cell.unit_id, cell.dimension)
            assert key not in seen, f"Duplicate cell: {key}"
            seen.add(key)

    def test_stress_round_trip(self):
        """Round-trip 200+ cells through JSON."""
        units = [_make_unit(f"u{i}", risk=i % 10, risk_signals=["security-sensitive"]) for i in range(100)]
        cs = _make_change_set(units)
        ledger = CoverageLedger.from_change_set(cs)

        # Mutate some cells
        for i in range(0, 100, 3):
            ledger.assign(f"u{i}", CoverageDimension.CORRECTNESS, f"t_{i}")
        for i in range(0, 100, 5):
            ledger.assign(f"u{i}", CoverageDimension.SECURITY, f"ts_{i}")
            ledger.record_finding(f"u{i}", CoverageDimension.SECURITY, f"fs_{i}")

        d = ledger.to_dict()
        json_str = json.dumps(d)
        restored = CoverageLedger.from_dict(json.loads(json_str))

        assert len(restored.cells) == len(ledger.cells)
        for orig, rest in zip(ledger.cells, restored.cells):
            assert orig.to_dict() == rest.to_dict()


# ── Resume / persistence ────────────────────────────────────────────────────


class TestResume:
    def test_resume_from_serialized_state(self):
        """Simulate: build ledger → serialize → resume → continue lifecycle."""
        cs = _make_change_set([_make_unit("u1"), _make_unit("u2")])
        ledger = CoverageLedger.from_change_set(cs)

        # First session: assign and fail
        ledger.assign("u1", CoverageDimension.CORRECTNESS, "t1")
        ledger.fail("u1", CoverageDimension.CORRECTNESS, "timeout")
        ledger.assign("u2", CoverageDimension.CORRECTNESS, "t2")

        # Serialize
        state = ledger.to_dict()
        json_blob = json.dumps(state)

        # Resume
        restored = CoverageLedger.from_dict(json.loads(json_blob))

        # Verify state
        u1 = restored.get_cell("u1", CoverageDimension.CORRECTNESS)
        assert u1.status == CoverageStatus.FAILED
        assert u1.attempts == 2  # +1 ASSIGNED, +1 FAILED

        u2 = restored.get_cell("u2", CoverageDimension.CORRECTNESS)
        assert u2.status == CoverageStatus.ASSIGNED

        # Continue: retry u1, complete u2
        restored.retry("u1", CoverageDimension.CORRECTNESS, "t1_retry")
        restored.record_finding("u1", CoverageDimension.CORRECTNESS, "f1")
        restored.record_finding("u2", CoverageDimension.CORRECTNESS, "f2")

        assert restored.get_cell("u1", CoverageDimension.CORRECTNESS).status == CoverageStatus.COVERED
        assert restored.get_cell("u2", CoverageDimension.CORRECTNESS).status == CoverageStatus.COVERED

    def test_resume_preserves_finding_ids(self):
        cs = _make_change_set([_make_unit("u1")])
        ledger = CoverageLedger.from_change_set(cs)
        ledger.assign("u1", CoverageDimension.CORRECTNESS, "t1")
        ledger.record_finding("u1", CoverageDimension.CORRECTNESS, "f_abc")

        restored = CoverageLedger.from_dict(ledger.to_dict())
        cell = restored.get_cell("u1", CoverageDimension.CORRECTNESS)
        assert "f_abc" in cell.finding_ids

    def test_resume_preserves_no_issue_evidence(self):
        cs = _make_change_set([_make_unit("u1")])
        ledger = CoverageLedger.from_change_set(cs)
        ledger.assign("u1", CoverageDimension.CORRECTNESS, "t1")
        ledger.close_no_issue("u1", CoverageDimension.CORRECTNESS, "Detailed review: no defect found.")

        restored = CoverageLedger.from_dict(ledger.to_dict())
        cell = restored.get_cell("u1", CoverageDimension.CORRECTNESS)
        assert cell.evidence == "Detailed review: no defect found."


# ── Helper functions ────────────────────────────────────────────────────────


class TestHelpers:
    def test_extract_risk_signals_strings(self):
        unit = {"risk_signals": ["security-sensitive", "cross-PR"]}
        assert _extract_risk_signals(unit) == ["security-sensitive", "cross-PR"]

    def test_extract_risk_signals_dicts(self):
        unit = {"risk_signals": [{"type": "security-sensitive"}, {"type": "error-handling"}]}
        assert _extract_risk_signals(unit) == ["security-sensitive", "error-handling"]

    def test_extract_risk_signals_mixed(self):
        unit = {"risk_signals": ["plain", {"type": "typed"}]}
        assert _extract_risk_signals(unit) == ["plain", "typed"]

    def test_extract_risk_signals_empty(self):
        assert _extract_risk_signals({}) == []
        assert _extract_risk_signals({"risk_signals": []}) == []

    def test_is_localization_path(self):
        assert _is_localization_path("src/i18n/messages.properties")
        assert _is_localization_path("src/locales/en.json")
        assert _is_localization_path("src/l10n/strings.arb")
        assert _is_localization_path("src/translations/fr.po")
        assert not _is_localization_path("src/app.py")
        assert not _is_localization_path("src/data.json")  # not in locale dir

    def test_has_cross_pr_signal(self):
        assert _has_cross_pr_signal({"risk_signals": ["cross-PR"]})
        assert _has_cross_pr_signal({"risk_signals": [{"type": "cross_PR"}]})
        assert _has_cross_pr_signal({"metadata": {"cross_pr": True}})
        assert not _has_cross_pr_signal({})
        assert not _has_cross_pr_signal({"risk_signals": ["security-sensitive"]})

    def test_coerce_int(self):
        assert _coerce_int(42) == 42
        assert _coerce_int("7") == 7
        assert _coerce_int(None) == 0
        assert _coerce_int("bad") == 0
        assert _coerce_int(3.7) == 3

    def test_coerce_float(self):
        assert _coerce_float(42) == 42.0
        assert _coerce_float(3.7) == 3.7
        assert _coerce_float("0.85") == 0.85
        assert _coerce_float(None) == 0.0
        assert _coerce_float("bad") == 0.0
        assert _coerce_float(0) == 0.0


# ── Edge cases ──────────────────────────────────────────────────────────────


class TestEdgeCases:
    def test_unit_with_no_optional_cap_skips_bounded(self):
        units = [_make_unit("u1")]
        cs = _make_change_set(units)
        ledger = CoverageLedger.from_change_set(cs, optional_cap=0)
        perf = ledger.get_cell("u1", CoverageDimension.PERFORMANCE)
        compat = ledger.get_cell("u1", CoverageDimension.COMPATIBILITY)
        assert perf is None
        assert compat is None

    def test_risk_value_preserved_on_cell(self):
        cs = _make_change_set([_make_unit("u1", risk=7)])
        ledger = CoverageLedger.from_change_set(cs)
        cell = ledger.get_cell("u1", CoverageDimension.CORRECTNESS)
        assert cell.risk == 7

    def test_path_and_line_preserved(self):
        cs = _make_change_set([_make_unit("u1", path="src/deep/nested/module.py", line=123)])
        ledger = CoverageLedger.from_change_set(cs)
        cell = ledger.get_cell("u1", CoverageDimension.CORRECTNESS)
        assert cell.path == "src/deep/nested/module.py"
        assert cell.line == 123

    def test_pending_cells_deterministic_order(self):
        """Same input → same order every time."""
        units = [_make_unit(f"u{i}", risk=5 - i) for i in range(5)]
        cs = _make_change_set(units)
        ledger = CoverageLedger.from_change_set(cs)

        order1 = [c.unit_id for c in ledger.pending_cells(CoverageDimension.CORRECTNESS)]
        order2 = [c.unit_id for c in ledger.pending_cells(CoverageDimension.CORRECTNESS)]
        assert order1 == order2

    def test_multiple_findings_on_one_cell(self):
        cs = _make_change_set([_make_unit("u1")])
        ledger = CoverageLedger.from_change_set(cs)
        ledger.assign("u1", CoverageDimension.CORRECTNESS, "t1")
        cell = ledger.get_cell("u1", CoverageDimension.CORRECTNESS)
        cell.add_finding("f1")
        cell.add_finding("f2")
        assert cell.finding_ids == ["f1", "f2"]

    def test_all_dimensions_can_be_created(self):
        """Ensure every dimension can be created via appropriate signals."""
        signal_map = {
            CoverageDimension.SECURITY: "security-sensitive-symbol",
            CoverageDimension.LOCALIZATION: "localization-resource",
            CoverageDimension.CROSS_PR: "cross-PR",
            CoverageDimension.ERROR_HANDLING: "error-handling",
            CoverageDimension.CONTRACT: "contract-surface",
            CoverageDimension.TESTING: "testing-scope",
        }
        for dim, signal in signal_map.items():
            cs = _make_change_set([_make_unit("u1", risk_signals=[signal])])
            ledger = CoverageLedger.from_change_set(cs)
            cell = ledger.get_cell("u1", dim)
            assert cell is not None, f"Missing cell for {dim.value} with signal {signal}"
            assert cell.mandatory is True


# ── SemanticUnit interoperability (risk_score / start_line) ────────────────


class TestSemanticUnitInterop:
    """Tests for interop with the actual SemanticUnit.to_dict() shape."""

    def test_risk_score_accepted_as_float(self):
        cs = _make_change_set([_make_unit("u1", risk_score=0.85)])
        ledger = CoverageLedger.from_change_set(cs)
        cell = ledger.get_cell("u1", CoverageDimension.CORRECTNESS)
        assert cell is not None
        assert cell.risk == pytest.approx(0.85)
        assert isinstance(cell.risk, float)

    def test_risk_score_priority_over_risk(self):
        """When both risk_score and risk are present, risk_score wins."""
        unit = _make_unit("u1", risk=5)
        unit["risk_score"] = 0.42
        cs = _make_change_set([unit])
        ledger = CoverageLedger.from_change_set(cs)
        cell = ledger.get_cell("u1", CoverageDimension.CORRECTNESS)
        assert cell.risk == pytest.approx(0.42)

    def test_risk_fallback_when_no_risk_score(self):
        """Legacy risk (int) is used when risk_score is absent."""
        cs = _make_change_set([_make_unit("u1", risk=7)])
        ledger = CoverageLedger.from_change_set(cs)
        cell = ledger.get_cell("u1", CoverageDimension.CORRECTNESS)
        assert cell.risk == pytest.approx(7.0)

    def test_start_line_accepted(self):
        cs = _make_change_set([_make_unit("u1", start_line=42)])
        ledger = CoverageLedger.from_change_set(cs)
        cell = ledger.get_cell("u1", CoverageDimension.CORRECTNESS)
        assert cell is not None
        assert cell.line == 42

    def test_start_line_priority_over_line(self):
        """When both start_line and line are present, start_line wins."""
        unit = _make_unit("u1", line=10)
        unit["start_line"] = 55
        cs = _make_change_set([unit])
        ledger = CoverageLedger.from_change_set(cs)
        cell = ledger.get_cell("u1", CoverageDimension.CORRECTNESS)
        assert cell.line == 55

    def test_line_fallback_when_no_start_line(self):
        cs = _make_change_set([_make_unit("u1", line=10)])
        ledger = CoverageLedger.from_change_set(cs)
        cell = ledger.get_cell("u1", CoverageDimension.CORRECTNESS)
        assert cell.line == 10

    def test_dict_risk_signals_with_type_key(self):
        """SemanticUnit.to_dict() always produces risk_signals as list[dict]."""
        cs = _make_change_set([_make_unit("u1", risk_signals=[{"type": "security-sensitive-symbol"}])])
        ledger = CoverageLedger.from_change_set(cs)
        sec = ledger.get_cell("u1", CoverageDimension.SECURITY)
        assert sec is not None
        assert sec.mandatory is True

    def test_risk_score_ordering_deterministic(self):
        """Float risk values produce deterministic ordering."""
        cs = _make_change_set(
            [
                _make_unit("u_low", risk_score=0.1),
                _make_unit("u_mid", risk_score=0.5),
                _make_unit("u_high", risk_score=0.9),
            ]
        )
        ledger = CoverageLedger.from_change_set(cs)
        pending = ledger.pending_cells(CoverageDimension.CORRECTNESS)
        risks = [c.risk for c in pending]
        assert risks == sorted(risks, reverse=True)
        assert [c.unit_id for c in pending] == ["u_high", "u_mid", "u_low"]

    def test_integration_fixture_matches_semantic_unit_shape(self):
        """The _make_semantic_unit fixture produces the exact SemanticUnit.to_dict() shape."""
        unit = _make_semantic_unit(
            "u1",
            path="src/auth.py",
            start_line=10,
            end_line=30,
            risk_score=0.75,
            risk_signals=[{"type": "security-sensitive-symbol"}],
        )
        cs = _make_change_set([unit])
        ledger = CoverageLedger.from_change_set(cs)

        cell = ledger.get_cell("u1", CoverageDimension.CORRECTNESS)
        assert cell is not None
        assert cell.risk == pytest.approx(0.75)
        assert cell.line == 10
        assert cell.path == "src/auth.py"

        sec = ledger.get_cell("u1", CoverageDimension.SECURITY)
        assert sec is not None
        assert sec.mandatory is True


# ── Empty unit ID rejection ────────────────────────────────────────────────


class TestEmptyUnitId:
    def test_empty_id_skipped(self):
        cs = _make_change_set([_make_unit(""), _make_unit("u1")])
        ledger = CoverageLedger.from_change_set(cs)
        # Only u1 should have cells; empty-id unit is skipped.
        assert ledger.get_cell("", CoverageDimension.CORRECTNESS) is None
        assert ledger.get_cell("u1", CoverageDimension.CORRECTNESS) is not None

    def test_whitespace_id_skipped(self):
        cs = _make_change_set([_make_unit("  "), _make_unit("u1")])
        ledger = CoverageLedger.from_change_set(cs)
        assert ledger.get_cell("  ", CoverageDimension.CORRECTNESS) is None
        assert ledger.get_cell("u1", CoverageDimension.CORRECTNESS) is not None

    def test_all_empty_ids_produce_empty_ledger(self):
        cs = _make_change_set([_make_unit(""), _make_unit("  ")])
        ledger = CoverageLedger.from_change_set(cs)
        assert ledger.cells == []
        assert ledger.is_complete()

    def test_empty_id_no_duplicate_keys(self):
        """Multiple empty-id units must not produce duplicate (unit_id, dim) keys."""
        cs = _make_change_set([_make_unit(""), _make_unit(""), _make_unit("u1")])
        ledger = CoverageLedger.from_change_set(cs)
        seen = set()
        for cell in ledger.cells:
            key = (cell.unit_id, cell.dimension)
            assert key not in seen
            seen.add(key)


# ── Abstain retry semantics ────────────────────────────────────────────────


class TestAbstainRetry:
    def test_abstained_is_retryable(self):
        cs = _make_change_set([_make_unit("u1")])
        ledger = CoverageLedger.from_change_set(cs)
        ledger.assign("u1", CoverageDimension.CORRECTNESS, "t1")
        ledger.abstain("u1", CoverageDimension.CORRECTNESS, "insufficient context")
        cell = ledger.get_cell("u1", CoverageDimension.CORRECTNESS)
        assert cell.status == CoverageStatus.ABSTAINED

        # Retry from abstained
        ledger.retry("u1", CoverageDimension.CORRECTNESS, "t2")
        cell = ledger.get_cell("u1", CoverageDimension.CORRECTNESS)
        assert cell.status == CoverageStatus.ASSIGNED
        assert "t2" in cell.assigned_task_ids

    def test_abstained_then_covered(self):
        """Full lifecycle: assign → abstain → retry → finding."""
        cs = _make_change_set([_make_unit("u1")])
        ledger = CoverageLedger.from_change_set(cs)
        ledger.assign("u1", CoverageDimension.CORRECTNESS, "t1")
        ledger.abstain("u1", CoverageDimension.CORRECTNESS, "no context")
        ledger.retry("u1", CoverageDimension.CORRECTNESS, "t2")
        ledger.record_finding("u1", CoverageDimension.CORRECTNESS, "f1")
        cell = ledger.get_cell("u1", CoverageDimension.CORRECTNESS)
        assert cell.status == CoverageStatus.COVERED

    def test_retry_on_covered_raises(self):
        cs = _make_change_set([_make_unit("u1")])
        ledger = CoverageLedger.from_change_set(cs)
        ledger.assign("u1", CoverageDimension.CORRECTNESS, "t1")
        ledger.record_finding("u1", CoverageDimension.CORRECTNESS, "f1")
        with pytest.raises(ValueError, match="must be failed or abstained"):
            ledger.retry("u1", CoverageDimension.CORRECTNESS, "t2")

    def test_retry_on_no_issue_raises(self):
        cs = _make_change_set([_make_unit("u1")])
        ledger = CoverageLedger.from_change_set(cs)
        ledger.assign("u1", CoverageDimension.CORRECTNESS, "t1")
        ledger.close_no_issue("u1", CoverageDimension.CORRECTNESS, "no defect")
        with pytest.raises(ValueError, match="must be failed or abstained"):
            ledger.retry("u1", CoverageDimension.CORRECTNESS, "t2")

    def test_retry_on_pending_raises(self):
        cs = _make_change_set([_make_unit("u1")])
        ledger = CoverageLedger.from_change_set(cs)
        with pytest.raises(ValueError, match="must be failed or abstained"):
            ledger.retry("u1", CoverageDimension.CORRECTNESS, "t1")

    def test_abstained_is_terminal(self):
        cs = _make_change_set([_make_unit("u1")])
        ledger = CoverageLedger.from_change_set(cs)
        ledger.assign("u1", CoverageDimension.CORRECTNESS, "t1")
        ledger.abstain("u1", CoverageDimension.CORRECTNESS, "reason")
        cell = ledger.get_cell("u1", CoverageDimension.CORRECTNESS)
        assert cell.is_terminal()


# ── Completion summary: terminal vs successfully_resolved ──────────────────


class TestCompletionSummarySemantics:
    def test_abstained_counts_as_resolved_but_not_success(self):
        cs = _make_change_set([_make_unit("u1")])
        ledger = CoverageLedger.from_change_set(cs)
        ledger.assign("u1", CoverageDimension.CORRECTNESS, "t1")
        ledger.abstain("u1", CoverageDimension.CORRECTNESS, "insufficient context")

        summary = ledger.completion_summary()
        assert summary["mandatory_resolved"] >= 1
        assert summary["mandatory_success"] == 0
        assert summary["mandatory_complete"] is True

    def test_failed_counts_as_resolved_but_not_success(self):
        cs = _make_change_set([_make_unit("u1")])
        ledger = CoverageLedger.from_change_set(cs)
        ledger.fail("u1", CoverageDimension.CORRECTNESS, "tool crash")

        summary = ledger.completion_summary()
        assert summary["mandatory_resolved"] >= 1
        assert summary["mandatory_success"] == 0

    def test_covered_counts_as_success(self):
        cs = _make_change_set([_make_unit("u1")])
        ledger = CoverageLedger.from_change_set(cs)
        ledger.assign("u1", CoverageDimension.CORRECTNESS, "t1")
        ledger.record_finding("u1", CoverageDimension.CORRECTNESS, "f1")

        summary = ledger.completion_summary()
        assert summary["mandatory_success"] >= 1
        assert summary["mandatory_resolved"] >= 1

    def test_no_issue_counts_as_success(self):
        cs = _make_change_set([_make_unit("u1")])
        ledger = CoverageLedger.from_change_set(cs)
        ledger.assign("u1", CoverageDimension.CORRECTNESS, "t1")
        ledger.close_no_issue("u1", CoverageDimension.CORRECTNESS, "no defect")

        summary = ledger.completion_summary()
        assert summary["mandatory_success"] >= 1

    def test_mixed_statuses_summary(self):
        """4 units with different resolutions — verify counts."""
        cs = _make_change_set([_make_unit(f"u{i}") for i in range(4)])
        ledger = CoverageLedger.from_change_set(cs, optional_cap=0)

        # u0: covered (success)
        ledger.assign("u0", CoverageDimension.CORRECTNESS, "t0")
        ledger.record_finding("u0", CoverageDimension.CORRECTNESS, "f0")
        # u1: no_issue (success)
        ledger.assign("u1", CoverageDimension.CORRECTNESS, "t1")
        ledger.close_no_issue("u1", CoverageDimension.CORRECTNESS, "no defect")
        # u2: abstained (terminal, not success)
        ledger.assign("u2", CoverageDimension.CORRECTNESS, "t2")
        ledger.abstain("u2", CoverageDimension.CORRECTNESS, "no context")
        # u3: failed (terminal, not success)
        ledger.fail("u3", CoverageDimension.CORRECTNESS, "crash")

        summary = ledger.completion_summary()
        assert summary["mandatory_resolved"] >= 4
        assert summary["mandatory_success"] >= 2  # u0, u1
        assert summary["mandatory_complete"] is True
        assert summary["complete"] is True
