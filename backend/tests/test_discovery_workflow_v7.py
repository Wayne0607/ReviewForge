"""Protocol and resource regressions; scripted decisions are not quality scores."""

from __future__ import annotations

import asyncio
import copy
import json
from types import SimpleNamespace

import pytest
from test_discovery_submission import fixture
from test_discovery_workflow_v6 import batch, lead, no_issue
from test_investigation_v3 import source_executor
from test_review_session_v5 import SessionModel, call, packet, receipt

from reviewforge.core.config import PipelineV4Config
from reviewforge.core.events import EventBus
from reviewforge.engine.context_pack import ContextPack, ContextSlice, UnitContext
from reviewforge.engine.hypothesis import HypothesisLedger
from reviewforge.engine.hypothesis_generator import HypothesisGenerator
from reviewforge.engine.pipeline_v4 import _run_llm_stages
from reviewforge.engine.request_budget import (
    BudgetedChatLLM,
    RequestBudget,
    estimate_request,
    external_request_budget_scope,
)


def inputs(count=2, *, large=False):
    state, changes, _, _ = fixture(count)
    state.head_sha = "head"
    pack = ContextPack()
    if large:
        for unit in changes.units:
            pack.units[unit.id] = UnitContext(
                unit.id,
                slices=[
                    ContextSlice(
                        "caller",
                        unit.path + "_caller.py",
                        1,
                        50,
                        "caller",
                        "# SOURCE_SENTINEL " + "x" * 7000,
                        "caller source",
                        "head",
                    )
                ],
            )
    return state, changes, pack


async def run(model, *, count=2, ledger=None, large=False, **options):
    state, changes, pack = inputs(count, large=large)
    ledger = ledger or HypothesisLedger("run", "head", "digest", protocol_version=7)
    await HypothesisGenerator(model, executor=source_executor([]), **options).run(
        state, pack, changes, ledger, resume=bool(ledger.discovery_blocks)
    )
    return ledger


def patches(data, *, change=None):
    result = []
    for item in data["repair_records"]:
        raw = item["rejected_record_untrusted"]
        values = {}
        for name in item["set_fields"]:
            if name == "source_refs":
                values[name] = [ref for side in item["required_source_sides"] for ref in side if ref in raw[name]][:12]
            else:
                values[name] = "Compared the changed operation and its supported consumer boundary."
        patch = {"record_id": item["record_id"], "set_fields": values, "remove_fields": item["remove_fields"]}
        if change:
            change(patch)
        result.append(patch)
    return receipt(call("repair_assessments", {"repairs": result}))


class CostModel(SessionModel):
    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        result = super()._generate(messages, stop, run_manager, **kwargs)
        message = result.generations[0].message
        incoming = estimate_request(messages, kwargs)
        outgoing = (len(json.dumps(message.tool_calls).encode()) + 2) // 3
        message.usage_metadata = {
            "input_tokens": incoming,
            "output_tokens": outgoing,
            "total_tokens": incoming + outgoing,
        }
        return result


@pytest.mark.asyncio
async def test_duplicate_discrepancy_is_optional_but_missing_claim_and_bad_sources_are_not():
    def answer(messages, *_):
        record = lead(packet(messages)["pending_units"][0])
        record.pop("discrepancy")
        return batch(record)

    model = SessionModel(handler=answer)
    ledger = await run(model, count=1)
    assert len(model.calls) == 1 and len(ledger.items) == 1 and not ledger.unresolved_units
    item = next(iter(ledger.items.values()))
    assert "discrepancy" not in item.discovery_basis
    old = SessionModel(handler=answer)
    prior = await run(old, count=1, ledger=HypothesisLedger("run", "head", "digest", protocol_version=6))
    assert prior.unresolved_units and not prior.items

    def missing(messages, *_):
        record = lead(packet(messages)["pending_units"][0])
        record.pop("claim")
        return batch(record)

    failed = await run(SessionModel(handler=missing), count=1)
    assert failed.unresolved_units and not failed.items


@pytest.mark.asyncio
async def test_repair_sends_only_rejected_fields_and_keeps_valid_sibling_and_original_sources():
    original = {}

    def answer(messages, options, turn):
        data = packet(messages)
        if turn == 1:
            first, second = data["pending_units"]
            original.update(no_issue(second))
            bad = original | {"checked": "Oversized boundary narrative. " * 30, "source_refs_note": "annotation"}
            return batch(no_issue(first), bad)
        assert data["session"]["phase"] == "field-repair"
        assert "SOURCE_SENTINEL" not in str(messages) and "rows" not in data
        assert [row["function"]["name"] for row in options["tools"]] == ["repair_assessments"]
        assert len(data["repair_records"]) == 1
        assert data["repair_records"][0]["rejected_record_untrusted"]["source_refs"] == original["source_refs"]
        return patches(data)

    model = CostModel(handler=answer)
    ledger = await run(model, large=True)
    assert len(model.calls) == 2 and len(ledger.no_issue_units) == 2 and not ledger.unresolved_units
    first, second = model.calls
    assert estimate_request(second[0], second[1]) < estimate_request(first[0], first[1]) / 2
    record = next(iter(ledger.discovery_blocks.values()))
    assert record["workflow"]["invalid_receipts"] == 1
    assert any(event["kind"] == "repair-draft" for event in record["session"]["events"])


@pytest.mark.asyncio
@pytest.mark.parametrize("attack", ["identity", "new-reference", "valid-field"])
async def test_patch_cannot_change_valid_fields_or_import_new_source(attack):
    def answer(messages, options, turn):
        data = packet(messages)
        if turn == 1:
            bad = no_issue(data["pending_units"][0])
            bad["source_refs"] *= 8
            return batch(bad)

        def corrupt(patch):
            if attack == "identity":
                patch["record_id"] = "different-draft"
            elif attack == "new-reference":
                patch["set_fields"]["source_refs"] = ["f999.R1"]
            else:
                patch["set_fields"]["checked"] = "Clean from an altered field."

        return patches(data, change=corrupt)

    model = SessionModel(handler=answer)
    ledger = await run(model, count=1)
    assert len(model.calls) == 3 and ledger.unresolved_units and not ledger.no_issue_units
    await run(model, count=1, ledger=HypothesisLedger.from_dict(ledger.to_dict()))
    assert len(model.calls) == 3


@pytest.mark.asyncio
async def test_reference_list_repair_revalidates_required_before_and_after():
    def answer(messages, options, turn):
        data = packet(messages)
        if turn == 1:
            bad = no_issue(data["pending_units"][0])
            bad["source_refs"] *= 8
            return batch(bad)
        if data["session"]["phase"] == "field-repair":
            return patches(data, change=lambda patch: patch["set_fields"].update(source_refs=["f0.R1"]))
        assert data["session"]["phase"] == "repair"  # Semantic error requires full source.
        return batch(
            {
                "kind": "deferred",
                "unit_id": data["pending_units"][0]["unit_id"],
                "reason": "The base-side checked boundary is unresolved.",
            }
        )

    ledger = await run(SessionModel(handler=answer), count=1)
    assert ledger.unresolved_units and not ledger.no_issue_units


@pytest.mark.asyncio
async def test_semantically_invalid_source_record_can_be_replaced_by_a_valid_full_submission():
    def answer(messages, options, turn):
        data = packet(messages)
        result = no_issue(data["pending_units"][0])
        if turn == 1:
            result["source_refs"] = ["f0.R1"]
        return batch(result)

    model = SessionModel(handler=answer)
    ledger = await run(model, count=1)
    assert len(model.calls) == 2 and len(ledger.no_issue_units) == 1 and not ledger.unresolved_units


@pytest.mark.asyncio
async def test_repair_of_repeated_candidate_at_capacity_does_not_allocate_another_slot():
    def answer(messages, options, turn):
        data = packet(messages)
        if turn == 1:
            original = lead(data["pending_units"][0])
            return batch(original, original | {"source_refs_note": "an extra annotation"})
        return patches(data)

    ledger = await run(SessionModel(handler=answer), count=1, max_hypotheses=1)
    assert len(ledger.items) == 1 and not ledger.unresolved_units


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["unit_id", "kind"])
async def test_malformed_non_string_identity_gets_bounded_feedback(field):
    def answer(messages, *_):
        raw = no_issue(packet(messages)["pending_units"][0])
        raw[field] = []
        return batch(raw)

    model = SessionModel(handler=answer)
    ledger = await run(model, count=1)
    assert len(model.calls) == 3 and ledger.unresolved_units and not ledger.no_issue_units


@pytest.mark.asyncio
async def test_valid_candidate_does_not_hide_invalid_sibling_in_same_unit():
    seen = []

    async def checkpoint(ledger):
        seen.append(copy.deepcopy(ledger.to_dict()))

    def answer(messages, options, turn):
        data = packet(messages)
        if turn == 1:
            unit = data["pending_units"][0]
            return batch(
                lead(unit),
                {
                    "kind": "deferred",
                    "unit_id": unit["unit_id"],
                    "reason": "Some consumer source is still unavailable.",
                    "checked": "extra",
                },
            )
        assert any(row["unresolved_units"] and row["items"] for row in seen)
        return patches(data)

    ledger = await run(SessionModel(handler=answer), count=1, on_update=checkpoint)
    assert len(ledger.items) == 1 and ledger.unresolved_units and not ledger.no_issue_units
    assert next(iter(ledger.discovery_blocks.values()))["units"]["file0.py:update"]["finished"]


@pytest.mark.asyncio
async def test_repair_draft_survives_cancel_and_rejects_tampering_after_restore():
    snapshots = []

    async def checkpoint(ledger):
        snapshots.append(copy.deepcopy(ledger.to_dict()))
        if any(record["workflow"].get("repairs") for record in ledger.discovery_blocks.values()):
            raise asyncio.CancelledError()

    def answer(messages, *_):
        return batch(no_issue(packet(messages)["pending_units"][0]) | {"checked": "narrative " * 80})

    with pytest.raises(asyncio.CancelledError):
        await run(SessionModel(handler=answer), count=1, on_update=checkpoint)
    restored = HypothesisLedger.from_dict(snapshots[-1])
    model = SessionModel(handler=lambda messages, *_: patches(packet(messages)))
    await run(model, count=1, ledger=restored)
    assert len(model.calls) == 1 and len(restored.no_issue_units) == 1
    tampered = HypothesisLedger.from_dict(snapshots[-1])
    draft = next(iter(next(iter(tampered.discovery_blocks.values()))["workflow"]["repairs"].values()))
    draft["raw"]["checked"] = "altered"
    with pytest.raises(ValueError, match="binding"):
        await run(model, count=1, ledger=tampered)


@pytest.mark.asyncio
async def test_tighter_outer_grant_heavy_inputs_and_bad_receipt_still_reach_investigation():
    state, changes, pack = inputs(6, large=True)
    ledger = HypothesisLedger("integrated", "head", "digest", protocol_version=7)
    seen = []

    def answer(messages, options, turn):
        data = packet(messages)
        if "case_id" in data:
            seen.append("investigator")
            return receipt(
                call(
                    "submit_unknown",
                    {
                        "case_id": data["case_id"],
                        "reason": "Missing contract.",
                        "missing_fact": "The production consumer contract is unavailable.",
                    },
                )
            )
        if data["session"]["phase"] == "field-repair":
            seen.append("repair")
            return patches(data)
        seen.append("discovery")
        units = data["pending_units"]
        records = [no_issue(unit) for unit in units]
        if units[0]["path"] == "file0.py":
            records[0] = lead(units[0])
            records[0].pop("discrepancy")
            records[-1]["checked"] = "boundary narrative " * 40
        return batch(*records)

    raw = CostModel(handler=answer)
    router = SimpleNamespace(get_llm=lambda *_: raw, effective=lambda *_: {"model": "offline"})
    runtime = SimpleNamespace(_model_router=router, _events=EventBus(), _db=None)
    grant = RequestBudget(70_000, editor_reserve=0)

    async def persist(_snapshot):
        pass

    with external_request_budget_scope(grant, identity="small-live-grant", on_change=persist):
        publication = await _run_llm_stages(
            runtime,
            state,
            changes,
            pack,
            ledger,
            SimpleNamespace(read=lambda _: "return new_value\n"),
            PipelineV4Config(protocol_version=7, investigator_concurrency=1),
            "en",
            {},
        )
    assert "repair" in seen and "investigator" in seen
    assert len(ledger.items) == 1 and len(ledger.no_issue_units) == 5 and not ledger.unresolved_units
    assert next(iter(ledger.items.values())).investigation_outcome == "insufficient-evidence"
    assert not publication.comments  # Scripted missing evidence cannot become a defect.
    assert 0 < grant.reported < 70_000 and grant.reported == ledger.request_budget["reported_tokens"]
    assert ledger.request_budget["limit"] == 250_000 and not grant.pending and not grant.unreported


@pytest.mark.asyncio
async def test_real_sdk_transports_the_field_patch_schema_and_reconstructed_record():
    import httpx

    from reviewforge.engine.openai_compat import ReasoningContentChatOpenAI

    requests = []

    def server(request):
        data = json.loads(request.content)
        requests.append(data)
        material = json.loads(data["messages"][1]["content"])
        assert data["tool_choice"] == "required" and "response_format" not in data
        if len(requests) == 1:
            first, second = material["pending_units"]
            response = batch(no_issue(first), no_issue(second) | {"checked": "boundary " * 80})
        else:
            assert len(data["tools"]) == 1 and data["tools"][0]["function"]["name"] == "repair_assessments"
            assert material["session"]["phase"] == "field-repair" and "rows" not in material
            record_id = material["repair_records"][0]["record_id"]
            assert record_id in json.dumps(data["tools"][0]["function"]["parameters"])
            response = patches(material)
        return httpx.Response(
            200,
            json={
                "id": "offline",
                "object": "chat.completion",
                "created": 0,
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
                                    "id": action["id"],
                                    "type": "function",
                                    "function": {
                                        "name": action["name"],
                                        "arguments": json.dumps(action["args"]),
                                    },
                                }
                                for action in response.tool_calls
                            ],
                        },
                    }
                ],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as client:
        model = ReasoningContentChatOpenAI(
            model="offline",
            api_key="test",
            base_url="https://example.invalid/v1",
            http_async_client=client,
            max_retries=0,
        )
        budget = RequestBudget(30_000, editor_reserve=0)
        ledger = await run(BudgetedChatLLM(model, budget, phase="discovery"))
    assert len(requests) == 2 and len(ledger.no_issue_units) == 2 and not ledger.unresolved_units
    assert budget.reported == 240 and not budget.pending
