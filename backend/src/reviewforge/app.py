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
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from langchain_openai import ChatOpenAI

from reviewforge.api.webhook import router as webhook_router
from reviewforge.core.config import ReviewForgeConfig
from reviewforge.core.database import Database
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

        # LLM clients — multi-model routing
        if mock_mode:
            from reviewforge.engine.mock_llm import MockChatLLM
            planner_llm = MockChatLLM()
            reviewer_llm = MockChatLLM()
            verifier_llm = MockChatLLM()
            logger.info("Mock mode: using MockChatLLM")
        else:
            from reviewforge.engine.model_router import ModelRouter
            router = ModelRouter(cfg.llm)
            planner_llm = router.get_llm("planner")
            reviewer_llm = router.get_llm("reviewer")
            verifier_llm = router.get_llm("verifier")

        # Database
        db = Database(Path(cfg.events_dir).parent / "reviewforge.db")
        await db.connect()
        logger.info("Database initialized")

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
            calibrator_llm=verifier_llm,
            db=db,
        )

        # Load plugins
        from reviewforge.engine.plugin_loader import PluginLoader
        plugin_loader = PluginLoader()
        plugins_dir = Path(__file__).parent / "plugins"
        plugins = plugin_loader.discover(plugins_dir)
        if plugins:
            orchestrator.register_plugin_reviewers(plugins)
            logger.info(f"Loaded {len(plugins)} plugin(s): {list(plugins.keys())}")

        # Store on app state
        app.state.orchestrator = orchestrator
        app.state.github_client = github
        app.state.webhook_secret = cfg.github.webhook_secret
        app.state.registry = registry
        app.state.config = cfg
        app.state.db = db

        logger.info(f"ReviewForge started: model={cfg.llm.model}, reviewers={len(cfg.reviewers)}")

        yield

        await db.close()
        await github.close()

    app = FastAPI(
        title="ReviewForge",
        description="AI multi-agent code review system",
        version="0.2.0",
        lifespan=lifespan,
    )

    # CORS (for frontend dev server)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Routers
    app.include_router(webhook_router)

    # Dashboard API
    from reviewforge.api.dashboard import router as dashboard_router
    app.include_router(dashboard_router)

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

    # Serve frontend static files (if built)
    static_dir = Path(__file__).parent / "static"
    if static_dir.exists():
        app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")

    return app
