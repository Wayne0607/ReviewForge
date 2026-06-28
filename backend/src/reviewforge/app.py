"""Application factory — wires everything together."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

from fastapi import FastAPI
from langchain_openai import ChatOpenAI

from reviewforge.api.webhook import router as webhook_router
from reviewforge.core.config import ReviewForgeConfig
from reviewforge.core.events import EventBus
from reviewforge.core.specs import SpecRegistry, build_registry
from reviewforge.engine.orchestrator import Orchestrator
from reviewforge.tools.gateway import ToolGateway
from reviewforge.tools.github_api import GitHubClient


def create_app(config_path: str | None = None) -> FastAPI:
    """Create and configure the FastAPI application."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        import os

        # Load config
        cfg = ReviewForgeConfig.load(config_path)
        mock_mode = os.environ.get("REVIEWFORGE_MOCK") == "1"

        # GitHub client
        if mock_mode:
            from reviewforge.tools.mock_github import MockGitHubClient
            github = MockGitHubClient()
            logger.info("Mock mode: using MockGitHubClient")
        else:
            github = GitHubClient(token=cfg.github.token)

        # Spec registry
        registry = build_registry()
        errors = registry.validate()
        if errors:
            raise RuntimeError(f"Spec validation failed: {errors}")

        # LLM clients
        if mock_mode:
            from reviewforge.engine.mock_llm import MockChatLLM
            planner_llm = MockChatLLM()
            reviewer_llm = MockChatLLM()
            verifier_llm = MockChatLLM()
            logger.info("Mock mode: using MockChatLLM")
        else:
            planner_llm = ChatOpenAI(
                base_url=cfg.llm.base_url, api_key=cfg.llm.api_key,
                model=cfg.llm.model, temperature=cfg.llm.temperature_planner,
            )
            reviewer_llm = ChatOpenAI(
                base_url=cfg.llm.base_url, api_key=cfg.llm.api_key,
                model=cfg.llm.model, temperature=cfg.llm.temperature_reviewer,
            )
            verifier_llm = ChatOpenAI(
                base_url=cfg.llm.base_url, api_key=cfg.llm.api_key,
                model=cfg.llm.model, temperature=cfg.llm.temperature_verifier,
            )

        # Event bus
        event_bus = EventBus(log_dir=Path(cfg.events_dir))

        # Tool gateway
        gateway = ToolGateway(registry, github)

        # Orchestrator
        orchestrator = Orchestrator(
            registry=registry,
            gateway=gateway,
            event_bus=event_bus,
            planner_llm=planner_llm,
            reviewer_llm=reviewer_llm,
            calibrator_llm=verifier_llm,  # calibrator uses the same LLM as verifier
        )

        # Store on app state
        app.state.orchestrator = orchestrator
        app.state.github_client = github
        app.state.webhook_secret = cfg.github.webhook_secret
        app.state.registry = registry
        app.state.config = cfg

        logger.info(f"ReviewForge started: model={cfg.llm.model}, reviewers={len(cfg.reviewers)}")

        yield

        await github.close()

    app = FastAPI(
        title="ReviewForge",
        description="AI multi-agent code review system",
        version="0.1.0",
        lifespan=lifespan,
    )

    app.include_router(webhook_router)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/api/v1/specs")
    async def get_specs():
        registry: SpecRegistry = app.state.registry
        return {
            "agents": {k: {"role": v.role, "description": v.description} for k, v in registry.agents.items()},
            "tools": {k: {"description": v.description} for k, v in registry.tools.items()},
            "skills": list(registry.skills),
        }

    @app.get("/api/v1/config")
    async def get_config():
        cfg: ReviewForgeConfig = app.state.config
        return {
            "llm": {"model": cfg.llm.model, "base_url": cfg.llm.base_url},
            "reviewers": [{"name": r.name, "type": r.type, "enabled": r.enabled} for r in cfg.reviewers],
            "confidence_threshold": cfg.confidence_threshold,
        }

    return app
