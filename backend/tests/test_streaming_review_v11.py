"""Real native fake-model receipts exercise discovery/investigation interleaving."""

from __future__ import annotations

import asyncio
import copy
import json
from types import SimpleNamespace

import pytest
from test_discovery_workflow_v6 import batch, lead, no_issue
from test_discovery_workflow_v7 import inputs
from test_hypothesis_ledger import _hypothesis
from test_investigation_v3 import source_executor
from test_review_session_v5 import SessionModel, call, packet, receipt
from test_shared_investigation_v11 import unknown

from reviewforge.core.config import PipelineV4Config
from reviewforge.core.events import EventBus
from reviewforge.engine import pipeline_v4
from reviewforge.engine.hypothesis import HypothesisLedger, HypothesisStatus, Observation
from reviewforge.engine.streaming_review import StreamingReviewScheduler, unfinished_discovery_units


def book(protocol=11):
    return HypothesisLedger("run", "head", "digest", protocol_version=protocol)


def runtime(model):
    return SimpleNamespace(
        _model_router=SimpleNamespace(get_llm=lambda *_: model),
        _events=EventBus(),
        _db=None,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", [10, 11])
async def test_actual_pipeline_receipts_interleave_before_last_discovery_and_resume_frozen_cases(protocol, monkeypatch):
    monkeypatch.setattr(pipeline_v4, "build_workspace_executor", lambda *_args, **_kwargs: source_executor([]))
    ledger = book(protocol)
    state, changes, pack = inputs(5)
    pack.workspace_digest = "digest"
    events = []
    first_path = ""
    discovery_groups = set()
    queried = False
    handoff_seen = False

    def answer(messages, _options, _turn):
        nonlocal first_path, queried, handoff_seen
        data = packet(messages)
        if "case_id" in data:
            events.append("investigation")
            return unknown(data)
        units = data["pending_units"]
        group = tuple(unit["unit_id"] for unit in units)
        events.append("discovery")
        first = not discovery_groups
        discovery_groups.add(group)
        if first:
            first_path = units[0]["path"]
            return batch(lead(units[0]), *map(no_issue, units[1:]))
        if protocol >= 11:
            if not queried:
                queried = True
                return receipt(call("query_review_workspace", {"paths": [first_path], "unit_ids": []}))
            response = json.loads(messages[-1].content)["controller_receipt"][0]
            assert any(note["actor"] == "investigation" for note in response["result"]["notes"])
            handoff_seen = True
        return batch(*map(no_issue, units))

    model = SessionModel(handler=answer)
    worker = runtime(model)
    config = PipelineV4Config(provider_max_attempts=1)
    await pipeline_v4._run_llm_stages(
        worker,
        state,
        changes,
        pack,
        ledger,
        SimpleNamespace(read=lambda _path: "return new_value"),
        config,
        "en",
        {},
    )
    assert len(discovery_groups) == 2
    if protocol == 11:
        assert handoff_seen and events.index("investigation") < max(
            i for i, value in enumerate(events) if value == "discovery"
        )
        assert ledger.streaming_review["candidates"]
        assert all(row["state"] == "completed" for row in ledger.streaming_review["candidates"].values())
    else:
        assert not ledger.streaming_review
        assert events.index("investigation") > max(i for i, value in enumerate(events) if value == "discovery")
    assert all(item.attempts == 1 for item in ledger.items.values())
    before = len(model.calls)
    restored = HypothesisLedger.from_dict(json.loads(json.dumps(ledger.to_dict(), sort_keys=True)))
    await pipeline_v4._run_llm_stages(
        worker,
        state,
        changes,
        pack,
        restored,
        SimpleNamespace(read=lambda _path: "return new_value"),
        config,
        "en",
        {},
        resume=True,
    )
    assert len(model.calls) == before


@pytest.mark.asyncio
async def test_scheduler_persists_queue_and_claim_and_does_not_retry_one_failure_at_final_drain():
    ledger = book()
    first = _hypothesis(1)
    second = _hypothesis(2)
    second.identity, second.id, second.claim = "other::case", "h_second", "A distinct original claim"
    ledger.upsert(first)
    ledger.upsert(second)
    calls, checkpoints = [], []

    async def checkpoint(current):
        checkpoints.append(copy.deepcopy(current.streaming_review))

    async def investigate(identities):
        identity = next(iter(identities))
        calls.append(identity)
        if identity == first.identity:
            ledger.items[identity].observations.append(
                Observation("obs_0", "read_file", "q", "service.py", (1, 1), "head", "d", "saved()", "success", (1, 1))
            )
            raise RuntimeError("first task failed after checkpoint")
        ledger.apply_verdict(identity, status="unknown", investigation_outcome="insufficient-evidence")

    scheduler = StreamingReviewScheduler(ledger, investigate, checkpoint)
    await scheduler.on_candidates(ledger, (first.identity, second.identity))
    await scheduler.drain()
    assert calls == [first.identity, second.identity]
    assert checkpoints[0]["candidates"][first.identity]["state"] == "queued"
    assert checkpoints[1]["candidates"][first.identity]["state"] == "running"
    assert ledger.items[first.identity].status == HypothesisStatus.OPEN
    assert ledger.streaming_review["candidates"][first.identity]["state"] == "interrupted"
    assert ledger.items[first.identity].observations[0].excerpt == "saved()"
    restored = HypothesisLedger.from_dict(ledger.to_dict())
    resumed_calls = []

    async def resume(identities):
        resumed_calls.extend(identities)
        restored.apply_verdict(first.identity, status="unknown", investigation_outcome="insufficient-evidence")

    await StreamingReviewScheduler(restored, resume, checkpoint).drain()
    assert resumed_calls == [first.identity]
    assert restored.items[first.identity].observations[0].excerpt == "saved()"


@pytest.mark.asyncio
async def test_cancelled_claim_and_unfinished_discovery_survive_restore_without_clean_coverage():
    ledger = book()
    item = _hypothesis(1)
    ledger.upsert(item)
    ledger.discovery_blocks["group"] = {
        "unit_ids": ["accepted", "unvisited"],
        "units": {"accepted": {"finished": True}},
    }
    entered, release = asyncio.Event(), asyncio.Event()
    snapshots = []

    async def checkpoint(current):
        snapshots.append(current.to_dict())

    async def investigate(_ids):
        entered.set()
        await release.wait()

    scheduler = StreamingReviewScheduler(ledger, investigate, checkpoint)
    task = asyncio.create_task(scheduler.drain())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert ledger.streaming_review["candidates"][item.identity]["state"] == "interrupted"
    restored = HypothesisLedger.from_dict(snapshots[-1])
    assert unfinished_discovery_units(restored) == {"unvisited"}
    assert "unvisited" not in restored.no_issue_units


@pytest.mark.asyncio
async def test_pipeline_exception_checkpoints_unfinished_discovery_and_restores_it(monkeypatch):
    ledger = book()
    ledger.discovery_blocks["group"] = {"unit_ids": ["unvisited"], "units": {}}
    snapshots = []

    async def stages(*_args, **_kwargs):
        raise asyncio.CancelledError()

    async def persist(_runtime, current):
        snapshots.append(current.to_dict())

    monkeypatch.setattr(pipeline_v4, "_run_llm_stages_cached", stages)
    monkeypatch.setattr(pipeline_v4, "_persist_ledger", persist)
    with pytest.raises(asyncio.CancelledError):
        await pipeline_v4._run_llm_stages(None, None, None, None, ledger, None, None, "en", {})
    restored = HypothesisLedger.from_dict(snapshots[-1])
    assert restored.unresolved_units == {"unvisited": "interrupted-discovery"}


@pytest.mark.asyncio
async def test_actual_pipeline_first_investigation_failure_does_not_block_later_groups_or_retry_at_drain(monkeypatch):
    monkeypatch.setattr(pipeline_v4, "build_workspace_executor", lambda *_args, **_kwargs: source_executor([]))
    ledger = book()
    state, changes, pack = inputs(5)
    pack.workspace_digest = "digest"
    cases = []
    groups = []

    def answer(messages, _options, _turn):
        data = packet(messages)
        if "case_id" in data:
            cases.append(data["case_id"])
            if len(cases) == 1:
                raise RuntimeError("first provider call failed")
            return unknown(data)
        units = data["pending_units"]
        groups.append(tuple(item["unit_id"] for item in units))
        return batch(lead(units[0]), *map(no_issue, units[1:]))

    model = SessionModel(handler=answer)
    await pipeline_v4._run_llm_stages(
        runtime(model),
        state,
        changes,
        pack,
        ledger,
        SimpleNamespace(read=lambda _: "return new_value"),
        PipelineV4Config(provider_max_attempts=1),
        "en",
        {},
    )
    assert len(groups) == 2 and len(cases) == len(set(cases)) == 2
    assert len(ledger.items) == 2 and all(item.attempts == 1 for item in ledger.items.values())
    assert sum(item.retryable for item in ledger.items.values()) == 1
    assert sorted(row["state"] for row in ledger.streaming_review["candidates"].values()) == [
        "completed",
        "interrupted",
    ]
    assert not ledger.unresolved_units


@pytest.mark.parametrize("field", ["run_id", "head_sha", "workspace_digest", "candidate_digest"])
@pytest.mark.asyncio
async def test_streaming_restore_rejects_changed_binding_or_case(field):
    ledger = book()
    item = _hypothesis(1)
    ledger.upsert(item)

    async def checkpoint(_ledger):
        pass

    async def investigate(_ids):
        ledger.apply_verdict(item.identity, status="unknown", investigation_outcome="insufficient-evidence")

    await StreamingReviewScheduler(ledger, investigate, checkpoint).drain()
    saved = ledger.to_dict()
    if field == "candidate_digest":
        saved["streaming_review"]["candidates"][item.identity][field] = "changed"
    else:
        saved["streaming_review"]["binding"][field] = "changed"
    with pytest.raises(ValueError, match="streaming-review"):
        HypothesisLedger.from_dict(saved)
