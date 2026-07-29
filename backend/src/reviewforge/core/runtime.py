"""Build and atomically swap the model-dependent ReviewForge runtime."""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Any

from reviewforge.core.config import LLMConfig, ReviewForgeConfig
from reviewforge.core.specs import SpecRegistry, build_registry
from reviewforge.engine.orchestrator import Orchestrator
from reviewforge.engine.publication_policy import PublicationPolicy, PublicationPolicyConfig
from reviewforge.tools.gateway import ToolGateway

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RuntimeBundle:
    registry: SpecRegistry
    gateway: ToolGateway
    orchestrator: Orchestrator


# Per-agent → role map.  Mirrors the 5-role model in model_router.ROLE_MAP
# so the runtime manager hands the orchestrator the right LLM for every
# internal agent (planner, reviewer families, verifier/calibrator/cross-PR,
# evidence prover/refuter/arbiter, and the publication gate).
_ROLE_AGENT_NAMES: dict[str, str] = {
    "planner": "planner",
    "verifier": "verifier",
    "calibrator": "calibrator",
    "cross_pr_analyzer": "cross_pr_analyzer",
    "evidence_prover": "evidence_prover",
    "evidence_refuter": "evidence_refuter",
    "evidence_arbiter": "evidence_arbiter",
    "publication_gate": "publication_gate",
    "fast_review": "performance_reviewer",
    "deep_review": "correctness_reviewer",
}


class LLMRuntimeManager:
    """Creates isolated runtime snapshots so active reviews remain untouched."""

    def __init__(
        self,
        *,
        config: ReviewForgeConfig,
        github: Any,
        event_bus: Any,
        db: Any,
        custom_agent_store: Any,
        mock_mode: bool,
    ) -> None:
        self.config = config
        self.github = github
        self.event_bus = event_bus
        self.db = db
        self.custom_agent_store = custom_agent_store
        self.mock_mode = mock_mode
        self.lock = asyncio.Lock()

    def build(self, llm_config: LLMConfig) -> RuntimeBundle:
        """Build a complete detached runtime; this does not change live state."""
        registry = build_registry()
        errors = registry.validate()
        if errors:
            raise RuntimeError(f"Spec validation failed: {errors}")

        model_router = None
        if self.mock_mode:
            from reviewforge.engine.mock_llm import MockChatLLM

            planner_llm = MockChatLLM()
            reviewer_llm = MockChatLLM()
            verifier_llm = MockChatLLM()
            publication_gate_llm = MockChatLLM()
        else:
            from reviewforge.engine.model_router import ModelRouter

            model_router = ModelRouter(llm_config)
            # Source a dedicated LLM for each internal agent from the
            # model_router so role overrides propagate automatically.  The
            # 5 fixed roles (planner / fast_review / deep_review / verifier
            # / publication_gate) get their own LLM, distinct from the
            # generic reviewer_llm used by the broad pass.
            planner_llm = model_router.get_llm("planner")
            reviewer_llm = model_router.get_llm("performance_reviewer")  # fast_review family
            verifier_llm = model_router.get_llm("verifier")
            publication_gate_llm = model_router.get_llm("publication_gate")
            # Invalidate any cached LLMs from a prior build so the new
            # build does not reuse stale instances after a hot-swap.
            model_router.invalidate_cache()

        gateway = ToolGateway(registry, self.github)
        cfg = self.config
        policy_cfg = PublicationPolicyConfig(
            enabled=cfg.publication_policy.enabled,
            mode=cfg.publication_policy.mode,
            budget_enabled=cfg.publication_policy.budget_enabled,
            max_comments=cfg.publication_policy.max_comments,
            high_risk_overflow=cfg.publication_policy.high_risk_overflow,
        )
        orchestrator = Orchestrator(
            registry=registry,
            gateway=gateway,
            event_bus=self.event_bus,
            planner_llm=planner_llm,
            reviewer_llm=reviewer_llm,
            calibrator_llm=verifier_llm,
            db=self.db,
            cross_pr_llm=verifier_llm if not self.mock_mode else None,
            github_client=self.github,
            model_router=model_router,
            agentic_reviewers=cfg.agentic_reviewers,
            agentic_default=cfg.agentic_default,
            escalation_enabled=cfg.escalation_enabled,
            escalation_confidence_min=cfg.escalation_confidence_min,
            escalation_confidence_max=cfg.escalation_confidence_max,
            escalation_max_steps=cfg.escalation_max_steps,
            escalation_max_tokens=cfg.escalation_max_tokens,
            escalation_llm=verifier_llm,
            publication_gate_enabled=cfg.publication_gate_enabled,
            publication_gate_max_steps=cfg.publication_gate_max_steps,
            publication_gate_max_tokens=cfg.publication_gate_max_tokens,
            publication_gate_concurrency=cfg.publication_gate_concurrency,
            publication_gate_dedup=cfg.publication_gate_dedup,
            root_cause_extended_families=cfg.root_cause_extended_families,
            publication_triage_enabled=cfg.publication_triage_enabled,
            publication_triage_batch_size=cfg.publication_triage_batch_size,
            publication_triage_concurrency=cfg.publication_triage_concurrency,
            publication_triage_max_candidates=cfg.publication_triage_max_candidates,
            publication_triage_context_lines=cfg.publication_triage_context_lines,
            publication_triage_max_tokens=cfg.publication_triage_max_tokens,
            # Always pass the dedicated publication_gate LLM so the gate
            # never silently reuses the broad-pass reviewer_llm.
            publication_gate_llm=publication_gate_llm,
            publication_policy=PublicationPolicy(policy_cfg),
            coverage_gap_enabled=cfg.coverage_gap_enabled,
            coverage_gap_min_risk_score=cfg.coverage_gap_min_risk_score,
            coverage_gap_max_cards=cfg.coverage_gap_max_cards,
            coverage_gap_min_confidence=cfg.coverage_gap_min_confidence,
            skills_dir=cfg.skills_dir,
            v3_enabled=cfg.v3.enabled,
            v3_coverage_min_risk_score=cfg.v3.coverage_min_risk_score,
            v3_coverage_max_cells_per_round=cfg.v3.coverage_max_cells_per_round,
            v3_coverage_max_attempts=cfg.v3.coverage_max_attempts,
            v3_evidence_mode=cfg.v3.evidence_mode,
            v3_evidence_max_candidates=cfg.v3.evidence_max_candidates,
        )

        if os.environ.get("REVIEWFORGE_ENABLE_PLUGINS") == "1":
            from pathlib import Path

            from reviewforge.engine.plugin_loader import PluginLoader

            plugins = PluginLoader().discover(Path(__file__).parent.parent / "plugins")
            if plugins:
                orchestrator.register_plugin_reviewers(plugins)
                logger.warning("Loaded %d executable plugin(s): %s", len(plugins), list(plugins))

        for spec in self.custom_agent_store.list():
            if not spec.get("enabled", True):
                continue
            try:
                orchestrator.register_config_agent(
                    reviewer_type=spec["reviewer_type"],
                    description=spec.get("description", ""),
                    allowed_tools=spec.get("allowed_tools", []),
                    model_profile=spec.get("model_profile", "default"),
                    max_steps=spec.get("max_steps", 6),
                    instructions=spec.get("instructions", ""),
                )
            except Exception as exc:
                logger.warning("Failed to register custom agent %s: %s", spec.get("reviewer_type"), exc)
        return RuntimeBundle(registry=registry, gateway=gateway, orchestrator=orchestrator)

    def activate(self, app: Any, bundle: RuntimeBundle, llm_config: LLMConfig) -> None:
        """Atomically publish a prepared runtime snapshot for future reviews."""
        self.config.llm = llm_config
        app.state.registry = bundle.registry
        app.state.gateway = bundle.gateway
        app.state.orchestrator = bundle.orchestrator
