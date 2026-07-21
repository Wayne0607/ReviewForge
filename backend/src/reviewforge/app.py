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
from reviewforge.core.database import Database
from reviewforge.core.events import EventBus
from reviewforge.core.specs import SpecRegistry, build_registry
from reviewforge.engine.orchestrator import Orchestrator
from reviewforge.tools.gateway import ToolGateway
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

        # Spec registry
        registry = build_registry()
        errors = registry.validate()
        if errors:
            raise RuntimeError(f"Spec validation failed: {errors}")

        # LLM clients — D6: multi-model routing
        model_router = None
        if mock_mode:
            from reviewforge.engine.mock_llm import MockChatLLM

            planner_llm = MockChatLLM()
            reviewer_llm = MockChatLLM()
            verifier_llm = MockChatLLM()
            logger.info("Mock mode: using MockChatLLM")
        else:
            from reviewforge.engine.model_router import ModelRouter

            model_router = ModelRouter(cfg.llm)
            planner_llm = model_router.get_llm("planner")
            reviewer_llm = model_router.get_llm("reviewer")
            verifier_llm = model_router.get_llm("verifier")

        # Database
        db = Database(Path(cfg.events_dir).parent / "reviewforge.db")
        await db.connect()
        orphaned = await db.fail_running_runs("orphaned by service restart")
        if orphaned:
            logger.warning(f"Marked {orphaned} orphaned running review(s) as failed")
        logger.info("Database initialized")

        # Event bus
        event_bus = EventBus(log_dir=Path(cfg.events_dir))

        # Tool gateway
        gateway = ToolGateway(registry, github)

        # Cross-PR analyzer LLM (reuses verifier model)
        cross_pr_llm = verifier_llm if not mock_mode else None

        # Orchestrator
        orchestrator = Orchestrator(
            registry=registry,
            gateway=gateway,
            event_bus=event_bus,
            planner_llm=planner_llm,
            reviewer_llm=reviewer_llm,
            calibrator_llm=verifier_llm,
            db=db,
            cross_pr_llm=cross_pr_llm,
            github_client=github,
            model_router=model_router,
            agentic_reviewers=cfg.agentic_reviewers,
            agentic_default=cfg.agentic_default,
            escalation_enabled=cfg.escalation_enabled,
            escalation_confidence_min=cfg.escalation_confidence_min,
            escalation_confidence_max=cfg.escalation_confidence_max,
            escalation_max_steps=cfg.escalation_max_steps,
            escalation_max_tokens=cfg.escalation_max_tokens,
            coverage_gap_enabled=cfg.coverage_gap_enabled,
            coverage_gap_min_risk_score=cfg.coverage_gap_min_risk_score,
            coverage_gap_max_cards=cfg.coverage_gap_max_cards,
            coverage_gap_min_confidence=cfg.coverage_gap_min_confidence,
            skills_dir=cfg.skills_dir,
        )

        # S4: 插件默认关闭，靠显式 env 开启
        if os.environ.get("REVIEWFORGE_ENABLE_PLUGINS") == "1":
            from reviewforge.engine.plugin_loader import PluginLoader

            plugin_loader = PluginLoader()
            plugins_dir = Path(__file__).parent / "plugins"
            plugins = plugin_loader.discover(plugins_dir)
            if plugins:
                orchestrator.register_plugin_reviewers(plugins)
                logger.warning(f"⚠️ 已加载 {len(plugins)} 个插件（执行任意代码）: {list(plugins.keys())}")
        else:
            logger.info("插件加载已禁用（设 REVIEWFORGE_ENABLE_PLUGINS=1 开启）")

        # Console-driven Skills + config-type Agents (CRUD via /api/v1/admin, hot-reloaded)
        from reviewforge.core.custom_store import CustomAgentStore, SkillStore

        app.state.skill_store = SkillStore(orchestrator.skills_dir)
        custom_agent_store = CustomAgentStore(Path(cfg.events_dir).parent / "custom_agents.json")
        loaded_agents = 0
        for spec in custom_agent_store.list():
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
                loaded_agents += 1
            except Exception as e:
                logger.warning(f"Failed to register custom agent {spec.get('reviewer_type')}: {e}")
        app.state.custom_agent_store = custom_agent_store
        if loaded_agents:
            logger.info(f"Loaded {loaded_agents} custom config-type agent(s)")

        # S7: 并发控制
        app.state.review_tasks = set()
        app.state.review_semaphore = asyncio.Semaphore(int(os.environ.get("REVIEWFORGE_MAX_CONCURRENT_REVIEWS", "3")))

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
