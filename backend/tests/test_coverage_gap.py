"""Tests for the selective high-risk coverage-gap pass."""

from __future__ import annotations

import json

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from reviewforge.core.config import ReviewForgeConfig
from reviewforge.core.events import EventBus
from reviewforge.core.specs import build_registry
from reviewforge.core.state import Finding, ReviewTask, StateStore
from reviewforge.engine.context_engine import render_impact_manifest
from reviewforge.engine.coverage_gap import build_evidence_cards, filter_gap_findings
from reviewforge.engine.orchestrator import Orchestrator
from reviewforge.tools.gateway import ToolGateway
from reviewforge.tools.mock_github import MockGitHubClient


def _manifest() -> dict:
    return {
        "version": 2,
        "files": [
            {
                "path": "src/service.py",
                "language": "python",
                "added_lines": [10, 12, 20],
                "changed_symbols": [
                    {"name": "process", "type": "function", "line": 10, "start_line": 10, "end_line": 15}
                ],
                "imports": [],
                "calls": [{"caller": "process", "callee": "authorize", "line": 12}],
            }
        ],
        "references": [{"symbol": "process", "paths": ["src/caller.py", "tests/test_service.py"], "status": "ok"}],
        "candidate_tests": ["tests/test_service.py"],
        "historical_graph": [],
        "wiki_pages": [
            {
                "title": "process",
                "source": {"path": "src/service.py", "sha": "head"},
                "facts": [{"kind": "call", "evidence": "authorize(user)"}],
            }
        ],
        "resource_files": [],
        "risk_signals": [{"type": "blast-radius", "file": "src/service.py", "symbol": "process", "reference_count": 2}],
    }


def test_build_evidence_cards_selects_only_uncovered_high_risk_symbols():
    cards = build_evidence_cards(_manifest(), [], min_risk_score=4, max_cards=3)

    assert len(cards) == 1
    assert cards[0].symbol == "process"
    assert cards[0].risk_score == 4
    assert cards[0].added_lines == (10, 12)
    assert cards[0].candidate_tests == ("tests/test_service.py",)
    assert "historical-context" in cards[0].coverage_gaps

    covered = Finding(file="src/service.py", line=12, message="observable failure", confidence=0.8)
    assert build_evidence_cards(_manifest(), [covered], min_risk_score=4) == []


def test_filter_gap_findings_requires_card_line_confidence_and_actionability():
    card = build_evidence_cards(_manifest(), [], min_risk_score=4)[0]
    findings = [
        Finding(file=card.file, line=12, category="logic-error", message="wrong branch", confidence=0.8),
        Finding(file=card.file, line=11, category="logic-error", message="not an added line", confidence=0.9),
        Finding(file=card.file, line=12, category="missing-test", message="needs tests", confidence=0.9),
        Finding(file=card.file, line=12, category="logic-error", message="weak guess", confidence=0.5),
    ]

    accepted, rejected = filter_gap_findings(findings, [card], min_confidence=0.65)

    assert len(accepted) == 1
    assert accepted[0].reviewer == "coverage_gap_reviewer"
    assert len(rejected) == 3


def test_render_manifest_filters_coverage_cards_with_task_files():
    manifest = _manifest()
    manifest["coverage_gap"] = {
        "cards": [
            {"file": "src/service.py", "symbol": "process"},
            {"file": "src/other.py", "symbol": "other"},
        ]
    }

    rendered = json.loads(render_impact_manifest(manifest, files=["src/service.py"]))

    assert rendered["coverage_gap"]["cards"] == [{"file": "src/service.py", "symbol": "process"}]


def test_coverage_gap_config_is_explicit_and_bounded(tmp_path, monkeypatch):
    monkeypatch.delenv("REVIEWFORGE_COVERAGE_GAP_ENABLED", raising=False)
    config_path = tmp_path / "reviewforge.yaml"
    config_path.write_text(
        "coverage_gap:\n  enabled: true\n  min_risk_score: 5\n  max_cards: 2\n  min_confidence: 0.7\n",
        encoding="utf-8",
    )

    config = ReviewForgeConfig.load(config_path)

    assert config.coverage_gap_enabled is True
    assert config.coverage_gap_min_risk_score == 5
    assert config.coverage_gap_max_cards == 2
    assert config.coverage_gap_min_confidence == 0.7

    config_path.write_text("coverage_gap:\n  enabled: 'false'\n", encoding="utf-8")
    assert ReviewForgeConfig.load(config_path).coverage_gap_enabled is False


class _CoverageGapLLM(BaseChatModel):
    calls: int = 0

    class Config:
        arbitrary_types_allowed = True

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls += 1
        assert "selective coverage-gap pass" in messages[-1].content
        content = json.dumps(
            {
                "findings": [
                    {
                        "file": "src/service.py",
                        "line": 12,
                        "severity": "error",
                        "category": "logic-error",
                        "message": "The changed authorization result is ignored.",
                        "suggestion": "Return or enforce the authorization result.",
                        "confidence": 0.82,
                    }
                ]
            }
        )
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=content))])

    @property
    def _llm_type(self):
        return "coverage-gap"

    @property
    def _identifying_params(self):
        return {}


async def test_orchestrator_coverage_gap_pass_adds_bounded_candidate():
    registry = build_registry()
    llm = _CoverageGapLLM()
    events = []
    event_bus = EventBus()
    event_bus.subscribe(events.append)
    orchestrator = Orchestrator(
        registry=registry,
        gateway=ToolGateway(registry, MockGitHubClient()),
        event_bus=event_bus,
        planner_llm=llm,
        reviewer_llm=llm,
        calibrator_llm=llm,
        coverage_gap_enabled=True,
    )
    state = StateStore(
        repo="owner/repo",
        pr_number=7,
        head_sha="head",
        files_changed=["src/service.py"],
        file_diffs={"src/service.py": "@@ -9,2 +10,3 @@\n+def process(user):\n+    result = authorize(user)"},
        impact_manifest=_manifest(),
    )

    await orchestrator._run_coverage_gap_pass(state, "run-1", set())

    findings = state.list_findings(status="candidate")
    assert len(findings) == 1
    assert findings[0].reviewer == "coverage_gap_reviewer"
    assert state.impact_manifest["coverage_gap"]["selected"] == 1
    assert state.list_tasks(status="completed")[0].reviewer == "coverage_gap_reviewer"
    assert [event.event_type for event in events][-1] == "coverage_gap.completed"
    assert llm.calls == 1


async def test_coverage_gap_pass_is_not_repeated_after_resume():
    registry = build_registry()
    llm = _CoverageGapLLM()
    orchestrator = Orchestrator(
        registry=registry,
        gateway=ToolGateway(registry, MockGitHubClient()),
        event_bus=EventBus(),
        planner_llm=llm,
        reviewer_llm=llm,
        calibrator_llm=llm,
        coverage_gap_enabled=True,
    )
    state = StateStore(impact_manifest=_manifest())
    state.add_task(ReviewTask(reviewer="coverage_gap_reviewer", files=["src/service.py"], status="completed"))

    await orchestrator._run_coverage_gap_pass(state, "run-1", set())

    assert llm.calls == 0
