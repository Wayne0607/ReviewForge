from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field, ValidationError
from test_discovery_submission import fixture
from test_investigation_v3 import candidate, source_executor, state

from reviewforge.core.config import PipelineV4Config, _pipeline_v4_from_dict
from reviewforge.engine.context_pack import ContextPack
from reviewforge.engine.hypothesis import HypothesisLedger
from reviewforge.engine.hypothesis_generator import HypothesisGenerator
from reviewforge.engine.investigator import Investigator
from reviewforge.engine.review_session import EvidenceStore, ReviewSession
from reviewforge.engine.session_tools import case_identity, decision_tools
from reviewforge.engine.wire_audit import WireAudit


class SessionModel(BaseChatModel):
    handler: Any
    calls: list = Field(default_factory=list)

    @property
    def _llm_type(self):
        return "offline-native-review-session"

    def bind_tools(self, tools, **kwargs):
        return self.bind(tools=tools, **kwargs)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls.append((messages, kwargs))
        message = self.handler(messages, kwargs, len(self.calls))
        message.usage_metadata = {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120}
        return ChatResult(generations=[ChatGeneration(message=message)])


def call(name, args, identity="record"):
    return {"name": name, "args": args, "id": identity, "type": "tool_call"}


def receipt(*calls):
    return AIMessage(content="", tool_calls=list(calls))


def packet(messages):
    return json.loads(messages[1].content)


def clean(unit):
    return {
        "unit_id": unit["unit_id"],
        "checked": "Compared the changed return with its visible operation boundary.",
        "source_refs": [ref for side in unit["required_source_sides"] for ref in side[:1]],
    }


@pytest.mark.asyncio
async def test_discovery_can_read_before_a_lead_and_acknowledged_work_is_not_reasked():
    review_state, changes, _, _ = fixture(2)
    executed = []

    async def execute(name, args):
        executed.append((name, args))
        return "assert fetch(existing_id) == old_value"

    def answer(messages, options, turn):
        data = packet(messages)
        units = data["pending_units"]
        assert all("tools" in options for _ in units)
        if turn == 1:
            assert len(units) == 2
            return receipt(
                call("submit_no_issue", clean(units[0])),
                call("read_file", {"path": "consumer.py", "start": "1", "end": "1"}, "read"),
            )
        assert len(units) == 1
        assert any(row[2] == "consumer.py" and "old_value" in row[4] for row in data["source"])
        return receipt(call("submit_no_issue", clean(units[0])))

    model = SessionModel(handler=answer)
    ledger = HypothesisLedger("run", "head", "digest", protocol_version=5)
    review_state.head_sha = "head"
    result = await HypothesisGenerator(model, executor=execute).run(review_state, ContextPack(), changes, ledger)
    assert len(model.calls) == 2 and result.blocks == 2
    assert len(executed) == 1 and executed[0][1]["start"] == 1
    assert len(ledger.no_issue_units) == 2 and not ledger.unresolved_units
    assert ledger.evidence_records
    assert any(row["kind"] == "tool-observation" for row in ledger.evidence_records.values())
    restored = HypothesisLedger.from_dict(ledger.to_dict())
    assert restored.evidence_records == ledger.evidence_records
    EvidenceStore(restored)


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["xml", "unregistered", "bad-reference"])
async def test_wrong_actions_receive_bounded_feedback_and_never_execute_invented_tools(bad):
    review_state, changes, _, _ = fixture(1)
    executed = []

    def answer(messages, options, turn):
        unit = packet(messages)["pending_units"][0]
        if turn == 1:
            if bad == "xml":
                return AIMessage(
                    content="<tool_call><function=read_file><parameter=ref>f0.R1</parameter></function></tool_call>"
                )
            if bad == "unregistered":
                return receipt(call("write_file", {"path": "app.py", "text": "owned"}))
            return receipt(call("submit_no_issue", clean(unit) | {"source_refs": ["f999.R1"]}))
        assert "controller_receipt" in messages[-1].content
        return receipt(call("submit_no_issue", clean(unit)))

    model = SessionModel(handler=answer)
    ledger = HypothesisLedger("run", "head", "digest", protocol_version=5)
    await HypothesisGenerator(model, executor=source_executor(executed)).run(
        review_state, ContextPack(), changes, ledger
    )
    assert len(model.calls) == 2 and not executed
    assert len(ledger.no_issue_units) == 1 and not ledger.unresolved_units


@pytest.mark.asyncio
async def test_bad_unit_does_not_erase_independently_valid_other_unit():
    review_state, changes, _, _ = fixture(2)

    def answer(messages, options, turn):
        units = packet(messages)["pending_units"]
        if turn == 1:
            return receipt(
                call("submit_no_issue", clean(units[0]), "valid"),
                call("submit_no_issue", clean(units[1]) | {"source_refs": ["f999.R1"]}, "bad"),
            )
        assert len(units) == 1
        return receipt(call("defer_unit", {"unit_id": units[0]["unit_id"], "reason": "Consumer contract unavailable."}))

    ledger = HypothesisLedger("run", "head", "digest", protocol_version=5)
    await HypothesisGenerator(SessionModel(handler=answer), executor=source_executor([])).run(
        review_state,
        ContextPack(),
        changes,
        ledger,
    )
    assert len(ledger.no_issue_units) == len(ledger.unresolved_units) == 1
    assert set(ledger.no_issue_units).isdisjoint(ledger.unresolved_units)


@pytest.mark.asyncio
async def test_a_result_cannot_cite_a_guessed_reference_created_by_a_parallel_read_in_the_same_receipt():
    review_state, changes, _, _ = fixture(1)
    review_state.head_sha = "head"
    captured = []

    async def checkpoint(ledger):
        captured.append(len(ledger.no_issue_units))

    def answer(messages, options, turn):
        unit = packet(messages)["pending_units"][0]
        if turn == 1:
            return receipt(
                call("read_file", {"path": "consumer.py", "start": 1, "end": 1}, "read"),
                call("submit_no_issue", clean(unit) | {"source_refs": clean(unit)["source_refs"] + ["f1.C1"]}),
            )
        assert not any(captured)
        assert "invalid-result-schema-or-reference" in messages[-1].content
        return receipt(call("submit_no_issue", clean(unit)))

    model = SessionModel(handler=answer)
    ledger = HypothesisLedger("run", "head", "digest", protocol_version=5)
    await HypothesisGenerator(model, executor=source_executor([]), on_update=checkpoint).run(
        review_state,
        ContextPack(),
        changes,
        ledger,
    )
    assert len(model.calls) == 2 and len(ledger.no_issue_units) == 1


@pytest.mark.asyncio
async def test_saved_request_cap_cannot_be_reset_by_resume_or_a_new_generator():
    review_state, changes, _, _ = fixture(1)
    model = SessionModel(handler=lambda *args: AIMessage(content="not a native action"))
    ledger = HypothesisLedger("run", "head", "digest", protocol_version=5)
    await HypothesisGenerator(model, executor=source_executor([]), session_request_limit=1).run(
        review_state,
        ContextPack(),
        changes,
        ledger,
    )
    restored = HypothesisLedger.from_dict(ledger.to_dict())
    await HypothesisGenerator(model, executor=source_executor([]), session_request_limit=32).run(
        review_state,
        ContextPack(),
        changes,
        restored,
        resume=True,
    )
    assert len(model.calls) == 1 and restored.unresolved_units and not restored.no_issue_units


@pytest.mark.asyncio
async def test_workload_scaled_session_cap_does_not_make_more_than_32_units_necessarily_incomplete():
    review_state, changes, _, _ = fixture(33)
    model = SessionModel(
        handler=lambda messages, options, turn: receipt(
            *[
                call("submit_no_issue", clean(unit), f"record-{index}")
                for index, unit in enumerate(packet(messages)["pending_units"])
            ]
        )
    )
    ledger = HypothesisLedger("run", "head", "digest", protocol_version=5)
    await HypothesisGenerator(model, executor=source_executor([])).run(review_state, ContextPack(), changes, ledger)
    assert len(ledger.no_issue_units) == 33 and not ledger.unresolved_units
    assert 8 < len(model.calls) <= 32


@pytest.mark.asyncio
async def test_discovery_announces_deadline_and_removes_reads_from_the_final_request():
    review_state, changes, _, _ = fixture(1)
    review_state.head_sha = "head"
    executed = []

    def answer(messages, options, turn):
        data = packet(messages)
        assert data["session"]["requests_remaining"] == 3 - turn
        names = {tool["function"]["name"] for tool in options["tools"]}
        if not data["session"]["last_turn"]:
            assert "read_file" in names
            return receipt(call("read_file", {"path": "consumer.py", "start": turn, "end": turn}))
        assert turn == 3 and names == {"submit_hypothesis", "submit_no_issue", "defer_unit"}
        return receipt(call("submit_no_issue", clean(data["pending_units"][0])))

    model = SessionModel(handler=answer)
    ledger = HypothesisLedger("run", "head", "digest", protocol_version=5)
    await HypothesisGenerator(model, executor=source_executor(executed)).run(
        review_state, ContextPack(), changes, ledger
    )
    assert len(model.calls) == 3 and len(executed) == 2
    assert len(ledger.no_issue_units) == 1 and not ledger.unresolved_units


@pytest.mark.asyncio
async def test_global_last_request_is_terminal_and_cannot_execute_a_late_read():
    review_state, changes, _, _ = fixture(1)
    executed = []

    def answer(messages, options, turn):
        assert packet(messages)["session"] == {"turn": 1, "requests_remaining": 0, "last_turn": True}
        assert "read_file" not in {tool["function"]["name"] for tool in options["tools"]}
        return receipt(call("read_file", {"path": "consumer.py", "start": 1, "end": 1}))

    model = SessionModel(handler=answer)
    ledger = HypothesisLedger("run", "head", "digest", protocol_version=5)
    await HypothesisGenerator(model, executor=source_executor(executed), session_request_limit=1).run(
        review_state, ContextPack(), changes, ledger
    )
    assert len(model.calls) == 1 and not executed and not ledger.no_issue_units
    assert ledger.unresolved_units


@pytest.mark.asyncio
async def test_discovery_preserves_a_useful_terminal_request_under_a_tight_physical_budget():
    from reviewforge.engine.request_budget import BudgetedChatLLM, RequestBudget

    review_state, changes, _, _ = fixture(1)
    executed = []

    def answer(messages, options, turn):
        assert packet(messages)["session"]["last_turn"]
        return receipt(call("submit_no_issue", clean(packet(messages)["pending_units"][0])))

    model = SessionModel(handler=answer)
    budget = RequestBudget(6000, editor_reserve=0)
    bounded = BudgetedChatLLM(model, budget, phase="discovery")
    ledger = HypothesisLedger("run", "head", "digest", protocol_version=5)
    await HypothesisGenerator(bounded, executor=source_executor(executed)).run(
        review_state, ContextPack(), changes, ledger
    )
    assert len(model.calls) == 1 and len(ledger.no_issue_units) == 1 and not executed
    assert budget.reported == 120 and not budget.pending and budget.denials == 0


@pytest.mark.asyncio
async def test_immutable_real_read_only_receipts_do_not_execute_a_read_after_the_announced_deadline():
    saved = json.loads((Path(__file__).parent / "fixtures/v5_discovery_read_only_receipts.json").read_text())
    review_state, changes, _, _ = fixture(1)
    review_state.head_sha = "head"
    executed = []

    def answer(messages, options, turn):
        response = saved["responses"][turn - 1]["response"]
        if turn == 3:
            assert packet(messages)["session"]["last_turn"]
        return AIMessage(
            content=response["content"],
            tool_calls=response["tool_calls"],
            additional_kwargs={"tool_calls": response["raw_tool_calls"]},
            response_metadata={"finish_reason": response["finish_reason"]},
        )

    model = SessionModel(handler=answer)
    ledger = HypothesisLedger("run", "head", "digest", protocol_version=5)
    await HypothesisGenerator(model, executor=source_executor(executed)).run(
        review_state, ContextPack(), changes, ledger
    )
    record = next(iter(ledger.discovery_blocks.values()))
    assert len(model.calls) == 3 and len(record["observations"]) == 4
    assert not ledger.items and not ledger.no_issue_units and ledger.unresolved_units
    assert any(event.get("errors") for event in record["session"]["events"])


@pytest.mark.asyncio
async def test_case_result_has_no_empty_proof_fields_and_can_refute_original_case():
    item = candidate()
    calls = []

    def answer(messages, options, turn):
        data = packet(messages)
        return receipt(
            call(
                "submit_refuted",
                {
                    "case_id": data["case_id"],
                    "reason": "Optional access guards this case.",
                    "premise": "mechanism",
                    "statement": "Optional access prevents dereferencing an absent calendar.",
                    "evidence": ["obs_1:e1"],
                },
            )
        )

    model = SessionModel(handler=answer)
    result = await Investigator(model, source_executor(calls), protocol_version=5).investigate(
        item, state(), ContextPack()
    )
    assert result.verdict == "refuted" and len(model.calls) == 1
    assert result.counterevidence is not None and result.assessment is None
    assert any(row["phase"] == "session-case-binding" for row in result.progress)


@pytest.mark.asyncio
async def test_missing_evidence_can_be_read_instead_of_becoming_a_terminal_unknown():
    executed = []
    item = candidate()

    def answer(messages, options, turn):
        data = packet(messages)
        if turn == 1:
            return receipt(call("read_file", {"path": "caller.ts", "start": "1", "end": "3"}))
        assert len(data["observations"]) == 3
        return receipt(
            call(
                "submit_refuted",
                {
                    "case_id": data["case_id"],
                    "reason": "Caller rejects missing input.",
                    "premise": "trigger",
                    "statement": "The actual caller guards missing calendars.",
                    "evidence": ["obs_2:e1"],
                },
            )
        )

    result = await Investigator(
        SessionModel(handler=answer), source_executor(executed), protocol_version=5
    ).investigate(
        item,
        state(),
        ContextPack(),
    )
    assert result.verdict == "refuted" and len(executed) == 3
    assert any(row.get("event") == "needs-evidence" for row in result.progress)


@pytest.mark.asyncio
async def test_changed_case_id_and_textual_confirmation_never_become_a_verdict():
    def answer(messages, options, turn):
        if turn == 1:
            return receipt(
                call(
                    "submit_refuted",
                    {
                        "case_id": "case_wrong",
                        "reason": "Source refutes it.",
                        "premise": "mechanism",
                        "statement": "Guard prevents it.",
                        "evidence": ["obs_1:e1"],
                    },
                )
            )
        return receipt(
            call(
                "submit_unknown",
                {
                    "case_id": packet(messages)["case_id"],
                    "reason": "CONFIRMED according to prose only.",
                    "missing_fact": "The relevant consumer contract is unavailable.",
                },
            )
        )

    result = await Investigator(SessionModel(handler=answer), source_executor([]), protocol_version=5).investigate(
        candidate(),
        state(),
        ContextPack(),
    )
    assert result.verdict == "unknown" and result.assessment is None and result.counterevidence is None
    assert result.reason == "The relevant consumer contract is unavailable."


def test_case_schemas_are_disjoint_and_bound_to_the_exact_input():
    item = candidate()
    tools = decision_tools(item, "head")
    assert [tool.name for tool in tools] == ["submit_confirmed", "submit_refuted", "submit_unknown"]
    schema = tools[-1].args_schema
    value = {"case_id": case_identity(item, "head"), "reason": "Missing contract.", "missing_fact": "Read the caller."}
    schema.model_validate(value)
    with pytest.raises(ValidationError):
        schema.model_validate(value | {"expected_evidence": []})
    changed = copy.deepcopy(item)
    changed.trigger = "the opposite input"
    assert case_identity(changed, "head") != value["case_id"]


@pytest.mark.asyncio
async def test_v5_never_bootstraps_or_adjudicates_an_unbound_structural_change():
    from test_structural_discovery_v3 import rename

    item = candidate()
    item.change_anchor = rename()
    executed = []
    model = SessionModel(handler=lambda *args: receipt())
    result = await Investigator(model, source_executor(executed), protocol_version=5).investigate(
        item,
        state(),
        ContextPack(),
    )
    assert result.outcome == "tool-error" and result.steps == 0
    assert not executed and not model.calls


def test_evidence_and_session_restore_do_not_accept_wrong_scope_or_reset_counters():
    ledger = HypothesisLedger("run", "head", "digest", protocol_version=5)
    store = EvidenceStore(ledger)
    identity = store.put({"kind": "test-source", "excerpt": "source bytes"})
    assert store.put({"kind": "test-source", "excerpt": "source bytes"}) == identity
    ledger.evidence_records[identity]["head_sha"] = "other-head"
    with pytest.raises(ValueError, match="restored-evidence"):
        EvidenceStore(ledger)
    state_record = {}
    session = ReviewSession(state_record, task_id="work", phase="discovery", max_turns=1)
    assert session.dispatch()
    assert not ReviewSession(state_record, task_id="work", phase="discovery", max_turns=9).dispatch()
    with pytest.raises(ValueError, match="binding"):
        ReviewSession(state_record, task_id="another", phase="discovery", max_turns=1)


def test_wire_receipts_observe_real_http_tools_without_headers_messages_or_response_text(tmp_path):
    audit = WireAudit(str(tmp_path))
    body = {
        "model": "test-model",
        "messages": [{"role": "user", "content": "PROMPT_SECRET"}],
        "tools": [{"type": "function", "function": {"name": "submit_unknown", "parameters": {"type": "object"}}}],
        "tool_choice": "required",
    }
    request = httpx.Request(
        "POST",
        "https://example.invalid/v1/chat/completions?key=URL_SECRET",
        headers={"Authorization": "Bearer KEY_SECRET"},
        json=body,
    )
    audit.request(request)
    response = httpx.Response(
        200,
        request=request,
        json={
            "choices": [
                {
                    "finish_reason": "tool_calls",
                    "message": {
                        "content": "RESPONSE_SECRET",
                        "tool_calls": [{"function": {"name": "find_definition"}}],
                    },
                }
            ]
        },
    )
    audit.response(response)
    records = list(tmp_path.glob("*.json"))
    assert len(records) == 2
    saved = "".join(path.read_text() for path in records)
    assert all(secret not in saved for secret in ["KEY_SECRET", "PROMPT_SECRET", "RESPONSE_SECRET", "URL_SECRET"])
    assert "submit_unknown" in saved and "find_definition" in saved


def test_new_default_and_old_explicit_versions_remain_available():
    assert PipelineV4Config().protocol_version == 10 and PipelineV4Config().mode == "legacy"
    for version in range(1, 11):
        assert _pipeline_v4_from_dict({"protocol_version": version}).protocol_version == version


@pytest.mark.asyncio
@pytest.mark.parametrize("index", [0, 1])
async def test_real_saved_protocol_failures_get_feedback_without_executing_wrong_argument_shapes(index):
    rows = json.loads((Path(__file__).parent / "fixtures/v5_protocol_failures.json").read_text("utf-8"))
    saved = rows[index]
    untouched = copy.deepcopy(saved)
    review_state, changes, _, _ = fixture(1)
    executed = []

    def answer(messages, options, turn):
        if turn == 1:
            return AIMessage(
                content=saved["content"],
                tool_calls=saved["tool_calls"],
                additional_kwargs={"reviewforge_raw_tool_calls": saved["raw_tool_calls"]}
                if saved["raw_tool_calls"]
                else {},
                response_metadata={"finish_reason": saved["finish_reason"]},
            )
        assert "controller_receipt" in messages[-1].content
        return receipt(call("submit_no_issue", clean(packet(messages)["pending_units"][0])))

    model = SessionModel(handler=answer)
    ledger = HypothesisLedger("run", "head", "digest", protocol_version=5)
    await HypothesisGenerator(model, executor=source_executor(executed)).run(
        review_state,
        ContextPack(),
        changes,
        ledger,
    )
    assert not executed and len(model.calls) == 2 and len(ledger.no_issue_units) == 1
    assert saved == untouched
