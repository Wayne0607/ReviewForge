"""Tests for the escalation reviewer — agentic verification of uncertain findings."""

from __future__ import annotations

import pytest

from reviewforge.core.specs import build_registry
from reviewforge.core.state import Finding, StateStore
from reviewforge.engine.escalation import (
    TRACE_CATEGORIES,
    EscalationReviewer,
    PublicationGateReviewer,
)
from reviewforge.engine.mock_llm import MockChatLLM
from reviewforge.tools.gateway import ToolGateway
from reviewforge.tools.mock_github import MockGitHubClient


@pytest.fixture
def gateway():
    return ToolGateway(build_registry(), MockGitHubClient())


@pytest.fixture
def state():
    s = StateStore(
        pr_number=1,
        repo="test/repo",
        head_sha="abc123",
        files_changed=["app.py"],
        diff_summary="--- app.py\n+import os\n+os.system(cmd)",
    )
    return s


def _make_finding(**overrides) -> Finding:
    defaults = {
        "file": "app.py",
        "line": 5,
        "severity": "warning",
        "category": "sql-injection",
        "message": "SQL injection risk",
        "suggestion": "Use parameterized queries",
        "confidence": 0.6,
    }
    defaults.update(overrides)
    return Finding(**defaults)


# ── should_escalate ──────────────────────────────────────────────


class TestShouldEscalate:
    def test_fuzzy_security_confidence_triggers(self):
        """Security confidence in [0.4, 0.7] should trigger escalation."""
        f = _make_finding(confidence=0.5, category="sql-injection")
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

    def test_style_category_does_not_escalate_on_fuzzy(self):
        """Non-security categories do not use the expensive agentic escalation path."""
        f_high = _make_finding(confidence=0.9, category="naming")
        f_low = _make_finding(confidence=0.2, category="naming")
        f_fuzzy = _make_finding(confidence=0.5, category="naming")
        assert EscalationReviewer.should_escalate(f_high) is False
        assert EscalationReviewer.should_escalate(f_low) is False
        assert EscalationReviewer.should_escalate(f_fuzzy) is False

    def test_custom_confidence_range(self):
        """Custom confidence range should be respected for security findings."""
        f = _make_finding(confidence=0.55, category="hardcoded-secrets")
        assert EscalationReviewer.should_escalate(f, confidence_min=0.5, confidence_max=0.6) is True
        assert EscalationReviewer.should_escalate(f, confidence_min=0.6, confidence_max=0.8) is False

    def test_boundary_values(self):
        """Security boundary confidence values should trigger."""
        f_min = _make_finding(confidence=0.4, category="sql-injection")
        f_max = _make_finding(confidence=0.7, category="sql-injection")
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
            pr_number=1,
            repo="t/t",
            head_sha="x",
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
            pr_number=1,
            repo="t/t",
            head_sha="x",
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


class TestPublicationGate:
    def test_all_confirmed_findings_require_verification(self):
        finding = _make_finding(
            confidence=0.99,
            category="naming",
            status="confirmed",
        )
        assert PublicationGateReviewer.should_escalate(finding) is True

    def test_prompt_is_strict_and_repository_grounded(self, gateway):
        gate = PublicationGateReviewer(MockChatLLM(), gateway)
        finding = _make_finding(status="confirmed")

        system, user = gate._build_prompt(finding)

        assert "read_file" in system.content
        assert "证据不足" in system.content
        assert "false_positive" in system.content
        assert finding.message in user.content

    @pytest.mark.asyncio
    async def test_valid_verdict_is_attributed_to_publication_gate(
        self,
        gateway,
        state,
        monkeypatch,
    ):
        gate = PublicationGateReviewer(MockChatLLM(), gateway)
        finding = _make_finding(status="confirmed")
        monkeypatch.setattr(gate, "_ensure_tools", lambda _state: ([], {}, None))

        async def verdict(*_args, **_kwargs):
            return {
                "verdict": "false_positive",
                "confidence": 0.95,
                "reason": "完整文件中的 guard 已排除该路径",
            }

        monkeypatch.setattr(gate, "_run_tool_loop", verdict)

        result = await gate.escalate(finding, state)

        assert result.status == "false_positive"
        assert result.verified_by == "publication-gate"

    @pytest.mark.asyncio
    async def test_inconclusive_verification_is_not_published(
        self,
        gateway,
        state,
        monkeypatch,
    ):
        gate = PublicationGateReviewer(MockChatLLM(), gateway)
        finding = _make_finding(status="confirmed")
        monkeypatch.setattr(gate, "_ensure_tools", lambda _state: ([], {}, None))

        async def no_verdict(*_args, **_kwargs):
            return None

        monkeypatch.setattr(gate, "_run_tool_loop", no_verdict)
        monkeypatch.setattr(gate, "_force_final_verdict", no_verdict)

        result = await gate.escalate(finding, state)

        assert result.status == "candidate"
        assert result.verified_by == "publication-gate-inconclusive"

    @pytest.mark.parametrize(
        ("reviewer", "category", "confidence"),
        [
            ("security_reviewer", "ssrf", 0.75),
            ("security_reviewer", "unsafe-postmessage", 0.85),
            ("localization_reviewer", "language-mismatch", 0.9),
            ("quality_reviewer", "null-safety", 0.9),
            ("correctness_reviewer", "nullish-vs-falsy-semantics", 0.85),
            ("correctness_reviewer", "error-handling", 0.85),
        ],
    )
    def test_high_cost_false_negative_families_are_recall_protected(
        self,
        reviewer,
        category,
        confidence,
    ):
        finding = _make_finding(
            reviewer=reviewer,
            category=category,
            confidence=confidence,
        )

        assert PublicationGateReviewer.recall_protected(finding) is True

    @pytest.mark.parametrize(
        ("reviewer", "category", "confidence"),
        [
            ("security_reviewer", "input-validation", 0.99),
            ("security_reviewer", "ssrf", 0.7),
            ("testing_reviewer", "test-assertion", 0.99),
            ("correctness_reviewer", "wrong-callee-contract", 0.99),
            ("quality_reviewer", "null-safety", 0.8),
        ],
    )
    def test_noisy_families_are_not_recall_protected(
        self,
        reviewer,
        category,
        confidence,
    ):
        finding = _make_finding(
            reviewer=reviewer,
            category=category,
            confidence=confidence,
        )

        assert PublicationGateReviewer.recall_protected(finding) is False

    @pytest.mark.asyncio
    async def test_recall_guard_retains_protected_finding(
        self,
        gateway,
        state,
        monkeypatch,
    ):
        gate = PublicationGateReviewer(MockChatLLM(), gateway)
        finding = _make_finding(
            status="confirmed",
            reviewer="security_reviewer",
            category="ssrf",
            confidence=0.8,
        )
        monkeypatch.setattr(gate, "_ensure_tools", lambda _state: ([], {}, None))

        async def verdict(*_args, **_kwargs):
            return {
                "verdict": "false_positive",
                "confidence": 0.95,
                "reason": "not enough data flow",
            }

        monkeypatch.setattr(gate, "_run_tool_loop", verdict)

        result = await gate.escalate(finding, state)

        assert result.status == "confirmed"
        assert result.confidence == 0.8
        assert result.verified_by == "publication-gate-recall-guard"
        assert "confidence=0.95" in result.verify_reason
        assert "not enough data flow" in result.verify_reason

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("reviewer", "category", "expected_status", "expected_verifier"),
        [
            ("correctness_reviewer", "logic-error", "candidate", "publication-gate-inconclusive"),
            ("security_reviewer", "ssrf", "confirmed", "publication-gate-recall-guard"),
        ],
    )
    async def test_provider_failure_isolated_per_finding(
        self,
        gateway,
        state,
        monkeypatch,
        reviewer,
        category,
        expected_status,
        expected_verifier,
    ):
        gate = PublicationGateReviewer(MockChatLLM(), gateway)
        finding = _make_finding(
            status="confirmed",
            reviewer=reviewer,
            category=category,
            confidence=0.85,
        )
        monkeypatch.setattr(gate, "_ensure_tools", lambda _state: ([], {}, None))

        async def provider_error(*_args, **_kwargs):
            raise RuntimeError("provider unavailable")

        monkeypatch.setattr(gate, "_run_tool_loop", provider_error)

        result = await gate.escalate(finding, state)

        assert result.status == expected_status
        assert result.verified_by == expected_verifier


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
