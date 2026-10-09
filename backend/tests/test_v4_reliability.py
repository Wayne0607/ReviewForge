from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

from reviewforge.core.config import PipelineV4Config
from reviewforge.core.database import Database
from reviewforge.core.events import EventBus
from reviewforge.core.state import StateStore
from reviewforge.engine.hypothesis import HypothesisLedger, HypothesisStatus
from reviewforge.engine.orchestrator import Orchestrator
from reviewforge.engine.pipeline_v4 import _persist_ledger, run_hypothesis_pipeline
from reviewforge.engine.semantic_diff import compile_semantic_changeset
from reviewforge.tools.workspace import WorkspaceUnavailable


class UsageLLM(BaseChatModel):
    responses: list[str] = Field(default_factory=list)

    def bind_tools(self, tools, **kwargs):
        return self.bind()

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        content = self.responses.pop(0) if self.responses else "{}"
        return ChatResult(
            generations=[
                ChatGeneration(
                    message=AIMessage(
                        content=content, usage_metadata={"input_tokens": 80, "output_tokens": 20, "total_tokens": 100}
                    )
                )
            ]
        )

    @property
    def _llm_type(self):
        return "usage-fixture"


def state_and_fake(tmp_path, *, db=None):
    state = StateStore(
        repo="owner/repo",
        pr_number=1,
        head_sha="abc",
        file_diffs={"app.py": "@@ -0,0 +1,2 @@\n+def f(x):\n+    return x.name\n"},
        impact_manifest={
            "files": [
                {
                    "path": "app.py",
                    "changed_symbols": [{"name": "f", "start_line": 1, "end_line": 2, "added_lines": [1, 2]}],
                }
            ]
        },
    )
    unit = compile_semantic_changeset(state).units[0]
    generator = UsageLLM(
        responses=[
            json.dumps(
                {
                    "hypotheses": [
                        {
                            "unit_id": unit.id,
                            "mechanism": "null-path",
                            "anchor_symbol": "f",
                            "claim": "null dereference",
                            "trigger": "x is None",
                            "impact": "crash",
                            "open_question": "can x be None?",
                            "refutation": "caller checks None",
                            "severity": "warning",
                            "sites": [{"path": "app.py", "line": 2, "excerpt": "return x.name"}],
                        }
                    ],
                    "no_issue_units": [],
                }
            )
        ]
    )
    investigator = UsageLLM(responses=['{"verdict":"unknown","reason":"not enough evidence"}'])
    workspace = SimpleNamespace(
        info=SimpleNamespace(source="api-fallback", file_count=1, byte_size=10, truncated=False, digest="d"),
        source="api-fallback",
        digest="d",
    )
    events = EventBus()
    events.set_run_id("run")
    fake = SimpleNamespace(
        _gateway=SimpleNamespace(workspace_for=AsyncMock(return_value=workspace)),
        _events=events,
        _db=db,
        _pipeline_v4_config=PipelineV4Config(mode="shadow"),
        _model_router=SimpleNamespace(
            get_llm=lambda name: {"hypothesis_generator": generator, "investigator": investigator}.get(name, UsageLLM())
        ),
    )
    return state, fake


@pytest.mark.asyncio
async def test_workspace_unavailable_cannot_complete_a_review(tmp_path) -> None:
    state, fake = state_and_fake(tmp_path)
    fake._gateway.workspace_for.side_effect = WorkspaceUnavailable()
    with pytest.raises(WorkspaceUnavailable):
        await run_hypothesis_pipeline(fake, state)


@pytest.mark.asyncio
async def test_pipeline_counts_actual_role_usage(tmp_path) -> None:
    db = Database(tmp_path / "db.sqlite")
    await db.connect()
    try:
        await db.create_run("run", "owner/repo", 1, "abc")
        state, fake = state_and_fake(tmp_path, db=db)
        seen = []
        fake._events.subscribe(seen.append)
        await run_hypothesis_pipeline(fake, state)
        rows = await db.get_token_usage("run")
        assert {row["agent_name"] for row in rows} == {"hypothesis_generator", "investigator"}
        assert sum(row["total_tokens"] for row in rows) == 200
        completed = next(event for event in seen if event.event_type == "pipeline_v4.completed")
        assert completed.data["tokens_by_agent"] == {"hypothesis_generator": 100, "investigator": 100}
        generated = next(event for event in seen if event.event_type == "hypothesis.generated")
        assert generated.data["tokens"] == 100
        investigation = next(event for event in seen if event.event_type == "investigation.completed")
        assert investigation.data["tokens"] == 100
        assert investigation.data["steps"] == 1
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_checkpoint_survives_interrupt_after_generation(tmp_path, monkeypatch) -> None:
    db = Database(tmp_path / "db.sqlite")
    await db.connect()
    try:
        await db.create_run("run", "owner/repo", 1, "abc")
        state, fake = state_and_fake(tmp_path, db=db)
        monkeypatch.setattr(
            "reviewforge.engine.pipeline_v4.Investigator.run", AsyncMock(side_effect=RuntimeError("interrupted"))
        )
        with pytest.raises(RuntimeError, match="interrupted"):
            await run_hypothesis_pipeline(fake, state)
        restored = await db.load_hypothesis_ledger("run")
        assert restored is not None
        assert restored.head_sha == "abc" and restored.workspace_digest == "d"
        assert len(restored.open()) == 1
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_checkpoint_keeps_empty_and_unresolved_generation(tmp_path) -> None:
    db = Database(tmp_path / "db.sqlite")
    await db.connect()
    try:
        await db.create_run("run", "owner/repo", 1, "abc")
        ledger = HypothesisLedger("run", "abc", "d")
        ledger.no_issue_units["u1"] = "checked caller contract"
        ledger.unresolved_units["u2"] = "generator parse failure"
        await _persist_ledger(SimpleNamespace(_db=db), ledger)
        restored = await db.load_hypothesis_ledger("run")
        assert restored is not None
        assert restored.to_dict() == ledger.to_dict()
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_primary_exception_is_failed_in_database_and_cleaned(tmp_path, monkeypatch) -> None:
    db = Database(tmp_path / "db.sqlite")
    await db.connect()
    try:
        orchestrator = object.__new__(Orchestrator)
        orchestrator._pipeline_v4_config = PipelineV4Config(mode="hypothesis")
        orchestrator._events = EventBus()
        cleanup = AsyncMock()
        orchestrator._gateway = SimpleNamespace(cleanup_workspace=cleanup)
        orchestrator._db = db
        monkeypatch.setattr(
            "reviewforge.engine.orchestrator.run_hypothesis_pipeline", AsyncMock(side_effect=WorkspaceUnavailable())
        )
        with pytest.raises(WorkspaceUnavailable):
            await orchestrator.run(StateStore(repo="owner/repo", pr_number=1, head_sha="abc"))
        rows = await db.get_runs(repo="owner/repo")
        assert rows[0]["status"] == "failed"
        cleanup.assert_awaited_once()
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_delivered_outbox_resume_skips_all_model_calls(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    await db.connect()
    try:
        await db.create_run("run", "owner/repo", 1, "abc")
        state, fake = state_and_fake(tmp_path, db=db)
        state.ledger = HypothesisLedger("run", "abc", "d")
        fake._pipeline_v4_config.mode = "hypothesis"
        fake._model_router.get_llm = lambda name: pytest.fail("resume reran LLM")
        await db.prepare_v4_publication("run", "abc", {"comments": [], "body": "summary"})
        await db.finish_v4_publication("run", {"id": 42})
        health = await run_hypothesis_pipeline(fake, state)
        assert health.completed
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_completed_investigation_is_durable_while_another_is_cancelled(tmp_path, monkeypatch):
    from copy import deepcopy

    from reviewforge.engine.context_pack import ContextPack
    from reviewforge.engine.investigator import InvestigationResult, Investigator

    db = Database(tmp_path / "db.sqlite")
    await db.connect()
    try:
        await db.create_run("run", "owner/repo", 1, "abc")
        state, fake = state_and_fake(tmp_path, db=db)
        await run_hypothesis_pipeline(fake, state)
        first = next(iter(state.ledger.items.values()))
        first.status = HypothesisStatus.OPEN
        second = deepcopy(first)
        second.identity += "_other"
        second.id = "h_other"
        second.status = HypothesisStatus.OPEN
        state.ledger.items[second.identity] = second
        finished = asyncio.Event()

        async def investigate(self, hypothesis, *args, **kwargs):
            if hypothesis.id == second.id:
                await asyncio.Event().wait()
            return InvestigationResult(verdict="confirmed", reason="grounded", severity="warning", strength="strong")

        async def checkpoint(ledger):
            await _persist_ledger(fake, ledger)
            if first.status == HypothesisStatus.CONFIRMED:
                finished.set()

        monkeypatch.setattr(Investigator, "investigate", investigate)
        task = asyncio.create_task(
            Investigator(UsageLLM(), None).run(state.ledger, state, ContextPack(), concurrency=2, on_update=checkpoint)
        )
        await asyncio.wait_for(finished.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        restored = await db.load_hypothesis_ledger("run")
        assert restored.items[first.identity].status == HypothesisStatus.CONFIRMED
        assert restored.items[second.identity].status == HypothesisStatus.OPEN
    finally:
        await db.close()
