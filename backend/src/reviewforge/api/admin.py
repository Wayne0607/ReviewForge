"""Admin API — console-driven CRUD for Skills and config-type Agents.

Mounted behind require_token (same as the dashboard). Mutations use POST (the app's
CORS allow_methods is GET/POST/OPTIONS only). Every change hot-reloads the live
orchestrator, so new Skills/Agents take effect on the NEXT review without a restart.

Config-type agents are pure declarative data — no code is uploaded or executed.
Reviewers needing custom Python still go through the gated file-plugin path.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from reviewforge.core.custom_store import ValidationError, normalize_agent
from reviewforge.core.llm_settings import (
    LLMSettingsError,
    apply_override,
    make_override,
    safe_settings,
    validate_endpoint_security,
    validate_llm_profiles,
)

router = APIRouter(prefix="/api/v1/admin")

# Built-in skills shipped with the package — manage via files, not the console.
BUILTIN_SKILLS = {
    "angular_patterns",
    "code_quality",
    "go_best_practices",
    "java_best_practices",
    "python_best_practices",
    "react_patterns",
    "ruby_best_practices",
    "rust_best_practices",
    "security_rules",
    "svelte_patterns",
    "testing_rules",
    "vue_patterns",
}


class SkillPayload(BaseModel):
    name: str
    description: str = ""
    reviewer_type: str = ""
    category: str = ""
    body: str
    languages: list[str] = Field(default_factory=list)
    frameworks: list[str] = Field(default_factory=list)
    references: list[str] = Field(default_factory=list)


class AgentPayload(BaseModel):
    reviewer_type: str
    description: str
    allowed_tools: list[str] | None = None
    model_profile: str = "default"
    max_steps: int = 6
    instructions: str = ""
    enabled: bool = True


class LLMSettingsPayload(BaseModel):
    base_url: str = Field(min_length=1, max_length=2048)
    model: str = Field(min_length=1, max_length=200)
    api_key: str | None = Field(default=None, max_length=4096)
    fast_model: str = Field(default="", max_length=200)
    accurate_model: str = Field(default="", max_length=200)


def _orch(request: Request):
    orch = getattr(request.app.state, "orchestrator", None)
    if orch is None:
        raise HTTPException(503, "orchestrator not ready")
    return orch


def _llm_candidate(payload: LLMSettingsPayload, request: Request):
    current = request.app.state.config.llm
    override = make_override(
        current,
        base_url=payload.base_url,
        model=payload.model,
        api_key=payload.api_key,
        fast_model=payload.fast_model,
        accurate_model=payload.accurate_model,
    )
    candidate = apply_override(request.app.state.bootstrap_llm_config, override)
    return override, candidate


async def _test_candidate(candidate, request: Request) -> dict[str, Any]:
    if getattr(request.app.state, "mock_mode", False):
        await asyncio.to_thread(validate_endpoint_security, candidate.base_url)
        return {
            "ok": True,
            "latency_ms": 0,
            "model": candidate.model,
            "tested_models": sorted(
                {
                    candidate.model,
                    candidate.profiles["fast"].model or candidate.model,
                    candidate.profiles["accurate"].model or candidate.model,
                }
            ),
        }
    return await validate_llm_profiles(candidate)


# ── LLM settings ─────────────────────────────────────────────


@router.get("/llm-settings")
async def get_llm_settings(request: Request) -> dict[str, Any]:
    return safe_settings(
        request.app.state.config.llm,
        getattr(request.app.state, "llm_settings_source", "startup"),
    )


@router.post("/llm-settings/test")
async def test_llm_settings(payload: LLMSettingsPayload, request: Request) -> dict[str, Any]:
    try:
        _, candidate = _llm_candidate(payload, request)
        return await _test_candidate(candidate, request)
    except LLMSettingsError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/llm-settings")
async def save_llm_settings(payload: LLMSettingsPayload, request: Request) -> dict[str, Any]:
    manager = request.app.state.llm_runtime_manager
    try:
        async with manager.lock:
            override, candidate = _llm_candidate(payload, request)
            connection = await _test_candidate(candidate, request)
            bundle = manager.build(candidate)
            request.app.state.llm_settings_store.save(override)
            manager.activate(request.app, bundle, candidate)
            request.app.state.llm_settings_source = "console"
    except LLMSettingsError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {
        "ok": True,
        "connection": connection,
        "settings": safe_settings(candidate, "console"),
    }


@router.post("/llm-settings/reset")
async def reset_llm_settings(request: Request) -> dict[str, Any]:
    manager = request.app.state.llm_runtime_manager
    async with manager.lock:
        candidate = apply_override(request.app.state.bootstrap_llm_config, None)
        bundle = manager.build(candidate)
        request.app.state.llm_settings_store.delete()
        manager.activate(request.app, bundle, candidate)
        request.app.state.llm_settings_source = "startup"
    return {
        "ok": True,
        "settings": safe_settings(candidate, "startup"),
    }


# ── Skills ───────────────────────────────────────────────────


@router.get("/skills")
async def list_skills(request: Request) -> dict[str, Any]:
    orch = _orch(request)
    orch.reload_skills()  # refresh from disk so the list is live
    metas = orch._skill_loader.list_all()
    return {
        "skills": [
            {
                "name": m.name,
                "description": m.description,
                "category": m.category,
                "reviewer_type": m.reviewer_type,
                "languages": m.languages,
                "frameworks": m.frameworks,
                "references": m.references,
                "is_builtin": m.name in BUILTIN_SKILLS,
            }
            for m in metas
        ]
    }


@router.get("/skills/{name}")
async def get_skill(name: str, request: Request) -> dict[str, Any]:
    store = request.app.state.skill_store
    try:
        raw = store.read(name)
    except ValidationError as e:
        raise HTTPException(400, str(e)) from e
    if raw is None:
        raise HTTPException(404, f"skill not found: {name}")
    meta = _orch(request)._skill_loader.get_meta(name)
    return {
        "name": name,
        "raw": raw,
        "body": _orch(request)._skill_loader.load(name).body if meta else "",
        "meta": {
            "description": meta.description if meta else "",
            "reviewer_type": meta.reviewer_type if meta else "",
            "category": meta.category if meta else "",
            "languages": meta.languages if meta else [],
            "frameworks": meta.frameworks if meta else [],
        },
        "is_builtin": name in BUILTIN_SKILLS,
    }


@router.post("/skills")
async def upsert_skill(payload: SkillPayload, request: Request) -> dict[str, Any]:
    if payload.name in BUILTIN_SKILLS:
        raise HTTPException(400, f"'{payload.name}' 是内置 skill，请通过文件管理，不在控制台改")
    store = request.app.state.skill_store
    try:
        store.write(
            name=payload.name,
            description=payload.description,
            reviewer_type=payload.reviewer_type,
            category=payload.category,
            body=payload.body,
            languages=payload.languages,
            frameworks=payload.frameworks,
            references=payload.references,
        )
    except ValidationError as e:
        raise HTTPException(400, str(e)) from e
    count = _orch(request).reload_skills()
    return {"ok": True, "name": payload.name, "skills_loaded": count}


@router.post("/skills/{name}/delete")
async def delete_skill(name: str, request: Request) -> dict[str, Any]:
    if name in BUILTIN_SKILLS:
        raise HTTPException(400, f"'{name}' 是内置 skill，不能在控制台删除")
    store = request.app.state.skill_store
    try:
        ok = store.delete(name)
    except ValidationError as e:
        raise HTTPException(400, str(e)) from e
    if not ok:
        raise HTTPException(404, f"skill not found: {name}")
    count = _orch(request).reload_skills()
    return {"ok": True, "name": name, "skills_loaded": count}


# ── Config-type Agents ───────────────────────────────────────


@router.get("/agents")
async def list_agents(request: Request) -> dict[str, Any]:
    store = request.app.state.custom_agent_store
    registry = request.app.state.registry
    custom_names = {a["name"] for a in store.list()}
    builtin = [
        {"name": n, "role": s.role, "description": s.description}
        for n, s in registry.agents.items()
        if n not in custom_names
    ]
    return {
        "custom": store.list(),
        "builtin": builtin,
        "available_tools": sorted(registry.tools.keys()),
    }


@router.post("/agents")
async def upsert_agent(payload: AgentPayload, request: Request) -> dict[str, Any]:
    registry = request.app.state.registry
    store = request.app.state.custom_agent_store
    try:
        spec = normalize_agent(payload.model_dump(), known_tools=set(registry.tools.keys()))
    except ValidationError as e:
        raise HTTPException(400, str(e)) from e

    store.upsert(spec)
    orch = _orch(request)
    if spec["enabled"]:
        orch.register_config_agent(
            reviewer_type=spec["reviewer_type"],
            description=spec["description"],
            allowed_tools=spec["allowed_tools"],
            model_profile=spec["model_profile"],
            max_steps=spec["max_steps"],
            instructions=spec["instructions"],
        )
    else:
        orch.unregister_config_agent(spec["reviewer_type"])
    return {"ok": True, "agent": spec}


@router.post("/agents/{reviewer_type}/delete")
async def delete_agent(reviewer_type: str, request: Request) -> dict[str, Any]:
    store = request.app.state.custom_agent_store
    if not store.delete(reviewer_type):
        raise HTTPException(404, f"agent not found: {reviewer_type}")
    _orch(request).unregister_config_agent(reviewer_type)
    return {"ok": True, "reviewer_type": reviewer_type}
