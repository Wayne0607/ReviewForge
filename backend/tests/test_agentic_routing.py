"""Tests for #1: agentic tool loop is the default for all reviewers (allowlist overrides)."""

from reviewforge.core.events import EventBus
from reviewforge.core.specs import build_registry
from reviewforge.engine.mock_llm import MockChatLLM
from reviewforge.engine.orchestrator import Orchestrator
from reviewforge.tools.gateway import ToolGateway
from reviewforge.tools.mock_github import MockGitHubClient


def _orch(**kw):
    reg = build_registry()
    return Orchestrator(
        registry=reg,
        gateway=ToolGateway(reg, MockGitHubClient()),
        event_bus=EventBus(),
        planner_llm=MockChatLLM(),
        reviewer_llm=MockChatLLM(),
        calibrator_llm=MockChatLLM(),
        db=None,
        **kw,
    )


def test_agentic_default_on_for_all_reviewers():
    orch = _orch(agentic_default=True)
    assert orch._create_reviewer("security_reviewer")._agentic is True
    assert orch._create_reviewer("style_reviewer")._agentic is True


def test_allowlist_overrides_default():
    orch = _orch(agentic_reviewers=["style_reviewer"], agentic_default=True)
    assert orch._create_reviewer("security_reviewer")._agentic is False
    assert orch._create_reviewer("style_reviewer")._agentic is True


def test_default_off_makes_all_single_shot():
    orch = _orch(agentic_default=False)
    assert orch._create_reviewer("security_reviewer")._agentic is False


def test_skill_attached_to_reviewer():
    # #6 integration: security reviewer gets its SKILL.md attached via the orchestrator
    orch = _orch(agentic_default=False)
    r = orch._create_reviewer("security_reviewer")
    assert r._skill_name == "security_rules"
    assert len(r._skill_body) > 50
