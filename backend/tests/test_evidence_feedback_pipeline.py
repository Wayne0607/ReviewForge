"""The real gate rejects missing contracts, then judges newly collected source."""

import copy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_proof_gate import GateLLM, candidate, ledger, observation
from test_source_audit import audit, verdict

from reviewforge.core.config import PipelineV4Config
from reviewforge.core.events import EventBus
from reviewforge.core.state import StateStore
from reviewforge.engine.context_pack import ContextPack
from reviewforge.engine.investigator import InvestigationResult, Investigator
from reviewforge.engine.pipeline_v4 import _run_llm_stages
from reviewforge.engine.proof_gate import accepted
from reviewforge.engine.publication_contracts import contract_catalog
from reviewforge.engine.semantic_diff import SemanticChangeSet


@pytest.mark.asyncio
async def test_missing_contract_feedback_must_pass_a_new_independent_audit_before_publication(monkeypatch):
    item = candidate()
    item.investigation_task_id = "task"
    book = ledger(item, protocol=4)
    book.investigation_tasks["task"] = {
        "members": [item.identity],
        "charged_to": item.identity,
        "budget_steps_limit": 4,
        "token_limit": 16000,
        "total_steps": 2,
        "total_tokens": 5000,
        "total_unreported_tokens": 0,
    }
    missing = audit()
    missing["contracts"] = []
    missing["gaps"] = ["Read the actual consumer's optional input contract."]
    added = replace(observation("obs_2", "interface Request { value?: Value; }"), path="consumer.ts")
    updated = audit()
    updated["contracts"][0]["evidence"] = [f"{item.id}/obs_2:e1"]
    future = copy.deepcopy(item)
    future.observations.append(added)
    future_book = ledger(future, protocol=4)
    future_book.source_audits[item.id] = {"facts": updated}
    final = verdict()
    final["contract_refs"] = [next(iter(contract_catalog(future, future_book)))]
    final["obligations"]["required_behavior"]["evidence"] = [f"{item.id}/obs_2:e1"]
    editor = GateLLM(replies=[{"audits": [missing]}, {"audits": [updated]}, {"reviews": [final]}])
    collector = AsyncMock(
        return_value=InvestigationResult(
            verdict="unknown",
            tokens=200,
            steps=1,
            observations=[*item.observations, added],
            progress=list(item.investigation_progress),
        )
    )
    monkeypatch.setattr(Investigator, "collect_evidence", collector)
    events = EventBus()
    orchestrator = SimpleNamespace(
        _model_router=SimpleNamespace(get_llm=lambda name: editor),
        _events=events,
        _db=None,
    )
    state = StateStore(
        repo="o/r",
        pr_number=1,
        head_sha="head",
        files_changed=["app.ts"],
        file_diffs={"app.ts": "@@ -1 +1 @@\n-return value?.id\n+return value.id\n"},
    )
    changeset = SemanticChangeSet(repo="o/r", pr_number=1, head_sha="head", units=[])
    publication = await _run_llm_stages(
        orchestrator,
        state,
        changeset,
        ContextPack(workspace_digest="workspace"),
        book,
        SimpleNamespace(),
        PipelineV4Config(protocol_version=4),
        "en",
        {},
        resume=True,
    )
    assert collector.await_count == 1
    assert collector.await_args.kwargs["token_limit"] == 11000
    assert collector.await_args.kwargs["max_steps"] == 2
    assert book.source_audit_batches == 2 and len(editor.calls) == 3
    assert accepted(item, book) and len(publication.comments) == 1
    record = book.investigation_tasks["task"]["evidence_followup"]
    assert record["original_review"]["decision"] == "unknown"
    assert book.investigation_tasks["task"]["total_tokens"] == 5200
    assert item.status.value == "confirmed" and item.claim == future.claim
