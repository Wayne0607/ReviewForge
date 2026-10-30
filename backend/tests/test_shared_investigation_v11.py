"""Deterministic cross-stage reuse and untrusted investigation handoffs."""

from __future__ import annotations

import asyncio
import copy
import json
from types import SimpleNamespace

import pytest
from test_discovery_workflow_v6 import batch, lead
from test_discovery_workflow_v7 import inputs
from test_investigation_v3 import candidate, source_executor, state
from test_review_session_v5 import SessionModel, call, packet, receipt

from reviewforge.core.config import PipelineV4Config
from reviewforge.core.events import EventBus
from reviewforge.engine import pipeline_v4
from reviewforge.engine.context_pack import ContextPack
from reviewforge.engine.discovery import SourceLine
from reviewforge.engine.hypothesis import HypothesisLedger
from reviewforge.engine.hypothesis_generator import HypothesisGenerator
from reviewforge.engine.investigation_evidence import EvidenceScope, SharedEvidenceCache
from reviewforge.engine.investigator import Investigator
from reviewforge.engine.review_session import EvidenceStore
from reviewforge.engine.review_workspace import SharedReviewWorkspace


def book():
    return HypothesisLedger("run", "head", "digest", protocol_version=11)


def unknown(data):
    return receipt(
        call(
            "submit_unknown",
            {
                "case_id": data["case_id"],
                "reason": "Missing contract.",
                "missing_fact": "Consumer contract is unknown.",
            },
        )
    )


@pytest.mark.asyncio
async def test_discovery_and_investigation_share_complete_source_with_separate_local_observations():
    ledger = book()
    review_state, changes, pack = inputs(1)
    pack.workspace_digest = "digest"
    scope = EvidenceScope("run", "head", "digest")
    cache = SharedEvidenceCache(scope, max_result_chars=None)
    text = "const consumer = old_value;\n" * 400
    calls = []
    read = {"path": "consumer.py", "start": 1, "end": 10000}

    async def execute(name, args):
        calls.append((name, args))
        if args["path"] == "consumer.py":
            return text
        return await source_executor([])(name, args)

    def discover(messages, _options, turn):
        if turn == 1:
            return receipt(call("read_file", read))
        return batch(lead(packet(messages)["pending_units"][0]))

    await HypothesisGenerator(
        SessionModel(handler=discover),
        executor=execute,
        evidence_cache=cache,
    ).run(review_state, pack, changes, ledger)
    discovery = next(iter(ledger.discovery_blocks.values()))["observations"]
    local_discovery = [obs for obs in discovery if obs["path"] == "consumer.py"]
    assert "".join(obs["excerpt"] for obs in local_discovery) == text

    def investigate(messages, _options, turn):
        if turn == 1:
            return receipt(call("read_file", read))
        return unknown(packet(messages))

    results = await Investigator(
        SessionModel(handler=investigate),
        execute,
        protocol_version=11,
        evidence_cache=cache,
    ).run(ledger, review_state, pack)
    local = [obs for obs in results[0].observations if obs.path == "consumer.py"]
    assert "".join(obs.excerpt for obs in local) == text
    assert len([entry for entry in calls if entry == ("read_file", read)]) == 1
    assert {obs.shared_result_id for obs in local} == {obs["shared_result_id"] for obs in local_discovery}
    assert all(obs.sha == "head" and len(obs.excerpt) <= 1200 for obs in local)
    assert cache.entry_count > 0  # An external owner keeps the cache alive after run().
    assert (await cache.execute("read_file", read, execute, scope=scope)).reused
    await cache.close()


@pytest.mark.asyncio
async def test_shared_explicit_diff_preserves_every_hunk_and_long_source_fragment():
    scope = EvidenceScope("run", "head", "digest")
    cache = SharedEvidenceCache(scope, max_result_chars=None)
    patch = "@@ -1,2 +1,2 @@\n-old\n+" + "long source " * 700 + "\n context\n"
    patch += "@@ -30 +30 @@\n-old tail\n+new tail\n"
    review_state = state()
    review_state.file_diffs = {"a.ts": patch}
    calls = []

    async def execute(name, args):
        calls.append((name, args))
        return patch

    consumers = [
        Investigator(None, execute, protocol_version=11, evidence_cache=cache, evidence_scope=scope) for _ in range(2)
    ]
    for worker in consumers:
        worker._prepare_investigation(candidate(), review_state, ContextPack(workspace_digest="digest"))
        reply = await worker._run_tool("read_diff", {"path": "a.ts", "start": 1, "end": 30})
        assert "new tail" in reply
        assert "".join(obs.excerpt for obs in worker._observations) == patch
        assert not any(entry.get("phase") == "diff-navigation" for entry in worker._progress)
    assert len(calls) == 1
    assert {obs.shared_result_id for obs in consumers[0]._observations} == {
        obs.shared_result_id for obs in consumers[1]._observations
    }
    await cache.close()


@pytest.mark.asyncio
async def test_pipeline_passes_one_cache_through_actual_discovery_and_investigation(monkeypatch):
    ledger = book()
    review_state, changes, pack = inputs(1)
    pack.workspace_digest = "digest"
    calls, caches = [], []
    text = "# shared consumer source\n" * 400
    base_cache = pipeline_v4.SharedEvidenceCache

    class TrackingCache(base_cache):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            caches.append(self)

    monkeypatch.setattr(pipeline_v4, "SharedEvidenceCache", TrackingCache)

    def read(path):
        calls.append(path)
        return text

    def answer(messages, _options, turn):
        data = packet(messages)
        if "case_id" in data:
            if not any(obs.get("path") == "consumer.py" for obs in data["observations"]):
                return receipt(call("read_file", {"path": "consumer.py", "start": 1, "end": 10000}))
            assert "discovery" in str(data["shared_review_workspace_untrusted"])
            return unknown(data)
        if turn == 1:
            return receipt(call("read_file", {"path": "consumer.py", "start": 1, "end": 10000}))
        if turn == 2:
            evidence_ids = [
                identity
                for identity, record in ledger.evidence_records.items()
                if record.get("observation", {}).get("path") == "consumer.py"
            ]
            return receipt(
                call(
                    "checkpoint_review_progress",
                    {
                        "summary": "Investigated the consumer source.",
                        "open_questions": ["What contract is required?"],
                        "resolved_questions": [],
                        "next_action": "Verify the consumer contract.",
                        "evidence_ids": evidence_ids,
                    },
                )
            )
        return batch(lead(data["pending_units"][0]))

    model = SessionModel(handler=answer)
    runtime = SimpleNamespace(
        _model_router=SimpleNamespace(get_llm=lambda *_: model),
        _events=EventBus(),
        _db=None,
    )
    result = await pipeline_v4._run_llm_stages(
        runtime,
        review_state,
        changes,
        pack,
        ledger,
        SimpleNamespace(read=read),
        PipelineV4Config(investigator_concurrency=1),
        "en",
        {},
    )
    assert len(caches) == 1 and calls.count("consumer.py") == 1
    assert not result.comments and ledger.items
    assert caches[0].entry_count == caches[0].byte_count == 0
    with pytest.raises(RuntimeError, match="closed"):
        await caches[0].execute("read_file", {}, source_executor([]), scope=caches[0].scope)
    notes = SharedReviewWorkspace(ledger).view(paths=["consumer.py"], role="investigation")["notes"]
    assert {note["actor"] for note in notes} == {"discovery", "investigation"}


@pytest.mark.asyncio
async def test_shared_notes_raw_evidence_and_checkpoint_survive_restore_without_becoming_proof():
    ledger = book()
    item = candidate()
    ledger.upsert(item)
    item = ledger.items[item.identity]
    store = EvidenceStore(ledger)
    workspace = SharedReviewWorkspace(ledger)
    worker = Investigator(
        None,
        source_executor([]),
        protocol_version=11,
        shared_workspace=workspace,
        evidence_store=store,
    )
    pack = ContextPack(workspace_digest="digest")
    worker._prepare_investigation(item, state(), pack)
    await worker._run_tool("read_file", {"path": "a.ts", "start": 1, "end": 20})
    evidence_id = store.observation(worker._observations[0])
    workspace.publish_progress(
        task_id="other-group",
        actor="discovery",
        unit_ids=[item.unit_id],
        paths=["a.ts"],
        summary="PRIVATE_UPSTREAM_JUDGMENT",
        open_questions=["Does the caller guard input?"],
        next_action="Read the caller.",
        evidence_ids=[evidence_id],
    )
    before = copy.deepcopy(ledger.evidence_records)
    reply = await worker._run_tool(
        "checkpoint_investigation",
        {
            "summary": "PRIVATE_INVESTIGATION_NOTE",
            "open_questions": ["caller guard?"],
            "resolved_questions": [],
            "next_action": "Read caller",
            "evidence_ids": [evidence_id],
        },
    )
    assert "untrusted-navigation-only" in reply
    item.investigation_progress = list(worker._progress)
    restored = HypothesisLedger.from_dict(ledger.to_dict())
    restored_item = restored.items[item.identity]
    restored_worker = Investigator(
        None,
        source_executor([]),
        protocol_version=11,
        shared_workspace=SharedReviewWorkspace(restored),
        evidence_store=EvidenceStore(restored),
    )
    restored_worker._prepare_investigation(restored_item, state(), pack)
    data = packet(restored_worker._exploration_chat(restored_item, pack))
    assert "PRIVATE_UPSTREAM_JUDGMENT" in str(data["shared_review_workspace_untrusted"])
    assert "PRIVATE_INVESTIGATION_NOTE" in str(data["shared_review_workspace_untrusted"])
    view = data["shared_review_workspace_untrusted"]
    assert view["evidence_catalog"] == []
    assert view["evidence_directory"]
    assert "query_review_workspace" in data["shared_workspace_instruction"]
    assert any(entry.get("phase") == "shared-checkpoint" for entry in restored_worker._progress)
    assert restored.evidence_records == before
    assert not any("PRIVATE_" in str(record) for record in restored.evidence_records.values())
    imported = await restored_worker._run_tool("read_shared_evidence", {"evidence_ids": [evidence_id]})
    assert "obs_0:e1" in imported and restored_worker._observations[0].excerpt == "return calendar?.id"
    assert restored_worker._observations[0].sha == "head"
    # The independent verifier receives only its source representation.
    from reviewforge.engine.source_audit import messages

    restored_item.observations = list(restored_worker._observations)
    assert "PRIVATE_" not in messages([restored_item], restored)[-1].content
    assert "PRIVATE_" not in str(SharedReviewWorkspace(restored).view(role="proof"))
    assert "PRIVATE_" not in str(SharedReviewWorkspace(restored).view(role="source_audit"))


@pytest.mark.asyncio
async def test_dependency_query_can_import_context_but_cannot_relabel_before_or_failures_as_head_source():
    ledger = book()
    store, workspace = EvidenceStore(ledger), SharedReviewWorkspace(ledger)
    context = store.source(SourceLine("context", "consumer.py", 10, "consumer(value)", changed=False))
    before = store.source(SourceLine("before", "a.ts", 1, "old_source", changed=True), base_sha="base")
    workspace.publish_progress(
        task_id="dependency-review",
        actor="discovery",
        paths=["consumer.py"],
        summary="DEPENDENCY_NOTE",
        evidence_ids=[context],
    )
    worker = Investigator(
        None, source_executor([]), protocol_version=11, shared_workspace=workspace, evidence_store=store
    )
    worker._prepare_investigation(candidate(), state(), ContextPack(workspace_digest="digest"))
    assert "DEPENDENCY_NOTE" not in str(packet(worker._exploration_chat(candidate(), ContextPack())))
    result = await worker._run_tool("query_review_workspace", {"paths": ["consumer.py"], "unit_ids": []})
    assert "DEPENDENCY_NOTE" in result and context in result
    mixed = await worker._run_tool("read_shared_evidence", {"evidence_ids": [context, before]})
    assert json.loads(mixed)["status"] == "error" and not worker._observations
    result = await worker._run_tool("read_shared_evidence", {"evidence_ids": [context]})
    assert "consumer(value)" in result and worker._observations[0].sha == "head"
    assert worker._observations[0].excerpt_line_range == (10, 10)
    long_text = "long_line" * 1500
    long_source = store.source(SourceLine("context", "long.py", 9, long_text, changed=False))
    count = len(worker._observations)
    reply = await worker._run_tool("read_shared_evidence", {"evidence_ids": [long_source]})
    assert "Saved evidence" in reply
    assert "".join(obs.excerpt for obs in worker._observations[count:]) == long_text
    assert all(obs.excerpt_line_range == (9, 9) for obs in worker._observations[count:])
    count = len(worker._observations)
    await worker._run_tool("read_shared_evidence", {"evidence_ids": [long_source]})
    assert len(worker._observations) == count


@pytest.mark.asyncio
async def test_transaction_delivers_workspace_success_and_repairable_errors_without_false_failures():
    ledger = book()
    workspace, store = SharedReviewWorkspace(ledger), EvidenceStore(ledger)
    evidence_id = store.source(SourceLine("context", "consumer.py", 10, "consumer(value)", changed=False))
    workspace.publish_progress(
        task_id="consumer-review",
        actor="discovery",
        paths=["consumer.py"],
        summary="Consumer contract still needs verification.",
        evidence_ids=[evidence_id],
    )
    imported_ids = []

    def answer(messages, _options, turn):
        data = packet(messages)
        feedback = json.loads(messages[-1].content).get("controller_receipt", []) if turn > 1 else []
        if turn == 1:
            return receipt(
                call(
                    "checkpoint_investigation",
                    {
                        "summary": "Checked changed operation; now checking the consumer.",
                        "open_questions": ["What does the consumer require?"],
                        "resolved_questions": [],
                        "next_action": "Read shared consumer evidence.",
                        "evidence_ids": [evidence_id],
                    },
                )
            )
        response = feedback[0]
        if turn == 2:
            assert response["action"] == "checkpoint_investigation" and "error" not in response
            assert response["result"]["note"]["next_action"] == "Read shared consumer evidence."
            assert not response["observations"]
            assert "Checked changed operation" in str(data["shared_review_workspace_untrusted"])
            assert data["shared_review_workspace_untrusted"]["evidence_catalog"] == []
            assert data["shared_review_workspace_untrusted"]["evidence_directory"]
            return receipt(call("query_review_workspace", {"paths": ["consumer.py"], "unit_ids": []}))
        if turn == 3:
            assert response["action"] == "query_review_workspace" and "error" not in response
            assert "Consumer contract still needs verification." in str(response["result"]["notes"])
            assert evidence_id in str(response["result"]["evidence_catalog"])
            return receipt(call("read_shared_evidence", {"evidence_ids": ["ev_unknown"]}))
        if turn == 4:
            assert response["error"]["code"] == "invalid-shared-workspace-action-or-evidence-binding"
            assert "result" not in response and not response["observations"]
            return receipt(call("read_shared_evidence", {"evidence_ids": [evidence_id]}))
        if turn == 5:
            assert "error" not in response and response["result"]["evidence"]
            imported_ids.extend(response["observations"])
            assert imported_ids == [response["result"]["evidence"][0]["observation_id"]]
            assert "consumer(value)" in response["result"]["evidence"][0]["saved_evidence"]
            assert "consumer(value)" in str(data["observations"])
            return receipt(call("read_shared_evidence", {"evidence_ids": [evidence_id]}))
        assert turn == 6
        assert "error" not in response and not response["observations"]
        assert [entry["observation_id"] for entry in response["result"]["evidence"]] == imported_ids
        return unknown(data)

    model = SessionModel(handler=answer)
    worker = Investigator(
        model, source_executor([]), protocol_version=11, shared_workspace=workspace, evidence_store=store
    )
    item, pack = candidate(), ContextPack(workspace_digest="digest")
    result = await worker.investigate(item, state(), pack)
    assert result.verdict == "unknown" and result.outcome == "insufficient-evidence"
    assert len(model.calls) == 6 and len(imported_ids) == 1
    item.observations, item.investigation_progress = result.observations, result.progress
    replay = await worker.investigate(item, state(), pack)
    assert replay.verdict == result.verdict and len(model.calls) == 6
    assert replay.observations == result.observations


def test_direct_worker_cannot_mix_workspaces_and_evidence_stores_from_different_runs():
    ledger, other = book(), HypothesisLedger("other", "head", "digest", protocol_version=11)
    worker = Investigator(
        None,
        source_executor([]),
        protocol_version=11,
        shared_workspace=SharedReviewWorkspace(other),
        evidence_store=EvidenceStore(ledger),
    )
    with pytest.raises(ValueError, match="scope"):
        worker._prepare_investigation(candidate(), state(), ContextPack(workspace_digest="digest"))


@pytest.mark.asyncio
async def test_investigator_rejects_external_cache_from_another_run_before_execution():
    ledger = book()
    cache = SharedEvidenceCache(EvidenceScope("another-run", "head", "digest"), max_result_chars=None)
    with pytest.raises(ValueError, match="scope"):
        await Investigator(None, source_executor([]), protocol_version=11, evidence_cache=cache).run(
            ledger,
            state(),
            ContextPack(workspace_digest="digest"),
        )
    await cache.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("interrupt", ["error", "cancel", "complete"])
async def test_pipeline_cache_is_owned_until_finally_and_closed_on_all_exits(monkeypatch, interrupt):
    entered, hold = asyncio.Event(), asyncio.Event()
    captured = []

    async def stages(*_args, **kwargs):
        captured.append(kwargs["evidence_cache"])
        entered.set()
        if interrupt == "error":
            raise RuntimeError("stage failed")
        if interrupt == "cancel":
            await hold.wait()
        return "publication"

    monkeypatch.setattr(pipeline_v4, "_run_llm_stages_cached", stages)
    task = asyncio.create_task(
        pipeline_v4._run_llm_stages(
            SimpleNamespace(),
            state(),
            None,
            ContextPack(workspace_digest="digest"),
            book(),
            None,
            None,
            "en",
            {},
        )
    )
    await entered.wait()
    if interrupt == "cancel":
        task.cancel()
    if interrupt in {"cancel", "error"}:
        with pytest.raises(asyncio.CancelledError if interrupt == "cancel" else RuntimeError):
            await task
    else:
        assert await task == "publication"
    with pytest.raises(RuntimeError, match="closed"):
        await captured[0].execute("read_file", {"path": "a"}, source_executor([]), scope=captured[0].scope)


@pytest.mark.asyncio
async def test_external_investigation_cache_cleanup_does_not_cancel_another_consumer(monkeypatch):
    ledger = book()
    ledger.upsert(candidate())
    entered, release = asyncio.Event(), asyncio.Event()
    scope = EvidenceScope("run", "head", "digest")
    cache = SharedEvidenceCache(scope, max_result_chars=None)

    async def execute(_name, _args):
        entered.set()
        await release.wait()
        return "source"

    async def cancelled(_worker, *_args, **_kwargs):
        await cache.execute("read_file", {"path": "shared"}, execute, scope=scope)
        raise AssertionError("cancelled worker must not finish")

    monkeypatch.setattr(Investigator, "investigate", cancelled)
    task = asyncio.create_task(
        Investigator(None, execute, protocol_version=11, evidence_cache=cache).run(
            ledger,
            state(),
            ContextPack(workspace_digest="digest"),
        )
    )
    await entered.wait()
    second = asyncio.create_task(cache.execute("read_file", {"path": "shared"}, execute, scope=scope))
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()
    assert (await second).text == "source"
    await cache.close()


@pytest.mark.asyncio
async def test_cancelled_standalone_run_closes_owned_cache_and_pending_reads(monkeypatch):
    from reviewforge.engine import investigator as investigation_module

    ledger = book()
    ledger.upsert(candidate())
    entered, stopped = asyncio.Event(), asyncio.Event()
    caches = []

    class TrackingCache(SharedEvidenceCache):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            caches.append(self)

    async def execute(_name, _args):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    async def investigate(worker, *_args, **_kwargs):
        await worker._evidence_cache.execute("read_file", {}, execute, scope=worker._evidence_cache.scope)

    monkeypatch.setattr(investigation_module, "SharedEvidenceCache", TrackingCache)
    monkeypatch.setattr(Investigator, "investigate", investigate)
    task = asyncio.create_task(
        Investigator(None, execute, protocol_version=11).run(
            ledger,
            state(),
            ContextPack(workspace_digest="digest"),
        )
    )
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stopped.is_set() and len(caches) == 1
    with pytest.raises(RuntimeError, match="closed"):
        await caches[0].execute("read_file", {}, execute, scope=caches[0].scope)
