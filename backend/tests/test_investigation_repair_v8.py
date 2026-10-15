"""Terminal formatting repair is bounded, durable and cannot grant a verdict."""

from __future__ import annotations

import asyncio
import copy
import json
from dataclasses import replace
from pathlib import Path

import pytest
from test_investigation_v3 import candidate, source_executor, state
from test_review_session_v5 import SessionModel, call, packet, receipt

from reviewforge.engine.context_pack import ContextPack
from reviewforge.engine.hypothesis import HypothesisLedger
from reviewforge.engine.investigator import Investigator
from reviewforge.engine.request_budget import BudgetedChatLLM, RequestBudget


def refutation(data):
    return {
        "case_id": data["case_id"],
        "reason": "Source shows guarded optional access. " * 30,
        "premise": "mechanism",
        "statement": "Optional access prevents this dereference.",
        "evidence": ["obs_1:e1"],
    }


def patch(data, **extra):
    return receipt(
        call(
            "repair_case_result",
            {
                "record_id": data["record_id"],
                "set_fields": {
                    "reason": "Optional access prevents the missing-calendar dereference.",
                    **extra,
                },
            },
        )
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", [7, 8])
async def test_last_turn_oversized_reason_gets_separate_field_repair_only_in_new_protocol(protocol):
    originals = []

    def answer(messages, options, turn):
        data = packet(messages)
        if turn == 1:
            originals.append(refutation(data))
            return receipt(call("submit_refuted", originals[0]))
        assert data["original"] == originals[0] and data["set_fields"] == ["reason"]
        assert "source" not in data and "observations" not in data
        assert [tool["function"]["name"] for tool in options["tools"]] == ["repair_case_result"]
        return patch(data)

    model = SessionModel(handler=answer)
    result = await Investigator(model, source_executor([]), protocol_version=protocol, max_steps=2).investigate(
        candidate(), state(), ContextPack()
    )
    if protocol == 7:
        assert result.verdict == "unknown" and result.reason == "session-step-limit" and len(model.calls) == 1
    else:
        assert result.verdict == "refuted" and len(model.calls) == 2 and result.steps == 2
        assert result.counterevidence.statement == originals[0]["statement"]
        saved = next(entry for entry in result.progress if entry.get("phase") == "case-result-repair")
        assert saved["status"] == "complete" and saved["attempts"] == 1 and saved["draft"]["raw"] == originals[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("extra", [{"case_id": "other-case"}, {"evidence": ["invented:e1"]}])
async def test_field_repair_cannot_change_valid_fields_and_never_rerolls_after_its_own_limit(extra):
    def answer(messages, options, turn):
        data = packet(messages)
        return receipt(call("submit_refuted", refutation(data))) if turn == 1 else patch(data, **extra)

    model = SessionModel(handler=answer)
    result = await Investigator(model, source_executor([]), protocol_version=8, max_steps=2).investigate(
        candidate(), state(), ContextPack()
    )
    assert result.verdict == "unknown" and result.reason == "case-result-repair-limit" and len(model.calls) == 3
    saved = next(entry for entry in result.progress if entry.get("phase") == "case-result-repair")
    assert saved["attempts"] == 2 and saved["status"] == "rejected"
    assert saved["errors"][0]["type"] == "extra_forbidden"


@pytest.mark.asyncio
async def test_wrong_case_and_missing_semantics_do_not_qualify_for_field_repair():
    def answer(messages, options, turn):
        args = refutation(packet(messages)) | {"case_id": "wrong-case"}
        return receipt(call("submit_refuted", args))

    model = SessionModel(handler=answer)
    result = await Investigator(model, source_executor([]), protocol_version=8, max_steps=2).investigate(
        candidate(), state(), ContextPack()
    )
    assert result.verdict == "unknown" and len(model.calls) == 1
    assert not any(row.get("phase") == "case-result-repair" for row in result.progress)


@pytest.mark.asyncio
async def test_cancel_restores_original_draft_and_consumed_attempt_without_new_evidence_turns():
    item = candidate()
    snapshots = []

    async def checkpoint(observations, progress):
        item.observations = copy.deepcopy(observations)
        item.investigation_progress = copy.deepcopy(progress)
        snapshots.append(copy.deepcopy(item))
        repair = next((row for row in progress if row.get("phase") == "case-result-repair"), None)
        if repair and repair["attempts"] == 1:
            raise asyncio.CancelledError()

    model = SessionModel(handler=lambda messages, *_: receipt(call("submit_refuted", refutation(packet(messages)))))
    with pytest.raises(asyncio.CancelledError):
        await Investigator(
            model, source_executor([]), protocol_version=8, max_steps=2, on_progress=checkpoint
        ).investigate(item, state(), ContextPack())
    restored = copy.deepcopy(snapshots[-1])
    model = SessionModel(handler=lambda messages, *_: patch(packet(messages)))
    executed = []
    result = await Investigator(model, source_executor(executed), protocol_version=8, max_steps=1).investigate(
        restored, state(), ContextPack()
    )
    assert result.verdict == "refuted" and result.steps == 0 and len(model.calls) == 1 and not executed
    repair = next(row for row in result.progress if row.get("phase") == "case-result-repair")
    assert repair["attempts"] == 2 and repair["status"] == "complete"
    restored.investigation_progress = copy.deepcopy(result.progress)
    replay = await Investigator(model, source_executor(executed), protocol_version=8).investigate(
        restored, state(), ContextPack()
    )
    assert replay.verdict == "refuted" and replay.steps == 0 and len(model.calls) == 1 and not executed
    tampered = copy.deepcopy(snapshots[-1])
    next(row for row in tampered.investigation_progress if row.get("phase") == "case-result-repair")["draft"]["raw"][
        "statement"
    ] = "changed"
    with pytest.raises(ValueError, match="binding"):
        await Investigator(model, source_executor(executed), protocol_version=8).investigate(
            tampered, state(), ContextPack()
        )


@pytest.mark.asyncio
async def test_real_825_character_draft_uses_sdk_field_patch_but_absent_evidence_cannot_be_confirmed():
    import httpx

    from reviewforge.engine.openai_compat import ReasoningContentChatOpenAI

    fixture = json.loads((Path(__file__).parent / "fixtures/v7_terminal_case_result.json").read_text("utf-8"))
    original = copy.deepcopy(fixture)
    item = replace(candidate(), **fixture["case"])
    review_state = state()
    review_state.head_sha = fixture["head_sha"]
    requests = []

    def server(request):
        data = json.loads(request.content)
        requests.append(data)
        assert data["tool_choice"] == "required" and "response_format" not in data
        if len(requests) == 1:
            response = fixture["tool_call"]
        else:
            material = json.loads(data["messages"][1]["content"])
            assert material["original"] == fixture["tool_call"]["args"]
            assert material["set_fields"] == ["reason"] and len(material["original"]["reason"]) == 825
            assert len(data["tools"]) == 1 and data["tools"][0]["function"]["name"] == "repair_case_result"
            response = patch(material).tool_calls[0]
        return httpx.Response(
            200,
            json={
                "id": "fixture",
                "model": "offline",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "result",
                                    "type": "function",
                                    "function": {
                                        "name": response["name"],
                                        "arguments": json.dumps(response["args"]),
                                    },
                                }
                            ],
                        },
                    }
                ],
                "usage": {"prompt_tokens": 100, "completion_tokens": 30, "total_tokens": 130},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as client:
        model = ReasoningContentChatOpenAI(
            model="offline",
            base_url="https://offline.invalid/v1",
            api_key="fake",
            http_async_client=client,
            max_retries=0,
        )
        result = await Investigator(model, source_executor([]), protocol_version=8, max_steps=2).investigate(
            item, review_state, ContextPack(workspace_digest=fixture["workspace_digest"])
        )
    assert len(requests) == 2 and result.verdict == "unknown" and result.outcome == "output-error"
    saved = next(row for row in result.progress if row.get("phase") == "case-result-repair")
    assert saved["status"] == "rejected" and saved["draft"]["raw"] == original["tool_call"]["args"]
    assert [(tuple(row["loc"]), row["type"]) for row in saved["errors"]] == [(("reason",), "string_too_long")]
    assert fixture == original


@pytest.mark.asyncio
async def test_scheduler_resumes_terminal_repair_when_original_task_has_zero_evidence_steps_left():
    item = candidate()
    book = HypothesisLedger("run", "head", "scope", protocol_version=8)
    book.upsert(item)
    snapshots = []
    meter = RequestBudget(None)

    async def budget_changed(snapshot):
        book.request_budget = copy.deepcopy(snapshot)

    async def checkpoint(ledger):
        snapshots.append(copy.deepcopy(ledger.to_dict()))
        if any(
            row.get("phase") == "case-result-repair" and row["attempts"] == 1
            for row in ledger.items[item.identity].investigation_progress
        ):
            raise asyncio.CancelledError()

    raw = SessionModel(handler=lambda messages, *_: receipt(call("submit_refuted", refutation(packet(messages)))))
    model = BudgetedChatLLM(raw, meter, phase="investigation", on_change=budget_changed)
    with pytest.raises(asyncio.CancelledError):
        await Investigator(model, source_executor([]), protocol_version=8, max_steps=2).run(
            book, state(), ContextPack(workspace_digest="scope"), request_budget=meter, on_update=checkpoint
        )
    book = HypothesisLedger.from_dict(snapshots[-1])
    meter = RequestBudget(None, saved=book.request_budget)
    raw = SessionModel(handler=lambda messages, *_: patch(packet(messages)))
    model = BudgetedChatLLM(raw, meter, phase="investigation", on_change=budget_changed)
    results = await Investigator(model, source_executor([]), protocol_version=8, max_steps=2).run(
        book, state(), ContextPack(workspace_digest="scope"), request_budget=meter
    )
    task = next(iter(book.investigation_tasks.values()))
    assert results[0].verdict == "refuted" and results[0].steps == 0 and len(raw.calls) == 1
    assert task["total_steps"] == task["budget_steps_limit"] == 2
    assert task["total_tokens"] == meter.reported == 240 and task["token_limit"] is None
