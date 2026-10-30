"""Offline protocol 11 handoffs, local citations and semantic cycle detection."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage
from test_discovery_submission import fixture
from test_discovery_workflow_v6 import batch, no_issue, run
from test_review_session_v5 import SessionModel, call, packet, receipt

from reviewforge.engine.discovery import SourceLine
from reviewforge.engine.discovery_aliases import source_aliases
from reviewforge.engine.discovery_workflow import Work
from reviewforge.engine.discovery_workspace import (
    progress_state,
    remember_evidence,
    repeated_progress,
    workspace_action,
    workspace_view,
)
from reviewforge.engine.hypothesis import HypothesisLedger, Observation
from reviewforge.engine.review_session import EvidenceStore, ReviewSession
from reviewforge.engine.review_workspace import SharedReviewWorkspace


def book(protocol=11):
    return HypothesisLedger("run", "head", "digest", protocol_version=protocol)


def note(summary="Follow the consumer boundary", evidence_ids=()):
    return {
        "summary": summary,
        "open_questions": ["Does the actual caller support replacements?"],
        "resolved_questions": [],
        "next_action": "Read the existing consumer source",
        "evidence_ids": list(evidence_ids),
    }


def worker(ledger=None, task="consumer"):
    ledger = ledger or book()
    _, _, block, _ = fixture(1)
    record = {"units": {}, "workflow": {"feedback": []}}
    session = ReviewSession(record, task_id=task, phase="discovery", max_turns=None)
    broker = SimpleNamespace(_observations=[], _obs_counter=0)
    work = Work(block, record, broker, source_aliases(block.source_refs, scoped=True), session, 0, 11)
    work.workspace = SharedReviewWorkspace(ledger)
    return work, EvidenceStore(ledger)


def source_observation(
    *, identity="producer_99", path="consumer.py", start=7, text="return stored_value", tool="read_file"
):
    return Observation(
        identity,
        tool,
        f'{tool} {{"path": "{path}", "start": 1, "end": 20}}',
        path,
        (1, 20),
        "head",
        "native-result-digest",
        text,
        "success",
        (start, start),
    )


@pytest.mark.asyncio
async def test_v11_model_queries_other_group_notes_and_imports_its_own_citable_source():
    ledger = book()
    store = EvidenceStore(ledger)
    identity = store.observation(source_observation())
    SharedReviewWorkspace(ledger).publish_progress(
        task_id="producer-group", actor="discovery", paths=["consumer.py"], **note(evidence_ids=[identity])
    )

    def answer(messages, _options, turn):
        data = packet(messages)
        if turn == 1:
            assert not data["shared_review_workspace"]["notes"]
            return receipt(call("query_review_workspace", {"paths": ["consumer.py"], "unit_ids": []}))
        if turn == 2:
            view = json.loads(messages[-1].content)["controller_receipt"][0]["result"]
            assert view["notes"][0]["task_id"] == "producer-group"
            assert view["evidence_catalog"][0]["evidence_id"] == identity
            return receipt(call("read_shared_evidence", {"evidence_ids": [identity]}))
        assert any(path == "consumer.py" for path in data["files"].values())
        assert any("return stored_value" in text for _, text in data["rows"])
        work = next(iter(ledger.discovery_blocks.values()))
        assert work["observations"][0]["id"] == "obs_0"
        assert work["observations"][0]["id"] != "producer_99"
        return batch(no_issue(data["pending_units"][0]))

    async def execute(*_):
        pytest.fail("existing raw evidence must not execute a source tool")

    model = SessionModel(handler=answer)
    _, ledger = await run(model, count=1, ledger=ledger, executor=execute)
    assert len(model.calls) == 3 and len(ledger.no_issue_units) == 1 and not ledger.unresolved_units


@pytest.mark.asyncio
async def test_discovery_groups_handoff_a_live_dependency_without_another_execution():
    ledger = book()
    steps = {"producer": 0, "consumer": 0}
    executed = []
    shared_identity = ""

    async def execute(name, args):
        executed.append((name, args))
        return "return stored_value"

    def answer(messages, _options, _turn):
        nonlocal shared_identity
        data = packet(messages)
        units = data["pending_units"]
        group = "producer" if len(units) == 4 else "consumer"
        steps[group] += 1
        step = steps[group]
        if group == "producer":
            if step == 1:
                return receipt(call("read_file", {"path": "consumer.py", "start": 7, "end": 7}))
            if step == 2:
                assert data["shared_review_workspace"]["evidence_catalog"] == []
                shared_identity = next(iter(data["shared_review_workspace"]["local_observations"].values()))
                return receipt(call("checkpoint_review_progress", note(evidence_ids=[shared_identity])))
            return batch(*map(no_issue, units))
        if step == 1:
            return receipt(call("checkpoint_review_progress", note("Need the common consumer contract")))
        if step == 2:
            return receipt(call("query_review_workspace", {"paths": ["consumer.py"], "unit_ids": []}))
        if step == 3:
            view = json.loads(messages[-1].content)["controller_receipt"][0]["result"]
            assert any(shared_identity in row["evidence_ids"] for row in view["notes"])
            return receipt(call("read_shared_evidence", {"evidence_ids": [shared_identity]}))
        assert any("stored_value" in text for _, text in data["rows"])
        assert any(row["evidence_ids"] == [shared_identity] for row in data["shared_review_workspace"]["notes"])
        return batch(*map(no_issue, units))

    model = SessionModel(handler=answer)
    await run(model, count=5, ledger=ledger, executor=execute)
    assert len(executed) == 1 and len(model.calls) == 7
    assert len(ledger.no_issue_units) == 5 and not ledger.unresolved_units


@pytest.mark.asyncio
async def test_checkpoint_own_progress_and_outside_dependency_survive_resume():
    ledger = book()

    def first(messages, _options, turn):
        view = packet(messages)["shared_review_workspace"]
        if turn == 1:
            return receipt(call("read_file", {"path": "consumer.py", "start": 7, "end": 7}))
        if turn == 2:
            assert view["evidence_catalog"] == []
            identity = next(iter(view["local_observations"].values()))
            return receipt(call("checkpoint_review_progress", note(evidence_ids=[identity])))
        assert view["notes"][0]["summary"] == note()["summary"]
        raise RuntimeError("offline provider interrupted")

    model = SessionModel(handler=first)
    _, ledger = await run(model, count=1, ledger=ledger)
    assert ledger.unresolved_units and len(model.calls) == 3
    restored = HypothesisLedger.from_dict(ledger.to_dict())

    def resumed(messages, *_):
        view = packet(messages)["shared_review_workspace"]
        assert view["notes"][0]["open_questions"] == note()["open_questions"]
        assert any(entry.get("path") == "consumer.py" for entry in view["evidence_directory"])
        return batch(no_issue(packet(messages)["pending_units"][0]))

    continuation = SessionModel(handler=resumed)
    await run(continuation, count=1, ledger=restored)
    assert len(continuation.calls) == 1 and not restored.unresolved_units


@pytest.mark.asyncio
async def test_unknown_evidence_and_model_supplied_identity_are_rejected_before_checkpoint():
    def answer(messages, _options, turn):
        if turn == 1:
            return receipt(call("checkpoint_review_progress", note(evidence_ids=["ev_unknown"])))
        if turn == 2:
            assert "invalid-shared-workspace-action" in messages[-1].content
            return receipt(call("checkpoint_review_progress", note() | {"task_id": "forged"}))
        assert "invalid-shared-workspace-schema" in messages[-1].content
        return batch(no_issue(packet(messages)["pending_units"][0]))

    ledger = book()
    await run(SessionModel(handler=answer), count=1, ledger=ledger)
    assert not ledger.shared_workspace["notes"] and not ledger.unresolved_units


@pytest.mark.asyncio
async def test_reworded_notes_cannot_manufacture_progress_or_clean_coverage():
    def answer(_messages, _options, turn):
        return receipt(call("checkpoint_review_progress", note(summary=f"Same investigation rewritten {turn}")))

    model = SessionModel(handler=answer)
    _, ledger = await run(model, count=1, ledger=book())
    assert len(model.calls) == 2 and not ledger.no_issue_units
    assert set(ledger.unresolved_units.values()) == {"generator no-progress-cycle"}
    saved = next(iter(ledger.discovery_blocks.values()))
    assert any(row["kind"] == "no-progress-cycle" for row in saved["session"]["events"])
    assert saved["workflow"]["feedback"][-1]["error"] == "no-progress-cycle"
    assert "remains incomplete" in saved["workflow"]["feedback"][-1]["instruction"]


@pytest.mark.asyncio
async def test_changing_read_windows_and_explanation_still_converges_without_new_source():
    def answer(_messages, _options, turn):
        response = receipt(call("read_file", {"path": "consumer.py", "start": 1, "end": 10 + turn}))
        response.content = f"Different explanation {turn}"
        return response

    model = SessionModel(handler=answer)
    _, ledger = await run(model, count=1, ledger=book())
    assert len(model.calls) == 3 and not ledger.no_issue_units
    assert set(ledger.unresolved_units.values()) == {"generator no-progress-cycle"}
    assert len(next(iter(ledger.discovery_blocks.values()))["observations"]) == 3


@pytest.mark.asyncio
async def test_new_source_navigation_valid_submission_and_repair_allow_continuation():
    async def execute(name, args):
        if name == "grep":
            return "- consumer.py:7: return stored_value"
        return "return stored_value" if args["path"] == "consumer.py" else "assert result == stored_value"

    def answer(messages, _options, turn):
        if turn == 1:
            return receipt(call("grep", {"pattern": "stored_value"}))
        if turn == 2:
            return receipt(call("read_file", {"path": "consumer.py", "start": 7, "end": 7}))
        if turn == 3:
            return receipt(call("read_file", {"path": "contract.py", "start": 1, "end": 1}))
        units = packet(messages)["pending_units"]
        if turn == 4:
            return batch(no_issue(units[0]) | {"source_refs": ["f999.R1"]})
        if turn == 5:
            return batch(no_issue(units[0]))
        return batch(*map(no_issue, units))

    model = SessionModel(handler=answer)
    _, ledger = await run(model, count=2, ledger=book(), executor=execute)
    assert len(model.calls) == 6 and len(ledger.no_issue_units) == 2 and not ledger.unresolved_units


@pytest.mark.asyncio
async def test_protocol_ten_keeps_original_packet_and_registered_actions():
    def answer(messages, options, _turn):
        assert "shared_review_workspace" not in packet(messages)
        names = {row["function"]["name"] for row in options["tools"]}
        assert not names.intersection({"checkpoint_review_progress", "read_shared_evidence", "query_review_workspace"})
        return batch(*map(no_issue, packet(messages)["pending_units"]))

    _, ledger = await run(SessionModel(handler=answer), count=2, ledger=book(10))
    assert len(ledger.no_issue_units) == 2 and not ledger.shared_workspace


def test_import_before_and_head_source_preserves_side_and_only_local_native_citations():
    work, store = worker()
    identity = store.source(SourceLine("before", "old.py", 12, "return old"), base_sha="base")
    workspace_action(work, "read_shared_evidence", {"evidence_ids": [identity]}, store)
    assert any(row.side == "before" and row.path == "old.py" for row in work.block.source_refs.values())
    assert not work.broker._observations
    assert all(row.side != "context" for row in work.block.source_refs.values() if row.path == "old.py")
    head_identity = store.source(SourceLine("context", "new.py", 12, "return new"))
    workspace_action(work, "read_shared_evidence", {"evidence_ids": [head_identity]}, store)
    observation = work.broker._observations[0]
    assert observation.tool == "read_file" and observation.sha == "head"
    assert observation.excerpt == "return new" and "return old" not in observation.excerpt
    assert "obs_0" in workspace_view(work)["local_observations"]


def test_semantic_state_ignores_observation_identity_query_window_and_response_text():
    work, _ = worker()
    obs = source_observation(tool="grep", text="- consumer.py:7: return stored_value")
    work.broker._observations.append(obs)
    first = progress_state(work)
    work.broker._observations.append(replace(obs, id="random_88", query="grep a new redundant query"))
    assert progress_state(work) == first
    response = receipt(call("grep", {"pattern": "stored_value"}))
    assert not repeated_progress(work, response)
    response.content = "new wording"
    assert repeated_progress(work, response)


def test_semantic_cycle_state_survives_restart_and_new_facts_reset_it():
    work, store = worker()
    response = receipt(call("read_file", {"path": "consumer.py", "start": 1, "end": 1}))
    assert not repeated_progress(work, response)
    saved = json.loads(json.dumps(work.flow))
    work.record["workflow"] = saved
    assert repeated_progress(work, response)
    work.broker._observations.append(source_observation(tool="grep", text="- consumer.py:7: return stored_value"))
    assert not repeated_progress(work, response)
    identity = store.observation(source_observation())
    workspace_action(work, "read_shared_evidence", {"evidence_ids": [identity]}, store)
    assert not repeated_progress(work, response)


def test_first_thinking_checkpoint_allows_a_valid_result_next_turn():
    work, store = worker()
    workspace_action(work, "checkpoint_review_progress", note(), store)
    assert not repeated_progress(work, receipt(call("checkpoint_review_progress", note())))
    assert not repeated_progress(work, batch({"kind": "no_issue"}))
    assert not store.ledger.evidence_records


def test_new_failed_search_direction_is_not_collapsed_into_previous_failure():
    work, _ = worker()
    response = receipt(call("grep", {"pattern": "first"}))
    work.broker._observations.append(replace(source_observation(tool="grep"), status="not_found", excerpt=""))
    assert not repeated_progress(work, response)
    work.broker._observations.append(
        replace(source_observation(tool="grep"), status="not_found", excerpt="", query='grep {"pattern": "second"}')
    )
    assert not repeated_progress(work, response)
    assert repeated_progress(work, response)


def test_non_action_explanation_rewrites_have_same_failure_transition():
    work, _ = worker()
    work.flow["feedback"] = [{"error": "non-action-text", "unit_id": ""}]
    assert not repeated_progress(work, AIMessage(content="thinking about the input"))
    assert repeated_progress(work, AIMessage(content="reworded thinking about the input"))


def test_different_workspace_path_queries_remain_available_for_handoff():
    work, _ = worker()
    first = receipt(call("query_review_workspace", {"paths": ["consumer.py"], "unit_ids": []}))
    second = receipt(call("query_review_workspace", {"paths": ["caller.py"], "unit_ids": []}))
    assert not repeated_progress(work, first)
    assert not repeated_progress(work, second)
    assert repeated_progress(work, second)


def test_diff_window_header_counts_do_not_count_as_new_source():
    work, _ = worker()
    first = source_observation(tool="read_diff", text="@@ -7,1 +7,1 @@\n-old\n+new")
    work.broker._observations.append(first)
    state = progress_state(work)
    work.broker._observations.append(replace(first, id="new_id", excerpt="@@ -7,2 +7,2 @@\n-old\n+new"))
    assert progress_state(work) == state
    work.broker._observations.append(replace(first, id="extra_source", excerpt="@@ -7,2 +7,2 @@\n-old\n+new\n context"))
    assert progress_state(work) != state


def test_same_diff_source_at_another_hunk_start_is_new_navigation():
    work, _ = worker()
    first = source_observation(tool="read_diff", text="@@ -7 +7 @@\n-old\n+new")
    work.broker._observations.append(first)
    state = progress_state(work)
    work.broker._observations.append(replace(first, id="new_location", excerpt="@@ -70 +70 @@\n-old\n+new"))
    assert progress_state(work) != state


def test_complete_diff_fragments_reassemble_before_comparing_source_locations():
    work, store = worker()
    from reviewforge.engine.source_navigation import _pieces

    body = "\n".join(f" same source line {index}" for index in range(100)) + "\n-old\n+new\n"
    first = "@@ -7,101 +7,101 @@\n" + body
    other_window = "@@ -7,102 +7,102 @@\n" + body

    def acquire(text):
        digest = hashlib.sha256(text.encode()).hexdigest()[:16]
        for index, (piece, _) in enumerate(_pieces(text, None)):
            obs = replace(
                source_observation(tool="read_diff"), id=f"{digest}-{index}", excerpt=piece, result_digest=digest
            )
            work.broker._observations.append(obs)
            work.record.setdefault("workspace_observation_ids", {})[obs.id] = store.observation(obs)

    acquire(first)
    state = progress_state(work)
    acquire(other_window)
    assert progress_state(work) == state


@pytest.mark.parametrize("tool,status", [("model_note", "success"), ("read_file", "not_found"), ("read_file", "error")])
def test_shared_import_prevalidates_complete_batch_before_any_local_source_or_observation(tool, status):
    work, store = worker()
    before = store.source(SourceLine("before", "old.py", 12, "return old"), base_sha="base")
    good = store.observation(source_observation())
    bad = store.observation(replace(source_observation(path="unknown.py", tool=tool), status=status))
    original = dict(work.block.source_refs)
    with pytest.raises(ValueError, match="not-successful-pinned-source"):
        workspace_action(work, "read_shared_evidence", {"evidence_ids": [before, good, bad]}, store)
    assert work.block.source_refs == original
    assert not work.broker._observations and work.broker._obs_counter == 0
    assert not work.record.get("workspace_evidence_ids")


def test_rephrasing_or_reordering_an_already_accepted_record_is_not_progress():
    work, _ = worker()
    record = {"kind": "hypothesis", "source_refs": ["f0.B1", "f0.R1"], "claim": "original claim"}
    work.flow["accepted_records"] = {"unit": [record]}
    initial = progress_state(work)
    work.flow["accepted_records"]["unit"].append(
        record | {"claim": "same claim rewritten", "source_refs": ["f0.R1", "f0.B1", "f0.B1"]}
    )
    assert progress_state(work) == initial


def test_automatic_view_aggregates_all_source_locations_without_readding_per_line_ids():
    work, store = worker()
    for line in range(1, 1001):
        identity = store.source(SourceLine("context", "file0.py", line, f"source line {line}"))
        remember_evidence(work, identity)
    view = workspace_view(work)
    assert view["evidence_catalog"] == []
    assert len(view["evidence_directory"]) == 1
    directory = view["evidence_directory"][0]
    assert directory["path"] == "file0.py" and directory["record_count"] == 1000
    assert directory["line_ranges"] == [[1, 1000]]
    assert not view["local_observations"]
    assert "ev_" not in json.dumps(view) and len(json.dumps(view)) < 2000
    query = workspace_action(work, "query_review_workspace", {"paths": ["file0.py"], "unit_ids": []}, store)
    assert len(query["evidence_catalog"]) == 1000
