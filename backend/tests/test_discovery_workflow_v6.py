from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from langchain_core.messages import AIMessage
from pydantic import ValidationError
from test_discovery_submission import fixture
from test_investigation_v3 import source_executor
from test_review_session_v5 import SessionModel, call, clean, packet, receipt

from reviewforge.engine.context_pack import ContextPack
from reviewforge.engine.discovery_aliases import source_aliases
from reviewforge.engine.discovery_session import source_packet
from reviewforge.engine.discovery_workflow import compact_packet
from reviewforge.engine.discovery_workflow_tools import assessment_tools
from reviewforge.engine.hypothesis import HypothesisLedger
from reviewforge.engine.hypothesis_generator import HypothesisGenerator
from reviewforge.engine.request_budget import BudgetedChatLLM, RequestBudget


def batch(*records):
    return receipt(call("submit_assessments", {"records": list(records)}))


def no_issue(unit):
    return clean(unit) | {"kind": "no_issue"}


def lead(unit):
    fields = clean(unit)
    fields.pop("checked")
    return fields | {
        "kind": "hypothesis",
        "mechanism": "contract-mismatch",
        "severity": "warning",
        "claim": "The replacement value may violate a consumer's stored-value contract.",
        "trigger": "An existing stored record is requested.",
        "impact": "The caller receives a replacement value.",
        "open_question": "Does the consumer require the stored value?",
        "refutation": "The caller supports replacements.",
        "discrepancy": "The changed return replaces the stored value.",
        "site_refs": unit["allowed_site_refs"][:1],
        "trigger_ref": unit["allowed_site_refs"][0],
    }


async def run(model, count=4, *, ledger=None, **options):
    state, changes, _, _ = fixture(count)
    state.head_sha = "head"
    ledger = ledger or HypothesisLedger("run", "head", "digest", protocol_version=6)
    result = await HypothesisGenerator(model, executor=options.pop("executor", source_executor([])), **options).run(
        state, ContextPack(), changes, ledger, resume=bool(ledger.discovery_blocks)
    )
    return result, ledger


@pytest.mark.asyncio
async def test_one_native_batch_commits_each_unit_independently():
    model = SessionModel(handler=lambda messages, *_: batch(*map(no_issue, packet(messages)["pending_units"])))
    result, ledger = await run(model)
    assert len(model.calls) == result.blocks == 1 and len(ledger.no_issue_units) == 4
    assert not ledger.unresolved_units


@pytest.mark.asyncio
async def test_partial_success_can_continue_beyond_three_turns_without_reasking_completed_units():
    seen = []

    def answer(messages, options, turn):
        units = packet(messages)["pending_units"]
        assert not set(seen).intersection(unit["unit_id"] for unit in units)
        seen.append(units[0]["unit_id"])
        return batch(no_issue(units[0]))

    model = SessionModel(handler=answer)
    _, ledger = await run(model)
    assert len(model.calls) == 4 and len(ledger.no_issue_units) == 4 and not ledger.unresolved_units


@pytest.mark.asyncio
async def test_v9_scales_requests_for_partial_batches_without_sacrificing_reads():
    def answer(messages, options, _turn):
        names = {tool["function"]["name"] for tool in options["tools"]}
        if "read_file" in names:
            return receipt(call("read_file", {"path": "consumer.py", "start": 1, "end": 1}))
        return batch(no_issue(packet(messages)["pending_units"][0]))

    model = SessionModel(handler=answer)
    book = HypothesisLedger("run", "head", "digest", protocol_version=9)
    _, book = await run(model, count=4, ledger=book, session_request_limit=4)
    assert len(model.calls) == 6 and len(book.no_issue_units) == 4
    assert not book.unresolved_units
    work = next(iter(book.discovery_blocks.values()))
    assert work["workflow"]["read_requests"] == 2
    assert work["workflow"]["result_requests"] == 4
    assert work["request_limit"] == 6
    restored = HypothesisLedger.from_dict(book.to_dict())
    await run(model, count=4, ledger=restored, session_request_limit=32)
    assert len(model.calls) == 6


@pytest.mark.asyncio
async def test_all_groups_receive_an_initial_opportunity_before_optional_continuations():
    groups = []

    def answer(messages, options, turn):
        data = packet(messages)
        groups.append(tuple(unit["unit_id"] for unit in data["pending_units"]))
        if turn <= 3:
            return receipt(call("read_file", {"path": "consumer.py", "start": turn, "end": turn}))
        return batch(*map(no_issue, data["pending_units"]))

    model = SessionModel(handler=answer)
    _, ledger = await run(model, count=9)
    assert len(set(groups[:3])) == 3 and len(model.calls) == 6
    assert len(ledger.no_issue_units) == 9 and not ledger.unresolved_units
    assert {record["request_limit"] for record in ledger.discovery_blocks.values()} == {32}


@pytest.mark.asyncio
async def test_read_allowance_cannot_consume_result_or_protocol_repair_allowances():
    executed = []

    def answer(messages, options, turn):
        data = packet(messages)
        names = {tool["function"]["name"] for tool in options["tools"]}
        if turn <= 2:
            assert "read_file" in names
            return receipt(call("read_file", {"path": "consumer.py", "start": turn, "end": turn}))
        assert names == {"submit_assessments"}
        if turn == 3:
            return receipt(call("read_file", {"path": "forbidden.py", "start": 1, "end": 2}))
        assert "unregistered-action" in messages[-1].content
        return batch(*map(no_issue, data["pending_units"]))

    model = SessionModel(handler=answer)
    _, ledger = await run(model, executor=source_executor(executed))
    assert len(model.calls) == 4 and len(executed) == 2 and len(ledger.no_issue_units) == 4
    work = next(iter(ledger.discovery_blocks.values()))
    assert work["workflow"]["read_requests"] == work["workflow"]["result_requests"] == 2


@pytest.mark.asyncio
async def test_missing_field_receipt_is_persisted_and_valid_sibling_is_not_erased():
    snapshots = []

    async def checkpoint(ledger):
        snapshots.append(copy.deepcopy(ledger.to_dict()))

    def answer(messages, options, turn):
        units = packet(messages)["pending_units"]
        if turn == 1:
            broken = lead(units[1])
            broken.pop("mechanism")
            return batch(no_issue(units[0]), broken)
        assert len(units) == 1
        feedback = json.loads(messages[-1].content)["controller_receipt"]
        bad = next(row for row in feedback if row.get("error") == "invalid-assessment-schema")
        assert bad["fields"] == [{"field": ["mechanism"], "type": "missing"}]
        assert "mechanism" not in bad["rejected_record_untrusted"]
        return batch(lead(units[0]))

    model = SessionModel(handler=answer)
    _, ledger = await run(model, count=2, on_update=checkpoint)
    assert len(model.calls) == 2 and len(ledger.no_issue_units) == len(ledger.items) == 1
    assert not ledger.unresolved_units
    assert any("invalid-assessment-schema" in json.dumps(s["discovery_blocks"]) for s in snapshots)


@pytest.mark.asyncio
async def test_invalid_responses_receive_only_two_repairs_and_resume_does_not_reset_them():
    model = SessionModel(handler=lambda *_: AIMessage(content="<tool_call>read_file</tool_call>"))
    _, ledger = await run(model, count=1)
    assert len(model.calls) == 3 and not ledger.no_issue_units
    restored = HypothesisLedger.from_dict(ledger.to_dict())
    await run(model, count=1, ledger=restored)
    assert len(model.calls) == 3 and restored.unresolved_units


@pytest.mark.asyncio
async def test_explicit_deferred_units_remain_uncovered_but_are_not_reasked_on_resume():
    model = SessionModel(
        handler=lambda messages, *_: batch(
            *[
                {"kind": "deferred", "unit_id": unit["unit_id"], "reason": "The consumer contract is unavailable."}
                for unit in packet(messages)["pending_units"]
            ]
        )
    )
    _, ledger = await run(model, count=2)
    await run(model, count=2, ledger=HypothesisLedger.from_dict(ledger.to_dict()))
    assert len(model.calls) == 1 and len(ledger.unresolved_units) == 2 and not ledger.no_issue_units


@pytest.mark.asyncio
async def test_global_hard_counter_and_head_binding_are_preserved_across_resume():
    model = SessionModel(handler=lambda *_: receipt(call("read_file", {"path": "x.py", "start": 1, "end": 2})))
    _, ledger = await run(model, count=1, session_request_limit=1)
    restored = HypothesisLedger.from_dict(ledger.to_dict())
    await run(model, count=1, ledger=restored, session_request_limit=32)
    assert len(model.calls) == 1 and not ledger.no_issue_units
    restored.head_sha = "different-head"
    with pytest.raises(ValueError):
        await run(model, count=1, ledger=restored)


def test_compact_source_is_lossless_including_segments_and_side_coordinates():
    _, _, block, _ = fixture(4)
    aliases = source_aliases(block.source_refs, scoped=True)
    old = source_packet(block, aliases, {u.id for u in block.units})
    compact = compact_packet(block, aliases, {u.id for u in block.units}, [])
    original = {row[0]: row[1:] for row in old["source"]}
    decoded = {}
    for alias, text in compact["rows"]:
        file_id, coord = alias.split(".", 1)
        side = {"B": "before", "R": "after", "C": "context"}[coord[0]]
        line = int(coord[1:].split(".", 1)[0])
        decoded[alias] = [side, compact["files"][file_id], line, text]
    assert decoded == original and compact["pending_units"] == old["pending_units"]
    assert len(json.dumps(compact)) < len(json.dumps(old))


@pytest.mark.asyncio
async def test_budget_refusal_is_before_provider_dispatch_and_does_not_claim_coverage():
    model = SessionModel(handler=lambda *_: batch())
    budget = RequestBudget(100, editor_reserve=0)
    _, ledger = await run(BudgetedChatLLM(model, budget, phase="discovery"), count=2)
    assert not model.calls and not budget.pending and not budget.reported
    assert len(ledger.unresolved_units) == 2 and not ledger.no_issue_units


@pytest.mark.asyncio
async def test_actual_outer_cancellation_keeps_source_and_charged_dispatch_counter():
    saved = []

    async def checkpoint(ledger):
        saved.append(copy.deepcopy(ledger.to_dict()))

    class InterruptModel(SessionModel):
        waiting: object = None

        async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
            if self.calls:
                self.waiting.set()
                await asyncio.Event().wait()
            return self._generate(messages, stop, run_manager, **kwargs)

    model = InterruptModel(
        handler=lambda *_: receipt(call("read_file", {"path": "consumer.py", "start": 1, "end": 2})),
        waiting=asyncio.Event(),
    )
    task = asyncio.create_task(run(model, count=1, on_update=checkpoint))
    await asyncio.wait_for(model.waiting.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    record = next(iter(saved[-1]["discovery_blocks"].values()))
    assert record["observations"] and record["session"]["turns"] == 2
    assert record["workflow"]["read_requests"] == 2


def test_real_saved_missing_mechanism_is_not_filled_by_batch_encoding():
    saved = json.loads((Path(__file__).parent / "fixtures/v6_terminal_failures.json").read_text(encoding="utf-8"))
    original = saved[0]["response"]["tool_calls"][0]["args"]
    block = SimpleNamespace(units=[SimpleNamespace(id=original["unit_id"])], structural_changes={})
    _, schemas = assessment_tools(block)
    with pytest.raises(ValidationError) as error:
        schemas["hypothesis"].model_validate(original | {"kind": "hypothesis"})
    assert [e["loc"] for e in error.value.errors()] == [("mechanism",)]
    assert "mechanism" not in original


@pytest.mark.asyncio
async def test_eighteen_units_can_read_and_submit_in_fifteen_calls_instead_of_stopping_at_ten():
    def answer(messages, options, turn):
        data = packet(messages)
        if data["session"]["phase"] == "acquire":
            return receipt(call("read_file", {"path": "consumer.py", "start": turn, "end": turn}))
        return batch(*map(no_issue, data["pending_units"]))

    model = SessionModel(handler=answer)
    _, ledger = await run(model, count=18)
    assert len(model.calls) == 15 and len(ledger.no_issue_units) == 18 and not ledger.unresolved_units


@pytest.mark.asyncio
@pytest.mark.parametrize("index", [1, 2])
async def test_real_saved_illegal_terminal_reads_receive_repair_and_never_execute(index):
    saved = json.loads((Path(__file__).parent / "fixtures/v6_terminal_failures.json").read_text(encoding="utf-8"))[
        index
    ]
    untouched = copy.deepcopy(saved)
    executed = []

    def answer(messages, options, turn):
        if turn <= 2:
            return receipt(call("read_file", {"path": "consumer.py", "start": turn, "end": turn}))
        if turn == 3:
            response = saved["response"]
            return AIMessage(
                content=response["content"],
                tool_calls=response["tool_calls"],
                additional_kwargs={"reviewforge_raw_tool_calls": response["raw_tool_calls"]},
                response_metadata={"finish_reason": response["finish_reason"]},
            )
        assert "unregistered-action" in messages[-1].content
        return batch(*map(no_issue, packet(messages)["pending_units"]))

    model = SessionModel(handler=answer)
    _, ledger = await run(model, count=2, executor=source_executor(executed))
    assert len(model.calls) == 4 and len(executed) == 2 and len(ledger.no_issue_units) == 2
    assert saved == untouched


@pytest.mark.asyncio
async def test_covered_units_and_historical_dispatches_cannot_be_reset_by_regrouping():
    model = SessionModel(handler=lambda messages, *_: batch(*map(no_issue, packet(messages)["pending_units"])))
    _, ledger = await run(model, count=1, session_request_limit=1)
    await run(model, count=2, ledger=HypothesisLedger.from_dict(ledger.to_dict()), session_request_limit=32)
    assert len(model.calls) == 1


@pytest.mark.asyncio
async def test_regrouping_reuses_acknowledgements_without_reasking_an_already_assessed_unit():
    acknowledged = set()

    def answer(messages, options, turn):
        units = packet(messages)["pending_units"]
        assert not acknowledged.intersection(unit["unit_id"] for unit in units)
        acknowledged.update(unit["unit_id"] for unit in units)
        return batch(*map(no_issue, units))

    model = SessionModel(handler=answer)
    _, ledger = await run(model, count=1)
    _, ledger = await run(model, count=2, ledger=HypothesisLedger.from_dict(ledger.to_dict()))
    assert len(model.calls) == 2 and len(ledger.no_issue_units) == 2 and not ledger.unresolved_units


@pytest.mark.asyncio
async def test_restored_read_counter_cannot_be_reset_while_retaining_the_total_dispatch_count():
    model = SessionModel(handler=lambda *_: AIMessage(content="invalid"))
    _, ledger = await run(model, count=1)
    record = next(iter(ledger.discovery_blocks.values()))
    record["workflow"]["read_requests"] = 0
    with pytest.raises(ValueError, match="counters"):
        await run(model, count=1, ledger=ledger)


@pytest.mark.asyncio
async def test_workflow_is_used_by_real_pipeline_and_releases_future_candidate_reserve():
    from reviewforge.core.config import PipelineV4Config
    from reviewforge.core.events import EventBus
    from reviewforge.engine.pipeline_v4 import _run_llm_stages

    state, changes, _, _ = fixture(2)
    state.head_sha = "head"
    ledger = HypothesisLedger("integrated", "head", "digest", protocol_version=6)
    model = SessionModel(handler=lambda messages, *_: batch(*map(no_issue, packet(messages)["pending_units"])))
    router = SimpleNamespace(get_llm=lambda *_: model, effective=lambda *_: {"model": "offline"})
    runtime = SimpleNamespace(_model_router=router, _events=EventBus(), _db=None)
    publication = await _run_llm_stages(
        runtime,
        state,
        changes,
        ContextPack(),
        ledger,
        SimpleNamespace(),
        PipelineV4Config(protocol_version=6),
        "en",
        {},
    )
    assert not publication.comments and len(ledger.no_issue_units) == 2 and not ledger.unresolved_units
    assert ledger.request_budget["reported_tokens"] == 120 and not ledger.request_budget["pending"]
    assert ledger.request_budget["allocation"]["future_candidate"] == 0


@pytest.mark.asyncio
async def test_real_request_costs_preserve_other_groups_results_under_limited_currency():
    from reviewforge.engine.request_budget import estimate_request

    class CostModel(SessionModel):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            result = super()._generate(messages, stop, run_manager, **kwargs)
            amount = estimate_request(messages, kwargs)
            result.generations[0].message.usage_metadata = {
                "input_tokens": amount,
                "output_tokens": 20,
                "total_tokens": amount + 20,
            }
            return result

    def answer(messages, options, turn):
        if packet(messages)["session"]["phase"] == "acquire":
            return receipt(call("read_file", {"path": "consumer.py", "start": turn, "end": turn}))
        return batch(*map(no_issue, packet(messages)["pending_units"]))

    model = CostModel(handler=answer)
    budget = RequestBudget(20000, editor_reserve=0)
    _, ledger = await run(BudgetedChatLLM(model, budget, phase="discovery"), count=9)
    assert len(ledger.no_issue_units) == 9 and not ledger.unresolved_units
    assert 0 < budget.reported <= 20000 and not budget.pending and budget.denials == 0


@pytest.mark.asyncio
async def test_real_sdk_delivers_batch_schema_and_repair_preserves_independent_valid_record(tmp_path):
    from reviewforge.engine.openai_compat import ReasoningContentChatOpenAI
    from reviewforge.engine.wire_audit import WireAudit

    requests = []
    audit = WireAudit(str(tmp_path))

    def server(request):
        data = json.loads(request.content)
        requests.append(data)
        tool = data["tools"][0]["function"]
        assert tool["name"] == "submit_assessments" and "records" in tool["parameters"]["required"]
        assert data["tool_choice"] == "required" and "response_format" not in data
        source = json.loads(data["messages"][1]["content"])
        assert "files" in source and "rows" in source and "source" not in source
        if len(requests) == 1:
            first, second = source["pending_units"]
            bad = lead(second)
            bad.pop("mechanism")
            records = [no_issue(first), bad]
        else:
            assert len(source["pending_units"]) == 1 and "mechanism" in data["messages"][-1]["content"]
            assert [t["function"]["name"] for t in data["tools"]] == ["submit_assessments"]
            records = [lead(source["pending_units"][0])]
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
                                    "id": f"batch-{len(requests)}",
                                    "type": "function",
                                    "function": {
                                        "name": "submit_assessments",
                                        "arguments": json.dumps({"records": records}),
                                    },
                                }
                            ],
                        },
                    }
                ],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
            },
        )

    transport = httpx.MockTransport(server)
    with httpx.Client(
        transport=transport, event_hooks={"request": [audit.request], "response": [audit.response]}
    ) as sync:
        async with httpx.AsyncClient(
            transport=transport, event_hooks={"request": [audit.async_request], "response": [audit.async_response]}
        ) as asynchronous:
            model = ReasoningContentChatOpenAI(
                model="offline",
                api_key="never-log-this",
                max_retries=0,
                base_url="https://example.invalid/v1",
                http_client=sync,
                http_async_client=asynchronous,
            )
            budget = RequestBudget(250000, editor_reserve=0)
            _, ledger = await run(BudgetedChatLLM(model, budget, phase="discovery"), count=2)
    assert len(requests) == 2 and budget.reported == 240 and not budget.pending
    assert len(ledger.items) == len(ledger.no_issue_units) == 1 and not ledger.unresolved_units
    assert len(list(tmp_path.glob("*-request.json"))) == len(list(tmp_path.glob("*-response.json"))) == 2
    assert "never-log-this" not in "".join(p.read_text(encoding="utf-8") for p in tmp_path.glob("*.json"))
