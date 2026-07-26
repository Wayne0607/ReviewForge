"""Application factory — wires everything together."""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from reviewforge.api.webhook import router as webhook_router
from reviewforge.core.auth import require_token
from reviewforge.core.config import ReviewForgeConfig
from reviewforge.core.custom_store import CustomAgentStore, SkillStore
from reviewforge.core.database import Database
from reviewforge.core.events import EventBus
from reviewforge.core.llm_settings import EncryptedLLMSettingsStore, LLMSettingsError, apply_override
from reviewforge.core.runtime import LLMRuntimeManager
from reviewforge.core.specs import SpecRegistry
from reviewforge.tools.github_api import GitHubClient


def _is_sensitive_fallback_path(path: str) -> bool:
    parts = [part for part in path.split("/") if part]
    sensitive_names = {".env", ".git", ".svn", "wp-config.php"}
    return any(part.startswith(".") or part in sensitive_names for part in parts)


def create_app(config_path: str | None = None) -> FastAPI:
    """Create and configure the FastAPI application."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Load config
        cfg = ReviewForgeConfig.load(config_path)
        mock_mode = os.environ.get("REVIEWFORGE_MOCK") == "1"
        bootstrap_llm_config = apply_override(cfg.llm, None)
        runtime_dir = Path(cfg.events_dir).parent
        llm_settings_store = EncryptedLLMSettingsStore(runtime_dir)
        try:
            stored_llm_override = llm_settings_store.load()
        except LLMSettingsError as exc:
            stored_llm_override = None
            logger.error("Ignoring unreadable console LLM settings: %s", exc)
        cfg.llm = apply_override(bootstrap_llm_config, stored_llm_override)

        # S1: 非 mock 模式下 webhook_secret 必填
        if not mock_mode and not cfg.github.webhook_secret:
            raise RuntimeError("GITHUB_WEBHOOK_SECRET 必填（本地测试请用 REVIEWFORGE_MOCK=1）")

        # S2: 非 mock 模式下 API token 必填
        if not mock_mode and not os.environ.get("REVIEWFORGE_API_TOKEN"):
            raise RuntimeError("REVIEWFORGE_API_TOKEN 必填（本地测试请用 REVIEWFORGE_MOCK=1）")

        # GitHub client
        if mock_mode:
            from reviewforge.tools.mock_github import MockGitHubClient

            github = MockGitHubClient()
            logger.info("Mock mode: using MockGitHubClient")
        else:
            github = GitHubClient(token=cfg.github.token)

        # Database
        db = Database(runtime_dir / "reviewforge.db")
        await db.connect()
        orphaned = await db.fail_running_runs("orphaned by service restart")
        if orphaned:
            logger.warning(f"Marked {orphaned} orphaned running review(s) as failed")
        logger.info("Database initialized")

        # Event bus
        event_bus = EventBus(log_dir=Path(cfg.events_dir))

        # Console-driven settings/agents are outside the Git tree and survive deploys.
        custom_agent_store = CustomAgentStore(runtime_dir / "custom_agents.json")
        runtime_manager = LLMRuntimeManager(
            config=cfg,
            github=github,
            event_bus=event_bus,
            db=db,
            custom_agent_store=custom_agent_store,
            mock_mode=mock_mode,
        )
        bundle = runtime_manager.build(cfg.llm)
        runtime_manager.activate(app, bundle, cfg.llm)

        app.state.skill_store = SkillStore(bundle.orchestrator.skills_dir)
        app.state.custom_agent_store = custom_agent_store
        app.state.llm_runtime_manager = runtime_manager
        app.state.llm_settings_store = llm_settings_store
        app.state.llm_settings_source = "console" if stored_llm_override else "startup"
        app.state.bootstrap_llm_config = bootstrap_llm_config
        app.state.mock_mode = mock_mode

        # S7: 并发控制
        app.state.review_tasks = set()
        app.state.review_semaphore = asyncio.Semaphore(int(os.environ.get("REVIEWFORGE_MAX_CONCURRENT_REVIEWS", "3")))

        # Store on app state
        app.state.github_client = github
        app.state.webhook_secret = cfg.github.webhook_secret
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

    # S3: 收紧 CORS
    cors_origins = os.environ.get("REVIEWFORGE_CORS_ORIGINS", "http://localhost:5173").split(",")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in cors_origins if o.strip()],
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )

    # Routers
    app.include_router(webhook_router)

    # Dashboard API (S2: 需要 token)
    from reviewforge.api.dashboard import router as dashboard_router

    app.include_router(dashboard_router, dependencies=[Depends(require_token)])

    # Admin API (console-driven Skill/Agent CRUD; S2: 需要 token)
    from reviewforge.api.admin import router as admin_router

    app.include_router(admin_router, dependencies=[Depends(require_token)])

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/api/v1/specs", dependencies=[Depends(require_token)])
    async def get_specs():
        registry: SpecRegistry = app.state.registry
        orchestrator = getattr(app.state, "orchestrator", None)
        if orchestrator is not None:
            orchestrator.reload_skills()
            skills = [m.name for m in orchestrator._skill_loader.list_all()]
        else:
            skills = list(registry.skills)
        return {
            "agents": {k: {"role": v.role, "description": v.description} for k, v in registry.agents.items()},
            "tools": {k: {"description": v.description} for k, v in registry.tools.items()},
            "skills": skills,
        }

    @app.get("/api/v1/config", dependencies=[Depends(require_token)])
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
        from fastapi.responses import FileResponse, JSONResponse

        app.mount("/assets", StaticFiles(directory=str(static_dir / "assets")), name="static-assets")

        @app.exception_handler(404)
        async def spa_fallback(request, exc):
            path = request.url.path
            if path.startswith("/api/") or path.startswith("/webhook") or path.startswith("/health"):
                return JSONResponse(status_code=404, content={"detail": "Not Found"})
            if _is_sensitive_fallback_path(path):
                return JSONResponse(status_code=404, content={"detail": "Not Found"})
            index_path = static_dir / "index.html"
            if index_path.exists():
                return FileResponse(str(index_path))
            return JSONResponse(status_code=404, content={"detail": "Not Found"})

    return app
