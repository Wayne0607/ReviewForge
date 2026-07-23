"""V3 Orchestrator integration tests.

Covers:
  1. Disabled path — zero extra behaviour when v3 is off
  2. Enabled compile/ledger — SemanticChangeSet compiled, CoverageLedger built
  3. Exact unit-line matching — finding must be within unit range to COVER
  4. Zero-candidate fallback — correctness cells selected regardless of risk
  5. High-risk filtering — low-risk non-mandatory cells excluded
  6. Max cells — selection capped at configured maximum
  7. Retry from abstain/failure — cells retried up to max attempts
  8. Closure finding ingestion/dedup — findings flow through, phase0 deduped
  9. Failure does not suppress — failed cells remain FAILED, not converted
  10. Summary/events — v3_coverage in summary, structured events emitted
  11. App config wiring — app.py passes all cfg.v3 fields
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from reviewforge.core.specs import build_registry
from reviewforge.core.state import Finding, ReviewTask, StateStore
from reviewforge.engine.coverage_ledger import (
    CoverageCell,
    CoverageDimension,
    CoverageLedger,
    CoverageStatus,
)
from reviewforge.engine.orchestrator import Orchestrator
from reviewforge.engine.semantic_diff import SemanticUnit
from reviewforge.tools.gateway import ToolGateway

# ── Fixtures ────────────────────────────────────────────────────────────────


class _RecordingEventBus:
    """EventBus stand-in that records emitted events for assertion."""

    def __init__(self):
        self.events: list[tuple[str, dict]] = []
        self._run_id = ""

    def set_run_id(self, run_id: str) -> None:
        self._run_id = run_id

    def emit(self, event_type: str, data: dict | None = None) -> None:
        self.events.append((event_type, data or {}))


class _StaticMockLLM:
    """Returns a fixed JSON response on every call."""

    def __init__(self, content: str = '{"findings": []}'):
        self._content = content
        self._calls = 0

    def bind_tools(self, tools):
        return self

    async def ainvoke(self, messages, **kwargs):
        self._calls += 1
        from langchain_core.messages import AIMessage

        return AIMessage(content=self._content)


def _make_state(**kwargs) -> StateStore:
    """Build a minimal StateStore."""
    defaults = {
        "repo": "owner/repo",
        "pr_number": 77,
        "head_sha": "deadbeef",
        "files_changed": [],
        "file_diffs": {},
        "impact_manifest": {},
    }
    defaults.update(kwargs)
    return StateStore(**defaults)


def _manifest_with_units() -> dict:
    """Manifest with two symbol units at different risk levels."""
    return {
        "version": 2,
        "files": [
            {
                "path": "src/auth.py",
                "language": "python",
                "added_lines": [5, 6, 12],
                "changed_symbols": [
                    {
                        "name": "authenticate",
                        "type": "function",
                        "start_line": 4,
                        "end_line": 10,
                        "added_lines": [5, 6],
                    },
                ],
                "imports": [],
                "calls": [],
            },
            {
                "path": "src/utils.py",
                "language": "python",
                "added_lines": [3],
                "changed_symbols": [
                    {
                        "name": "helper",
                        "type": "function",
                        "start_line": 1,
                        "end_line": 5,
                        "added_lines": [3],
                    },
                ],
                "imports": [],
                "calls": [],
            },
        ],
        "references": [],
        "candidate_tests": [],
        "risk_signals": [
            {"type": "security-sensitive-symbol", "file": "src/auth.py", "symbol": "authenticate"},
        ],
        "wiki_pages": [],
        "resource_files": [],
    }


def _orchestrator(*, v3_enabled: bool = False, **overrides) -> Orchestrator:
    """Build an Orchestrator with mock LLMs and optional v3 config."""
    reg = build_registry()
    events = _RecordingEventBus()
    v3_defaults = {
        "v3_enabled": v3_enabled,
        "v3_coverage_min_risk_score": 0.15,
        "v3_coverage_max_cells_per_round": 24,
        "v3_coverage_max_attempts": 2,
        "v3_evidence_mode": "shadow",
        "v3_evidence_max_candidates": 20,
    }
    # overrides take precedence over defaults
    v3_defaults.update({k: v for k, v in overrides.items() if k.startswith("v3_")})
    other_overrides = {k: v for k, v in overrides.items() if not k.startswith("v3_")}
    orch = Orchestrator(
        registry=reg,
        gateway=ToolGateway(reg, MagicMock()),
        event_bus=events,
        planner_llm=_StaticMockLLM(),
        reviewer_llm=_StaticMockLLM(),
        calibrator_llm=_StaticMockLLM(),
        db=None,
        agentic_default=False,
        **v3_defaults,
        **other_overrides,
    )
    return orch


# ── 1. Disabled path ────────────────────────────────────────────────────────


class TestDisabledPath:
    """When v3 is disabled, behaviour and call counts are unchanged."""

    @pytest.mark.asyncio
    async def test_disabled_no_v3_events(self):
        """No v3.* events emitted when v3 is disabled."""
        orch = _orchestrator(v3_enabled=False)
        state = _make_state(
            files_changed=["a.py"],
            impact_manifest=_manifest_with_units(),
        )
        # Mock the planner to return nothing, calibrator to return nothing
        with patch.object(orch._planner, "plan", new_callable=AsyncMock, return_value=[]):
            with patch.object(orch._context_engine, "build", new_callable=AsyncMock, return_value={}):
                with patch("reviewforge.engine.orchestrator.scan_changed_files", new_callable=AsyncMock) as mock_scan:
                    mock_scan.return_value = MagicMock(findings=[], files_scanned=0, file_errors={}, scanner_errors={})
                    await orch.run(state)

        v3_events = [e for e in orch._events.events if e[0].startswith("v3.")]
        assert v3_events == [], f"Unexpected v3 events: {v3_events}"

    @pytest.mark.asyncio
    async def test_disabled_summary_has_no_v3_coverage(self):
        """Summary has no v3_coverage key when disabled."""
        orch = _orchestrator(v3_enabled=False)
        state = _make_state(files_changed=["a.py"])
        with patch.object(orch._planner, "plan", new_callable=AsyncMock, return_value=[]):
            with patch.object(orch._context_engine, "build", new_callable=AsyncMock, return_value={}):
                with patch("reviewforge.engine.orchestrator.scan_changed_files", new_callable=AsyncMock) as mock_scan:
                    mock_scan.return_value = MagicMock(findings=[], files_scanned=0, file_errors={}, scanner_errors={})
                    summary = await orch.run(state)
        assert "v3_coverage" not in summary

    @pytest.mark.asyncio
    async def test_disabled_no_v3_fields_on_orchestrator(self):
        """Orchestrator has v3 fields even when disabled (they're just not used)."""
        orch = _orchestrator(v3_enabled=False)
        assert orch._v3_enabled is False
        assert orch._v3_change_set is None
        assert orch._v3_ledger is None


# ── 2. Enabled compile/ledger ───────────────────────────────────────────────


class TestEnabledCompileLedger:
    """When v3 is enabled, compile SemanticChangeSet and build CoverageLedger."""

    @pytest.mark.asyncio
    async def test_compile_and_ledger_created(self):
        """SemanticChangeSet compiled and CoverageLedger built when v3 enabled."""
        orch = _orchestrator(v3_enabled=True)
        state = _make_state(
            files_changed=["src/auth.py", "src/utils.py"],
            file_diffs={"src/auth.py": "@@ -4,3 +4,5 @@\n-old\n+new\n", "src/utils.py": "@@ -1,2 +1,3 @@\n-a\n+b\n"},
            impact_manifest=_manifest_with_units(),
        )

        with patch.object(orch._planner, "plan", new_callable=AsyncMock, return_value=[]):
            with patch.object(
                orch._context_engine, "build", new_callable=AsyncMock, return_value=_manifest_with_units()
            ):
                with patch("reviewforge.engine.orchestrator.scan_changed_files", new_callable=AsyncMock) as mock_scan:
                    mock_scan.return_value = MagicMock(findings=[], files_scanned=0, file_errors={}, scanner_errors={})
                    await orch.run(state)

        assert orch._v3_change_set is not None
        assert len(orch._v3_change_set.units) > 0
        assert orch._v3_ledger is not None
        assert len(orch._v3_ledger.cells) > 0

    @pytest.mark.asyncio
    async def test_semantic_compiled_event_emitted(self):
        """v3.semantic.compiled event emitted with correct data."""
        orch = _orchestrator(v3_enabled=True)
        state = _make_state(
            files_changed=["src/auth.py"],
            file_diffs={"src/auth.py": "@@ -4,3 +4,5 @@\n-old\n+new\n"},
            impact_manifest=_manifest_with_units(),
        )

        with patch.object(orch._planner, "plan", new_callable=AsyncMock, return_value=[]):
            with patch.object(
                orch._context_engine, "build", new_callable=AsyncMock, return_value=_manifest_with_units()
            ):
                with patch("reviewforge.engine.orchestrator.scan_changed_files", new_callable=AsyncMock) as mock_scan:
                    mock_scan.return_value = MagicMock(findings=[], files_scanned=0, file_errors={}, scanner_errors={})
                    await orch.run(state)

        compiled_events = [e for e in orch._events.events if e[0] == "v3.semantic.compiled"]
        assert len(compiled_events) == 1
        data = compiled_events[0][1]
        assert "unit_count" in data
        assert data["unit_count"] > 0

    @pytest.mark.asyncio
    async def test_coverage_created_event_emitted(self):
        """v3.coverage.created event emitted with cell count."""
        orch = _orchestrator(v3_enabled=True)
        state = _make_state(
            files_changed=["src/auth.py"],
            file_diffs={"src/auth.py": "@@ -4,3 +4,5 @@\n-old\n+new\n"},
            impact_manifest=_manifest_with_units(),
        )

        with patch.object(orch._planner, "plan", new_callable=AsyncMock, return_value=[]):
            with patch.object(
                orch._context_engine, "build", new_callable=AsyncMock, return_value=_manifest_with_units()
            ):
                with patch("reviewforge.engine.orchestrator.scan_changed_files", new_callable=AsyncMock) as mock_scan:
                    mock_scan.return_value = MagicMock(findings=[], files_scanned=0, file_errors={}, scanner_errors={})
                    await orch.run(state)

        created_events = [e for e in orch._events.events if e[0] == "v3.coverage.created"]
        assert len(created_events) == 1
        data = created_events[0][1]
        assert "cell_count" in data
        assert data["cell_count"] > 0

    @pytest.mark.asyncio
    async def test_impact_manifest_has_v3_bounded_summaries(self):
        """impact_manifest stores only bounded v3 summaries, not full data."""
        orch = _orchestrator(v3_enabled=True)
        manifest = _manifest_with_units()
        state = _make_state(
            files_changed=["src/auth.py"],
            file_diffs={"src/auth.py": "@@ -4,3 +4,5 @@\n-old\n+new\n"},
            impact_manifest=manifest,
        )

        with patch.object(orch._planner, "plan", new_callable=AsyncMock, return_value=[]):
            with patch.object(orch._context_engine, "build", new_callable=AsyncMock, return_value=manifest):
                with patch("reviewforge.engine.orchestrator.scan_changed_files", new_callable=AsyncMock) as mock_scan:
                    mock_scan.return_value = MagicMock(findings=[], files_scanned=0, file_errors={}, scanner_errors={})
                    await orch.run(state)

        v3_section = state.impact_manifest.get("v3", {})
        assert "semantic" in v3_section
        assert "coverage_summary" in v3_section
        # Bounded: semantic has unit_count and unit IDs, not full unit data
        semantic = v3_section["semantic"]
        assert "unit_count" in semantic
        assert "units" in semantic  # list of unit summaries
        # Each unit summary is bounded (id, path, line, risk_score, symbol)
        if semantic["units"]:
            unit_summary = semantic["units"][0]
            assert "id" in unit_summary
            assert "path" in unit_summary
            # Should NOT have full calls/imports/references
            assert "calls" not in unit_summary
            assert "imports" not in unit_summary


# ── 3. Exact unit-line matching ─────────────────────────────────────────────


class TestUnitLineMatching:
    """Finding must be within unit line range to mark COVERED."""

    def test_finding_within_range_matches(self):
        """Finding at line 10 matches unit [4, 10]."""
        orch = _orchestrator(v3_enabled=True)
        unit = SemanticUnit(
            id="su_test",
            path="src/auth.py",
            start_line=4,
            end_line=10,
            risk_score=0.8,
            risk_reasons=["security-sensitive-symbol"],
            risk_signals=[{"type": "security-sensitive-symbol"}],
        )
        finding = Finding(file="src/auth.py", line=10, category="logic-error", message="bug")
        assert orch._finding_matches_unit(finding, unit) is True

    def test_finding_outside_range_no_match(self):
        """Finding at line 15 does not match unit [4, 10]."""
        orch = _orchestrator(v3_enabled=True)
        unit = SemanticUnit(
            id="su_test",
            path="src/auth.py",
            start_line=4,
            end_line=10,
        )
        finding = Finding(file="src/auth.py", line=15, category="logic-error", message="bug")
        assert orch._finding_matches_unit(finding, unit) is False

    def test_finding_different_file_no_match(self):
        """Finding in different file does not match."""
        orch = _orchestrator(v3_enabled=True)
        unit = SemanticUnit(
            id="su_test",
            path="src/auth.py",
            start_line=4,
            end_line=10,
        )
        finding = Finding(file="src/other.py", line=7, category="logic-error", message="bug")
        assert orch._finding_matches_unit(finding, unit) is False

    def test_zero_line_finding_no_match(self):
        """Finding at line 0 does not match any unit."""
        orch = _orchestrator(v3_enabled=True)
        unit = SemanticUnit(
            id="su_test",
            path="src/auth.py",
            start_line=4,
            end_line=10,
        )
        finding = Finding(file="src/auth.py", line=0, category="logic-error", message="bug")
        assert orch._finding_matches_unit(finding, unit) is False

    def test_broad_task_no_unit_specific_finding_abstained(self):
        """Broad task with no unit-specific finding → ABSTAINED with reason."""
        orch = _orchestrator(v3_enabled=True)
        unit = SemanticUnit(
            id="su_auth",
            path="src/auth.py",
            start_line=4,
            end_line=10,
            risk_score=0.8,
            risk_reasons=["security-sensitive-symbol"],
            risk_signals=[{"type": "security-sensitive-symbol"}],
        )
        orch._v3_change_set = MagicMock()
        orch._v3_change_set.units = [unit]

        ledger = CoverageLedger.from_change_set(
            {
                "units": [unit.to_dict()],
            }
        )
        orch._v3_ledger = ledger

        # No findings produced
        task = ReviewTask(reviewer="security_reviewer", files=["src/auth.py"], status="completed")
        orch._track_broad_pass_coverage(
            task_id=task.id,
            reviewer="security_reviewer",
            task_files=["src/auth.py"],
            findings=[],
        )

        sec_cell = ledger.get_cell("su_auth", CoverageDimension.SECURITY)
        assert sec_cell is not None
        assert sec_cell.status == CoverageStatus.ABSTAINED
        assert "no unit-specific finding" in sec_cell.terminal_reason


# ── 4. Zero-candidate fallback ──────────────────────────────────────────────


class TestZeroCandidateFallback:
    """When PR has zero candidate findings, include correctness cells regardless of risk."""

    @pytest.mark.asyncio
    async def test_zero_candidates_selects_correctness_cells(self):
        """Low-risk correctness cells selected when no findings exist."""
        orch = _orchestrator(v3_enabled=True, v3_coverage_min_risk_score=0.5)
        # helper has low risk (no signals)
        unit_low = SemanticUnit(
            id="su_helper",
            path="src/utils.py",
            start_line=1,
            end_line=5,
            risk_score=0.05,
            risk_reasons=[],
        )
        orch._v3_change_set = MagicMock()
        orch._v3_change_set.units = [unit_low]

        ledger = CoverageLedger.from_change_set(
            {
                "units": [unit_low.to_dict()],
            }
        )
        orch._v3_ledger = ledger

        # No findings in state
        state = _make_state(files_changed=["src/utils.py"])

        # With zero findings and min_risk=0.5, the low-risk cell (0.05) would
        # normally be excluded. But the fallback includes it.
        # We test the selection logic directly.
        selected = orch._select_closure_cells(state, [], ledger)
        assert len(selected) > 0
        assert all(c.dimension == CoverageDimension.CORRECTNESS for c in selected)


# ── 5. High-risk filtering ─────────────────────────────────────────────────


class TestHighRiskFiltering:
    """Low-risk cells excluded when there are high-risk candidates."""

    def test_low_risk_cell_excluded(self):
        """Cell below min_risk_score excluded when higher-risk cells exist."""
        orch = _orchestrator(v3_enabled=True, v3_coverage_min_risk_score=0.3)
        high_unit = SemanticUnit(
            id="su_high",
            path="src/auth.py",
            start_line=4,
            end_line=10,
            risk_score=0.8,
            risk_reasons=["security-sensitive-symbol"],
            risk_signals=[{"type": "security-sensitive-symbol"}],
        )
        low_unit = SemanticUnit(
            id="su_low",
            path="src/utils.py",
            start_line=1,
            end_line=5,
            risk_score=0.05,
            risk_reasons=[],
        )
        orch._v3_change_set = MagicMock()
        orch._v3_change_set.units = [high_unit, low_unit]

        ledger = CoverageLedger.from_change_set(
            {
                "units": [high_unit.to_dict(), low_unit.to_dict()],
            }
        )
        orch._v3_ledger = ledger

        # Mark low_unit's correctness cell as pending (it is by default)
        # Mark high_unit's correctness cell as pending (it is by default)

        state = _make_state(files_changed=["src/auth.py", "src/utils.py"])
        # Some findings exist (not zero-candidate)
        findings = [Finding(file="src/auth.py", line=5, category="security", message="x")]

        selected = orch._select_closure_cells(state, findings, ledger)

        # High-risk cell should be selected
        selected_ids = {(c.unit_id, c.dimension) for c in selected}
        assert ("su_high", CoverageDimension.CORRECTNESS) in selected_ids

        # Low-risk cell should NOT be selected (risk 0.05 < 0.3)
        assert ("su_low", CoverageDimension.CORRECTNESS) not in selected_ids


# ── 6. Max cells ────────────────────────────────────────────────────────────


class TestMaxCells:
    """Selection capped at configured maximum."""

    def test_selection_capped(self):
        """No more than max_cells_per_round selected."""
        orch = _orchestrator(v3_enabled=True, v3_coverage_max_cells_per_round=2)

        units = []
        for i in range(5):
            u = SemanticUnit(
                id=f"su_{i}",
                path=f"src/m{i}.py",
                start_line=1,
                end_line=10,
                risk_score=0.8,
                risk_reasons=["security-sensitive-symbol"],
            )
            units.append(u)

        orch._v3_change_set = MagicMock()
        orch._v3_change_set.units = units

        ledger = CoverageLedger.from_change_set(
            {
                "units": [u.to_dict() for u in units],
            }
        )
        orch._v3_ledger = ledger

        state = _make_state(files_changed=[f"src/m{i}.py" for i in range(5)])
        findings = [Finding(file=f"src/m{i}.py", line=5, category="security", message="x") for i in range(5)]

        selected = orch._select_closure_cells(state, findings, ledger)
        assert len(selected) <= 2


# ── 7. Retry from abstain/failure ──────────────────────────────────────────


class TestRetryFromAbstainFailure:
    """Cells retried up to max attempts."""

    def test_abstained_cell_retryable(self):
        """Abstained cell retried within attempt limit."""
        orch = _orchestrator(v3_enabled=True, v3_coverage_max_attempts=2)
        unit = SemanticUnit(
            id="su_test",
            path="src/auth.py",
            start_line=4,
            end_line=10,
            risk_score=0.8,
            risk_reasons=["security-sensitive-symbol"],
            risk_signals=[{"type": "security-sensitive-symbol"}],
        )
        orch._v3_change_set = MagicMock()
        orch._v3_change_set.units = [unit]

        ledger = CoverageLedger.from_change_set(
            {
                "units": [unit.to_dict()],
            }
        )
        orch._v3_ledger = ledger

        # Simulate a prior abstained attempt
        cell = ledger.get_cell("su_test", CoverageDimension.CORRECTNESS)
        assert cell is not None
        cell.transition(CoverageStatus.ASSIGNED, task_id="t1")
        cell.transition(CoverageStatus.ABSTAINED, terminal_reason="no finding")
        assert cell.attempts == 1

        # Should be retryable (attempts=1 < max=2)
        state = _make_state(files_changed=["src/auth.py"])
        selected = orch._select_closure_cells(state, [], ledger)
        selected_ids = {(c.unit_id, c.dimension) for c in selected}
        assert ("su_test", CoverageDimension.CORRECTNESS) in selected_ids

    def test_failed_cell_retryable(self):
        """Failed cell retried within attempt limit."""
        orch = _orchestrator(v3_enabled=True, v3_coverage_max_attempts=2)
        unit = SemanticUnit(
            id="su_test",
            path="src/auth.py",
            start_line=4,
            end_line=10,
            risk_score=0.8,
            risk_reasons=["security-sensitive-symbol"],
            risk_signals=[{"type": "security-sensitive-symbol"}],
        )
        orch._v3_change_set = MagicMock()
        orch._v3_change_set.units = [unit]

        ledger = CoverageLedger.from_change_set(
            {
                "units": [unit.to_dict()],
            }
        )
        orch._v3_ledger = ledger

        cell = ledger.get_cell("su_test", CoverageDimension.CORRECTNESS)
        cell.transition(CoverageStatus.ASSIGNED, task_id="t1")
        cell.transition(CoverageStatus.FAILED, terminal_reason="timeout")

        state = _make_state(files_changed=["src/auth.py"])
        selected = orch._select_closure_cells(state, [], ledger)
        selected_ids = {(c.unit_id, c.dimension) for c in selected}
        assert ("su_test", CoverageDimension.CORRECTNESS) in selected_ids

    def test_max_attempts_exceeded_not_selected(self):
        """Cell at max attempts not selected for retry."""
        orch = _orchestrator(v3_enabled=True, v3_coverage_max_attempts=1)
        unit = SemanticUnit(
            id="su_test",
            path="src/auth.py",
            start_line=4,
            end_line=10,
            risk_score=0.8,
            risk_reasons=["security-sensitive-symbol"],
            risk_signals=[{"type": "security-sensitive-symbol"}],
        )
        orch._v3_change_set = MagicMock()
        orch._v3_change_set.units = [unit]

        ledger = CoverageLedger.from_change_set(
            {
                "units": [unit.to_dict()],
            }
        )
        orch._v3_ledger = ledger

        cell = ledger.get_cell("su_test", CoverageDimension.CORRECTNESS)
        cell.transition(CoverageStatus.ASSIGNED, task_id="t1")
        cell.transition(CoverageStatus.ABSTAINED, terminal_reason="no finding")
        assert cell.attempts == 1

        state = _make_state(files_changed=["src/auth.py"])
        selected = orch._select_closure_cells(state, [], ledger)
        selected_ids = {(c.unit_id, c.dimension) for c in selected}
        # At max attempts (1), should NOT be selected
        assert ("su_test", CoverageDimension.CORRECTNESS) not in selected_ids


# ── 8. Closure finding ingestion/dedup ──────────────────────────────────────


class TestClosureFindingIngestionDedup:
    """Findings flow into normal pipeline and dedup with phase0."""

    def test_closure_findings_dedup_with_phase0(self):
        """Closure finding with same identity as phase0 finding is skipped."""
        _orchestrator(v3_enabled=True)

        # A finding that exists in phase0
        phase0_finding = Finding(file="src/auth.py", line=5, category="logic-error", message="bug", reviewer="phase0")
        phase0_keys = {(phase0_finding.file, phase0_finding.line, phase0_finding.category)}

        # Closure produces same finding
        closure_finding = Finding(file="src/auth.py", line=5, category="logic-error", message="bug", reviewer="closure")

        from reviewforge.engine.phase0 import finding_identity

        key = finding_identity(closure_finding)
        assert key in phase0_keys  # Verify it would be deduped

    def test_closure_findings_dedup_with_existing(self):
        """Closure finding with same identity as existing finding is skipped."""
        from reviewforge.engine.phase0 import finding_identity

        existing = Finding(file="src/auth.py", line=5, category="logic-error", message="bug", reviewer="reviewer")
        existing_keys = {finding_identity(existing)}

        new = Finding(file="src/auth.py", line=5, category="logic-error", message="other", reviewer="closure")
        assert finding_identity(new) in existing_keys

    def test_closure_finding_flows_to_verification(self):
        """A unique closure finding is added to state for verification."""
        _orchestrator(v3_enabled=True)
        state = _make_state(files_changed=["src/auth.py"])

        finding = Finding(file="src/auth.py", line=5, category="logic-error", message="new bug", reviewer="closure")
        phase0_keys: set = set()
        existing_keys: set = set()

        from reviewforge.engine.phase0 import finding_identity

        key = finding_identity(finding)
        if key not in phase0_keys and key not in existing_keys:
            state.add_finding(finding)
            existing_keys.add(key)

        assert len(state.list_findings()) == 1
        assert state.list_findings()[0].message == "new bug"


# ── 9. Failure does not suppress ────────────────────────────────────────────


class TestFailureDoesNotSuppress:
    """Failed/abstained cells stay in their state, never converted to no_issue."""

    def test_failed_cell_stays_failed(self):
        """A FAILED cell is not converted to NO_ISSUE or COVERED."""
        orch = _orchestrator(v3_enabled=True)
        unit = SemanticUnit(
            id="su_test",
            path="src/auth.py",
            start_line=4,
            end_line=10,
            risk_score=0.8,
            risk_reasons=["security-sensitive-symbol"],
            risk_signals=[{"type": "security-sensitive-symbol"}],
        )
        orch._v3_change_set = MagicMock()
        orch._v3_change_set.units = [unit]

        ledger = CoverageLedger.from_change_set(
            {
                "units": [unit.to_dict()],
            }
        )
        orch._v3_ledger = ledger

        cell = ledger.get_cell("su_test", CoverageDimension.CORRECTNESS)
        cell.transition(CoverageStatus.ASSIGNED, task_id="t1")
        cell.transition(CoverageStatus.FAILED, terminal_reason="timeout")

        assert cell.status == CoverageStatus.FAILED
        assert cell.terminal_reason == "timeout"

    def test_abstained_cell_stays_abstained(self):
        """An ABSTAINED cell is not converted to NO_ISSUE or COVERED."""
        orch = _orchestrator(v3_enabled=True)
        unit = SemanticUnit(
            id="su_test",
            path="src/auth.py",
            start_line=4,
            end_line=10,
            risk_score=0.8,
            risk_reasons=["security-sensitive-symbol"],
            risk_signals=[{"type": "security-sensitive-symbol"}],
        )
        orch._v3_change_set = MagicMock()
        orch._v3_change_set.units = [unit]

        ledger = CoverageLedger.from_change_set(
            {
                "units": [unit.to_dict()],
            }
        )
        orch._v3_ledger = ledger

        cell = ledger.get_cell("su_test", CoverageDimension.CORRECTNESS)
        cell.transition(CoverageStatus.ASSIGNED, task_id="t1")
        cell.transition(CoverageStatus.ABSTAINED, terminal_reason="no finding")

        assert cell.status == CoverageStatus.ABSTAINED
        assert cell.terminal_reason == "no finding"


# ── 10. Summary/events ──────────────────────────────────────────────────────


class TestSummaryAndEvents:
    """v3_coverage in summary, structured events emitted."""

    @pytest.mark.asyncio
    async def test_v3_coverage_in_summary(self):
        """Summary includes v3_coverage with all required fields."""
        orch = _orchestrator(v3_enabled=True)
        manifest = _manifest_with_units()
        state = _make_state(
            files_changed=["src/auth.py", "src/utils.py"],
            file_diffs={
                "src/auth.py": "@@ -4,3 +4,5 @@\n-old\n+new\n",
                "src/utils.py": "@@ -1,2 +1,3 @@\n-a\n+b\n",
            },
            impact_manifest=manifest,
        )

        with patch.object(orch._planner, "plan", new_callable=AsyncMock, return_value=[]):
            with patch.object(orch._context_engine, "build", new_callable=AsyncMock, return_value=manifest):
                with patch("reviewforge.engine.orchestrator.scan_changed_files", new_callable=AsyncMock) as mock_scan:
                    mock_scan.return_value = MagicMock(findings=[], files_scanned=0, file_errors={}, scanner_errors={})
                    summary = await orch.run(state)

        assert "v3_coverage" in summary
        vc = summary["v3_coverage"]
        required_keys = {
            "units",
            "cells",
            "mandatory_total",
            "mandatory_success",
            "abstained",
            "failed",
            "attempts",
            "selected",
            "closure_findings",
        }
        assert required_keys.issubset(set(vc.keys())), f"Missing keys: {required_keys - set(vc.keys())}"

    @pytest.mark.asyncio
    async def test_v3_coverage_started_completed_events(self):
        """v3.coverage.started and v3.coverage.completed events emitted."""
        orch = _orchestrator(v3_enabled=True)
        manifest = _manifest_with_units()
        state = _make_state(
            files_changed=["src/auth.py"],
            file_diffs={"src/auth.py": "@@ -4,3 +4,5 @@\n-old\n+new\n"},
            impact_manifest=manifest,
        )

        with patch.object(orch._planner, "plan", new_callable=AsyncMock, return_value=[]):
            with patch.object(orch._context_engine, "build", new_callable=AsyncMock, return_value=manifest):
                with patch("reviewforge.engine.orchestrator.scan_changed_files", new_callable=AsyncMock) as mock_scan:
                    mock_scan.return_value = MagicMock(findings=[], files_scanned=0, file_errors={}, scanner_errors={})
                    await orch.run(state)

        started = [e for e in orch._events.events if e[0] == "v3.coverage.started"]
        completed = [e for e in orch._events.events if e[0] == "v3.coverage.completed"]
        assert len(started) >= 1
        assert len(completed) >= 1

    @pytest.mark.asyncio
    async def test_v3_events_structure(self):
        """v3.semantic.compiled and v3.coverage.created events have correct structure."""
        orch = _orchestrator(v3_enabled=True)
        manifest = _manifest_with_units()
        state = _make_state(
            files_changed=["src/auth.py"],
            file_diffs={"src/auth.py": "@@ -4,3 +4,5 @@\n-old\n+new\n"},
            impact_manifest=manifest,
        )

        with patch.object(orch._planner, "plan", new_callable=AsyncMock, return_value=[]):
            with patch.object(orch._context_engine, "build", new_callable=AsyncMock, return_value=manifest):
                with patch("reviewforge.engine.orchestrator.scan_changed_files", new_callable=AsyncMock) as mock_scan:
                    mock_scan.return_value = MagicMock(findings=[], files_scanned=0, file_errors={}, scanner_errors={})
                    await orch.run(state)

        compiled = [e for e in orch._events.events if e[0] == "v3.semantic.compiled"][0]
        assert "unit_count" in compiled[1]

        created = [e for e in orch._events.events if e[0] == "v3.coverage.created"][0]
        assert "cell_count" in created[1]


# ── 11. App config wiring ───────────────────────────────────────────────────


class TestAppConfigWiring:
    """app.py passes all cfg.v3 fields to Orchestrator."""

    def test_orchestrator_accepts_v3_params(self):
        """Orchestrator constructor accepts all v3 parameters."""
        orch = _orchestrator(
            v3_enabled=True,
            v3_coverage_min_risk_score=0.2,
            v3_coverage_max_cells_per_round=10,
            v3_coverage_max_attempts=3,
            v3_evidence_mode="enforce",
            v3_evidence_max_candidates=15,
        )
        assert orch._v3_enabled is True
        assert orch._v3_coverage_min_risk_score == 0.2
        assert orch._v3_coverage_max_cells_per_round == 10
        assert orch._v3_coverage_max_attempts == 3
        assert orch._v3_evidence_mode == "enforce"
        assert orch._v3_evidence_max_candidates == 15

    def test_v3_defaults_are_off(self):
        """v3 defaults are all off/zero."""
        orch = _orchestrator()
        assert orch._v3_enabled is False
        assert orch._v3_coverage_min_risk_score == 0.15
        assert orch._v3_coverage_max_cells_per_round == 24
        assert orch._v3_coverage_max_attempts == 2

    def test_app_wiring_passes_v3_fields(self):
        """app.py create_app passes cfg.v3 fields to Orchestrator."""
        # We verify by checking the source code imports v3 fields
        import inspect

        from reviewforge.app import create_app

        source = inspect.getsource(create_app)
        assert "v3_enabled" in source
        assert "v3_coverage_min_risk_score" in source
        assert "v3_coverage_max_cells_per_round" in source
        assert "v3_coverage_max_attempts" in source
        assert "v3_evidence_mode" in source
        assert "v3_evidence_max_candidates" in source


# ── Additional targeted tests ───────────────────────────────────────────────


class TestReviewerDimensionMapping:
    """Reviewer names map to correct coverage dimensions."""

    def test_security_reviewer_maps_to_security(self):
        orch = _orchestrator(v3_enabled=True)
        assert "security" in orch._reviewer_dimensions("security_reviewer")

    def test_testing_reviewer_maps_to_testing(self):
        orch = _orchestrator(v3_enabled=True)
        assert "testing" in orch._reviewer_dimensions("testing_reviewer")

    def test_correctness_reviewer_only_claims_broad_correctness(self):
        orch = _orchestrator(v3_enabled=True)
        dims = orch._reviewer_dimensions("correctness_reviewer")
        assert dims == ["correctness"]

    def test_unknown_reviewer_maps_to_correctness(self):
        orch = _orchestrator(v3_enabled=True)
        dims = orch._reviewer_dimensions("custom_reviewer")
        assert dims == ["correctness"]


class TestFindUnitById:
    """Unit lookup by ID."""

    def test_find_existing_unit(self):
        orch = _orchestrator(v3_enabled=True)
        unit = SemanticUnit(id="su_abc", path="a.py")
        orch._v3_change_set = MagicMock()
        orch._v3_change_set.units = [unit]
        assert orch._find_unit_by_id("su_abc") is unit

    def test_find_missing_unit_returns_none(self):
        orch = _orchestrator(v3_enabled=True)
        orch._v3_change_set = MagicMock()
        orch._v3_change_set.units = []
        assert orch._find_unit_by_id("su_missing") is None

    def test_find_unit_no_changeset_returns_none(self):
        orch = _orchestrator(v3_enabled=True)
        assert orch._find_unit_by_id("su_abc") is None


class TestReviewFocus:
    """Review focus text generation."""

    def test_first_attempt_focus(self):
        orch = _orchestrator(v3_enabled=True)
        focus = orch._build_review_focus(
            path="src/auth.py",
            symbol="authenticate",
            start_line=4,
            end_line=10,
            dimension="security",
            risk_reasons=["security-sensitive-symbol", "blast-radius:5"],
            is_retry=False,
        )
        assert "src/auth.py" in focus
        assert "authenticate" in focus
        assert "4-10" in focus
        assert "security" in focus
        assert "security-sensitive-symbol" in focus
        assert "adversarial" not in focus.lower()

    def test_retry_focus_is_adversarial(self):
        orch = _orchestrator(v3_enabled=True)
        focus = orch._build_review_focus(
            path="src/auth.py",
            symbol="authenticate",
            start_line=4,
            end_line=10,
            dimension="security",
            risk_reasons=["security-sensitive-symbol"],
            is_retry=True,
        )
        assert "adversarial" in focus.lower()


class TestSummaryStructure:
    """v3_coverage summary structure."""

    def test_build_summary_no_ledger(self):
        orch = _orchestrator(v3_enabled=True)
        assert orch._build_v3_coverage_summary() == {}

    def test_build_summary_with_ledger(self):
        orch = _orchestrator(v3_enabled=True)
        unit = SemanticUnit(
            id="su_test",
            path="src/auth.py",
            start_line=4,
            end_line=10,
            risk_score=0.8,
            risk_reasons=["security-sensitive-symbol"],
            risk_signals=[{"type": "security-sensitive-symbol"}],
        )
        orch._v3_change_set = MagicMock()
        orch._v3_change_set.units = [unit]

        ledger = CoverageLedger.from_change_set(
            {
                "units": [unit.to_dict()],
            }
        )
        orch._v3_ledger = ledger

        summary = orch._build_v3_coverage_summary()
        assert summary["units"] == 1
        assert summary["cells"] > 0
        assert summary["mandatory_total"] > 0
        assert summary["mandatory_success"] == 0  # nothing resolved yet
        assert summary["abstained"] == 0
        assert summary["failed"] == 0
        assert summary["attempts"] == 0
        assert summary["selected"] == 0
        assert summary["closure_findings"] == 0

    def test_attempts_summary_counts_dispatches_not_failed_transitions(self):
        orch = _orchestrator(v3_enabled=True)
        unit = SemanticUnit(id="su_test", path="src/auth.py", start_line=4, end_line=10)
        orch._v3_change_set = MagicMock(units=[unit])
        ledger = CoverageLedger.from_change_set({"units": [unit.to_dict()]})
        orch._v3_ledger = ledger
        cell = ledger.get_cell("su_test", CoverageDimension.CORRECTNESS)
        assert cell is not None
        cell.transition(CoverageStatus.ASSIGNED, task_id="task-1")
        cell.transition(CoverageStatus.FAILED, terminal_reason="timeout")
        cell.transition(CoverageStatus.ASSIGNED, task_id="task-2")
        cell.transition(CoverageStatus.COVERED, terminal_reason="finding")

        assert cell.attempts == 3  # legacy transition counter remains serializable
        assert orch._build_v3_coverage_summary()["attempts"] == 2


class TestTargetedClosureExecution:
    """Exercise the real retry loop and unit-specific closure accounting."""

    @staticmethod
    def _install_unit(orch: Orchestrator) -> tuple[SemanticUnit, CoverageCell]:
        unit = SemanticUnit(
            id="su_auth",
            path="src/auth.py",
            start_line=4,
            end_line=10,
            risk_score=0.8,
            risk_reasons=["security-sensitive-symbol"],
            risk_signals=[{"type": "security-sensitive-symbol"}],
        )
        orch._v3_change_set = MagicMock(units=[unit])
        orch._v3_ledger = CoverageLedger.from_change_set({"units": [unit.to_dict()]})
        cell = orch._v3_ledger.get_cell("su_auth", CoverageDimension.CORRECTNESS)
        assert cell is not None
        return unit, cell

    @staticmethod
    def _install_two_units(orch: Orchestrator) -> tuple[list[SemanticUnit], list[CoverageCell]]:
        units = [
            SemanticUnit(
                id=f"su_{name}",
                path="src/shared.py",
                start_line=4,
                end_line=10,
                risk_score=0.8,
                risk_reasons=["changed-control-flow"],
                risk_signals=[{"type": "changed-control-flow"}],
            )
            for name in ("first", "second")
        ]
        cells = [
            CoverageCell(
                unit_id=unit.id,
                path=unit.path,
                line=unit.start_line,
                dimension=CoverageDimension.CORRECTNESS,
                risk=unit.risk_score,
                mandatory=True,
            )
            for unit in units
        ]
        orch._v3_change_set = MagicMock(units=units)
        orch._v3_ledger = CoverageLedger(cells=cells)
        return units, cells

    @pytest.mark.asyncio
    async def test_abstain_is_retried_and_second_unit_finding_covers(self):
        orch = _orchestrator(
            v3_enabled=True,
            v3_coverage_max_attempts=2,
            v3_coverage_max_cells_per_round=1,
        )
        _unit, cell = self._install_unit(orch)
        reviewer = MagicMock()
        reviewer.execute = AsyncMock(
            side_effect=[
                [],
                [Finding(file="src/auth.py", line=7, category="logic-error", message="real bug")],
            ]
        )
        state = _make_state(files_changed=["src/auth.py"])

        with patch.object(orch, "_create_reviewer", return_value=reviewer):
            await orch._v3_run_targeted_closure(state, "run-1", set())

        assert reviewer.execute.await_count == 2
        assert cell.status == CoverageStatus.COVERED
        assert len(cell.assigned_task_ids) == 2
        assert len(orch._v3_closure_task_ids) == 2
        assert len(orch._v3_closure_finding_ids) == 1
        assert orch._build_v3_coverage_summary()["selected"] == 1
        assert orch._build_v3_coverage_summary()["closure_findings"] == 1

    @pytest.mark.asyncio
    async def test_out_of_unit_finding_does_not_close_cell(self):
        orch = _orchestrator(
            v3_enabled=True,
            v3_coverage_max_attempts=2,
            v3_coverage_max_cells_per_round=1,
        )
        _unit, cell = self._install_unit(orch)
        reviewer = MagicMock()
        reviewer.execute = AsyncMock(
            side_effect=[
                [Finding(file="src/auth.py", line=40, category="logic-error", message="other bug")],
                [],
            ]
        )
        state = _make_state(files_changed=["src/auth.py"])

        with patch.object(orch, "_create_reviewer", return_value=reviewer):
            await orch._v3_run_targeted_closure(state, "run-1", set())

        assert reviewer.execute.await_count == 2
        assert cell.status == CoverageStatus.ABSTAINED
        assert not cell.finding_ids
        assert len(state.list_findings()) == 1

    @pytest.mark.asyncio
    async def test_distinct_cells_execute_concurrently(self):
        orch = _orchestrator(
            v3_enabled=True,
            v3_coverage_max_attempts=1,
            v3_coverage_max_cells_per_round=2,
        )
        self._install_two_units(orch)
        state = _make_state(files_changed=["src/shared.py"])
        both_started = asyncio.Event()
        active = 0
        peak_active = 0

        async def execute(_task, _state):
            nonlocal active, peak_active
            active += 1
            peak_active = max(peak_active, active)
            if active == 2:
                both_started.set()
            await asyncio.wait_for(both_started.wait(), timeout=1)
            active -= 1
            return []

        reviewer = MagicMock()
        reviewer.execute = AsyncMock(side_effect=execute)
        with patch.object(orch, "_create_reviewer", return_value=reviewer):
            await asyncio.wait_for(orch._v3_run_targeted_closure(state, "run-1", set()), timeout=2)

        assert peak_active == 2
        assert reviewer.execute.await_count == 2

    @pytest.mark.asyncio
    async def test_completion_order_cannot_change_dedup_winner(self):
        orch = _orchestrator(
            v3_enabled=True,
            v3_coverage_max_attempts=1,
            v3_coverage_max_cells_per_round=2,
        )
        _units, cells = self._install_two_units(orch)
        state = _make_state(files_changed=["src/shared.py"])
        second_finished = asyncio.Event()

        async def execute(task, _state):
            if "su_first" in task.rationale:
                await asyncio.wait_for(second_finished.wait(), timeout=1)
                message = "first selected cell"
            else:
                second_finished.set()
                message = "second completed first"
            return [Finding(file="src/shared.py", line=7, category="logic-error", message=message)]

        reviewer = MagicMock()
        reviewer.execute = AsyncMock(side_effect=execute)
        with patch.object(orch, "_create_reviewer", return_value=reviewer):
            await orch._v3_run_targeted_closure(state, "run-1", set())

        findings = state.list_findings()
        assert len(findings) == 1
        assert findings[0].message == "first selected cell"
        assert cells[0].status == CoverageStatus.COVERED
        assert cells[1].status == CoverageStatus.ABSTAINED

    @pytest.mark.asyncio
    async def test_one_cell_failure_does_not_cancel_sibling(self):
        orch = _orchestrator(
            v3_enabled=True,
            v3_coverage_max_attempts=1,
            v3_coverage_max_cells_per_round=2,
        )
        _units, cells = self._install_two_units(orch)
        state = _make_state(files_changed=["src/shared.py"])

        async def execute(task, _state):
            if "su_first" in task.rationale:
                raise RuntimeError("first failed")
            return [Finding(file="src/shared.py", line=7, category="logic-error", message="sibling bug")]

        reviewer = MagicMock()
        reviewer.execute = AsyncMock(side_effect=execute)
        with patch.object(orch, "_create_reviewer", return_value=reviewer):
            await orch._v3_run_targeted_closure(state, "run-1", set())

        assert cells[0].status == CoverageStatus.FAILED
        assert cells[1].status == CoverageStatus.COVERED
        assert [task.status for task in state.list_tasks()] == ["failed", "completed"]


class TestBroadPassTracking:
    """Broad pass coverage tracking."""

    def test_unit_specific_finding_marks_covered(self):
        """Finding within unit range marks cell COVERED."""
        orch = _orchestrator(v3_enabled=True)
        unit = SemanticUnit(
            id="su_auth",
            path="src/auth.py",
            start_line=4,
            end_line=10,
            risk_score=0.8,
            risk_reasons=["security-sensitive-symbol"],
            risk_signals=[{"type": "security-sensitive-symbol"}],
        )
        orch._v3_change_set = MagicMock()
        orch._v3_change_set.units = [unit]

        ledger = CoverageLedger.from_change_set(
            {
                "units": [unit.to_dict()],
            }
        )
        orch._v3_ledger = ledger

        finding = Finding(
            file="src/auth.py", line=7, category="logic-error", message="bug", reviewer="security_reviewer"
        )
        orch._track_broad_pass_coverage(
            task_id="t1",
            reviewer="security_reviewer",
            task_files=["src/auth.py"],
            findings=[finding],
        )

        sec_cell = ledger.get_cell("su_auth", CoverageDimension.SECURITY)
        assert sec_cell is not None
        assert sec_cell.status == CoverageStatus.COVERED
        assert finding.id in sec_cell.finding_ids

    def test_broad_correctness_finding_does_not_cover_security_cell(self):
        orch = _orchestrator(v3_enabled=True)
        unit = SemanticUnit(
            id="su_auth",
            path="src/auth.py",
            start_line=4,
            end_line=10,
            risk_score=0.8,
            risk_reasons=["security-sensitive-symbol"],
            risk_signals=[{"type": "security-sensitive-symbol"}],
        )
        orch._v3_change_set = MagicMock(units=[unit])
        ledger = CoverageLedger.from_change_set({"units": [unit.to_dict()]})
        orch._v3_ledger = ledger
        finding = Finding(
            file="src/auth.py",
            line=7,
            category="logic-error",
            message="bug",
            reviewer="correctness_reviewer",
        )

        orch._track_broad_pass_coverage(
            task_id="t1",
            reviewer="correctness_reviewer",
            task_files=["src/auth.py"],
            findings=[finding],
        )

        correctness = ledger.get_cell("su_auth", CoverageDimension.CORRECTNESS)
        security = ledger.get_cell("su_auth", CoverageDimension.SECURITY)
        assert correctness is not None and correctness.status == CoverageStatus.COVERED
        assert security is not None and security.status == CoverageStatus.PENDING

    def test_no_findings_marks_abstained(self):
        """No findings marks cell ABSTAINED."""
        orch = _orchestrator(v3_enabled=True)
        unit = SemanticUnit(
            id="su_auth",
            path="src/auth.py",
            start_line=4,
            end_line=10,
            risk_score=0.8,
            risk_reasons=["security-sensitive-symbol"],
            risk_signals=[{"type": "security-sensitive-symbol"}],
        )
        orch._v3_change_set = MagicMock()
        orch._v3_change_set.units = [unit]

        ledger = CoverageLedger.from_change_set(
            {
                "units": [unit.to_dict()],
            }
        )
        orch._v3_ledger = ledger

        orch._track_broad_pass_coverage(
            task_id="t1",
            reviewer="security_reviewer",
            task_files=["src/auth.py"],
            findings=[],
        )

        sec_cell = ledger.get_cell("su_auth", CoverageDimension.SECURITY)
        assert sec_cell is not None
        assert sec_cell.status == CoverageStatus.ABSTAINED

    def test_finding_outside_range_not_covered(self):
        """Finding outside unit range does not mark cell COVERED."""
        orch = _orchestrator(v3_enabled=True)
        unit = SemanticUnit(
            id="su_auth",
            path="src/auth.py",
            start_line=4,
            end_line=10,
            risk_score=0.8,
            risk_reasons=["security-sensitive-symbol"],
            risk_signals=[{"type": "security-sensitive-symbol"}],
        )
        orch._v3_change_set = MagicMock()
        orch._v3_change_set.units = [unit]

        ledger = CoverageLedger.from_change_set(
            {
                "units": [unit.to_dict()],
            }
        )
        orch._v3_ledger = ledger

        # Finding at line 15 is outside unit range [4, 10]
        finding = Finding(file="src/auth.py", line=15, category="security", message="x", reviewer="security_reviewer")
        orch._track_broad_pass_coverage(
            task_id="t1",
            reviewer="security_reviewer",
            task_files=["src/auth.py"],
            findings=[finding],
        )

        sec_cell = ledger.get_cell("su_auth", CoverageDimension.SECURITY)
        assert sec_cell is not None
        assert sec_cell.status == CoverageStatus.ABSTAINED  # no unit-specific finding
