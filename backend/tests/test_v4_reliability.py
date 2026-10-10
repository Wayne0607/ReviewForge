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
@pytest.mark.parametrize("inline_limit", [1, 5])
@pytest.mark.parametrize("lose_supplement_receipt", [False, True])
async def test_partial_publication_resumes_investigation_and_only_sends_new_confirmed(
    tmp_path, monkeypatch, inline_limit, lose_supplement_receipt
):
    from reviewforge.engine.hypothesis import Hypothesis, Mechanism, Site
    from reviewforge.engine.investigator import InvestigationResult, Investigator

    db = Database(tmp_path / "db.sqlite")
    await db.connect()
    try:
        await db.create_run("run", "owner/repo", 1, "abc")
        state, fake = state_and_fake(tmp_path, db=db)
        fake._pipeline_v4_config.mode = "hypothesis"
        fake._pipeline_v4_config.publish_max_inline = inline_limit
        fake._pipeline_v4_config.publish_max_inline_overflow = inline_limit
        state.file_diffs["app.py"] = "@@ -0,0 +1,4 @@\n+def f(x):\n+    return x.name\n+enabled = True\n+other = None\n"
        unit = compile_semantic_changeset(state).units[0]
        ledger = state.ledger = HypothesisLedger("run", "abc", "d")
        for identity, mechanism, status in (
            ("a", Mechanism.NULL_PATH, HypothesisStatus.CONFIRMED),
            ("b", Mechanism.ERROR_PATH, HypothesisStatus.OPEN),
        ):
            ledger.upsert(
                Hypothesis(
                    id=identity,
                    identity=f"{unit.id}::{mechanism.value}::{identity}",
                    unit_id=unit.id,
                    mechanism=mechanism,
                    claim=f"issue {identity}",
                    trigger="input is None",
                    impact="crash",
                    open_question="is it reachable?",
                    refutation="caller prevents it",
                    sites=[Site("app.py", 2, "return x.name")],
                    severity="error",
                    source="generator",
                    status=status,
                    evidence_strength="strong" if status == HypothesisStatus.CONFIRMED else "none",
                )
            )
        generator = AsyncMock(
            return_value=SimpleNamespace(
                accepted=0, dropped_unanchored=0, dropped_invalid=0, dropped_overflow=0, blocks=1, failed_blocks=0
            )
        )
        monkeypatch.setattr("reviewforge.engine.pipeline_v4.HypothesisGenerator.run", generator)
        investigated = []

        async def investigate(self, hypothesis, *args, **kwargs):
            investigated.append(hypothesis.id)
            if len(investigated) == 1:
                return InvestigationResult(verdict="unknown", reason="provider interrupted", retryable=True)
            return InvestigationResult(verdict="confirmed", reason="grounded", severity="error", strength="strong")

        monkeypatch.setattr(Investigator, "investigate", investigate)
        calls, roles = [], []

        async def invoke(name, params, *args, **kwargs):
            calls.append(params)
            if len(calls) == 2 and lose_supplement_receipt:
                from reviewforge.tools.github_api import GitHubAPIError

                raise GitHubAPIError("receipt lost", kind="network", retryable=True)
            return {
                "delivered_indexes": list(range(len(params["comments"]))),
                "review": {"id": len(calls)},
                "compatibility": False,
            }

        fake._gateway.invoke = invoke

        def llm(name):
            roles.append(name)
            return UsageLLM()

        fake._model_router.get_llm = llm
        assert not (await run_hypothesis_pipeline(fake, state)).completed
        frozen = await db.get_v4_publication("run")
        assert len(calls) == 1 and "issue a" in calls[0]["comments"][0]["body"]
        # Resume from persisted state and a new SQLite connection.
        await db.close()
        await db.connect()
        state.ledger = await db.load_hypothesis_ledger("run")
        health = await run_hypothesis_pipeline(fake, state)
        assert health.completed is (not lose_supplement_receipt)
        assert investigated == ["b", "b"]
        assert generator.await_count == 1
        assert roles.count("editor") == 1
        assert len(calls) == 2
        new_text = calls[1]["body"] + "".join(comment["body"] for comment in calls[1]["comments"])
        assert "issue b" in new_text and "issue a" not in new_text
        assert sum(len(call["comments"]) for call in calls) <= inline_limit
        assert calls[0]["delivery_key"] != calls[1]["delivery_key"]
        assert (await db.get_v4_publication("run"))["payload"] == frozen["payload"]
        roles_before = list(roles)
        assert (await run_hypothesis_pipeline(fake, state)).completed
        if lose_supplement_receipt:
            assert calls[2]["reconcile_only"] is True
            assert calls[2]["delivery_key"] == calls[1]["delivery_key"]
        assert len(calls) == (3 if lose_supplement_receipt else 2) and roles == roles_before
        calls_before = len(calls)
        assert (await run_hypothesis_pipeline(fake, state)).completed
        assert len(calls) == calls_before and roles == roles_before
        records = await db.list_v4_publications("run")
        assert len(records) == 2 and all(record["status"] == "delivered" for record in records)
        assert "coverage" not in calls[0]
        # Discovery retries can add sites to an already confirmed identity.
        # Each new fact must survive, without a duplicate inline or another LLM.
        existing = next(item for item in state.ledger.items.values() if item.id == "a")
        for line, excerpt in ((3, "enabled = True"), (4, "other = None")):
            existing.sites.append(Site("app.py", line, excerpt))
            assert (await run_hypothesis_pipeline(fake, state)).completed
            assert calls[-1]["comments"] == []
            assert f"app.py:{line}" in calls[-1]["body"]
            assert roles == roles_before
            assert calls[-1]["delivery_key"] != calls[-2]["delivery_key"]
        assert (await db.get_v4_publication("run"))["payload"] == frozen["payload"]
        calls_before = len(calls)
        assert (await run_hypothesis_pipeline(fake, state)).completed
        assert len(calls) == calls_before
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_ambiguous_frozen_delivery_blocks_new_models_and_supplements(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    await db.connect()
    try:
        await db.create_run("run", "owner/repo", 1, "abc")
        state, fake = state_and_fake(tmp_path, db=db)
        await run_hypothesis_pipeline(fake, state)
        next(iter(state.ledger.items.values())).status = HypothesisStatus.OPEN
        fake._pipeline_v4_config.mode = "hypothesis"
        fake._model_router.get_llm = lambda name: pytest.fail("models ran before delivery reconciliation")
        await db.prepare_v4_publication(
            "run", "abc", {"comments": [], "body": "unconfirmed", "coverage": {"confirmed_ids": []}}
        )
        await db.claim_v4_publication("run")
        fake._gateway.invoke = AsyncMock(return_value={})
        health = await run_hypothesis_pipeline(fake, state)
        assert not health.completed and health.delivery.retryable
        assert fake._gateway.invoke.call_args.args[1]["reconcile_only"] is True
        assert len(await db.list_v4_publications("run")) == 1
        assert next(iter(state.ledger.items.values())).status == HypothesisStatus.OPEN
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
