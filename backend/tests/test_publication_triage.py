from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage

from reviewforge.core.config import ReviewForgeConfig
from reviewforge.core.database import Database
from reviewforge.core.events import EventBus
from reviewforge.core.specs import build_registry
from reviewforge.core.state import Finding, StateStore
from reviewforge.engine.mock_llm import MockChatLLM
from reviewforge.engine.orchestrator import Orchestrator
from reviewforge.engine.publication_triage import (
    VERDICT_CONFIRMED,
    VERDICT_FALSE_POSITIVE,
    VERDICT_NEEDS_TOOL,
    PublicationTriage,
    PublicationTriageConfig,
    TriageStats,
)
from reviewforge.tools.gateway import ToolGateway
from reviewforge.tools.mock_github import MockGitHubClient


class _StaticLLM:
    def __init__(self, content: str = "", error: Exception | None = None) -> None:
        self.content = content
        self.error = error
        self.calls = []

    async def ainvoke(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        if self.error:
            raise self.error
        return AIMessage(content=self.content)


def _state() -> StateStore:
    return StateStore(
        pr_number=1,
        repo="owner/repo",
        head_sha="head",
        files_changed=["app.py"],
        diff_summary="--- app.py\n@@ -1,2 +1,3 @@\n old\n+bad()\n tail",
        file_diffs={
            "app.py": "@@ -1,2 +1,3 @@\n old\n+bad()\n tail",
        },
    )


def _finding(
    finding_id: str,
    *,
    reviewer: str = "correctness_reviewer",
    category: str = "logic-error",
    confidence: float = 0.7,
    verified_by: str = "",
) -> Finding:
    return Finding(
        id=finding_id,
        file="app.py",
        line=2,
        severity="warning",
        category=category,
        message=f"problem {finding_id}",
        suggestion="fix it",
        confidence=confidence,
        reviewer=reviewer,
        status="confirmed",
        verified_by=verified_by,
    )


def _response(*rows: tuple[str, str]) -> str:
    return json.dumps(
        {
            "verdicts": [
                {
                    "id": finding_id,
                    "verdict": verdict,
                    "confidence": 0.95,
                    "reason": f"{verdict} evidence",
                }
                for finding_id, verdict in rows
            ]
        }
    )


@pytest.mark.asyncio
async def test_triage_parses_mixed_batch_and_bounds_context():
    llm = _StaticLLM(
        _response(
            ("f1", VERDICT_CONFIRMED),
            ("f2", VERDICT_FALSE_POSITIVE),
            ("f3", VERDICT_NEEDS_TOOL),
        )
    )
    triage = PublicationTriage(
        llm,
        config=PublicationTriageConfig(enabled=True, batch_size=6),
    )

    verdicts, stats = await triage.classify(
        [_finding("f1"), _finding("f2"), _finding("f3")],
        _state(),
    )

    assert [verdicts[key].verdict for key in ("f1", "f2", "f3")] == [
        VERDICT_NEEDS_TOOL,
        VERDICT_FALSE_POSITIVE,
        VERDICT_NEEDS_TOOL,
    ]
    assert stats.triage_batches == 1
    assert stats.triage_confirmed == 0
    assert stats.triage_filtered == 1
    assert stats.triage_needs_tool == 2
    prompt = llm.calls[0][0][1].content
    assert "<UNTRUSTED_DIFF>" in prompt
    assert "bad()" in prompt
    assert len(prompt) < 10_000


@pytest.mark.asyncio
async def test_only_independently_checkable_findings_can_bypass_tools():
    detector = _finding("detector", verified_by="detector")
    localized = _finding(
        "localized",
        reviewer="localization_reviewer",
        category="language-mismatch",
        confidence=0.95,
    )
    ordinary = _finding("ordinary", confidence=0.99)
    triage = PublicationTriage(
        _StaticLLM(
            _response(
                ("detector", VERDICT_CONFIRMED),
                ("localized", VERDICT_CONFIRMED),
                ("ordinary", VERDICT_CONFIRMED),
            )
        ),
        config=PublicationTriageConfig(enabled=True),
    )

    verdicts, stats = await triage.classify(
        [detector, localized, ordinary],
        _state(),
    )

    assert verdicts["detector"].verdict == VERDICT_CONFIRMED
    assert verdicts["localized"].verdict == VERDICT_CONFIRMED
    assert verdicts["ordinary"].verdict == VERDICT_NEEDS_TOOL
    assert stats.triage_confirmed == 2
    assert stats.triage_needs_tool == 1


@pytest.mark.asyncio
async def test_invalid_batch_response_routes_everything_to_tools_and_retries():
    triage = PublicationTriage(
        _StaticLLM('{"verdicts": [{"id": "f1", "verdict": "confirmed"}]}'),
        config=PublicationTriageConfig(enabled=True),
    )

    verdicts, stats = await triage.classify(
        [_finding("f1"), _finding("f2")],
        _state(),
    )

    assert {verdict.verdict for verdict in verdicts.values()} == {VERDICT_NEEDS_TOOL}
    assert stats.triage_failed == 1
    assert stats.triage_needs_tool == 2
    assert stats.retryable is True
    assert stats.provider_errors == 0


@pytest.mark.asyncio
async def test_provider_failure_is_structured_and_never_approves():
    triage = PublicationTriage(
        _StaticLLM(error=TimeoutError("provider timed out")),
        config=PublicationTriageConfig(enabled=True),
    )

    verdicts, stats = await triage.classify([_finding("f1")], _state())

    assert verdicts["f1"].verdict == VERDICT_NEEDS_TOOL
    assert stats.retryable is True
    assert stats.provider_errors == 1
    assert "TimeoutError" in stats.errors[0]
    assert "provider timed out" not in stats.errors[0]


@pytest.mark.asyncio
async def test_recall_guard_routes_negative_triage_verdict_to_tools():
    protected = _finding(
        "protected",
        reviewer="localization_reviewer",
        category="language-mismatch",
        confidence=0.9,
    )
    triage = PublicationTriage(
        _StaticLLM(_response(("protected", VERDICT_FALSE_POSITIVE))),
        config=PublicationTriageConfig(enabled=True),
    )

    verdicts, stats = await triage.classify([protected], _state())

    assert verdicts["protected"].verdict == VERDICT_NEEDS_TOOL
    assert stats.triage_filtered == 0
    assert stats.triage_needs_tool == 1


@pytest.mark.asyncio
async def test_orchestrator_sends_only_needs_tool_to_agentic_gate():
    state = _state()
    direct = _finding(
        "direct",
        reviewer="localization_reviewer",
        category="language-mismatch",
        confidence=0.95,
    )
    rejected = _finding("rejected")
    unresolved = _finding("unresolved")
    for finding in (direct, rejected, unresolved):
        state.add_finding(finding)

    triage_llm = _StaticLLM(
        _response(
            ("direct", VERDICT_CONFIRMED),
            ("rejected", VERDICT_FALSE_POSITIVE),
            ("unresolved", VERDICT_NEEDS_TOOL),
        )
    )
    registry = build_registry()
    orchestrator = Orchestrator(
        registry=registry,
        gateway=ToolGateway(registry, MockGitHubClient()),
        event_bus=EventBus(),
        planner_llm=triage_llm,
        reviewer_llm=triage_llm,
        calibrator_llm=triage_llm,
        publication_gate_llm=triage_llm,
        publication_gate_enabled=True,
        publication_triage_enabled=True,
    )

    class _Gate:
        received: list[str] = []

        async def escalate_batch(self, findings, _state, concurrency):
            self.received = [finding.id for finding in findings]
            for finding in findings:
                finding.status = "confirmed"
                finding.verified_by = "publication-gate"
            return findings

    gate = _Gate()
    orchestrator._publication_gate_reviewer = gate
    stats = await orchestrator._run_publication_gate(state)

    assert gate.received == ["unresolved"]
    assert state.get_finding("direct").status == "confirmed"
    assert state.get_finding("rejected").status == "false_positive"
    assert state.get_finding("unresolved").status == "confirmed"
    assert stats.agentic_attempted == 1
    assert stats.agentic_confirmed == 1


@pytest.mark.asyncio
async def test_agentic_inconclusive_does_not_block_independent_confirmations():
    state = _state()
    confirmed = _finding("confirmed")
    inconclusive = _finding("inconclusive")
    for finding in (confirmed, inconclusive):
        state.add_finding(finding)

    triage_llm = _StaticLLM(
        _response(
            ("confirmed", VERDICT_NEEDS_TOOL),
            ("inconclusive", VERDICT_NEEDS_TOOL),
        )
    )
    registry = build_registry()
    orchestrator = Orchestrator(
        registry=registry,
        gateway=ToolGateway(registry, MockGitHubClient()),
        event_bus=EventBus(),
        planner_llm=triage_llm,
        reviewer_llm=triage_llm,
        calibrator_llm=triage_llm,
        publication_gate_llm=triage_llm,
        publication_gate_enabled=True,
        publication_triage_enabled=True,
    )

    class _Gate:
        async def escalate_batch(self, findings, _state, concurrency):
            findings[0].status = "confirmed"
            findings[0].verified_by = "publication-gate"
            findings[1].status = "candidate"
            findings[1].verified_by = "publication-gate-inconclusive"
            return findings

    orchestrator._publication_gate_reviewer = _Gate()
    stats = await orchestrator._run_publication_gate(state)

    assert state.get_finding("confirmed").status == "confirmed"
    assert state.get_finding("inconclusive").status == "candidate"
    assert stats.agentic_inconclusive == 1
    assert stats.retryable is False
    assert stats.errors == []


@pytest.mark.asyncio
async def test_gate_provider_failure_leaves_no_unverified_confirmed_finding():
    state = _state()
    state.add_finding(_finding("f1"))
    failing_llm = _StaticLLM(error=RuntimeError("quota exhausted"))
    registry = build_registry()
    orchestrator = Orchestrator(
        registry=registry,
        gateway=ToolGateway(registry, MockGitHubClient()),
        event_bus=EventBus(),
        planner_llm=failing_llm,
        reviewer_llm=failing_llm,
        calibrator_llm=failing_llm,
        publication_gate_llm=failing_llm,
        publication_gate_enabled=True,
        publication_triage_enabled=True,
    )

    class _FailedGate:
        async def escalate_batch(self, findings, _state, concurrency):
            for finding in findings:
                finding.status = "candidate"
                finding.verified_by = "publication-gate-provider-error"
            return findings

    orchestrator._publication_gate_reviewer = _FailedGate()
    stats = await orchestrator._run_publication_gate(state)

    assert state.list_findings(status="confirmed") == []
    assert state.get_finding("f1").status == "candidate"
    assert stats.retryable is True
    assert stats.provider_errors == 2
    assert stats.triage_failed == 1
    assert stats.agentic_inconclusive == 1


def test_publication_triage_config_loads_from_yaml(tmp_path):
    config_path = tmp_path / "reviewforge.yaml"
    config_path.write_text(
        """
publication_triage:
  enabled: true
  batch_size: 4
  concurrency: 2
  max_candidates: 18
  context_lines: 9
  max_tokens: 2500
""",
        encoding="utf-8",
    )

    config = ReviewForgeConfig.load(config_path)

    assert config.publication_triage_enabled is True
    assert config.publication_triage_batch_size == 4
    assert config.publication_triage_concurrency == 2
    assert config.publication_triage_max_candidates == 18
    assert config.publication_triage_context_lines == 9
    assert config.publication_triage_max_tokens == 2500


@pytest.mark.asyncio
async def test_retryable_publication_failure_skips_comments_and_fails_db_run(
    tmp_path,
):
    database = Database(tmp_path / "reviewforge.db")
    await database.connect()
    registry = build_registry()
    llm = MockChatLLM()
    orchestrator = Orchestrator(
        registry=registry,
        gateway=ToolGateway(registry, MockGitHubClient()),
        event_bus=EventBus(),
        planner_llm=llm,
        reviewer_llm=llm,
        calibrator_llm=llm,
        db=database,
        publication_gate_enabled=True,
    )
    state = _state()
    state.add_finding(_finding("publish-blocked"))
    failed_stats = TriageStats(
        provider_errors=1,
        retryable=True,
        errors=["LLM provider call failed (RateLimitError, status=429)"],
    )

    with patch.object(
        orchestrator._planner,
        "plan",
        new_callable=AsyncMock,
        return_value=[],
    ):
        with patch.object(
            orchestrator._context_engine,
            "build",
            new_callable=AsyncMock,
            return_value={},
        ):
            with patch(
                "reviewforge.engine.orchestrator.scan_changed_files",
                new_callable=AsyncMock,
                return_value=MagicMock(
                    findings=[],
                    files_scanned=0,
                    file_errors={},
                    scanner_errors={},
                ),
            ):
                with patch.object(
                    orchestrator,
                    "_run_publication_gate",
                    new_callable=AsyncMock,
                    return_value=failed_stats,
                ):
                    with patch.object(
                        orchestrator,
                        "_post_comments",
                        new_callable=AsyncMock,
                    ) as post_comments:
                        summary = await orchestrator.run(state)

    assert summary["status"] == "partial"
    assert summary["retryable"] is True
    post_comments.assert_not_awaited()
    runs = await database.get_runs(repo="owner/repo")
    assert runs[0]["status"] == "failed"
    await database.close()
