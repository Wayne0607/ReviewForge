"""Tests for the escalation reviewer — agentic verification of uncertain findings."""

from __future__ import annotations

import json

import pytest

from reviewforge.core.specs import build_registry
from reviewforge.core.state import Finding, StateStore
from reviewforge.engine.escalation import TRACE_CATEGORIES, EscalationReviewer
from reviewforge.engine.mock_llm import MockChatLLM
from reviewforge.tools.gateway import ToolGateway
from reviewforge.tools.mock_github import MockGitHubClient


@pytest.fixture
def gateway():
    return ToolGateway(build_registry(), MockGitHubClient())


@pytest.fixture
def state():
    s = StateStore(
        pr_number=1, repo="test/repo", head_sha="abc123",
        files_changed=["app.py"],
        diff_summary="--- app.py\n+import os\n+os.system(cmd)",
    )
    return s


def _make_finding(**overrides) -> Finding:
    defaults = {
        "file": "app.py", "line": 5, "severity": "warning",
        "category": "sql-injection", "message": "SQL injection risk",
        "suggestion": "Use parameterized queries", "confidence": 0.6,
    }
    defaults.update(overrides)
    return Finding(**defaults)


# ── should_escalate ──────────────────────────────────────────────

class TestShouldEscalate:
    def test_fuzzy_confidence_triggers(self):
        """Confidence in [0.4, 0.7] should trigger escalation."""
        f = _make_finding(confidence=0.5, category="naming")
        assert EscalationReviewer.should_escalate(f) is True

    def test_high_confidence_no_escalate(self):
        """High-confidence non-trace finding should NOT escalate."""
        f = _make_finding(confidence=0.9, category="naming")
        assert EscalationReviewer.should_escalate(f) is False

    def test_low_confidence_no_escalate(self):
        """Low-confidence non-trace finding should NOT escalate."""
        f = _make_finding(confidence=0.2, category="naming")
        assert EscalationReviewer.should_escalate(f) is False

    def test_trace_category_escalates_when_uncertain(self):
        """Trace-type categories escalate when confidence < 0.85."""
        for cat in TRACE_CATEGORIES:
            f = _make_finding(confidence=0.7, category=cat)
            assert EscalationReviewer.should_escalate(f) is True, f"{cat} should escalate at conf=0.7"

    def test_trace_category_skips_when_high_confidence(self):
        """Trace-type categories skip when confidence >= 0.85."""
        for cat in TRACE_CATEGORIES:
            f = _make_finding(confidence=0.95, category=cat)
            assert EscalationReviewer.should_escalate(f) is False, f"{cat} should not escalate at conf=0.95"

    def test_style_category_only_on_fuzzy(self):
        """Non-trace categories only escalate when confidence is fuzzy."""
        f_high = _make_finding(confidence=0.9, category="naming")
        f_low = _make_finding(confidence=0.2, category="naming")
        f_fuzzy = _make_finding(confidence=0.5, category="naming")
        assert EscalationReviewer.should_escalate(f_high) is False
        assert EscalationReviewer.should_escalate(f_low) is False
        assert EscalationReviewer.should_escalate(f_fuzzy) is True

    def test_custom_confidence_range(self):
        """Custom confidence range should be respected."""
        f = _make_finding(confidence=0.55, category="naming")
        assert EscalationReviewer.should_escalate(f, confidence_min=0.5, confidence_max=0.6) is True
        assert EscalationReviewer.should_escalate(f, confidence_min=0.6, confidence_max=0.8) is False

    def test_boundary_values(self):
        """Boundary confidence values should trigger."""
        f_min = _make_finding(confidence=0.4, category="naming")
        f_max = _make_finding(confidence=0.7, category="naming")
        assert EscalationReviewer.should_escalate(f_min) is True
        assert EscalationReviewer.should_escalate(f_max) is True


# ── escalate (mock LLM) ──────────────────────────────────────────

class TestEscalate:
    @pytest.mark.asyncio
    async def test_escalate_high_confidence_skips(self):
        """High-confidence non-trace finding should be returned unchanged."""
        llm = MockChatLLM()
        esc = EscalationReviewer(llm, ToolGateway(build_registry(), MockGitHubClient()))
        state = StateStore(pr_number=1, repo="t/t", files_changed=["f.py"])
        f = _make_finding(confidence=0.9, category="naming")

        result = await esc.escalate(f, state)
        assert result.id == f.id
        assert result.verified_by == ""  # unchanged

    @pytest.mark.asyncio
    async def test_escalate_updates_finding(self):
        """Escalated finding should get escalation verdict."""
        llm = MockChatLLM()
        gw = ToolGateway(build_registry(), MockGitHubClient())
        esc = EscalationReviewer(llm, gw)
        state = StateStore(
            pr_number=1, repo="t/t", head_sha="x",
            files_changed=["app.py"],
            diff_summary="--- app.py\n+os.system(cmd)",
        )
        f = _make_finding(confidence=0.5, category="sql-injection")

        result = await esc.escalate(f, state)
        # Mock LLM returns a mock finding, so escalation should produce some result
        assert result.verified_by in ("escalation", "escalation-inconclusive")

    @pytest.mark.asyncio
    async def test_escalate_batch_skips_high_confidence(self):
        """Batch escalation should skip high-confidence non-trace findings."""
        llm = MockChatLLM()
        gw = ToolGateway(build_registry(), MockGitHubClient())
        esc = EscalationReviewer(llm, gw)
        state = StateStore(pr_number=1, repo="t/t", files_changed=["f.py"])

        findings = [
            _make_finding(confidence=0.9, category="naming"),  # skip
            _make_finding(confidence=0.2, category="naming"),  # skip
        ]

        result = await esc.escalate_batch(findings, state)
        assert len(result) == 2
        # Both should be unchanged
        for r in result:
            assert r.verified_by == ""

    @pytest.mark.asyncio
    async def test_escalate_batch_processes_trace_findings(self):
        """Batch escalation should process trace-type findings."""
        llm = MockChatLLM()
        gw = ToolGateway(build_registry(), MockGitHubClient())
        esc = EscalationReviewer(llm, gw)
        state = StateStore(
            pr_number=1, repo="t/t", head_sha="x",
            files_changed=["app.py"],
            diff_summary="--- app.py\n+import os",
        )

        findings = [
            _make_finding(confidence=0.6, category="command-injection"),  # trace + fuzzy → escalate
            _make_finding(confidence=0.9, category="naming"),  # high conf, non-trace → skip
        ]

        result = await esc.escalate_batch(findings, state)
        assert len(result) == 2
        # First should be escalated (or at least attempted)
        assert result[0].verified_by in ("escalation", "escalation-inconclusive")
        # Second should be unchanged
        assert result[1].verified_by == ""


# ── _parse_verdict ───────────────────────────────────────────────

class TestParseVerdict:
    def test_parse_clean_json(self):
        esc = EscalationReviewer(MockChatLLM(), ToolGateway(build_registry(), MockGitHubClient()))
        result = esc._parse_verdict('{"verdict": "confirmed", "confidence": 0.9, "reason": "real"}')
        assert result["verdict"] == "confirmed"
        assert result["confidence"] == 0.9

    def test_parse_json_in_markdown(self):
        esc = EscalationReviewer(MockChatLLM(), ToolGateway(build_registry(), MockGitHubClient()))
        content = '```json\n{"verdict": "false_positive", "confidence": 0.2, "reason": "safe"}\n```'
        result = esc._parse_verdict(content)
        assert result["verdict"] == "false_positive"

    def test_parse_json_with_surrounding_text(self):
        esc = EscalationReviewer(MockChatLLM(), ToolGateway(build_registry(), MockGitHubClient()))
        content = 'After analysis, here is my verdict: {"verdict": "confirmed", "confidence": 0.85, "reason": "yes"}'
        result = esc._parse_verdict(content)
        assert result["verdict"] == "confirmed"

    def test_parse_invalid_returns_none(self):
        esc = EscalationReviewer(MockChatLLM(), ToolGateway(build_registry(), MockGitHubClient()))
        assert esc._parse_verdict("not json at all") is None
        assert esc._parse_verdict("") is None


# ── _apply_verdict ───────────────────────────────────────────────

class TestApplyVerdict:
    def test_apply_confirmed(self):
        esc = EscalationReviewer(MockChatLLM(), ToolGateway(build_registry(), MockGitHubClient()))
        f = _make_finding(confidence=0.5)
        result = esc._apply_verdict(f, {"verdict": "confirmed", "confidence": 0.9, "reason": "real issue"})
        assert result.status == "confirmed"
        assert result.confidence == 0.9
        assert result.verified_by == "escalation"
        assert result.verify_reason == "real issue"

    def test_apply_false_positive(self):
        esc = EscalationReviewer(MockChatLLM(), ToolGateway(build_registry(), MockGitHubClient()))
        f = _make_finding(confidence=0.5)
        result = esc._apply_verdict(f, {"verdict": "false_positive", "confidence": 0.1, "reason": "safe code"})
        assert result.status == "false_positive"
        assert result.confidence == 0.1
