"""Work can cross old quotas while source identity and semantic gates still hold."""

import copy
import json

import pytest
from test_discovery_workflow_v6 import batch, lead, no_issue, run
from test_investigation_v3 import candidate, source_executor, state
from test_proof_gate import GateLLM, ledger, observation
from test_proof_gate import candidate as confirmed_candidate
from test_review_session_v5 import SessionModel, call, packet, receipt
from test_source_audit import audit, verdict

from reviewforge.engine.context_pack import ContextPack
from reviewforge.engine.execution_policy import DEEPSEEK_OUTPUT_CAPACITY, repeated_state
from reviewforge.engine.hypothesis import HypothesisLedger
from reviewforge.engine.investigator import Investigator
from reviewforge.engine.proof_gate import ProofGate, accepted
from reviewforge.engine.review_session import EvidenceStore
from reviewforge.engine.session_tools import case_identity
from reviewforge.engine.source_audit import consume, current, run_source_audit


@pytest.mark.asyncio
async def test_discovery_can_read_past_old_turn_and_request_allowances_then_complete():
    def answer(messages, options, turn):
        assert options["max_tokens"] == DEEPSEEK_OUTPUT_CAPACITY
        assert "requests_remaining" not in packet(messages)["session"]
        assert "read_file" in {tool["function"]["name"] for tool in options["tools"]}
        if turn <= 10:
            return receipt(call("read_file", {"path": f"caller{turn}.py", "start": 1, "end": 2}))
        reply = batch(no_issue(packet(messages)["pending_units"][0]))
        reply.content = "I checked the saved source before submitting this assessment."
        return reply

    model = SessionModel(handler=answer)
    book = HypothesisLedger("run", "head", "digest", protocol_version=10)
    _, book = await run(model, count=1, ledger=book, session_request_limit=1)
    assert len(model.calls) == 11 and len(book.no_issue_units) == 1 and not book.unresolved_units
    record = next(iter(book.discovery_blocks.values()))
    assert record["request_limit"] is None and record["session"]["max_turns"] is None
    assert HypothesisLedger.from_dict(book.to_dict()).discovery_blocks == book.discovery_blocks


@pytest.mark.asyncio
async def test_partial_delivery_does_not_remove_read_tools_or_cap_candidate_volume():
    def answer(messages, options, turn):
        units = packet(messages)["pending_units"]
        if turn == 1:
            return batch(no_issue(units[0]))
        if turn == 2:
            return receipt(call("read_file", {"path": "consumer.py", "start": 1, "end": 2}))
        records = [
            lead(units[0]) | {"claim": f"Independent proposed contract violation {index}."} for index in range(15)
        ]
        return batch(*records)

    model = SessionModel(handler=answer)
    book = HypothesisLedger("run", "head", "digest", protocol_version=10)
    _, book = await run(model, count=2, ledger=book, max_hypotheses=1, discovery_max_hypotheses_per_block=1)
    assert len(model.calls) == 3 and len(book.items) == 15 and not book.unresolved_units


@pytest.mark.asyncio
async def test_investigation_keeps_reads_after_multiple_failed_deliveries_and_old_step_cap():
    item = candidate()

    def answer(messages, options, turn):
        assert "read_file" in {tool["function"]["name"] for tool in options["tools"]}
        if turn <= 4:
            return receipt(
                call(
                    "submit_unknown",
                    {
                        "case_id": case_identity(item, "head"),
                        "reason": f"Different attempted correction {turn}",
                        "missing_fact": "",
                    },
                )
            )
        if turn <= 12:
            return receipt(call("read_file", {"path": f"caller{turn}.py", "start": 1, "end": 2}))
        return receipt(
            call(
                "submit_unknown",
                {
                    "case_id": case_identity(item, "head"),
                    "reason": "The caller policy is not specified.",
                    "missing_fact": "The caller's absent-value policy.",
                },
            )
        )

    model = SessionModel(handler=answer)
    worker = Investigator(model, source_executor([]), protocol_version=10, max_steps=1)
    result = await worker.investigate(item, state(), ContextPack())
    assert len(model.calls) == 13 and result.outcome == "insufficient-evidence"
    saved = next(row for row in result.progress if row.get("phase") == "case-transaction-v10")
    assert saved["binding"]["evidence_limit"] is None
    assert saved["state"] == "complete" and saved["evidence_steps"] > 8
    # A saved result is delivered by revalidation, never by another model vote.
    item.investigation_progress = result.progress
    item.observations = result.observations
    await Investigator(model, source_executor([]), protocol_version=10).investigate(item, state(), ContextPack())
    assert len(model.calls) == 13


@pytest.mark.asyncio
async def test_scheduler_processes_every_candidate_even_when_legacy_limit_is_one():
    items = [candidate(f"item{index}") for index in range(3)]
    book = HypothesisLedger("run", "head", "digest", protocol_version=10)
    for item in items:
        book.upsert(item)

    def answer(messages, *_):
        return receipt(
            call(
                "submit_unknown",
                {
                    "case_id": packet(messages)["case_id"],
                    "reason": "Missing caller policy.",
                    "missing_fact": "The exact caller requirement.",
                },
            )
        )

    model = SessionModel(handler=answer)
    await Investigator(model, source_executor([])).run(book, state(), ContextPack(), max_hypotheses_per_pr=1)
    assert len(model.calls) == len(book.investigation_tasks) == 3
    assert all(task["budget_steps_limit"] is None for task in book.investigation_tasks.values())


@pytest.mark.asyncio
async def test_an_exact_failed_transition_is_explicitly_stalled_without_claiming_coverage():
    model = SessionModel(handler=lambda *_: receipt(call("unregistered", {})))
    book = HypothesisLedger("run", "head", "digest", protocol_version=10)
    _, book = await run(model, count=1, ledger=book)
    assert len(model.calls) == 2
    assert list(book.unresolved_units.values()) == ["generator no-progress-cycle"]
    assert not book.no_issue_units and not book.items


def test_cycle_detection_is_about_repeated_state_not_total_attempts():
    saved = {}
    for index in range(100):
        assert not repeated_state(saved, state={"source": index}, receipt="same read")
    restored = copy.deepcopy(saved)
    assert repeated_state(restored, state={"source": 99}, receipt="same read")
    assert not repeated_state(restored, state={"source": 99}, receipt="different repair")


@pytest.mark.asyncio
async def test_full_requested_source_is_saved_beyond_old_character_windows():
    text = "\n".join(f"value{index} = {'x' * 90}" for index in range(250)) + "\n"

    async def execute(*_):
        return text

    worker = Investigator(None, execute, protocol_version=10)
    worker._state = state()
    await worker._run_tool("read_file", {"path": "app.py", "start": 1, "end": 250})
    assert "".join(obs.excerpt for obs in worker._observations) == text
    assert all(not row["truncated"] for row in worker._progress if row.get("phase") == "observation-window")


def test_evidence_store_and_source_audit_have_no_record_or_reference_volume_quota():
    item = confirmed_candidate()
    item.observations.extend(observation(f"obs_{index}", f"value{index}\n") for index in range(2, 13))
    raw = audit()
    raw["execution"][0]["evidence"] = [f"h_bug/obs_{index}:e1" for index in [0, *range(2, 13)]]
    raw["gaps"] = ["x" * 40_000]
    saved, rejected = consume(raw, item, protocol_version=10)
    assert saved == raw and not rejected
    store = EvidenceStore(ledger(item, protocol=10))
    assert store.put({"source": "x" * (4 * 1024 * 1024 + 1)})
    raw["execution"][0]["evidence"].append("invented/obs_0:e1")
    saved, rejected = consume(raw, item, protocol_version=10)
    assert not saved["execution"] and rejected


@pytest.mark.asyncio
async def test_source_audit_can_correct_more_than_two_distinct_bad_envelopes():
    item = confirmed_candidate()
    model = GateLLM(replies=[f"bad envelope {i}" for i in range(5)] + [{"audits": [audit()]}])
    book = ledger(item, protocol=10)
    await run_source_audit(model, book)
    assert len(model.calls) == 6 and current(item, book)
    assert book.source_audits[item.id]["attempts"] == 6


@pytest.mark.asyncio
async def test_publication_can_correct_more_than_two_distinct_bad_envelopes():
    item = confirmed_candidate()
    model = GateLLM(replies=[{"audits": [audit()]}, *[f"bad envelope {i}" for i in range(5)], {"reviews": [verdict()]}])
    book = ledger(item, protocol=10)
    await ProofGate(model).run(book)
    assert len(model.calls) == 7 and accepted(item, book)
    assert book.publication_reviews[item.id]["attempts"] == 6
    await ProofGate(model).run(book)
    assert len(model.calls) == 7


@pytest.mark.asyncio
async def test_schema_failure_cycle_stops_but_never_turns_into_a_semantic_vote():
    item = confirmed_candidate()
    model = GateLLM(replies=[{"audits": [audit()]}, "invalid envelope", "invalid envelope"])
    book = ledger(item, protocol=10)
    await ProofGate(model).run(book)
    assert len(model.calls) == 3 and not accepted(item, book)
    assert book.publication_reviews[item.id]["reason"] == "no-progress-cycle"


def test_deepseek_automatic_tool_choice_preserves_thinking_and_drops_only_json_framing():
    from reviewforge.engine.llm_protocol_v4 import request_options
    from reviewforge.engine.token_tracker import RunContext, TrackedChatLLM

    # Use the existing request wrapper so planning and physical dispatch see
    # identical options, with no API request or shared-model mutation.
    model = SessionModel(handler=lambda *_: receipt())
    tracked = TrackedChatLLM(model, RunContext(), "generator", deepseek_native_tools=True, automatic_native_tools=True)
    options = request_options("deepseek-flash", "generator", protocol_version=10)
    assert "extra_body" not in options and "reasoning_effort" not in options
    actual = tracked._effective_options(options | {"tools": [{"type": "function"}], "tool_choice": "auto"})
    assert "response_format" not in actual and "extra_body" not in actual
    # Forced tools remain provider-compatible even if another caller uses one.
    forced = tracked._effective_options({"tools": [{}], "tool_choice": {"type": "function", "function": {"name": "x"}}})
    assert forced["extra_body"]["thinking"]["type"] == "disabled"


def test_reference_lookup_respects_model_requested_volume_without_hidden_sampling_caps():
    from reviewforge.engine.reference_navigation import select_reference_hits
    from reviewforge.tools.workspace import GrepHit

    hits = [GrepHit(path="a.py", line=index, text="target()") for index in range(1, 41)]
    result = select_reference_hits(hits, path="", max_hits=40, cap_hits=False)
    assert len(result.hits) == 40 and not result.truncated and result.per_file_limit is None
    assert len(select_reference_hits(hits, path="", max_hits=40).hits) < 40


@pytest.mark.asyncio
async def test_invalid_citation_is_repairable_but_valid_conclusion_is_frozen():
    item = candidate()

    def answer(messages, options, turn):
        if turn == 1:
            return receipt(
                call(
                    "submit_refuted",
                    {
                        "case_id": packet(messages)["case_id"],
                        "reason": "The access is guarded.",
                        "premise": "mechanism",
                        "statement": "The access is guarded.",
                        "evidence": ["obs_999:e1"],
                    },
                )
            )
        assert "rejected_result_untrusted" in messages[-1].content
        return receipt(
            call(
                "submit_unknown",
                {
                    "case_id": packet(messages)["case_id"],
                    "reason": "Caller policy is missing.",
                    "missing_fact": "Caller policy for an absent value.",
                },
            )
        )

    model = SessionModel(handler=answer)
    result = await Investigator(model, source_executor([]), protocol_version=10).investigate(
        item, state(), ContextPack()
    )
    assert len(model.calls) == 2 and result.outcome == "insufficient-evidence"
    saved = next(row for row in result.progress if row.get("phase") == "case-transaction-v10")
    assert saved["rejected_decisions"] and saved["decision"]["name"] == "submit_unknown"


@pytest.mark.asyncio
async def test_all_batches_are_verified_and_published_past_old_four_calls_and_five_comments():
    from langchain_core.messages import AIMessage

    from reviewforge.engine.editor import Editor, resumed_publication

    items = [confirmed_candidate(f"h_{index}") for index in range(7)]
    for item in items:
        item.identity += item.id
        # Each indivisible case exceeds the soft batch target. All source is
        # retained; later batches must still execute after the fourth request.
        item.observations.extend(observation(f"obs_{index}", "x" * 1100) for index in range(2, 32))
    book = ledger(*items, protocol=10)

    def answer(messages, options, turn):
        entries = packet(messages)["items"]
        if "hypothesis_id" in entries[0]:
            raw = {"audits": [audit(entry["hypothesis_id"]) for entry in entries]}
        else:
            reviews = []
            for entry in entries:
                row = json.loads(json.dumps(verdict()).replace("h_bug", entry["candidate"]["id"]))
                row["contract_refs"] = [next(iter(entry["independent_contracts"]))]
                reviews.append(row)
            raw = {"reviews": reviews}
        return AIMessage(content=json.dumps(raw))

    model = SessionModel(handler=answer)
    await ProofGate(model).run(book)
    assert len(model.calls) == 14 and all(accepted(item, book) for item in items)
    publication = await Editor(None, max_inline=1, max_inline_overflow=1).run(book, None)
    assert len(publication.comments) == 7
    resumed = resumed_publication(
        book,
        covered_ids=set(),
        published_clusters=set(),
        inline_used=99,
        max_inline=1,
        max_inline_overflow=1,
        output_language="en",
    )
    assert len(resumed.comments) == 7


@pytest.mark.asyncio
async def test_missing_source_followup_has_no_leftover_step_allowance():
    from test_evidence_followup import Worker
    from test_evidence_followup import result as collection_result
    from test_evidence_followup import run as followup

    item = confirmed_candidate()
    item.investigation_task_id = "task"
    book = ledger(item, protocol=10)
    book.investigation_tasks["task"] = {
        "members": [item.identity],
        "charged_to": item.identity,
        "budget_steps_limit": None,
        "token_limit": None,
        "budget_policy": "usage-only-v1",
        "total_steps": 100,
        "total_tokens": 100_000,
        "total_unreported_tokens": 0,
    }
    source = audit()
    source.update(contracts=[], gaps=["Find the consumer's supported-input requirement."])
    await ProofGate(GateLLM(replies=[{"audits": [source]}])).run(book)
    worker = Worker(collection_result(item, steps=20))
    assert await followup(worker, book)
    assert worker.calls[0][1]["max_steps"] is None and worker.calls[0][1]["token_limit"] is None
    assert book.investigation_tasks["task"]["total_steps"] == 120


def test_restored_session_cannot_silently_change_its_execution_policy():
    from reviewforge.engine.review_session import ReviewSession

    saved = {}
    ReviewSession(saved, task_id="task", phase="discovery", max_turns=3)
    with pytest.raises(ValueError, match="session-policy-mismatch"):
        ReviewSession(saved, task_id="task", phase="discovery", max_turns=None)
