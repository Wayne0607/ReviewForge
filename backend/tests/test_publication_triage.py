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
from reviewforge.engine.orchestrator import CommentDeliveryResult, Orchestrator
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
    file: str = "app.py",
    line: int = 2,
    reviewer: str = "correctness_reviewer",
    category: str = "logic-error",
    confidence: float = 0.7,
    verified_by: str = "",
) -> Finding:
    return Finding(
        id=finding_id,
        file=file,
        line=line,
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
async def test_grounded_local_finding_can_bypass_per_finding_tool_loop():
    source = "def calculate(total, discount):\n    return total + discount\n"

    class _SourceGitHub:
        async def get_file_content(self, repo, ref, file_path):
            return source

    finding = _finding(
        "local",
        category="wrong-logic",
        confidence=0.91,
    )
    finding.line = 2
    response = json.dumps(
        {
            "verdicts": [
                {
                    "id": "local",
                    "verdict": VERDICT_CONFIRMED,
                    "confidence": 0.96,
                    "reason": "The changed calculation adds the discount.",
                    "evidence_quote": "return total + discount",
                }
            ]
        }
    )
    registry = build_registry()
    triage = PublicationTriage(
        _StaticLLM(response),
        config=PublicationTriageConfig(enabled=True),
        gateway=ToolGateway(registry, _SourceGitHub()),
    )

    verdicts, stats = await triage.classify([finding], _state())

    assert verdicts["local"].verdict == VERDICT_CONFIRMED
    assert stats.triage_confirmed == 1
    assert stats.triage_needs_tool == 0


@pytest.mark.asyncio
async def test_ungrounded_quote_cannot_bypass_per_finding_tool_loop():
    class _SourceGitHub:
        async def get_file_content(self, repo, ref, file_path):
            return "def calculate(total, discount):\n    return total - discount\n"

    response = json.dumps(
        {
            "verdicts": [
                {
                    "id": "local",
                    "verdict": VERDICT_CONFIRMED,
                    "confidence": 0.99,
                    "reason": "Claimed evidence is absent.",
                    "evidence_quote": "return total + discount",
                }
            ]
        }
    )
    registry = build_registry()
    triage = PublicationTriage(
        _StaticLLM(response),
        config=PublicationTriageConfig(enabled=True),
        gateway=ToolGateway(registry, _SourceGitHub()),
    )

    verdicts, stats = await triage.classify(
        [_finding("local", category="wrong-logic", confidence=0.99)],
        _state(),
    )

    assert verdicts["local"].verdict == VERDICT_NEEDS_TOOL
    assert stats.triage_needs_tool == 1


def test_triage_batches_keep_root_representatives_from_one_file_together():
    findings = [
        _finding("a2"),
        _finding("a1"),
        Finding(
            id="b1",
            file="other.py",
            line=1,
            message="problem",
            status="confirmed",
        ),
    ]
    findings[0].line = 20
    findings[1].line = 10

    batches = PublicationTriage._group_batches(findings, batch_size=6)

    assert [[finding.id for finding in batch] for batch in batches] == [
        ["a1", "a2"],
        ["b1"],
    ]


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
async def test_gate_dedup_filters_absorbed_findings_in_state():
    state = _state()
    lower = _finding(
        "lower",
        file="src/app.py",
        line=12,
        category="sql-injection",
        confidence=0.80,
    )
    higher = _finding(
        "higher",
        file="src/app.py",
        line=12,
        category="SQL_INJECTION",
        confidence=0.95,
    )
    for finding in (lower, higher):
        state.add_finding(finding)

    llm = _StaticLLM()
    registry = build_registry()
    orchestrator = Orchestrator(
        registry=registry,
        gateway=ToolGateway(registry, MockGitHubClient()),
        event_bus=EventBus(),
        planner_llm=llm,
        reviewer_llm=llm,
        calibrator_llm=llm,
        publication_gate_llm=llm,
        publication_gate_enabled=True,
        publication_triage_enabled=False,
        publication_gate_dedup=True,
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

    assert gate.received == ["higher"]
    assert state.get_finding("higher").status == "confirmed"
    assert state.get_finding("lower").status == "false_positive"
    assert state.get_finding("lower").verified_by == "publication-gate-dedup"
    assert stats.dedup_input == 2
    assert stats.dedup_collapsed == 1
    assert stats.dedup_output == 1


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
        # Phase 2 (perf/gate-dedup-20260729): disable dedup so the test
        # keeps separate findings for the gate to action.
        publication_gate_dedup=False,
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
        # Phase 2 (perf/gate-dedup-20260729): keep both findings so the
        # gate can verify and distinguish confirmed vs inconclusive.
        publication_gate_dedup=False,
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


def test_extended_root_cause_families_env_kill_switch(tmp_path, monkeypatch):
    config_path = tmp_path / "reviewforge.yaml"
    config_path.write_text("{}\n", encoding="utf-8")
    monkeypatch.setenv("REVIEWFORGE_ROOT_CAUSE_EXTENDED_FAMILIES", "false")

    config = ReviewForgeConfig.load(config_path)

    assert config.root_cause_extended_families is False


@pytest.mark.asyncio
async def test_retryable_publication_failure_publishes_verified_comments_and_fails_db_run(
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
                        return_value=CommentDeliveryResult(reported=1),
                    ) as post_comments:
                        summary = await orchestrator.run(state)

    assert summary["status"] == "partial"
    assert summary["retryable"] is True
    post_comments.assert_awaited_once()
    assert summary["comment_delivery"]["reported"] == 1
    runs = await database.get_runs(repo="owner/repo")
    assert runs[0]["status"] == "failed"
    await database.close()


@pytest.mark.asyncio
async def test_model_independent_evidence_bypasses_triage_and_agentic_gate():
    state = _state()
    state.add_finding(_finding("detector-proof", verified_by="detector"))
    llm = _StaticLLM(error=AssertionError("protected evidence must not call an LLM"))
    registry = build_registry()
    orchestrator = Orchestrator(
        registry=registry,
        gateway=ToolGateway(registry, MockGitHubClient()),
        event_bus=EventBus(),
        planner_llm=llm,
        reviewer_llm=llm,
        calibrator_llm=llm,
        publication_gate_llm=llm,
        publication_gate_enabled=True,
        publication_triage_enabled=True,
    )
    gate = AsyncMock()
    orchestrator._publication_gate_reviewer = gate

    stats = await orchestrator._run_publication_gate(state)

    protected = state.get_finding("detector-proof")
    assert protected.status == "confirmed"
    assert protected.verified_by == "detector"
    assert stats.evidence_bypassed == 1
    assert stats.dedup_input == 1
    assert stats.dedup_output == 1
    assert llm.calls == []
    gate.escalate_batch.assert_not_awaited()


@pytest.mark.asyncio
async def test_reviewer_consensus_prioritizes_but_does_not_bypass_agentic_gate():
    state = _state()
    consensus = _finding(
        "consensus-ssrf",
        category="ssrf",
        reviewer="security_reviewer",
        confidence=0.98,
    )
    consensus.message = "Unvalidated callback URL enables SSRF"
    state.add_finding(consensus)
    state.impact_manifest["publication_evidence"] = {
        "consensus_ids": [consensus.id],
    }
    llm = _StaticLLM(error=AssertionError("consensus must go directly to the tool gate"))
    registry = build_registry()
    orchestrator = Orchestrator(
        registry=registry,
        gateway=ToolGateway(registry, MockGitHubClient()),
        event_bus=EventBus(),
        planner_llm=llm,
        reviewer_llm=llm,
        calibrator_llm=llm,
        publication_gate_llm=llm,
        publication_gate_enabled=True,
        publication_triage_enabled=True,
        publication_gate_dedup=False,
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

    assert gate.received == ["consensus-ssrf"]
    assert state.get_finding("consensus-ssrf").verified_by == "publication-gate"
    assert stats.evidence_bypassed == 0
    assert stats.consensus_routed == 1
    assert stats.triage_batches == 0
    assert stats.agentic_attempted == 1
    assert stats.agentic_confirmed == 1
    assert llm.calls == []


@pytest.mark.asyncio
async def test_evidence_dedup_requires_the_same_defect_mechanism():
    state = StateStore(
        pr_number=1,
        repo="owner/repo",
        head_sha="head",
        files_changed=["app.py"],
        diff_summary=(
            "--- app.py\n"
            "@@ -0,0 +1,8 @@\n"
            "+async def dispatch(user_id, payload, target_channels):\n"
            "+    prefs = load_user_preferences(user_id)\n"
            "+    blob = _read_cached_payload(user_id)\n"
            "+    loop = asyncio.get_event_loop()\n"
            "+    return await loop.run_in_executor(\n"
            "+        None, dispatch_review_completed, user_id, payload, target_channels\n"
            "+    )\n"
            "+\n"
        ),
        file_diffs={
            "app.py": (
                "@@ -0,0 +1,8 @@\n"
                "+async def dispatch(user_id, payload, target_channels):\n"
                "+    prefs = load_user_preferences(user_id)\n"
                "+    blob = _read_cached_payload(user_id)\n"
                "+    loop = asyncio.get_event_loop()\n"
                "+    return await loop.run_in_executor(\n"
                "+        None, dispatch_review_completed, user_id, payload, target_channels\n"
                "+    )\n"
                "+\n"
            )
        },
    )
    cache = _finding(
        "cache-blocking",
        line=3,
        category="event-loop-blocking",
    )
    cache.message = "_read_cached_payload performs blocking cache I/O in the async event loop"
    executor = _finding(
        "executor-contract",
        line=6,
        category="wrong-return-contract",
    )
    executor.message = (
        "The blocking _read_cached_payload helper is nearby, but this defect is "
        "dispatch_review_completed receiving target_channels positionally even though "
        "the parameter is keyword-only, causing TypeError"
    )
    state.add_finding(cache)
    state.add_finding(executor)
    llm = _StaticLLM()
    registry = build_registry()
    orchestrator = Orchestrator(
        registry=registry,
        gateway=ToolGateway(registry, MockGitHubClient()),
        event_bus=EventBus(),
        planner_llm=llm,
        reviewer_llm=llm,
        calibrator_llm=llm,
        publication_gate_llm=llm,
        publication_gate_enabled=True,
        publication_triage_enabled=False,
        publication_gate_dedup=False,
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

    assert state.get_finding("cache-blocking").verified_by == "publication-evidence"
    assert state.get_finding("executor-contract").verified_by == "publication-gate"
    assert gate.received == ["executor-contract"]
    assert stats.evidence_bypassed == 1
    assert stats.evidence_collapsed == 0
    assert stats.agentic_attempted == 1


@pytest.mark.asyncio
async def test_changed_source_evidence_collapses_same_proof_before_publication():
    state = StateStore(
        pr_number=1,
        repo="owner/repo",
        head_sha="head",
        files_changed=["app.py"],
        diff_summary=(
            "--- app.py\n"
            "@@ -0,0 +1,2 @@\n"
            "+def hash_password(password: str) -> str:\n"
            "+    return hashlib.md5(password.encode()).hexdigest()\n"
        ),
        file_diffs={
            "app.py": (
                "@@ -0,0 +1,2 @@\n"
                "+def hash_password(password: str) -> str:\n"
                "+    return hashlib.md5(password.encode()).hexdigest()\n"
            )
        },
    )
    weak_one = _finding(
        "weak-one",
        line=2,
        category="weak-password-hashing",
    )
    weak_one.message = "hash_password uses unsalted MD5 for password storage"
    state.add_finding(weak_one)
    weak_two = _finding(
        "weak-two",
        line=2,
        category="crypto",
    )
    weak_two.message = "password hashing relies on MD5"
    state.add_finding(weak_two)
    llm = _StaticLLM(error=AssertionError("protected evidence must not call an LLM"))
    registry = build_registry()
    orchestrator = Orchestrator(
        registry=registry,
        gateway=ToolGateway(registry, MockGitHubClient()),
        event_bus=EventBus(),
        planner_llm=llm,
        reviewer_llm=llm,
        calibrator_llm=llm,
        publication_gate_llm=llm,
        publication_gate_enabled=True,
        publication_triage_enabled=True,
    )
    gate = AsyncMock()
    orchestrator._publication_gate_reviewer = gate

    stats = await orchestrator._run_publication_gate(state)

    assert len(state.list_findings(status="confirmed")) == 1
    duplicate = state.list_findings(status="false_positive")
    assert len(duplicate) == 1
    assert duplicate[0].verified_by == "publication-evidence-duplicate"
    assert stats.evidence_bypassed == 1
    assert stats.evidence_collapsed == 1
    assert gate.escalate_batch.await_count == 0


@pytest.mark.asyncio
async def test_resume_after_gate_provider_error_skips_all_expensive_stages(tmp_path):
    database = Database(tmp_path / "reviewforge.db")
    await database.connect()
    run_id = "publication-retry"
    await database.create_run(
        run_id=run_id,
        repo="owner/repo",
        pr_number=1,
        head_sha="head",
    )
    retry_finding = _finding("retry-only")
    retry_finding.status = "candidate"
    retry_finding.verified_by = "publication-gate-provider-error"
    await database.insert_finding(run_id, retry_finding.to_dict())
    await database.fail_run(run_id, "publication provider rate limited")

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
    resumed_stats = TriageStats(agentic_confirmed=1)

    with (
        patch.object(orchestrator._planner, "plan", new_callable=AsyncMock) as planner,
        patch.object(orchestrator._context_engine, "build", new_callable=AsyncMock) as context,
        patch(
            "reviewforge.engine.orchestrator.scan_changed_files",
            new_callable=AsyncMock,
        ) as phase0,
        patch.object(
            orchestrator,
            "_run_publication_gate",
            new_callable=AsyncMock,
            return_value=resumed_stats,
        ) as publication_gate,
        patch.object(
            orchestrator,
            "_post_comments",
            new_callable=AsyncMock,
            return_value=CommentDeliveryResult(reported=1),
        ) as post_comments,
    ):
        summary = await orchestrator.run(state)

    planner.assert_not_awaited()
    context.assert_not_awaited()
    phase0.assert_not_awaited()
    publication_gate.assert_awaited_once_with(state, candidate_ids={"retry-only"})
    post_comments.assert_awaited_once()
    assert summary["resume"] == {
        "mode": "publication-only",
        "retried_findings": 1,
    }
    assert summary.get("retryable") is None
    runs = await database.get_runs(repo="owner/repo")
    assert runs[0]["status"] == "completed"
    await database.close()
