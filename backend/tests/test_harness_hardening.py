"""Tests for harness hardening: plugin compatibility, Gateway schema validation, resume."""

import asyncio

import pytest

from reviewforge.core.database import Database
from reviewforge.core.events import EventBus
from reviewforge.core.specs import build_registry
from reviewforge.core.state import StateStore
from reviewforge.engine.mock_llm import MockChatLLM
from reviewforge.engine.orchestrator import Orchestrator
from reviewforge.engine.reviewers import BaseReviewer
from reviewforge.tools.gateway import ToolGateway
from reviewforge.tools.mock_github import MockGitHubClient


class _PluginReviewer(BaseReviewer):
    """A plugin using the documented (llm, registry, gateway) constructor signature."""

    plugin_name = "demo_plugin"
    plugin_type = "custom"

    def __init__(self, llm, registry, gateway):
        super().__init__(
            name=self.plugin_name,
            reviewer_type=self.plugin_type,
            llm=llm,
            registry=registry,
            gateway=gateway,
            max_steps=4,
        )


def _orch(db=None, **kw):
    reg = build_registry()
    return Orchestrator(
        registry=reg,
        gateway=ToolGateway(reg, MockGitHubClient()),
        event_bus=EventBus(),
        planner_llm=MockChatLLM(),
        reviewer_llm=MockChatLLM(),
        calibrator_llm=MockChatLLM(),
        db=db,
        **kw,
    )


def test_plugin_reviewer_constructs_with_base_signature():
    orch = _orch(agentic_default=True)
    orch.register_plugin_reviewers({"demo_plugin": _PluginReviewer})
    r = orch._create_reviewer("demo_plugin")  # must not raise despite base-only __init__
    assert r is not None
    assert r._agentic is True
    assert r._events is orch._events


def test_gateway_rejects_missing_and_mistyped_params():
    reg = build_registry()
    gw = ToolGateway(reg, MockGitHubClient())
    state = StateStore(pr_number=1, repo="o/r", head_sha="h", files_changed=["a.py"])
    with pytest.raises(ValueError):  # read_diff requires file_path
        asyncio.run(gw.invoke("read_diff", {}, state, agent_name="security_reviewer"))
    with pytest.raises(ValueError):  # post_comment line must be integer, not str
        asyncio.run(
            gw.invoke(
                "post_comment",
                {"file_path": "a.py", "line": "nope", "body": "b", "severity": "info"},
                state,
                agent_name="orchestrator",
            )
        )


async def test_resume_skips_completed_reviewers_and_keeps_findings(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.connect()
    # A prior run that crashed (status 'failed'): one reviewer done + one finding.
    await db.create_run(run_id="r1", repo="o/r", pr_number=5, head_sha="HEAD", base_sha="B")
    await db.insert_finding(
        "r1",
        {
            "id": "f1",
            "file": "a.py",
            "line": 3,
            "severity": "error",
            "category": "security",
            "message": "prior finding",
            "confidence": 0.9,
            "reviewer": "security_reviewer",
            "status": "confirmed",
        },
    )
    await db.insert_metric("r1", "security_reviewer", findings_count=1, status="completed")
    await db.fail_run("r1", "simulated crash")  # mark failed → resumable (not an active run)

    assert (await db.get_resumable_run("o/r", 5, "HEAD"))["run_id"] == "r1"

    orch = _orch(db=db, agentic_default=False)
    state = StateStore(pr_number=5, repo="o/r", head_sha="HEAD", files_changed=["a.py"], diff_summary="--- a.py\n+x")
    await orch.run(state)

    # security_reviewer was already completed → rehydrated as a completed task (not re-run)
    assert any(t.reviewer == "security_reviewer" and t.status == "completed" for t in state.list_tasks())
    # prior finding survived the resumed run
    assert any(f.id == "f1" for f in state.list_findings())
    await db.close()


async def test_fail_running_runs_marks_orphaned_reviews(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.connect()
    await db.create_run(run_id="running1", repo="o/r", pr_number=6, head_sha="H", base_sha="B")

    assert (await db.get_run("running1"))["status"] == "running"
    assert await db.fail_running_runs("orphaned by restart") == 1

    run = await db.get_run("running1")
    assert run["status"] == "failed"
    assert "orphaned by restart" in run["summary_json"]
    await db.close()
