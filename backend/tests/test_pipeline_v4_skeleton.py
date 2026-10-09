from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from reviewforge.core.config import PipelineV4Config
from reviewforge.core.events import EventBus
from reviewforge.core.state import StateStore
from reviewforge.engine.hypothesis import HypothesisLedger
from reviewforge.engine.orchestrator import Orchestrator
from reviewforge.engine.pipeline_v4 import run_hypothesis_pipeline
from reviewforge.engine.run_health import RunHealth
from reviewforge.tools.workspace import WorkspaceInfo


@pytest.mark.asyncio
async def test_legacy_dispatch_is_exactly_legacy(monkeypatch) -> None:
    legacy = AsyncMock(return_value={"confirmed": 1})
    fake = SimpleNamespace(_pipeline_v4_config=PipelineV4Config(mode="legacy"), _run_legacy=legacy)
    shadow = AsyncMock()
    monkeypatch.setattr("reviewforge.engine.orchestrator.run_hypothesis_pipeline", shadow)
    state = StateStore()
    assert await Orchestrator.run(fake, state) == {"confirmed": 1}
    legacy.assert_awaited_once_with(state)
    shadow.assert_not_awaited()


@pytest.mark.asyncio
async def test_shadow_preserves_legacy_result_and_cleans_exactly_once(monkeypatch) -> None:
    expected = {"confirmed": 2, "tasks_failed": 0}
    legacy = AsyncMock(return_value=expected)
    shadow = AsyncMock()
    cleanup = AsyncMock()
    fake = SimpleNamespace(
        _pipeline_v4_config=PipelineV4Config(mode="shadow"),
        _run_legacy=legacy,
        _gateway=SimpleNamespace(cleanup_workspace=cleanup),
    )
    monkeypatch.setattr("reviewforge.engine.orchestrator.run_hypothesis_pipeline", shadow)
    state = StateStore()
    assert await Orchestrator.run(fake, state) is expected
    shadow.assert_awaited_once_with(fake, state)
    cleanup.assert_awaited_once_with(state)


@pytest.mark.asyncio
async def test_shadow_failure_is_non_blocking_and_cleanup_runs(monkeypatch) -> None:
    cleanup = AsyncMock()
    events = SimpleNamespace(emit=Mock())
    fake = SimpleNamespace(
        _pipeline_v4_config=PipelineV4Config(mode="shadow"),
        _run_legacy=AsyncMock(return_value={"confirmed": 0}),
        _gateway=SimpleNamespace(cleanup_workspace=cleanup),
        _events=events,
    )
    monkeypatch.setattr(
        "reviewforge.engine.orchestrator.run_hypothesis_pipeline",
        AsyncMock(side_effect=RuntimeError("boom")),
    )
    state = StateStore()
    assert await Orchestrator.run(fake, state) == {"confirmed": 0}
    cleanup.assert_awaited_once_with(state)
    events.emit.assert_called_once_with("pipeline_v4.failed", {"mode": "shadow", "error_type": "RuntimeError"})


@pytest.mark.asyncio
async def test_skeleton_emits_complete_workspace_and_context_events(tmp_path) -> None:
    workspace = SimpleNamespace(
        info=WorkspaceInfo(
            repo="owner/repo",
            head_repo="owner/repo",
            head_sha="abc",
            root=tmp_path,
            file_count=3,
            byte_size=42,
            digest="digest",
            truncated=False,
            source="tarball",
        ),
        source="tarball",
        digest="digest",
    )
    events = EventBus()
    seen = []
    events.subscribe(seen.append)
    events.set_run_id("run")
    fake = SimpleNamespace(
        _gateway=SimpleNamespace(workspace_for=AsyncMock(return_value=workspace)),
        _events=events,
        _pipeline_v4_config=PipelineV4Config(),
    )
    state = StateStore(repo="owner/repo", pr_number=1, head_sha="abc")
    await run_hypothesis_pipeline(fake, state)
    workspace_event, context_event = seen[:2]
    assert workspace_event.event_type == "workspace.built"
    assert set(workspace_event.data) == {"source", "file_count", "byte_size", "truncated", "digest", "ms"}
    assert context_event.event_type == "context_pack.built"
    assert set(context_event.data) == {"units", "slices", "truncated_units", "chars"}
    assert seen[2].event_type == "pipeline_v4.completed"
    assert state.ledger is not None
    assert state.ledger.run_id == "run"


@pytest.mark.asyncio
async def test_hypothesis_dispatch_runs_new_path_and_cleans_up(monkeypatch) -> None:
    cleanup = AsyncMock()
    orchestrator = object.__new__(Orchestrator)
    orchestrator._pipeline_v4_config = PipelineV4Config(mode="hypothesis")
    orchestrator._events = EventBus()
    orchestrator._gateway = SimpleNamespace(cleanup_workspace=cleanup)
    orchestrator._db = None
    monkeypatch.setattr(
        "reviewforge.engine.orchestrator.run_hypothesis_pipeline",
        AsyncMock(return_value=RunHealth.build()),
    )
    state = StateStore(repo="owner/repo", pr_number=1, head_sha="abc")

    summary = await orchestrator.run(state)

    assert summary["mode"] == "hypothesis"
    assert summary["status"] == "completed"
    assert summary["confirmed"] == 0
    cleanup.assert_awaited_once_with(state)


@pytest.mark.asyncio
async def test_hypothesis_dispatch_resumes_restored_ledger(monkeypatch) -> None:
    restored = HypothesisLedger("resume123", "abc", "digest")
    cleanup = AsyncMock()

    class _DB:
        def __init__(self) -> None:
            self.create_run = AsyncMock()

        async def get_resumable_run(self, repo, pr_number, head_sha):
            return {"run_id": "resume123"}

        async def restart_run(self, run_id):
            return True

        async def load_hypothesis_ledger(self, run_id):
            return restored

        async def has_active_run_for_head(self, repo, pr_number, head_sha):
            return False

        async def complete_run(self, run_id, summary):
            return None

        async def fail_run(self, run_id, message, summary=None):
            return None

    db = _DB()
    events = EventBus()
    seen = []
    events.subscribe(seen.append)
    orchestrator = object.__new__(Orchestrator)
    orchestrator._pipeline_v4_config = PipelineV4Config(mode="hypothesis")
    orchestrator._events = events
    orchestrator._gateway = SimpleNamespace(cleanup_workspace=cleanup)
    orchestrator._db = db
    monkeypatch.setattr(
        "reviewforge.engine.orchestrator.run_hypothesis_pipeline",
        AsyncMock(return_value=RunHealth.build()),
    )
    state = StateStore(repo="owner/repo", pr_number=1, head_sha="abc")

    summary = await orchestrator.run(state)

    assert summary["status"] == "completed"
    assert state.ledger is restored
    db.create_run.assert_not_awaited()
    assert seen[-1].event_type == "review.resumed"
