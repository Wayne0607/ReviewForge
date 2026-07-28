"""Tests for the escalation reviewer — agentic verification of uncertain findings."""

from __future__ import annotations

import asyncio

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from reviewforge.core.specs import build_registry
from reviewforge.core.state import Finding, StateStore
from reviewforge.engine.budget import TokenBudget
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


class _CountingTool:
    def __init__(self, results, delay: float = 0.0):
        self.results = list(results)
        self.delay = delay
        self.calls = 0

    async def ainvoke(self, _args):
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        result = self.results[min(self.calls - 1, len(self.results) - 1)]
        if isinstance(result, Exception):
            raise result
        return result


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


class TestVerificationToolCache:
    @pytest.mark.asyncio
    async def test_same_successful_call_is_cached(self, gateway, state):
        reviewer = EscalationReviewer(MockChatLLM(), gateway)
        reviewer._ensure_tools(state)
        tool = _CountingTool(["repository evidence"])

        first = await reviewer._invoke_verification_tool("read_file", {"file_path": "app.py"}, tool)
        second = await reviewer._invoke_verification_tool("read_file", {"file_path": "app.py"}, tool)

        assert first == second == "repository evidence"
        assert tool.calls == 1

    @pytest.mark.asyncio
    async def test_concurrent_calls_share_single_flight(self, gateway, state):
        reviewer = EscalationReviewer(MockChatLLM(), gateway)
        reviewer._ensure_tools(state)
        tool = _CountingTool(["repository evidence"], delay=0.01)

        results = await asyncio.gather(
            reviewer._invoke_verification_tool("search_code", {"pattern": "target"}, tool),
            reviewer._invoke_verification_tool("search_code", {"pattern": "target"}, tool),
        )

        assert results == ["repository evidence", "repository evidence"]
        assert tool.calls == 1

    @pytest.mark.asyncio
    async def test_exception_is_not_cached(self, gateway, state):
        reviewer = EscalationReviewer(MockChatLLM(), gateway)
        reviewer._ensure_tools(state)
        tool = _CountingTool([RuntimeError("temporary failure"), "recovered evidence"])

        with pytest.raises(RuntimeError, match="temporary failure"):
            await reviewer._invoke_verification_tool("read_file", {"file_path": "app.py"}, tool)
        result = await reviewer._invoke_verification_tool("read_file", {"file_path": "app.py"}, tool)

        assert result == "recovered evidence"
        assert tool.calls == 2

    @pytest.mark.asyncio
    async def test_error_sentinel_is_not_cached(self, gateway, state):
        reviewer = EscalationReviewer(MockChatLLM(), gateway)
        reviewer._ensure_tools(state)
        tool = _CountingTool(["Search failed: rate limited", "recovered evidence"])

        first = await reviewer._invoke_verification_tool("search_code", {"pattern": "target"}, tool)
        second = await reviewer._invoke_verification_tool("search_code", {"pattern": "target"}, tool)

        assert first == "Search failed: rate limited"
        assert second == "recovered evidence"
        assert tool.calls == 2

    @pytest.mark.asyncio
    async def test_cache_is_invalidated_by_head_sha(self, gateway, state):
        reviewer = EscalationReviewer(MockChatLLM(), gateway)
        reviewer._ensure_tools(state)
        tool = _CountingTool(["old-head evidence", "new-head evidence"])

        first = await reviewer._invoke_verification_tool("read_file", {"file_path": "app.py"}, tool)
        state.head_sha = "new-head"
        reviewer._ensure_tools(state)
        second = await reviewer._invoke_verification_tool("read_file", {"file_path": "app.py"}, tool)

        assert first == "old-head evidence"
        assert second == "new-head evidence"
        assert tool.calls == 2


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
    async def test_forced_final_verdict_uses_raw_model_without_tools(self, gateway):
        class _RawFinalLLM:
            async def ainvoke(self, _messages):
                return AIMessage(
                    content='{"verdict":"confirmed","confidence":0.9,"reason":"grounded"}',
                )

        gate = PublicationGateReviewer(_RawFinalLLM(), gateway)

        result = await gate._force_final_verdict([], TokenBudget(100))

        assert result == {
            "verdict": "confirmed",
            "confidence": 0.9,
            "reason": "grounded",
            "_tool_evidence": "",
        }

    def test_confirmed_verdict_requires_exact_tool_evidence(self):
        finding = _make_finding(status="confirmed")
        result = PublicationGateReviewer._apply_verdict(
            finding,
            {
                "verdict": "confirmed",
                "confidence": 0.9,
                "reason": "grounded",
                "evidence_quote": "return user.is_admin",
                "_tool_evidence": "if active:\n    return user.is_admin\n",
            },
        )

        assert result.status == "confirmed"
        assert result.verified_by == "escalation"

    def test_line_numbers_fences_and_whitespace_are_presentation_only(self):
        finding = _make_finding(status="confirmed")
        result = PublicationGateReviewer._apply_verdict(
            finding,
            {
                "verdict": "confirmed",
                "confidence": 0.9,
                "reason": "grounded",
                "evidence_quote": "```python\nif active:\n return user.is_admin\n```",
                "_tool_evidence": "41: if active:\r\n42:     return user.is_admin\r\n",
            },
        )

        assert result.status == "confirmed"
        assert result.verified_by == "escalation"

    def test_noncontiguous_exact_tool_fragments_are_grounded(self):
        finding = _make_finding(status="confirmed")
        result = PublicationGateReviewer._apply_verdict(
            finding,
            {
                "verdict": "confirmed",
                "confidence": 0.94,
                "reason": "The lock no longer covers the check-and-build sequence.",
                "evidence_quote": "cacheMu.Lock()\ndefer cacheMu.Unlock()\n...\ncacheMu.Lock()\ncache[key] = idx",
                "_tool_evidence": (
                    "88: cacheMu.Lock()\n89: defer cacheMu.Unlock()\n"
                    "120: idx := buildIndex()\n137: cacheMu.Lock()\n138: cache[key] = idx\n"
                ),
            },
        )

        assert result.status == "confirmed"
        assert result.verified_by == "escalation"

    def test_noncontiguous_quote_rejects_any_invented_fragment(self):
        finding = _make_finding(status="confirmed")
        result = PublicationGateReviewer._apply_verdict(
            finding,
            {
                "verdict": "confirmed",
                "confidence": 0.94,
                "reason": "claimed",
                "evidence_quote": "cacheMu.Lock()\n...\ncache[key] = invented",
                "_tool_evidence": "88: cacheMu.Lock()\n138: cache[key] = idx\n",
            },
        )

        assert result.status == "false_positive"
        assert result.verified_by == "publication-gate-ungrounded"

    @pytest.mark.parametrize(
        "quote",
        [
            "",
            "return ok",
            "not in the transcript at all",
            "first evidence unrelated words second evidence",
        ],
    )
    def test_ungrounded_confirmation_is_rejected(self, quote):
        finding = _make_finding(status="confirmed")
        result = PublicationGateReviewer._apply_verdict(
            finding,
            {
                "verdict": "confirmed",
                "confidence": 0.9,
                "reason": "claimed",
                "evidence_quote": quote,
                "_tool_evidence": "first evidence\nactual repository code\nsecond evidence",
            },
        )

        assert result.status == "false_positive"
        assert result.verified_by == "publication-gate-ungrounded"
        assert "ungrounded approval" in result.verify_reason

    def test_tool_transcript_is_attached_to_verdict(self):
        result = EscalationReviewer._attach_tool_evidence(
            {"verdict": "confirmed"},
            [
                ToolMessage(content="first evidence", tool_call_id="one"),
                ToolMessage(content="second evidence", tool_call_id="two"),
            ],
        )

        assert result["_tool_evidence"] == "first evidence\nsecond evidence"

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
    async def test_ungrounded_verdict_keeps_distinct_attribution(
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
                "verdict": "confirmed",
                "confidence": 0.95,
                "reason": "claimed",
                "evidence_quote": "not found anywhere in evidence",
                "_tool_evidence": "actual repository evidence",
            }

        monkeypatch.setattr(gate, "_run_tool_loop", verdict)

        result = await gate.escalate(finding, state)

        assert result.status == "false_positive"
        assert result.verified_by == "publication-gate-ungrounded"

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
            ("localization_reviewer", "language-mismatch", 0.9),
            ("quality_reviewer", "null-safety", 0.9),
            ("correctness_reviewer", "nullish-vs-falsy-semantics", 0.85),
            ("correctness_reviewer", "error-handling", 0.85),
            ("correctness_reviewer", "race-condition", 0.85),
            ("testing_reviewer", "logic-error", 0.9),
            ("performance_reviewer", "thread-safety", 0.88),
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
            ("security_reviewer", "ssrf", 0.99),
            ("security_reviewer", "unsafe-postmessage", 0.99),
            ("testing_reviewer", "test-assertion", 0.99),
            ("correctness_reviewer", "wrong-callee-contract", 0.99),
            ("quality_reviewer", "null-safety", 0.8),
            ("correctness_reviewer", "logic-error", 0.8),
            ("testing_reviewer", "logic-error", 0.8),
            ("performance_reviewer", "thread-safety", 0.8),
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

    @pytest.mark.parametrize(
        "category",
        ["command-injection", "authorization-bypass", "data-corruption"],
    )
    def test_high_cost_security_is_protected_from_operational_failure(
        self,
        category,
    ):
        critical = _make_finding(
            reviewer="security_reviewer",
            category=category,
            confidence=0.9,
        )
        noisy = _make_finding(
            reviewer="security_reviewer",
            category="ssrf",
            confidence=0.9,
        )

        assert PublicationGateReviewer.operational_recall_protected(critical) is True
        assert PublicationGateReviewer.operational_recall_protected(noisy) is False

    def test_high_confidence_contract_is_protected_only_when_gate_is_inconclusive(self):
        contract = _make_finding(
            reviewer="correctness_reviewer",
            category="wrong-callee-contract",
            confidence=0.9,
        )

        assert PublicationGateReviewer.recall_protected(contract) is False
        assert PublicationGateReviewer.operational_recall_protected(contract) is True

    @pytest.mark.parametrize(
        "category",
        [
            "missing-context-field",
            "null-reference",
            "race-condition",
            "wrong-argument-contract",
            "wrong-caller-callee-contract",
            "wrong-logic",
        ],
    )
    def test_high_signal_correctness_families_are_operationally_protected(
        self,
        category,
    ):
        finding = _make_finding(
            reviewer="correctness_reviewer",
            category=category,
            confidence=0.8,
        )

        assert PublicationGateReviewer.operational_recall_protected(finding) is True

    def test_broad_combined_contract_category_is_not_operationally_protected(self):
        finding = _make_finding(
            reviewer="correctness_reviewer",
            category="wrong-caller-callee-argument-return-contract",
            confidence=0.99,
        )

        assert PublicationGateReviewer.operational_recall_protected(finding) is False

    def test_high_confidence_quality_correctness_is_operationally_protected(self):
        finding = _make_finding(
            reviewer="quality_reviewer",
            category="correctness",
            confidence=0.95,
        )

        assert PublicationGateReviewer.operational_recall_protected(finding) is True

    @pytest.mark.asyncio
    async def test_explicit_negative_verdict_is_never_overridden(
        self,
        gateway,
        state,
        monkeypatch,
    ):
        gate = PublicationGateReviewer(MockChatLLM(), gateway)
        finding = _make_finding(
            status="confirmed",
            reviewer="correctness_reviewer",
            category="error-handling",
            confidence=0.85,
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

        assert result.status == "false_positive"
        assert result.confidence == 0.95
        assert result.verified_by == "publication-gate"
        assert result.verify_reason == "not enough data flow"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("reviewer", "category", "expected_status", "expected_verifier"),
        [
            ("correctness_reviewer", "logic-error", "candidate", "publication-gate-provider-error"),
            ("security_reviewer", "ssrf", "candidate", "publication-gate-provider-error"),
            ("security_reviewer", "command-injection", "candidate", "publication-gate-provider-error"),
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
