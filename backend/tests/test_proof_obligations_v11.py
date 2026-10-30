"""Missing proof inputs trigger source work, never a new candidate decision."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage
from test_evidence_followup import Worker, result
from test_proof_gate import GateLLM, candidate, ledger, observation, review
from test_review_session_v5 import SessionModel, call, packet, receipt
from test_source_audit import audit

from reviewforge.core.state import StateStore
from reviewforge.engine.context_pack import ContextPack
from reviewforge.engine.evidence_followup import run_evidence_followup
from reviewforge.engine.hypothesis import HypothesisLedger
from reviewforge.engine.investigation_evidence import EvidenceScope, SharedEvidenceCache
from reviewforge.engine.investigator import Investigator, build_workspace_executor
from reviewforge.engine.proof_gate import ProofGate, completed
from reviewforge.engine.proof_obligations import source_facts, source_state
from reviewforge.engine.source_audit import current


async def ready():
    item = candidate()
    item.investigation_task_id = "task"
    item.observations.append(
        replace(
            observation("obs_2", "function consumer(value) { return lookup(value); }\n"),
            path="consumer.ts",
            query='read_file {"path":"consumer.ts","start":1,"end":1}',
            excerpt_line_range=(1, 1),
        )
    )
    book = ledger(item, protocol=11)
    book.investigation_tasks["task"] = {
        "members": [item.identity],
        "charged_to": item.identity,
        "budget_steps_limit": None,
        "token_limit": None,
        "budget_policy": "usage-only-v1",
        "total_steps": 2,
        "total_tokens": 100,
        "total_unreported_tokens": 0,
    }
    missing = audit(item.id)
    missing.update(contracts=[], gaps=["Locate the caller's supported-input requirement."])
    await ProofGate(GateLLM(replies=[{"audits": [missing]}])).run(book)
    assert completed(item, book) and current(item, book)
    return item, book


async def follow(worker, book, update=None):
    return await run_evidence_followup(
        worker,
        book,
        StateStore(head_sha="head", files_changed=["app.ts"], file_diffs={"app.ts": "patch"}),
        ContextPack(workspace_digest="workspace"),
        update,
    )


async def reaudit_missing(item, book, *, gap="Locate the caller's supported-input requirement."):
    missing = audit(item.id)
    missing.update(contracts=[], gaps=[gap])
    await ProofGate(GateLLM(replies=[{"audits": [missing]}])).run(book)


@pytest.mark.asyncio
async def test_structured_obligations_direct_actual_collector_to_observed_consumer_then_blind_reaudit():
    item, book = await ready()
    original = copy.deepcopy(item.to_dict())
    paths = []

    async def execute(name, args):
        paths.append((name, args))
        if name == "find_references":
            return "- spec.ts:12: lookup(absentValue)"
        return "assert.equal(lookup(undefined), null); // absent values are supported\n"

    def answer(messages, _options, turn):
        data = packet(messages)
        assert "candidate" not in data and "proof_duty" not in data
        assert "supported absent input must return null" not in json.dumps(data).lower()
        obligations = data["proof_obligations"]
        assert "behavior-contract" in {row["kind"] for row in obligations}
        assert any(
            action["args"].get("symbol") == "lookup"
            for obligation in obligations
            for action in obligation["next_actions"]
        )
        assert all(row["existing_refs"] and row["paths"] for row in obligations)
        if turn == 1:
            return receipt(call("find_references", {"symbol": "lookup"}))
        if turn == 2:
            assert "spec.ts" in str(data["source_navigation"])
            return receipt(call("read_file", {"path": "spec.ts", "start": 12, "end": 12}))
        return AIMessage(content="No further source is needed for this collection segment.")

    model = SessionModel(handler=answer)
    worker = Investigator(model, execute, protocol_version=11)
    assert await follow(worker, book)
    assert len(model.calls) == 3 and len(paths) == 2
    assert item.claim == original["claim"] and item.assessment.to_dict() == original["assessment"]
    assert not current(item, book)
    new_observation = next(obs for obs in item.observations if obs.path == "spec.ts")
    reconstructed = audit(item.id)
    reconstructed["contracts"][0].update(
        kind="test",
        evidence=[f"{item.id}/{new_observation.id}:e1"],
        statement="The repository test supports absent values and requires null.",
    )
    semantic_unknown = {
        key: value
        for key, value in review(item.id, "unknown").items()
        if key in {"hypothesis_id", "decision", "reason", "missing_obligations"}
    }
    verifier = GateLLM(replies=[{"audits": [reconstructed]}, {"reviews": [semantic_unknown]}])
    await ProofGate(verifier).run(book)
    blind = json.loads(verifier.calls[0][0][-1].content)["items"][0]
    assert set(blind) == {"hypothesis_id", "observations", "applicable_rules", "knowledge_sources"}
    assert "proof_obligations" not in str(blind) and item.claim not in str(blind)
    assert completed(item, book) and book.publication_reviews[item.id]["validated"]
    assert not await follow(worker, book) and len(model.calls) == 3
    record = book.investigation_tasks["task"]["evidence_followup_v11"]
    assert record["segments"][0]["new_facts"] > 0
    assert book.investigation_tasks["task"]["total_tokens"] == 460


@pytest.mark.asyncio
async def test_new_sources_allow_more_than_one_segment_and_restore_while_same_state_stops():
    item, book = await ready()
    for number in range(4):
        addition = replace(
            observation(f"obs_extra{number}", f"consumer premise {number}\n"),
            path=f"consumer{number}.ts",
            excerpt_line_range=(1, 1),
        )
        worker = Worker(result(item, additions=[addition], unreported=0))
        assert await follow(worker, book)
        await reaudit_missing(item, book)
        book = HypothesisLedger.from_dict(book.to_dict())
        item = book.items[item.identity]
    task = book.investigation_tasks["task"]
    assert len(task["evidence_followup_v11"]["segments"]) == 4 and task["total_tokens"] == 220
    no_progress = Worker(result(item, additions=[], unreported=0))
    assert not await follow(no_progress, book)
    assert completed(item, book)
    snapshot = HypothesisLedger.from_dict(book.to_dict())
    assert not await follow(no_progress, snapshot) and len(no_progress.calls) == 1
    assert snapshot.investigation_tasks["task"]["evidence_followup_v11"]["state"] == "incomplete"


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [RuntimeError("PRIVATE_TRANSPORT"), asyncio.CancelledError()])
async def test_failed_or_cancelled_collection_preserves_new_sources_and_exact_usage_for_resume(error):
    item, book = await ready()
    addition = replace(observation("obs_new", "new consumer premise\n"), path="consumer_new.ts")
    worker = Worker(result(item, additions=[addition], tokens=30, unreported=4), error=error)
    if isinstance(error, asyncio.CancelledError):
        with pytest.raises(asyncio.CancelledError):
            await follow(worker, book)
    else:
        assert await follow(worker, book)
    assert any(obs.id == "obs_new" for obs in item.observations)
    saved = book.investigation_tasks["task"]["evidence_followup_v11"]["segments"][0]
    assert saved["state"] == "interrupted" and saved["new_facts"]
    assert saved["charged"] == {"tokens": 30, "unreported_tokens": 4, "steps": 1}
    assert "PRIVATE_TRANSPORT" not in str(saved)
    await reaudit_missing(item, book)
    restored = HypothesisLedger.from_dict(book.to_dict())
    restored_item = restored.items[item.identity]
    next_worker = Worker(result(restored_item, additions=[], tokens=20, unreported=0))
    assert not await follow(next_worker, restored)
    assert restored.investigation_tasks["task"]["total_tokens"] == 150
    assert restored.investigation_tasks["task"]["total_unreported_tokens"] == 4


@pytest.mark.asyncio
async def test_real_cancelled_collector_checkpoints_new_source_before_next_dispatch_and_restores():
    item, book = await ready()
    checkpoints = []

    async def checkpoint(current_book):
        checkpoints.append(copy.deepcopy(current_book.to_dict()))

    async def execute(_name, _args):
        return "// a newly acquired consumer contract\n"

    class CancellingModel(SessionModel):
        async def ainvoke(self, messages, config=None, **kwargs):
            if self.calls:
                self.calls.append((messages, kwargs))
                raise asyncio.CancelledError()
            return self._generate(messages, **kwargs).generations[0].message

    model = CancellingModel(
        handler=lambda *_: receipt(
            call(
                "read_file",
                {"path": "contract.ts", "start": 1, "end": 1},
            )
        )
    )
    with pytest.raises(asyncio.CancelledError):
        await follow(Investigator(model, execute, protocol_version=11), book, checkpoint)
    assert len(model.calls) == 2
    restored = HypothesisLedger.from_dict(checkpoints[-1])
    saved_item = restored.items[item.identity]
    assert any(obs.path == "contract.ts" for obs in saved_item.observations)
    task = restored.investigation_tasks["task"]
    assert task["total_tokens"] == 220 and task["total_steps"] == 4
    assert task["evidence_followup_v11"]["segments"][0]["state"] == "interrupted"
    await reaudit_missing(saved_item, restored)
    worker = Worker(result(saved_item, additions=[], tokens=20, unreported=0))
    assert not await follow(worker, restored)
    assert task["total_tokens"] == 240


@pytest.mark.asyncio
async def test_other_frozen_candidate_is_not_reaudited_or_redispatched_for_this_obligation():
    item, book = await ready()
    other = candidate("h_frozen")
    other.investigation_task_id = "other-task"
    book.items[other.identity] = other
    book.investigation_tasks["other-task"] = {
        **copy.deepcopy(book.investigation_tasks["task"]),
        "members": [other.identity],
        "charged_to": other.identity,
    }
    semantic_unknown = {
        key: value
        for key, value in review(other.id, "unknown").items()
        if key in {"hypothesis_id", "decision", "reason", "missing_obligations"}
    }
    verifier = GateLLM(replies=[{"audits": [audit(other.id)]}, {"reviews": [semantic_unknown]}])
    await ProofGate(verifier).run(book)
    frozen_item, frozen_audit, frozen_review = map(
        copy.deepcopy,
        (
            other.to_dict(),
            book.source_audits[other.id],
            book.publication_reviews[other.id],
        ),
    )
    worker = Worker(result(item, additions=[replace(observation("obs_new", "new contract\n"), path="new.ts")]))
    assert await follow(worker, book)
    missing = audit(item.id)
    missing.update(contracts=[], gaps=["Read the consumer contract."])
    recheck = GateLLM(replies=[{"audits": [missing]}])
    await ProofGate(recheck).run(book)
    assert len(recheck.calls) == 1
    assert [entry["hypothesis_id"] for entry in json.loads(recheck.calls[0][0][-1].content)["items"]] == [item.id]
    assert other.to_dict() == frozen_item
    assert book.source_audits[other.id] == frozen_audit and book.publication_reviews[other.id] == frozen_review


@pytest.mark.asyncio
async def test_failed_direction_or_rephrased_windows_cannot_make_source_progress():
    item, book = await ready()
    queries = []

    async def execute(name, args):
        queries.append((name, args))
        return "No results"

    def answer(_messages, _options, turn):
        return receipt(call("grep", {"pattern": "lookup", "max_hits": turn * 10}))

    model = SessionModel(handler=answer)
    assert not await follow(Investigator(model, execute, protocol_version=11), book)
    assert len(queries) == 1 and len(model.calls) == 2
    record = book.investigation_tasks["task"]["evidence_followup_v11"]
    assert record["state"] == "incomplete"
    assert record["segments"][0]["failed_directions"][0]["args"] == {"pattern": "lookup", "glob": ""}
    assert not await follow(Investigator(model, execute, protocol_version=11), book)
    assert len(model.calls) == 2
    before = source_state(item.observations)
    assert source_state([*item.observations, replace(item.observations[0], id="window", query="different")]) == before
    already_read = item.observations[-1]
    same_search_hit = replace(
        already_read, id="other-tool", tool="grep", excerpt=("- consumer.ts:1: " + already_read.excerpt.rstrip("\n"))
    )
    assert source_state([*item.observations, same_search_hit]) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["old_marker", "finite", "missing_counter", "binding"])
async def test_legacy_or_mismatched_followup_state_cannot_gain_new_execution_policy(damage):
    item, book = await ready()
    task = book.investigation_tasks["task"]
    if damage == "old_marker":
        task["evidence_followup"] = {"state": "completed"}
    elif damage == "finite":
        task.update(budget_steps_limit=8, token_limit=10000)
    elif damage == "missing_counter":
        task.pop("total_tokens")
    else:
        task["evidence_followup_v11"] = {"binding": {}, "binding_id": "other", "segments": []}
    worker = Worker(result(item))
    if damage == "binding":
        with pytest.raises(ValueError, match="binding"):
            await follow(worker, book)
    else:
        assert not await follow(worker, book)
    assert not worker.calls


@pytest.mark.asyncio
async def test_targeted_run_only_dispatches_selected_identity_and_old_protocol_rejects_filter():
    from test_investigation_v3 import candidate as open_candidate
    from test_investigation_v3 import source_executor, state
    from test_shared_investigation_v11 import unknown

    selected, other = open_candidate("first"), open_candidate("second")
    book = HypothesisLedger("run", "head", "digest", protocol_version=11)
    book.upsert(selected)
    book.upsert(other)
    model = SessionModel(handler=lambda messages, *_: unknown(packet(messages)))
    await Investigator(model, source_executor([]), protocol_version=11).run(
        book,
        state(),
        ContextPack(workspace_digest="digest"),
        target_identities={selected.identity},
    )
    assert len(model.calls) == 1 and book.items[other.identity].status.value == "open"
    assert not book.items[other.identity].investigation_task_id
    book.protocol_version = 10
    with pytest.raises(ValueError, match="protocol 11"):
        await Investigator(model, source_executor([]), protocol_version=10).run(
            book,
            state(),
            ContextPack(workspace_digest="digest"),
            target_identities={other.identity},
        )


@pytest.mark.asyncio
async def test_actual_review_context_source_hook_preserves_full_doc_receipt_and_bypasses_source_cache():
    from test_investigation_v3 import candidate as open_candidate

    reads = []
    content = "Supported absent values must return null.\n" * 400

    async def read_async(path):
        reads.append(path)
        return content

    workspace = SimpleNamespace(
        head_sha="head",
        digest="workspace",
        source="tarball",
        truncated=False,
        list_paths=lambda: ["README.md"],
        read_async=read_async,
    )
    state = StateStore(repo="owner/repo", head_sha="head")
    execute = build_workspace_executor(workspace, state, protocol_version=11)
    scope = EvidenceScope("run", "head", "workspace")
    cache = SharedEvidenceCache(scope, max_result_chars=None)
    for _ in range(2):
        worker = Investigator(None, execute, protocol_version=11, evidence_cache=cache, evidence_scope=scope)
        item = open_candidate()
        worker._prepare_investigation(item, state, ContextPack(workspace_digest="workspace"))
        names = {tool.name for tool in worker._build_tools()}
        assert {"query_review_knowledge", "read_review_knowledge", "run_repository_test"} <= names
        await worker._run_tool("query_review_knowledge", {"query": "", "paths": []})
        assert all(obs.status == "not_found" for obs in worker._observations)
        data = packet(worker._exploration_chat(item, ContextPack(workspace_digest="workspace")))
        assert "README.md" in str(data["review_context_navigation"])
        before = len(worker._observations)
        await worker._run_tool("read_review_knowledge", {"path": "README.md"})
        raw = "".join(obs.excerpt for obs in worker._observations[before:])
        assert json.loads(raw)["content"] == content
        assert all(
            obs.status == "success" and obs.tool == "read_review_knowledge" for obs in worker._observations[before:]
        )
        metadata = next(row for row in worker._progress if row.get("phase") == "review-context-receipt")
        assert metadata["receipt_sha256"] == hashlib.sha256(raw.encode()).hexdigest()
        assert metadata["observation_ids"] == [obs.id for obs in worker._observations[before:]]
        assert any(
            row.get("phase") == "source-window-v9" and row["observation_ids"] == metadata["observation_ids"]
            for row in worker._progress
        )
    assert reads == ["README.md", "README.md"] and cache.entry_count == 0
    await cache.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["unavailable", "error", "timeout", "resource-limit"])
async def test_failed_isolated_execution_receipts_are_navigation_and_never_successful_source(status):
    from test_investigation_v3 import candidate as open_candidate

    from reviewforge.engine.review_context import ReviewContext

    workspace = SimpleNamespace(head_sha="head", digest="workspace")
    state = StateStore(repo="owner/repo", head_sha="head")
    context = ReviewContext(workspace, state, protocol_version=11)

    async def execute(name, args):
        return json.dumps(
            {
                "status": status,
                "head_sha": "head",
                "workspace_digest": "workspace",
                "reason": "No actual test execution.",
            }
        )

    execute.review_context = context
    worker = Investigator(None, execute, protocol_version=11)
    item = open_candidate()
    worker._prepare_investigation(item, state, ContextPack(workspace_digest="workspace"))
    await worker._run_tool("run_repository_test", {"runtime": "python-unittest", "path": "tests/test_source.py"})
    assert not any(obs.status == "success" for obs in worker._observations)
    from reviewforge.engine.proof_gate import _packet

    item.observations, item.investigation_progress = worker._observations, worker._progress
    blind_source = _packet(item)["observations"]
    assert all(not row["citable"] for row in blind_source)
    assert "No actual test execution" not in str(blind_source)


def execution_receipt(**changes):
    return {
        "status": "success",
        "head_sha": "head",
        "workspace_digest": "workspace",
        "runtime": "node-test",
        "path": "tests/test_source.mjs",
        "argv": ["node", "--test", "tests/test_source.mjs"],
        "image_id": "sha256:" + "a" * 64,
        "execution_image_id": "sha256:" + "b" * 64,
        "source_sha256": "c" * 64,
        "exit_code": 0,
        "stdout": "ok 1 - source\n# duration_ms 1.2\n",
        "stderr": "",
        "started_at": "2026-10-30T01:00:00Z",
        "finished_at": "2026-10-30T01:00:01Z",
        **changes,
    }


async def saved_context_receipt(raw, *, counter=0, tool="run_repository_test"):
    from test_investigation_v3 import candidate as open_candidate

    from reviewforge.engine.review_context import ReviewContext

    state = StateStore(repo="owner/repo", head_sha="head")
    context = ReviewContext(SimpleNamespace(head_sha="head", digest="workspace"), state, protocol_version=11)

    async def execute(_name, _args):
        return json.dumps(raw, ensure_ascii=False)

    execute.review_context = context
    worker = Investigator(None, execute, protocol_version=11)
    worker._prepare_investigation(open_candidate(), state, ContextPack(workspace_digest="workspace"))
    worker._obs_counter = counter
    args = {"runtime": raw["runtime"], "path": raw["path"]} if tool == "run_repository_test" else {"path": raw["path"]}
    await worker._run_tool(tool, args)
    return worker._observations, worker._progress


@pytest.mark.asyncio
async def test_verified_execution_progress_ignores_clocks_container_identity_and_node_durations():
    first = execution_receipt()
    repeated = execution_receipt(
        started_at="2026-10-30T02:00:00Z",
        finished_at="2026-10-30T02:00:02Z",
        execution_image_id="sha256:" + "d" * 64,
        stdout="ok 1 - source\n# duration_ms 923.4\n",
    )
    observations, progress = await saved_context_receipt(first)
    other_observations, other_progress = await saved_context_receipt(repeated, counter=100)
    expected = source_facts(observations, progress)
    assert len(expected) == 1
    assert expected == source_facts(other_observations, other_progress)
    assert source_state(observations, progress) == source_state(
        [*observations, *other_observations],
        [*progress, *other_progress],
    )
    assert "923.4" in "".join(obs.excerpt for obs in other_observations)
    assert not source_facts(observations)  # Fragments without complete controller metadata are not progress.
    damaged = copy.deepcopy(progress)
    next(row for row in damaged if row.get("phase") == "review-context-receipt")["receipt_sha256"] = "wrong"
    assert not source_facts(observations, damaged)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"source_sha256": "e" * 64},
        {"exit_code": 1},
        {"argv": ["node", "--test", "tests/different.mjs"]},
        {"image_id": "sha256:" + "f" * 64},
    ],
)
async def test_verified_execution_progress_changes_with_source_input_or_exit_result(change):
    before, before_progress = await saved_context_receipt(execution_receipt())
    after, after_progress = await saved_context_receipt(execution_receipt(**change), counter=100)
    assert len(source_facts(after, after_progress) - source_facts(before, before_progress)) == 1


@pytest.mark.asyncio
async def test_knowledge_progress_uses_verified_revision_and_content_hash_not_json_transport():
    content = "Supported absent values must return null.\n" * 100
    raw = {
        "status": "success",
        "head_sha": "head",
        "workspace_digest": "workspace",
        "kind": "documentation",
        "path": "README.md",
        "revision": "head",
        "content": content,
        "content_sha256": hashlib.sha256(content.encode()).hexdigest(),
    }
    before, before_progress = await saved_context_receipt(raw, tool="read_review_knowledge")
    after, after_progress = await saved_context_receipt(
        dict(reversed(list(raw.items()))), counter=100, tool="read_review_knowledge"
    )
    assert len(source_facts(before, before_progress)) == 1
    assert source_facts(before, before_progress) == source_facts(after, after_progress)
    raw["content"] += "A new requirement."
    raw["content_sha256"] = hashlib.sha256(raw["content"].encode()).hexdigest()
    changed, changed_progress = await saved_context_receipt(raw, counter=200, tool="read_review_knowledge")
    assert len(source_facts(changed, changed_progress) - source_facts(before, before_progress)) == 1


@pytest.mark.asyncio
async def test_normal_investigation_cannot_turn_reexecuted_timing_noise_into_progress(monkeypatch):
    from test_investigation_v3 import candidate as open_candidate
    from test_investigation_v3 import source_executor, state

    from reviewforge.engine.review_context import ReviewContext

    calls = []
    review_state = state()
    context = ReviewContext(SimpleNamespace(head_sha="head", digest="workspace"), review_state, protocol_version=11)

    async def execute(name, args):
        if name != "run_repository_test":
            return await source_executor([])(name, args)
        calls.append(args)
        return json.dumps(
            execution_receipt(
                started_at=f"2026-10-30T0{len(calls)}:00:00Z",
                stdout=f"# duration_ms {len(calls)}\n",
                execution_image_id="sha256:" + str(len(calls)) * 64,
            )
        )

    execute.review_context = context
    model = SessionModel(
        handler=lambda *_: receipt(
            call(
                "run_repository_test",
                {"runtime": "node-test", "path": "tests/test_source.mjs"},
            )
        )
    )
    worker = Investigator(model, execute, protocol_version=11)
    original_query = worker._query_observation
    original_run = worker._run_tool

    async def force_actual_read(name, args):
        if name == "run_repository_test":
            worker._tool_counts.clear()
        return await original_run(name, args)

    # Exercise the semantic guard independently of the local identical-query optimization.
    monkeypatch.setattr(
        worker,
        "_query_observation",
        lambda name, args: None if name == "run_repository_test" else original_query(name, args),
    )
    monkeypatch.setattr(worker, "_run_tool", force_actual_read)
    result = await worker.investigate(open_candidate(), review_state, ContextPack(workspace_digest="workspace"))
    assert result.reason == "no-progress-cycle" and result.verdict == "unknown"
    assert len(calls) == len(model.calls) == 3
    assert len([row for row in result.progress if row.get("phase") == "review-context-receipt"]) == 3
