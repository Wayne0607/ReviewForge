"""Persisted v3 stage counts survive resume and real dashboard HTTP responses."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from langchain_core.messages import AIMessage

from reviewforge.api.dashboard import router as dashboard_router
from reviewforge.core.config import PipelineV4Config
from reviewforge.core.database import Database
from reviewforge.core.events import EventBus
from reviewforge.core.state import StateStore
from reviewforge.engine.hypothesis import (
    ContractAssessment,
    EvidenceCitation,
    Hypothesis,
    HypothesisLedger,
    HypothesisStatus,
    Mechanism,
    Observation,
    Site,
)
from reviewforge.engine.orchestrator import Orchestrator
from reviewforge.engine.proof_gate import OBLIGATIONS, ProofGate
from reviewforge.engine.run_health import RunHealth


def proposed(identity, status=HypothesisStatus.CONFIRMED):
    source = "return value.id\n"
    return Hypothesis(
        id=identity,
        identity=f"unit::{identity}::lookup",
        unit_id="unit",
        mechanism=Mechanism.NULL_PATH,
        claim=f"Supported missing value raises ({identity})",
        trigger="The optional value is absent",
        impact="A supported request raises instead of returning null",
        open_question="Is the value required?",
        refutation="The caller checks value",
        sites=[Site("app.py", 1, "return value.id")],
        severity="warning",
        source="generator",
        status=status,
        observations=[
            Observation("obs_0", "read_file", "app.py", "app.py", (1, 1), "head", "digest", source, "success")
        ],
        assessment=ContractAssessment(
            "Optional absent input must return null",
            "It raises",
            "conflict",
            [EvidenceCitation("obs_0", source)],
            [EvidenceCitation("obs_0", source)],
        ),
    )


def response_for(identity, decision):
    raw = {
        "hypothesis_id": identity,
        "decision": decision,
        "reason": "Original candidate review",
        "obligations": {},
        "counterevidence": None,
        "missing_obligations": [],
    }
    if decision == "accepted":
        raw["obligations"] = {
            name: {"statement": "Established from the supplied real source", "evidence": [f"{identity}/obs_0:e1"]}
            for name in OBLIGATIONS
        }
    elif decision == "refuted":
        raw["counterevidence"] = {
            "premise": "mechanism",
            "statement": "Source protects the receiver",
            "evidence": [f"{identity}/obs_0:e1"],
        }
    else:
        raw["reason"] = "The supported caller binding was not saved"
        raw["missing_obligations"] = ["supported_input"]
    return raw


async def reviewed_book():
    book = HypothesisLedger("run", "head", "workspace", protocol_version=3)
    decisions = {
        "h_accepted": "accepted",
        "h_refuted": "refuted",
        "h_unknown": "unknown",
        "h_error": "accepted",
        "h_stale": "refuted",
    }
    for identity in decisions:
        book.upsert(proposed(identity))
    reviews = [response_for(identity, decision) for identity, decision in decisions.items()]
    next(raw for raw in reviews if raw["hypothesis_id"] == "h_error")["obligations"].pop("required_behavior")
    model = SimpleNamespace(
        ainvoke=AsyncMock(
            return_value=AIMessage(
                content=json.dumps({"reviews": reviews}),
                usage_metadata={"input_tokens": 100, "output_tokens": 100, "total_tokens": 200},
            )
        )
    )
    await ProofGate(model).run(book)
    assert model.ainvoke.await_count == 1
    book.items["unit::h_stale::lookup"].claim = "The original facts changed after the old refutation"
    book.upsert(proposed("h_inv_refuted", HypothesisStatus.REFUTED))
    book.upsert(proposed("h_inv_unknown", HypothesisStatus.UNKNOWN))
    return book


def orchestrator(database):
    value = object.__new__(Orchestrator)
    value._db = database
    value._events = EventBus()
    value._pipeline_v4_config = PipelineV4Config(mode="hypothesis", protocol_version=3)
    value._gateway = SimpleNamespace(cleanup_workspace=AsyncMock())
    return value


async def client_for(database):
    app = FastAPI()
    app.state.db = database
    app.include_router(dashboard_router)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_v3_summary_counts_current_proof_persists_resumes_and_reaches_http(tmp_path, monkeypatch):
    database = Database(tmp_path / "summary.sqlite")
    await database.connect()
    try:
        await database.create_run("run", "o/r", 1, "head")
        book = await reviewed_book()
        original = book.to_dict()
        await database.checkpoint_hypothesis_ledger(book)
        await database.fail_run("run", "fixture interruption")

        async def pipeline(_, state):
            assert state.ledger.to_dict() == original  # actual DB resume restored the proof receipts
            return RunHealth.build(protocol_version=3, publication_failures=2, publication_retryable=True)

        monkeypatch.setattr("reviewforge.engine.orchestrator.run_hypothesis_pipeline", pipeline)
        state = StateStore(repo="o/r", pr_number=1, head_sha="head")
        driver = orchestrator(database)
        summary = await driver._run_hypothesis(state)
        assert summary == {
            "status": "partial",
            "mode": "hypothesis",
            "confirmed": 1,
            "refuted": 1,
            "unknown": 1,
            "retryable": True,
            "protocol_version": 3,
            "total_findings": 7,
            "investigation_confirmed": 5,
            "publication_verified": 1,
            "publication_refuted": 1,
            "publication_unknown": 1,
            "publication_failures": 2,
        }
        assert state.ledger.to_dict() == original and not await database.get_findings(run_id="run")
        stored = json.loads((await database.get_run("run"))["summary_json"])
        assert {name: stored[name] for name in summary} == summary
        await database.close()
        await database.connect()
        async with await client_for(database) as client:
            detail = await client.get("/api/v1/dashboard/reviews/run")
            listed = await client.get("/api/v1/dashboard/reviews")
        assert detail.status_code == listed.status_code == 200
        delivered = detail.json()
        actual = delivered["run"]["summary"]
        assert {name: actual[name] for name in summary} == summary
        assert actual["false_positives"] == actual["tasks_completed"] == actual["tasks_failed"] == 0
        assert listed.json()["runs"][0]["summary"] == actual
        assert delivered["findings"] == []  # no legacy findings were fabricated
        driver._gateway.cleanup_workspace.assert_awaited_once_with(state)
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_gate_refuted_investigation_confirmed_summary_is_not_a_publishable_defect(tmp_path, monkeypatch):
    database = Database(tmp_path / "refuted.sqlite")
    await database.connect()
    try:
        await database.create_run("run", "o/r", 1, "head")
        book = HypothesisLedger("run", "head", "workspace", protocol_version=3)
        book.upsert(proposed("h_refuted"))
        model = SimpleNamespace(
            ainvoke=AsyncMock(
                return_value=AIMessage(content=json.dumps({"reviews": [response_for("h_refuted", "refuted")]}))
            )
        )
        await ProofGate(model).run(book)
        await database.checkpoint_hypothesis_ledger(book)
        await database.fail_run("run", "fixture interruption")
        monkeypatch.setattr(
            "reviewforge.engine.orchestrator.run_hypothesis_pipeline",
            AsyncMock(return_value=RunHealth.build(protocol_version=3)),
        )
        state = StateStore(repo="o/r", pr_number=1, head_sha="head")
        summary = await orchestrator(database)._run_hypothesis(state)
        assert summary["confirmed"] == summary["publication_verified"] == 0
        assert summary["investigation_confirmed"] == summary["publication_refuted"] == 1
        assert summary["refuted"] == summary["unknown"] == summary["publication_failures"] == 0
        assert next(iter(state.ledger.items.values())).status == HypothesisStatus.CONFIRMED
        async with await client_for(database) as client:
            actual = (await client.get("/api/v1/dashboard/reviews/run")).json()["run"]["summary"]
        assert actual["confirmed"] == 0 and actual["investigation_confirmed"] == actual["publication_refuted"] == 1
    finally:
        await database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", [1, 2])
async def test_v1_v2_primary_summary_preserves_the_original_shape(tmp_path, monkeypatch, protocol):
    database = Database(tmp_path / "old.sqlite")
    await database.connect()
    try:
        await database.create_run("run", "o/r", 1, "head")
        book = HypothesisLedger("run", "head", "workspace", protocol_version=protocol)
        book.upsert(proposed("h_first"))
        await database.checkpoint_hypothesis_ledger(book)
        await database.fail_run("run", "fixture interruption")
        monkeypatch.setattr(
            "reviewforge.engine.orchestrator.run_hypothesis_pipeline",
            AsyncMock(return_value=RunHealth.build(protocol_version=protocol)),
        )
        summary = await orchestrator(database)._run_hypothesis(StateStore(repo="o/r", pr_number=1, head_sha="head"))
        assert summary == {
            "status": "completed",
            "mode": "hypothesis",
            "confirmed": 1,
            "refuted": 0,
            "unknown": 0,
            "retryable": False,
        }
    finally:
        await database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stored",
    [
        {"mode": "legacy", "protocol_version": 3, "confirmed": 999},
        {"mode": "shadow", "protocol_version": 3, "confirmed": 999},
        {"mode": "hypothesis", "protocol_version": 1, "confirmed": 999},
        {"mode": "hypothesis", "protocol_version": 2, "confirmed": 999},
        {"mode": "hypothesis", "confirmed": 999},
    ],
)
async def test_other_modes_old_protocol_and_unversioned_dashboard_keep_actual_finding_counts(tmp_path, stored):
    database = Database(tmp_path / "old-dashboard.sqlite")
    await database.connect()
    try:
        await database.create_run("run", "o/r", 1, "head")
        await database.insert_finding(
            "run",
            {
                "id": "legacy",
                "file": "app.py",
                "line": 1,
                "severity": "warning",
                "category": "quality",
                "message": "actual legacy issue",
                "confidence": 0.9,
                "status": "reported",
            },
        )
        await database.complete_run("run", stored)
        async with await client_for(database) as client:
            detail = (await client.get("/api/v1/dashboard/reviews/run")).json()
            listed = (await client.get("/api/v1/dashboard/reviews")).json()
        expected = {"total_findings": 1, "confirmed": 1, "false_positives": 0, "tasks_completed": 0, "tasks_failed": 0}
        assert detail["run"]["summary"] == expected
        assert listed["runs"][0]["summary"] == expected
    finally:
        await database.close()
