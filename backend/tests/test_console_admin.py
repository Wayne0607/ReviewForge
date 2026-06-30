"""Console-driven Skill/Agent CRUD: persistence, validation, hot-reload, and the HTTP API."""

import pytest

from reviewforge.core.custom_store import (
    CustomAgentStore,
    SkillStore,
    ValidationError,
    normalize_agent,
)
from reviewforge.core.events import EventBus
from reviewforge.core.specs import build_registry
from reviewforge.engine.mock_llm import MockChatLLM
from reviewforge.engine.orchestrator import Orchestrator
from reviewforge.skills.loader import SkillLoader
from reviewforge.tools.gateway import ToolGateway
from reviewforge.tools.mock_github import MockGitHubClient


def _orch():
    reg = build_registry()
    return Orchestrator(
        registry=reg,
        gateway=ToolGateway(reg, MockGitHubClient()),
        event_bus=EventBus(),
        planner_llm=MockChatLLM(),
        reviewer_llm=MockChatLLM(),
        calibrator_llm=MockChatLLM(),
    )


# ── stores + validation ──────────────────────────────────────


def test_skill_store_rejects_bad_names(tmp_path):
    store = SkillStore(tmp_path)
    with pytest.raises(ValidationError):
        store.write(name="Bad Name", description="d", reviewer_type="", body="x")
    with pytest.raises(ValidationError):
        store.write(name="../escape", description="d", reviewer_type="", body="x")
    with pytest.raises(ValidationError):  # empty body
        store.write(name="ok_name", description="d", reviewer_type="", body="   ")


def test_normalize_agent_validation():
    tools = set(build_registry().tools.keys())
    with pytest.raises(ValidationError):  # collides with built-in type
        normalize_agent({"reviewer_type": "security", "description": "d"}, tools)
    with pytest.raises(ValidationError):  # unknown tool
        normalize_agent({"reviewer_type": "compliance", "description": "d", "allowed_tools": ["nope"]}, tools)
    with pytest.raises(ValidationError):  # bad slug
        normalize_agent({"reviewer_type": "Compliance!", "description": "d"}, tools)
    spec = normalize_agent({"reviewer_type": "compliance", "description": "检查合规"}, tools)
    assert spec["name"] == "compliance_reviewer"
    assert spec["allowed_tools"]  # defaulted


def test_custom_agent_store_roundtrip(tmp_path):
    path = tmp_path / "custom_agents.json"
    store = CustomAgentStore(path)
    store.upsert({"reviewer_type": "compliance", "name": "compliance_reviewer", "description": "d", "enabled": True})
    # reload from disk in a fresh store
    store2 = CustomAgentStore(path)
    assert store2.get("compliance")["description"] == "d"
    assert store2.delete("compliance") is True
    assert CustomAgentStore(path).get("compliance") is None


# ── orchestrator hot-reload ──────────────────────────────────


def test_register_config_agent_creates_working_reviewer():
    orch = _orch()
    name = orch.register_config_agent(
        reviewer_type="compliance",
        description="检查数据合规",
        allowed_tools=["read_diff"],
        instructions="重点检查 PII 是否脱敏",
    )
    assert name == "compliance_reviewer"
    assert "compliance_reviewer" in orch._registry.agents
    rv = orch._create_reviewer("compliance_reviewer")
    assert rv is not None
    assert rv.reviewer_type == "compliance"
    # inline instructions survive _attach_skill (not clobbered)
    assert rv._skill_body == "重点检查 PII 是否脱敏"

    assert orch.unregister_config_agent("compliance") is True
    assert "compliance_reviewer" not in orch._registry.agents
    assert orch._create_reviewer("compliance_reviewer") is None


def test_reload_skills_picks_up_new_skill(tmp_path):
    skills_dir = tmp_path / "skills"
    store = SkillStore(skills_dir)
    orch = _orch()
    orch._skill_loader = SkillLoader(skills_dir)
    assert orch.reload_skills() == 0

    store.write(name="compliance_rules", description="d", reviewer_type="compliance", body="规则正文")
    assert orch.reload_skills() == 1
    assert "compliance" in orch._skills_by_type

    assert store.delete("compliance_rules") is True
    assert orch.reload_skills() == 0


# ── HTTP API end-to-end ──────────────────────────────────────


def test_admin_api_skill_and_agent_roundtrip(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from reviewforge.api.admin import router as admin_router

    skills_dir = tmp_path / "skills"
    orch = _orch()
    orch._skill_loader = SkillLoader(skills_dir)

    app = FastAPI()
    app.include_router(admin_router)  # no auth wrapper in test
    app.state.orchestrator = orch
    app.state.registry = orch._registry
    app.state.skill_store = SkillStore(skills_dir)
    app.state.custom_agent_store = CustomAgentStore(tmp_path / "custom_agents.json")
    client = TestClient(app)

    # create a skill
    r = client.post(
        "/api/v1/admin/skills",
        json={"name": "compliance_rules", "description": "d", "reviewer_type": "compliance", "body": "规则"},
    )
    assert r.status_code == 200 and r.json()["ok"]
    assert "compliance_rules" in [s["name"] for s in client.get("/api/v1/admin/skills").json()["skills"]]

    # built-in skills are protected from console writes
    r = client.post("/api/v1/admin/skills", json={"name": "security_rules", "description": "x", "body": "y"})
    assert r.status_code == 400

    # create a config-type agent → hot-registered into the live orchestrator
    r = client.post(
        "/api/v1/admin/agents",
        json={"reviewer_type": "compliance", "description": "检查合规", "instructions": "检查 PII"},
    )
    assert r.status_code == 200
    assert "compliance_reviewer" in orch._extra_reviewers

    # collision with a built-in type is rejected
    r = client.post("/api/v1/admin/agents", json={"reviewer_type": "security", "description": "x"})
    assert r.status_code == 400

    # delete the agent → removed from the live orchestrator
    assert client.post("/api/v1/admin/agents/compliance/delete").status_code == 200
    assert "compliance_reviewer" not in orch._extra_reviewers
