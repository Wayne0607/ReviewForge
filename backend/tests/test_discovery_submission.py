"""Native discovery transport never grants source or clean-coverage permission."""

from __future__ import annotations

import copy
import json

import httpx
import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import Field, ValidationError
from test_discovery_v3 import ScriptedLLM, candidate, changeset, clean, diff, unit
from test_structural_discovery_v3 import Workspace, inputs

from reviewforge.core.state import StateStore
from reviewforge.engine.context_pack import ContextPack
from reviewforge.engine.discovery import DiscoveryLimits, plan_discovery
from reviewforge.engine.discovery_aliases import (
    expand_response_refs,
    source_aliases,
    target_site_choices,
    target_source_choices,
)
from reviewforge.engine.discovery_submission import (
    _record_payload,
    bind_discovery_submission,
    discovery_submission_tools,
    parse_discovery_submission,
)
from reviewforge.engine.hypothesis import HypothesisLedger
from reviewforge.engine.hypothesis_generator import (
    HypothesisGenerationResult,
    HypothesisGenerator,
    _discovery_output_floor,
    _discovery_request_plan,
    _right_lines_by_path,
)
from reviewforge.engine.openai_compat import ReasoningContentChatOpenAI
from reviewforge.engine.provider_retry import RetryChatLLM, RetryPolicy
from reviewforge.engine.request_budget import BudgetedChatLLM, RequestBudget


def fixture(count=2):
    units = [unit(f"file{index}.py") for index in range(count)]
    state = StateStore(file_diffs={item.path: diff() for item in units})
    changes = changeset(*units)
    block = plan_discovery(changes, ContextPack(), state.file_diffs, limits=DiscoveryLimits())[0]
    return state, changes, block, source_aliases(block.source_refs)


def assessment(block, aliases, item=None):
    item = item or block.units[0]
    left, right = target_source_choices(block, item.id, aliases)
    return {
        "unit_id": item.id,
        "checked": "Compared the changed return operation with its delivered before behavior.",
        "before": "The operation returned the stored old value.",
        "after": "The operation returns the replacement value.",
        "observable": "The caller receives the replacement value for the same input.",
        "source_refs": [*left[:1], *right[:1]],
        **({"change_fields": []} if block.structural_changes else {}),
    }


def hypothesis(block, aliases, item=None):
    raw = assessment(block, aliases, item)
    del raw["checked"]
    site = target_site_choices(block, raw["unit_id"], aliases)[0]
    return raw | {
        "mechanism": "contract-mismatch",
        "severity": "warning",
        "claim": "The changed return may disagree with the stored-value consumer contract.",
        "trigger": "The caller requests its stored record value.",
        "impact": "The returned value can differ from that record.",
        "open_question": "Does the real consumer require the stored value?",
        "refutation": "The consumer explicitly supports replacement values.",
        "discrepancy": "The changed return differs from the prior stored-value operation.",
        "source_roles": ["behavior"] * len(raw["source_refs"]) + ["trigger"],
        "source_refs": [*raw["source_refs"], site],
        "site_refs": [site],
    }


def call(name, args, identity="record"):
    return {"name": name, "args": args, "id": identity, "type": "tool_call"}


async def run_response(response, *, protocol=4, count=2):
    state, changes, block, aliases = fixture(count)
    llm = ScriptedLLM(responses=[response])
    ledger = HypothesisLedger("run", "abc", "digest", protocol_version=protocol)
    result = await HypothesisGenerator(llm).run(state, ContextPack(), changes, ledger)
    return result, ledger, llm, block, aliases


async def consume_native_response(response, *, count=2):
    """Exercise the multi-call parser independently of production wire routing.

    Production uses native tools only for a single target. The collector itself
    still validates multiple calls/targets, complete prefixes and global errors.
    The scripted request here is solely a parser fixture, not a routed GEN pass.
    """
    state, _, block, aliases = fixture(count)
    llm = ScriptedLLM(responses=[response])
    tools = discovery_submission_tools(block, aliases)
    receipt = await bind_discovery_submission(llm, tools).ainvoke([HumanMessage(content="Native collector fixture")])
    framed, errors = parse_discovery_submission(receipt, tools, block, aliases)
    ledger = HypothesisLedger("run", "abc", "digest", protocol_version=4)
    record = {
        "units": {},
        "submission_errors": errors,
        "output_framing": {"complete": framed.complete, "recovery": framed.recovery},
    }
    ledger.discovery_blocks[f"generator:{block.id}"] = record
    result = HypothesisGenerationResult()
    await HypothesisGenerator(llm)._consume_discovery(
        expand_response_refs(framed.payload, aliases), block, ledger, result, _right_lines_by_path(state), 12, record
    )
    return result, ledger, llm, block, aliases


def test_flat_schemas_require_all_fields_and_keep_provider_syntax_separate_from_membership():
    _, _, block, aliases = fixture()
    saved = copy.deepcopy(block)
    tools = discovery_submission_tools(block, aliases)
    assert [tool.name for tool in tools] == ["submit_hypothesis", "submit_no_issue", "defer_unit"]
    for tool in tools:
        parameters = tool.args_schema.model_json_schema()
        assert parameters["additionalProperties"] is False
        assert "change_fields" not in parameters["properties"]
        assert set(parameters["required"]) == set(parameters["properties"])
        assert all(field["type"] in {"string", "array"} for field in parameters["properties"].values())
        assert all(
            field["items"]["type"] == "string"
            for field in parameters["properties"].values()
            if field["type"] == "array"
        )
        assert all(blocked not in json.dumps(parameters) for blocked in ('"null"', '"anyOf"', '"$ref"'))
    schema = tools[0].args_schema
    raw = hypothesis(block, aliases)
    schema.model_validate(raw)
    for invalid in [raw | {"refutation": "x" * 601}, {key: value for key, value in raw.items() if key != "refutation"}]:
        with pytest.raises(ValidationError):
            schema.model_validate(invalid)
    foreign = raw | {"unit_id": "outside-block"}
    with pytest.raises(ValidationError):
        schema.model_validate(foreign)
    # Pattern acceptance is deliberately not block membership permission.
    unknown = raw | {"source_refs": ["r999", *raw["source_refs"][1:]]}
    schema.model_validate(unknown)
    framed, errors = parse_discovery_submission(
        AIMessage(content="", tool_calls=[call("submit_hypothesis", unknown)]), tools, block, aliases
    )
    assert errors and not framed.complete and not framed.payload["hypotheses"]
    assert block == saved
    assert set(tools[-1].args_schema.model_fields) == {"unit_id", "reason"}
    assert "refutation" not in tools[1].args_schema.model_fields


@pytest.mark.parametrize("name", ["submit_hypothesis", "submit_no_issue"])
@pytest.mark.parametrize("old_fields", [[], ["path"], ["previous_path", "path"]])
def test_text_records_reject_old_or_invented_structural_fields_instead_of_ignoring_them(name, old_fields):
    _, _, block, aliases = fixture(1)
    args = (hypothesis if name == "submit_hypothesis" else assessment)(block, aliases)
    tools = discovery_submission_tools(block, aliases)
    schema = next(tool.args_schema for tool in tools if tool.name == name)
    schema.model_validate(args)
    args["change_fields"] = old_fields
    with pytest.raises(ValidationError):
        schema.model_validate(args)
    framed, errors = parse_discovery_submission(
        AIMessage(content="", tool_calls=[call(name, args)]), tools, block, aliases
    )
    assert errors and not framed.complete and not any(framed.payload.values())
    with pytest.raises(ValueError):
        _record_payload(name, args, block, aliases)


def test_mixed_block_still_requires_explicit_empty_structural_selection_for_text_target():
    state, changes, pack, _ = inputs(workspace=Workspace(updated=True), protocol=4)
    text_unit = unit("service.py")
    changes.units.append(text_unit)
    state.file_diffs[text_unit.path] = diff()
    block = plan_discovery(changes, pack, state.file_diffs, limits=DiscoveryLimits(), file_changes=state.file_changes)[
        0
    ]
    assert block.structural_changes and text_unit in block.units
    aliases = source_aliases(block.source_refs)
    tools = discovery_submission_tools(block, aliases)
    args = assessment(block, aliases, text_unit)
    assert args["change_fields"] == []
    framed, errors = parse_discovery_submission(
        AIMessage(content="", tool_calls=[call("submit_no_issue", args)]), tools, block, aliases
    )
    assert framed.complete and not errors and len(framed.payload["no_issue_units"]) == 1
    for invalid in (
        args | {"change_fields": ["path"]},
        {key: value for key, value in args.items() if key != "change_fields"},
    ):
        framed, errors = parse_discovery_submission(
            AIMessage(content="", tool_calls=[call("submit_no_issue", invalid)]), tools, block, aliases
        )
        assert errors and not framed.complete and not framed.payload["no_issue_units"]


def test_provider_schema_does_not_repeat_large_source_alias_inventories():
    _, changes, small, short = fixture(1)
    distinct_segments = "return " + "".join(f"{index:04d}" + "x" * 236 for index in range(130))
    large = plan_discovery(
        changes, ContextPack(), {changes.units[0].path: diff(after=distinct_segments)}, limits=DiscoveryLimits()
    )[0]
    long = source_aliases(large.source_refs)
    assert len(long) > 100 and len(short) == 2
    schemas = [
        json.dumps([tool.args_schema.model_json_schema() for tool in discovery_submission_tools(block, aliases)])
        for block, aliases in ((small, short), (large, long))
    ]
    assert abs(len(schemas[1]) - len(schemas[0])) < 30
    assert '"pattern": "^r[0-9]+$"' in schemas[1]
    assert not any(f'"{alias}"' in schemas[1] for alias in long)


@pytest.mark.parametrize("raw", [None, [], {}, "", "encoded"])
def test_empty_raw_lists_and_none_allow_text_but_malformed_raw_never_falls_back(raw):
    _, _, block, aliases = fixture(1)
    response = AIMessage(
        content='{"hypotheses":[],"no_issue_units":[]}', additional_kwargs={"reviewforge_raw_tool_calls": raw}
    )
    framed, errors = parse_discovery_submission(response, discovery_submission_tools(block, aliases), block, aliases)
    if raw is None or raw == []:
        assert framed is None and not errors
    else:
        assert framed is not None and not framed.complete and errors


def test_sites_exclude_short_braces_context_before_and_other_unit_locations():
    from reviewforge.engine.discovery import DiscoveryBlock, SourceLine

    target = [
        SourceLine("after", "shared.py", 3, "return changed_value", changed=True),
        SourceLine("after", "shared.py", 4, "        }", changed=True),
    ]
    before = SourceLine("before", "shared.py", 3, "return previous_value", changed=True)
    context = SourceLine("context", "shared.py", 3, target[0].text)
    sibling = SourceLine("after", "shared.py", 20, "return neighbor_value", changed=True)
    block = DiscoveryBlock("block", [], "", "", "", "", "")
    block.facts = [*target, before, context, sibling]
    block.unit_facts = {"target": target, "sibling": [sibling]}
    block.register_source_refs()
    aliases = source_aliases(block.source_refs)
    choices = target_site_choices(block, "target", aliases)
    assert len(choices) == 1
    fact = block.resolve_source_fact(aliases[choices[0]])
    assert fact["side"] == "after" and fact["line"] == 3 and fact["excerpt"] == target[0].text


@pytest.mark.asyncio
async def test_multiple_same_name_calls_and_deferred_keep_distinct_candidates_without_fake_clean():
    _, _, block, aliases = fixture()
    first = hypothesis(block, aliases)
    second = first | {
        "claim": "A distinct caller may use the return during recovery.",
        "trigger": "Recovery reads the value.",
    }
    response = AIMessage(
        content="",
        tool_calls=[
            call("submit_hypothesis", first, "first"),
            call("submit_hypothesis", second, "second"),
            call(
                "defer_unit",
                {"unit_id": first["unit_id"], "reason": "The recovery consumer was not delivered."},
                "defer",
            ),
            call("submit_no_issue", assessment(block, aliases, block.units[1]), "clean"),
        ],
    )
    result, ledger, llm, _, _ = await consume_native_response(response)
    assert result.accepted == 2 and len(llm.calls) == 1
    assert all(item.status.value == "open" for item in ledger.items.values())
    assert first["unit_id"] in ledger.unresolved_units
    assert block.units[1].id in ledger.no_issue_units
    assert "violation" not in next(iter(ledger.items.values())).discovery_basis


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        "missing",
        "long",
        "unknown-unit",
        "unknown-ref",
        "before-site",
        "roles",
        "array-string",
        "extra",
        "change",
        "blank",
        "wrong-tool",
    ],
)
async def test_bad_native_record_retains_valid_candidate_and_never_repairs_or_cleans(failure):
    _, _, block, aliases = fixture()
    valid = hypothesis(block, aliases)
    bad = hypothesis(block, aliases, block.units[1])
    name = "submit_hypothesis"
    if failure == "missing":
        del bad["refutation"]
    elif failure == "long":
        bad["claim"] = "x" * 791
    elif failure == "unknown-unit":
        bad["unit_id"] = "overview-only-unit"
    elif failure == "unknown-ref":
        bad["source_refs"][0] = "r999"
    elif failure == "before-site":
        bad["site_refs"] = [target_source_choices(block, block.units[1].id, aliases)[0][0]]
    elif failure == "roles":
        bad["source_roles"] = ["trigger"]
    elif failure == "array-string":
        bad["source_refs"] = json.dumps(bad["source_refs"])
    elif failure == "extra":
        bad["assessment"] = "invented"
    elif failure == "change":
        bad["change_fields"] = ["previous_path", "path"]
    elif failure == "blank":
        bad["refutation"] = " "
    else:
        name = "unknown_submission"
    response = AIMessage(
        content="",
        tool_calls=[
            call("submit_hypothesis", valid, "valid"),
            call(name, bad, "bad"),
            call("submit_no_issue", assessment(block, aliases, block.units[1]), "clean"),
        ],
    )
    result, ledger, llm, _, _ = await consume_native_response(response)
    assert result.accepted == 1 and len(llm.calls) == 1
    assert set(ledger.unresolved_units) == {item.id for item in block.units}
    assert not ledger.no_issue_units
    record = next(iter(ledger.discovery_blocks.values()))
    assert record["submission_errors"] and not record["output_framing"]["complete"]
    assert "repair_plan" not in record


@pytest.mark.asyncio
async def test_raw_truncated_tail_preserves_only_complete_call_and_not_text_fallback():
    _, _, block, aliases = fixture()
    args = hypothesis(block, aliases)
    raw = {"id": "valid", "type": "function", "function": {"name": "submit_hypothesis", "arguments": json.dumps(args)}}
    tail = {"id": "cut", "type": "function", "function": {"name": "submit_no_issue", "arguments": '{"unit_id":'}}
    response = AIMessage(
        content=json.dumps({"hypotheses": [], "no_issue_units": [clean(item) for item in block.units]}),
        tool_calls=[call("submit_hypothesis", args, "valid")],
        invalid_tool_calls=[{"name": "submit_no_issue", "args": '{"unit_id":', "id": "cut", "error": "truncated"}],
        additional_kwargs={"reviewforge_raw_tool_calls": [raw, tail]},
    )
    result, ledger, llm, _, _ = await consume_native_response(response)
    assert result.accepted == 1 and not ledger.no_issue_units and len(llm.calls) == 1
    assert len(ledger.unresolved_units) == len(block.units)


@pytest.mark.asyncio
async def test_length_finish_cannot_make_complete_native_clean_calls_prove_full_coverage():
    _, _, block, aliases = fixture()
    response = AIMessage(
        content="",
        tool_calls=[
            call("submit_no_issue", assessment(block, aliases, item), f"clean-{index}")
            for index, item in enumerate(block.units)
        ],
        response_metadata={"finish_reason": "length"},
    )
    _, ledger, llm, _, _ = await consume_native_response(response)
    assert not ledger.no_issue_units and len(ledger.unresolved_units) == len(block.units)
    assert len(llm.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["complete", "fence", "root-closer", "prefix", "invalid"])
async def test_no_native_response_uses_same_receipt_strict_text_without_new_request(shape):
    _, _, block, _ = fixture(1)
    raw = json.dumps({"hypotheses": [candidate(block.units[0])], "no_issue_units": []})
    if shape == "fence":
        raw = f"```json\n{raw}\n```"
    elif shape == "root-closer":
        raw = raw[:-1]
    elif shape == "prefix":
        raw = raw[:-1] + ',"deferred_units":[{"unit_id":"cut'
    elif shape == "invalid":
        raw = raw.replace('"hypotheses":', '"hypotheses":[],"hypotheses":', 1)
    result, ledger, llm, _, _ = await run_response(raw, count=1)
    assert len(llm.calls) == 1 and result.accepted == (shape != "invalid")
    assert bool(ledger.unresolved_units) == (shape in {"prefix", "invalid"})


@pytest.mark.asyncio
async def test_v3_still_uses_text_and_one_original_format_repair():
    state, changes, block, _ = fixture(1)
    llm = ScriptedLLM(
        responses=["malformed", json.dumps({"hypotheses": [candidate(block.units[0])], "no_issue_units": []})]
    )
    ledger = HypothesisLedger("run", "abc", "digest", protocol_version=3)
    result = await HypothesisGenerator(llm).run(state, ContextPack(), changes, ledger)
    assert result.accepted == 1 and len(llm.calls) == 2 and not llm.bindings


@pytest.mark.asyncio
@pytest.mark.parametrize("selection", [None, [], ["path"], ["path", "path"], ["previous_path", "path"]])
async def test_structural_records_require_explicit_fields_and_real_consumer(selection):
    state, changes, pack, item = inputs(workspace=Workspace(updated=True), protocol=4)
    block = plan_discovery(changes, pack, state.file_diffs, limits=DiscoveryLimits(), file_changes=state.file_changes)[
        0
    ]
    aliases = source_aliases(block.source_refs)
    consumer = next(alias for alias, ref in aliases.items() if block.source_refs[ref].kind == "reference")
    args = {
        "unit_id": item.id,
        "checked": "Compared the consumer literal lookup against both verified paths.",
        "before": "The resource used the previous filename.",
        "after": "The same resource bytes use the new filename.",
        "observable": "The delivered consumer uses the new filename.",
        "source_refs": [consumer],
        "change_fields": selection,
    }
    if selection is None:
        args.pop("change_fields")
        with pytest.raises(ValueError):
            _record_payload("submit_no_issue", args, block, aliases)
    for tool in discovery_submission_tools(block, aliases)[:-1]:
        schema = tool.args_schema.model_json_schema()
        assert "change_fields" in schema["required"]
        assert schema["properties"]["change_fields"]["maxItems"] == 2
    llm = ScriptedLLM(responses=[AIMessage(content="", tool_calls=[call("submit_no_issue", args)])])
    ledger = HypothesisLedger("run", state.head_sha, "digest", protocol_version=4)
    await HypothesisGenerator(llm).run(state, pack, changes, ledger)
    valid = selection == ["previous_path", "path"]
    assert bool(ledger.no_issue_units) == valid
    assert bool(ledger.unresolved_units) != valid
    if valid:
        saved = next(iter(ledger.discovery_blocks.values()))["units"][item.id]["behavior"]["change_facts"]
        assert {fact["field"] for fact in saved} == {"previous_path", "path"}
        assert all(fact["change_id"] == state.file_changes[item.path].id for fact in saved)


class NativeModel(BaseChatModel):
    calls: list = Field(default_factory=list)

    def bind_tools(self, tools, **kwargs):
        return self.bind(tools=[convert_to_openai_tool(tool) for tool in tools], **kwargs)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls.append((messages, kwargs))
        rows = messages[-1].content.split("## Units\n", 1)[1].split("\n\n", 1)[0]
        calls = []
        for line in rows.splitlines():
            if not line.startswith('["'):
                continue
            row = json.loads(line)
            args = {
                "unit_id": row[0],
                "checked": "Checked the delivered changed return operation.",
                "before": "The operation returned the old value.",
                "after": "The operation returns the new value.",
                "observable": "The caller sees the new value.",
                "source_refs": [*row[8][:1], *row[9][:1]],
            }
            calls.append(call("submit_no_issue", args, f"record-{len(calls)}"))
        content = ""
        if not kwargs.get("tools"):
            content = json.dumps(
                {
                    "hypotheses": [],
                    "no_issue_units": [
                        {
                            "unit_id": entry["args"]["unit_id"],
                            "checked": entry["args"]["checked"],
                            "behavior": {
                                **{field: entry["args"][field] for field in ("before", "after", "observable")},
                                "source_facts": [{"source_ref": ref} for ref in entry["args"]["source_refs"]],
                            },
                        }
                        for entry in calls
                    ],
                }
            )
            calls = []
        return ChatResult(
            generations=[
                ChatGeneration(
                    message=AIMessage(
                        content=content,
                        tool_calls=calls,
                        usage_metadata={"input_tokens": 99, "output_tokens": 1, "total_tokens": 100},
                    )
                )
            ]
        )

    @property
    def _llm_type(self):
        return "offline-native-discovery"


@pytest.mark.asyncio
async def test_final_batch_request_drives_budget_split_without_lowering_floor_or_call_limit():
    state, changes, block, aliases = fixture(4)
    ledger = HypothesisLedger("run", "abc", "digest", protocol_version=4)
    model = NativeModel()
    generator = HypothesisGenerator(model, discovery_max_hypotheses_per_block=12)
    from langchain_core.messages import HumanMessage, SystemMessage

    messages = [
        SystemMessage(content=generator._discovery_system_prompt(12, protocol_version=4, unit_count=len(block.units))),
        HumanMessage(content=generator._render_discovery_message(block, ledger, 12)),
    ]
    plan = _discovery_request_plan(model, messages, ledger)
    floor = _discovery_output_floor(4, 12, protocol_version=4)
    budget = RequestBudget(250_000, discovery_limit=plan.input_tokens + floor - 1)
    result = await HypothesisGenerator(
        BudgetedChatLLM(model, budget, phase="discovery"), discovery_max_blocks=2, discovery_max_hypotheses_per_block=12
    ).run(state, ContextPack(), changes, ledger)
    parent = ledger.discovery_blocks[f"generator:{block.id}"]
    assert parent["status"] == "partitioned" and not parent.get("request_attempted")
    assert parent["request_plan"]["input_tokens"] == plan.input_tokens
    assert parent["request_plan"]["useful_output_floor"] == floor
    assert 0 < len(model.calls) == result.blocks <= 2
    assert all("tools" not in options for _, options in model.calls)
    assert set(ledger.no_issue_units) | set(ledger.unresolved_units) == {item.id for item in changes.units}
    assert budget.discovery_limit == plan.input_tokens + floor - 1 and budget.limit == 250_000


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response_kind", ["complete", "text", "400", "truncated", "duplicate", "invented-path", "old-empty-change"]
)
async def test_real_adapter_serializes_tools_and_budget_includes_schema_without_400_fallback(response_kind):
    state, changes, block, aliases = fixture(1)
    requests, snapshots = [], []
    args = hypothesis(block, aliases)
    if response_kind in {"invented-path", "old-empty-change"}:
        args["change_fields"] = ["path"] if response_kind == "invented-path" else []

    def handle(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if response_kind == "400":
            return httpx.Response(
                400, json={"error": {"message": "unsupported tool_choice", "type": "invalid_request_error"}}
            )
        calls = [
            {
                "id": "record",
                "type": "function",
                "function": {"name": "submit_hypothesis", "arguments": json.dumps(args)},
            }
        ]
        if response_kind in {"truncated", "duplicate"}:
            tail = (
                '{"unit_id":'
                if response_kind == "truncated"
                else '{"unit_id":"foreign",' + json.dumps(assessment(block, aliases))[1:]
            )
            calls.append({"id": "tail", "type": "function", "function": {"name": "submit_no_issue", "arguments": tail}})
        text = (
            json.dumps({"hypotheses": [candidate(block.units[0])], "no_issue_units": []})
            if response_kind == "text"
            else None
        )
        return httpx.Response(
            200,
            json={
                "id": "offline",
                "object": "chat.completion",
                "created": 0,
                "model": "fixture",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": text,
                            "tool_calls": None if response_kind == "text" else calls,
                        },
                    }
                ],
                "usage": {"prompt_tokens": 120, "completion_tokens": 80, "total_tokens": 200},
            },
        )

    async def changed(snapshot):
        snapshots.append(snapshot)

    transport = httpx.MockTransport(handle)
    with httpx.Client(transport=transport) as sync_client:
        async with httpx.AsyncClient(transport=transport) as async_client:
            model = ReasoningContentChatOpenAI(
                model="fixture",
                api_key="offline-only",
                max_retries=0,
                base_url="https://example.invalid/v1",
                http_client=sync_client,
                http_async_client=async_client,
            )
            budget = RequestBudget(250_000, discovery_limit=80_000)
            wrapped = RetryChatLLM(
                BudgetedChatLLM(model, budget, phase="discovery", on_change=changed),
                RetryPolicy(max_attempts=3, base_delay_seconds=0),
            ).bind(extra_body={"enable_thinking": False})
            ledger = HypothesisLedger("run", "abc", "digest", protocol_version=4)
            result = await HypothesisGenerator(wrapped).run(state, ContextPack(), changes, ledger)
    assert len(requests) == 1
    wire = requests[0]
    assert wire["enable_thinking"] is False and wire["tool_choice"] == "required"
    assert len(wire["tools"]) == 3 and "response_format" not in wire
    assert all(tool["function"]["parameters"]["additionalProperties"] is False for tool in wire["tools"])
    assert all("change_fields" not in tool["function"]["parameters"]["properties"] for tool in wire["tools"])
    record = next(iter(ledger.discovery_blocks.values()))
    reservation = next(iter(snapshots[0]["pending"].values()))["reserved_tokens"]
    assert reservation == record["request_plan"]["input_tokens"] + record["request_plan"]["max_output_tokens"]
    assert (
        (wire.get("max_tokens") or wire["max_completion_tokens"]) == record["request_plan"]["max_output_tokens"] <= 8192
    )
    if response_kind == "400":
        assert result.accepted == 0 and ledger.unresolved_units and budget.snapshot()["unreported_reserved_tokens"] > 0
    elif response_kind in {"complete", "text"}:
        assert result.accepted == 1 and not ledger.unresolved_units and budget.snapshot()["reported_tokens"] == 200
    elif response_kind in {"invented-path", "old-empty-change"}:
        assert result.accepted == 0 and ledger.unresolved_units and not ledger.no_issue_units
        assert record["submission_errors"] and not record["output_framing"]["complete"]
        assert budget.snapshot()["reported_tokens"] == 200
    else:
        assert result.accepted == 1 and ledger.unresolved_units and not ledger.no_issue_units
        assert record["submission_errors"] and not record["output_framing"]["complete"]
        assert budget.snapshot()["reported_tokens"] == 200
