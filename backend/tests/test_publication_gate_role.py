"""Verify PublicationGateReviewer uses the publication_gate role LLM.

The orchestrator must receive a dedicated ``publication_gate_llm`` and
must not silently fall back to the broad-pass ``reviewer_llm`` when
the role override is configured.  This test exercises the wiring without
modifying calibrator/cross_pr/token_tracker.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from reviewforge.core.events import EventBus
from reviewforge.core.specs import build_registry
from reviewforge.engine.model_router import ModelRouter
from reviewforge.engine.orchestrator import Orchestrator
from reviewforge.engine.publication_policy import PublicationPolicy
from reviewforge.tools.gateway import ToolGateway
from reviewforge.tools.mock_github import MockGitHubClient


@dataclass
class StubLLM:
    """A stand-in LLM whose ``.invoke`` records the call site."""

    label: str
    calls: list[str] = field(default_factory=list)

    async def ainvoke(self, *args, **kwargs):  # noqa: D401
        self.calls.append(self.label)
        return _StubMessage()


@dataclass
class _StubMessage:
    content: str = "{}"


def _build_orchestrator(*, gate_llm, reviewer_llm=None):
    """Build an Orchestrator with a stub planner/reviewer/calibrator + the gate_llm under test."""
    reg = build_registry()
    planner = StubLLM("planner")
    reviewer = reviewer_llm or StubLLM("reviewer")
    calibrator = StubLLM("calibrator")
    return (
        reg,
        Orchestrator(
            registry=reg,
            gateway=ToolGateway(reg, MockGitHubClient()),
            event_bus=EventBus(),
            planner_llm=planner,
            reviewer_llm=reviewer,
            calibrator_llm=calibrator,
            publication_gate_enabled=True,
            publication_gate_llm=gate_llm,
            publication_policy=PublicationPolicy(_StubPolicy()),
        ),
    )


@dataclass
class _StubPolicyCfg:
    enabled: bool = False
    mode: str = "off"
    max_comments: int = 4
    high_risk_overflow: int = 1


class _StubPolicy:
    def __init__(self):
        self._cfg = _StubPolicyCfg()

    @property
    def enabled(self) -> bool:
        return False

    @property
    def mode(self) -> str:
        return "off"

    def pre_filter(self, *args, **kwargs):
        from reviewforge.engine.publication_policy import PolicyDecision

        return PolicyDecision(kept=[], dropped=[], metrics={})


def test_orchestrator_accepts_independent_publication_gate_llm():
    """publication_gate_llm is stored and used to build the gate reviewer."""
    gate = StubLLM("gate")
    reg, orch = _build_orchestrator(gate_llm=gate)
    # The orchestrator's stored publication_gate_llm_raw should reference our stub.
    assert orch._publication_gate_llm_raw is gate
    # Default fallback when not provided must be the reviewer_llm.
    reg2 = build_registry()
    fallback_reviewer = StubLLM("reviewer")
    orch_fallback = Orchestrator(
        registry=reg2,
        gateway=ToolGateway(reg2, MockGitHubClient()),
        event_bus=EventBus(),
        planner_llm=StubLLM("p"),
        reviewer_llm=fallback_reviewer,
        calibrator_llm=StubLLM("c"),
        publication_gate_enabled=True,
    )
    assert orch_fallback._publication_gate_llm_raw is fallback_reviewer


def test_model_router_routes_publication_gate_to_role():
    """publication_gate must resolve via its own role, not reviewer_llm."""
    from reviewforge.core.config import LLMConfig, ModelProfile, RoleOverride

    cfg = LLMConfig(
        base_url="https://api.example.com/v1",
        api_key="sk",
        model="g",
        profiles={
            "fast": ModelProfile(model="fast", temperature=0.1, max_tokens=4096),
            "accurate": ModelProfile(model="accurate", temperature=0.0, max_tokens=8192),
        },
        role_overrides={
            "publication_gate": RoleOverride(
                base_url="https://gate.example.com/v1",
                api_key="sk-gate",
                model="gate",
            ),
            "fast_review": RoleOverride(model="fast-v2"),
        },
    )
    router = ModelRouter(cfg)
    # Publication gate has a dedicated role override
    assert router.effective("publication_gate")["model"] == "gate"
    # Reviewer-role agents use the fast_review override, distinct from the gate
    assert router.effective("performance_reviewer")["model"] == "fast-v2"
    # Deep reviewers without an override fall through to accurate profile.
    cfg_no_override = LLMConfig(
        base_url="https://api.example.com/v1",
        api_key="sk",
        model="g",
        profiles={
            "fast": ModelProfile(model="fast", temperature=0.1, max_tokens=4096),
            "accurate": ModelProfile(model="accurate", temperature=0.0, max_tokens=8192),
        },
    )
    assert ModelRouter(cfg_no_override).effective("security_reviewer")["model"] == "accurate"


def test_orchestrator_accepts_independent_escalation_llm():
    reg = build_registry()
    reviewer = StubLLM("reviewer")
    verifier = StubLLM("verifier")
    orchestrator = Orchestrator(
        registry=reg,
        gateway=ToolGateway(reg, MockGitHubClient()),
        event_bus=EventBus(),
        planner_llm=StubLLM("planner"),
        reviewer_llm=reviewer,
        calibrator_llm=verifier,
        escalation_llm=verifier,
    )
    assert orchestrator._escalation_llm_raw is verifier
    assert orchestrator._escalation_llm_raw is not reviewer
