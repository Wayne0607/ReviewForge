"""Actual failed receipts plus adversarial delivery/recovery boundaries.

Saved decisions are transport fixtures, never correctness labels. Every replay
still passes the original case/evidence validator; absence cannot confirm.
"""

from __future__ import annotations

import asyncio
import copy
import json
from dataclasses import replace
from pathlib import Path

import pytest
from pydantic import ValidationError
from test_discovery_submission import fixture as discovery_fixture
from test_investigation_repair_v8 import refutation
from test_investigation_v3 import candidate, source_executor, state
from test_review_session_v5 import SessionModel, call, packet, receipt

from reviewforge.engine.context_pack import ContextPack
from reviewforge.engine.discovery_workflow_tools import assessment_tools
from reviewforge.engine.hypothesis import Hypothesis, HypothesisLedger
from reviewforge.engine.investigation_transaction import MAX_DELIVERY_REQUESTS, PHASE
from reviewforge.engine.investigator import Investigator
from reviewforge.engine.request_budget import BudgetedChatLLM, RequestBudget, external_request_budget_scope
from reviewforge.engine.session_tools import decision_tools

DATA = json.loads((Path(__file__).parent / "fixtures/v8_case_delivery_failures.json").read_text("utf-8"))


def saved_fixture(row):
    item = Hypothesis.from_dict(copy.deepcopy(row["case"]))
    item.investigation_progress = []
    review_state = state()
    review_state.head_sha = DATA["head_sha"]
    review_state.file_diffs = {}
    review_state.files_changed = [site.path for site in item.sites]
    return item, review_state, ContextPack(workspace_digest=DATA["workspace_digest"])


@pytest.mark.asyncio
@pytest.mark.parametrize("index", range(6))
async def test_all_six_original_oversized_native_results_deliver_without_rewriting(index):
    row = copy.deepcopy(DATA["cases"][index])
    original = copy.deepcopy(row["tool_call"])
    item, review_state, pack = saved_fixture(row)
    schema8 = {tool.name: tool.args_schema for tool in decision_tools(item, DATA["head_sha"], protocol_version=8)}
    with pytest.raises(ValidationError):
        schema8[original["name"]].model_validate(original["args"])
    model = SessionModel(handler=lambda *_: receipt(call(original["name"], original["args"])))
    executed = []
    worker = Investigator(model, source_executor(executed), protocol_version=9)
    worker._max_steps = 0  # Frozen evidence is exhausted; delivery is independent.
    result = await worker.investigate(item, review_state, pack)
    assert len(model.calls) == 1 and not executed and result.steps == 0
    assert result.verdict == ("refuted" if original["name"] == "submit_refuted" else "unknown")
    assert result.outcome != "output-error"
    transaction = next(row for row in result.progress if row.get("phase") == PHASE)
    assert transaction["decision"] == {"name": original["name"], "args": original["args"]}
    assert transaction["state"] == "complete" and transaction["delivery_attempts"] == 1
    assert row["tool_call"] == original  # No trimming or model rewrite.


def test_discovery_record_and_case_reason_have_no_cosmetic_length_gate_but_remain_bounded():
    _, _, block, _ = discovery_fixture(1)
    _, schemas = assessment_tools(block, protocol_version=9)
    raw = {"kind": "deferred", "unit_id": block.units[0].id, "reason": "长" * 900}
    assert schemas["deferred"].model_validate(raw).reason == raw["reason"]
    with pytest.raises(ValidationError):
        assessment_tools(block, protocol_version=8)[1]["deferred"].model_validate(raw)
    with pytest.raises(ValidationError, match="transport-record-too-large"):
        schemas["deferred"].model_validate(raw | {"reason": "长" * 12_000})
    with pytest.raises(ValidationError):
        schemas["deferred"].model_validate(raw | {"reason": ""})


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["hypothesis", "no_issue", "deferred"])
async def test_discovery_consumer_preserves_long_native_prose_not_just_its_schema(kind):
    from test_discovery_workflow_v6 import batch, lead, no_issue
    from test_discovery_workflow_v7 import run

    original = {}

    def answer(messages, *_):
        unit = packet(messages)["pending_units"][0]
        if kind == "hypothesis":
            record = lead(unit)
            field = "claim"
        elif kind == "no_issue":
            record = no_issue(unit)
            field = "checked"
        else:
            record, field = {"kind": "deferred", "unit_id": unit["unit_id"]}, "reason"
        record[field] = "The exact source boundary remains the same. " * 22
        original.update(record)
        return batch(record)

    model = SessionModel(handler=answer)
    book = await run(model, count=1, ledger=HypothesisLedger("run", "head", "digest", protocol_version=9))
    assert len(model.calls) == 1
    block = next(iter(book.discovery_blocks.values()))
    assert block["units"][original["unit_id"]]["finished"]
    if kind == "hypothesis":
        assert next(iter(book.items.values())).claim == original["claim"].strip()
    elif kind == "no_issue":
        assert book.no_issue_units and not book.unresolved_units
    else:
        assert "deferred" in book.unresolved_units[original["unit_id"]]


def test_independent_source_reconstruction_also_separates_reason_length_from_source_validity():
    from test_proof_gate import candidate as source_candidate
    from test_source_audit import audit

    from reviewforge.engine.source_audit import consume, validate

    raw = audit()
    raw["execution"][0]["statement"] *= 32
    assert len(raw["execution"][0]["statement"]) > 600
    with pytest.raises(ValueError, match="source-fact"):
        validate(raw, source_candidate(), protocol_version=8)
    saved, rejected = consume(raw, source_candidate(), protocol_version=9)
    assert saved == raw and not rejected
    raw["execution"][0]["evidence"] = ["absent/obs_0:e1"]
    saved, rejected = consume(raw, source_candidate(), protocol_version=9)
    assert not saved["execution"] and len(rejected) == 1


@pytest.mark.asyncio
async def test_new_source_audit_is_saved_and_revalidated_under_its_own_format_version():
    from test_proof_gate import GateLLM, ledger
    from test_proof_gate import candidate as source_candidate
    from test_source_audit import audit

    from reviewforge.engine.source_audit import current, run_source_audit

    item = source_candidate()
    book = ledger(item, protocol=9)
    raw = audit()
    raw["execution"][0]["statement"] *= 32
    await run_source_audit(GateLLM(replies=[{"audits": [raw]}]), book)
    assert current(item, book) and book.source_audits[item.id]["version"] == 2
    assert book.source_audits[item.id]["facts"]["execution"] == raw["execution"]


@pytest.mark.asyncio
async def test_separate_delivery_still_cannot_exceed_the_finite_external_grant():
    raw = SessionModel(handler=lambda *_: pytest.fail("External limit must deny before any paid request"))
    meter, grant = RequestBudget(None), RequestBudget(100, editor_reserve=0)

    async def changed(_snapshot):
        pass

    with external_request_budget_scope(grant, identity="finite-test-grant", on_change=changed):
        worker = Investigator(
            BudgetedChatLLM(raw, meter, phase="investigation"), source_executor([]), protocol_version=9
        )
        worker._max_steps = 0
        result = await worker.investigate(candidate(), state(), ContextPack())
    assert result.verdict == "unknown" and result.outcome == "budget-exhausted" and not raw.calls
    assert meter.reported == grant.reported == 0


@pytest.mark.asyncio
async def test_actual_unregistered_last_turn_read_gets_delivery_feedback_without_execution():
    item, review_state, pack = saved_fixture(DATA["last_turn"])
    bad = DATA["last_turn"]["tool_call"]

    def answer(messages, options, turn):
        data = packet(messages)
        assert {tool["function"]["name"] for tool in options["tools"]} == {
            "submit_confirmed",
            "submit_refuted",
            "submit_unknown",
        }
        if turn == 1:
            return receipt(call(bad["name"], bad["args"]))
        feedback = json.loads(messages[-1].content)["controller_receipt"]
        assert any(row.get("error") == "unregistered-action" for row in feedback)
        return receipt(
            call(
                "submit_unknown",
                {
                    "case_id": data["case_id"],
                    "reason": "UI transition not established.",
                    "missing_fact": "The effect of editing after an invalid-email response.",
                },
            )
        )

    model = SessionModel(handler=answer)
    executed = []
    worker = Investigator(model, source_executor(executed), protocol_version=9)
    worker._max_steps = 0
    result = await worker.investigate(item, review_state, pack)
    assert result.verdict == "unknown" and result.outcome == "insufficient-evidence"
    assert len(model.calls) == 2 and result.steps == 0 and not executed


@pytest.mark.asyncio
async def test_last_evidence_step_can_read_then_deliver_without_consuming_another_evidence_step():
    executed = []

    def answer(messages, options, turn):
        data = packet(messages)
        if turn == 1:
            assert data["session"]["last_turn"] is False
            return receipt(call("read_file", {"path": "consumer.ts", "start": 20, "end": 21}))
        assert data["session"]["last_turn"] is True
        assert any(observation["path"] == "consumer.ts" for observation in data["observations"])
        return receipt(call("submit_refuted", refutation(data)))

    model = SessionModel(handler=answer)
    result = await Investigator(model, source_executor(executed), protocol_version=9, max_steps=2).investigate(
        candidate(), state(), ContextPack()
    )
    assert result.verdict == "refuted" and result.steps == 2 and len(model.calls) == 2
    assert any(name == "read_file" and args["path"] == "consumer.ts" for name, args in executed)


@pytest.mark.asyncio
@pytest.mark.parametrize("mutate", ["case", "evidence", "empty-proof", "mixed"])
async def test_long_prose_does_not_bypass_case_evidence_or_exclusive_result_checks(mutate):
    def answer(messages, *_):
        data = packet(messages)
        args = refutation(data)
        if mutate == "case":
            args["case_id"] = "different-case"
        if mutate == "evidence":
            args["evidence"] = ["obs_unseen:e1"]
        if mutate == "empty-proof":
            args["statement"] = ""
        calls = [call("submit_refuted", args)]
        if mutate == "mixed":
            calls.append(call("read_file", {"path": "injected.ts"}, "other"))
        return receipt(*calls)

    model = SessionModel(handler=answer)
    result = await Investigator(model, source_executor([]), protocol_version=9, max_steps=1).investigate(
        candidate(), state(), ContextPack()
    )
    assert result.verdict == "unknown" and result.outcome == "output-error"
    assert len(model.calls) <= MAX_DELIVERY_REQUESTS
    assert not any(item.path == "injected.ts" for item in result.observations)


@pytest.mark.asyncio
async def test_delivery_counter_and_original_decision_survive_cancellation_and_zero_step_scheduler_restore():
    item = candidate()
    book = HypothesisLedger("run", "head", "scope", protocol_version=9)
    book.upsert(item)
    meter = RequestBudget(None)
    snapshots = []

    async def budget_changed(snapshot):
        book.request_budget = copy.deepcopy(snapshot)

    async def checkpoint(ledger):
        snapshots.append(copy.deepcopy(ledger.to_dict()))
        rows = ledger.items[item.identity].investigation_progress
        if any(row.get("phase") == PHASE and row.get("delivery_attempts") == 1 for row in rows):
            raise asyncio.CancelledError()

    raw = SessionModel(handler=lambda *_: receipt(call("read_file", {"path": "a.ts", "start": 50, "end": 60})))
    model = BudgetedChatLLM(raw, meter, phase="investigation", on_change=budget_changed)
    with pytest.raises(asyncio.CancelledError):
        await Investigator(model, source_executor([]), protocol_version=9, max_steps=2).run(
            book, state(), ContextPack(workspace_digest="scope"), request_budget=meter, on_update=checkpoint
        )
    restored = HypothesisLedger.from_dict(snapshots[-1])
    meter = RequestBudget(None, saved=restored.request_budget)
    raw = SessionModel(handler=lambda messages, *_: receipt(call("submit_refuted", refutation(packet(messages)))))
    results = await Investigator(
        BudgetedChatLLM(raw, meter, phase="investigation"), source_executor([]), protocol_version=9, max_steps=2
    ).run(restored, state(), ContextPack(workspace_digest="scope"), request_budget=meter)
    result = results[0]
    assert result.verdict == "refuted" and result.steps == 0 and len(raw.calls) == 1
    row = next(row for row in result.progress if row.get("phase") == PHASE)
    assert row["delivery_attempts"] == 2 and row["evidence_steps"] == 2
    task = next(iter(restored.investigation_tasks.values()))
    assert task["total_steps"] == task["budget_steps_limit"] == 2
    assert meter.reported == task["total_tokens"] == 240
    # Completed delivery revalidates the saved decision without a paid reroll.
    completed = copy.deepcopy(restored.items[item.identity])
    worker = Investigator(raw, source_executor([]), protocol_version=9)
    worker._max_steps = 0
    replay = await worker.investigate(completed, state(), ContextPack(workspace_digest="scope"))
    assert replay.verdict == "refuted" and len(raw.calls) == 1 and replay.steps == 0
    tampered = copy.deepcopy(completed)
    next(row for row in tampered.investigation_progress if row.get("phase") == PHASE)["decision"]["args"]["reason"] = (
        "edited"
    )
    with pytest.raises(ValueError, match="decision-mismatch"):
        await worker.investigate(tampered, state(), ContextPack(workspace_digest="scope"))
    tampered = copy.deepcopy(completed)
    tampered.observations[0] = replace(tampered.observations[0], excerpt="replacement source")
    with pytest.raises(ValueError, match="evidence-mismatch"):
        await worker.investigate(tampered, state(), ContextPack(workspace_digest="scope"))


@pytest.mark.asyncio
async def test_real_search_hit_uses_actual_call_instead_of_import_and_exposes_missing_argument_window():
    row = next(row for row in DATA["cases"] if row["case"]["id"] == "h_1e09afc6")
    item, review_state, pack = saved_fixture(row)
    # Recreate the point just after the real grep, before the old default read.
    item.observations = [obs for obs in item.observations if obs.id != "obs_7"]
    source = DATA["source"]
    executed = []

    async def execute(name, args):
        assert name == "read_file" and args["path"] == source["path"]
        executed.append(args)
        return "".join(source["text"].splitlines(keepends=True)[args["start"] - 1 : args["end"]])

    worker = Investigator(SessionModel(handler=lambda *_: None), execute, protocol_version=9)
    worker._prepare_investigation(item, review_state, pack)
    await worker._run_tool("read_file", {"path": source["path"]})
    observed = list(worker._observations[len(item.observations) :])
    saved = "".join(item.excerpt for item in observed)
    assert executed[0]["start"] == 120 and executed[0]["end"] == 184
    assert "await sendAddGuestsEmails(evt, guests)" in saved
    assert "attendees: attendeesList" in saved
    assert all(len(item.excerpt) <= 1200 for item in observed)
    data = packet(worker._exploration_chat(item, pack))
    windows = data["source_navigation"]["adjacent_windows"]
    previous = next(window["args"] for window in windows if window["args"]["end"] == 119)
    before = len(worker._observations)
    await worker._run_tool("read_file", previous)
    saved_previous = "".join(item.excerpt for item in worker._observations[before:])
    assert "bookingAttendees.attendees.map" in saved_previous
    assert "createMany" in saved_previous
    assert "bookingAttendees.attendees.map" not in saved  # No omitted source promoted.
    # An explicit user/model range always wins over the navigation heuristic.
    await worker._run_tool("read_file", {"path": source["path"], "start": 1, "end": 11})
    assert worker._observations[-1].excerpt_line_range == (1, 11)


@pytest.mark.asyncio
async def test_cancel_after_decision_saved_before_delivery_never_reasks_or_rerolls():
    item = candidate()

    async def checkpoint(observations, progress):
        item.observations = copy.deepcopy(observations)
        item.investigation_progress = copy.deepcopy(progress)
        if any(row.get("phase") == PHASE and row.get("state") == "delivering" for row in progress):
            raise asyncio.CancelledError()

    model = SessionModel(handler=lambda messages, *_: receipt(call("submit_refuted", refutation(packet(messages)))))
    with pytest.raises(asyncio.CancelledError):
        await Investigator(
            model, source_executor([]), protocol_version=9, max_steps=1, on_progress=checkpoint
        ).investigate(item, state(), ContextPack())
    replay = await Investigator(model, source_executor([]), protocol_version=9).investigate(
        item, state(), ContextPack()
    )
    assert replay.verdict == "refuted" and replay.steps == 0 and len(model.calls) == 1


@pytest.mark.asyncio
async def test_actual_oversized_result_and_unregistered_read_through_real_sdk_wire():
    import httpx

    from reviewforge.engine.openai_compat import ReasoningContentChatOpenAI

    row = next(row for row in DATA["cases"] if row["case"]["id"] == "h_604c1dc7")
    item, review_state, pack = saved_fixture(row)
    requests = []

    def server(request):
        data = json.loads(request.content)
        requests.append(data)
        assert data["tool_choice"] == "required" and "response_format" not in data
        schema = next(
            tool["function"]["parameters"] for tool in data["tools"] if tool["function"]["name"] == "submit_refuted"
        )
        assert "maxLength" not in schema["properties"]["reason"]
        if len(requests) == 1:
            response = DATA["last_turn"]["tool_call"]
        else:
            feedback = json.loads(data["messages"][-1]["content"])["controller_receipt"]
            previous = next(item["previous_receipt"] for item in feedback if "previous_receipt" in item)
            assert previous["calls"][0]["name"] == "read_file"
            response = row["tool_call"]
        return httpx.Response(
            200,
            json={
                "id": "v9-regression",
                "object": "chat.completion",
                "created": 0,
                "model": "fixture",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "decision",
                                    "type": "function",
                                    "function": {"name": response["name"], "arguments": json.dumps(response["args"])},
                                }
                            ],
                        },
                    }
                ],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as client:
        model = ReasoningContentChatOpenAI(
            model="fixture",
            base_url="https://offline.invalid/v1",
            api_key="fake",
            max_retries=0,
            http_async_client=client,
        )
        worker = Investigator(model, source_executor([]), protocol_version=9)
        worker._max_steps = 0
        result = await worker.investigate(item, review_state, pack)
    assert result.verdict == "refuted" and len(requests) == 2 and result.tokens == 240
    transaction = next(item for item in result.progress if item.get("phase") == PHASE)
    assert len(transaction["decision"]["args"]["reason"]) == 1083
