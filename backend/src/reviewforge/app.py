"""Application factory — wires everything together."""

from __future__ import annotations

import logging
import os
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
from reviewforge.core.events import EventBus
from reviewforge.core.specs import SpecRegistry, build_registry
from reviewforge.engine.orchestrator import Orchestrator
from reviewforge.tools.gateway import ToolGateway
from reviewforge.tools.github_api import GitHubClient


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Startup
        github = GitHubClient(token=os.environ["GITHUB_TOKEN"])
        registry = build_registry()

        # Validate specs
        errors = registry.validate()
        if errors:
            raise RuntimeError(f"Spec validation failed: {errors}")

        # Build LLM clients
        base_url = os.environ.get("LLM_BASE_URL", "https://token-plan-cn.xiaomimimo.com/v1")
        api_key = os.environ.get("LLM_API_KEY", "")
        model = os.environ.get("REVIEWFORGE_MODEL", "MiMo")

        planner_llm = ChatOpenAI(base_url=base_url, api_key=api_key, model=model, temperature=0)
        reviewer_llm = ChatOpenAI(base_url=base_url, api_key=api_key, model=model, temperature=0.1)
        verifier_llm = ChatOpenAI(base_url=base_url, api_key=api_key, model=model, temperature=0)

        gateway = ToolGateway(registry, github)
        event_bus = EventBus(log_path=Path(".reviewforge/events.jsonl"))

        orchestrator = Orchestrator(
            registry=registry,
            gateway=gateway,
            event_bus=event_bus,
            planner_llm=planner_llm,
            reviewer_llm=reviewer_llm,
            verifier_llm=verifier_llm,
        )

        # Store on app state
        app.state.orchestrator = orchestrator
        app.state.github_client = github
        app.state.webhook_secret = os.environ.get("GITHUB_WEBHOOK_SECRET", "")
        app.state.registry = registry

        yield

        # Shutdown
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

    return app
