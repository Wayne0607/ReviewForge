"""Physical planning and source-preserving partitions, without provider traffic."""

from __future__ import annotations

import json

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field
from test_discovery_v3 import candidate, changeset, clean, diff, unit

from reviewforge.core.state import StateStore
from reviewforge.engine.context_pack import ContextPack, ContextSlice, UnitContext
from reviewforge.engine.discovery import DiscoveryLimits, plan_discovery, split_discovery
from reviewforge.engine.hypothesis import HypothesisLedger
from reviewforge.engine.hypothesis_generator import HypothesisGenerator, _discovery_available, _discovery_output_floor
from reviewforge.engine.provider_retry import RetryChatLLM, RetryPolicy
from reviewforge.engine.request_budget import BudgetedChatLLM, RequestBudget, estimate_request
from reviewforge.engine.token_tracker import RunContext, TrackedChatLLM


class CoverageModel(BaseChatModel):
    calls: list = Field(default_factory=list)
    units: dict = Field(default_factory=dict)
    usage: int = 100
    fail: bool = False
    raw: str = ""
    candidate_mode: bool = False

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls.append((messages, kwargs))
        if self.fail:
            raise TimeoutError("offline transport failure")
        if self.raw:
            body = self.raw
        else:
            table = messages[-1].content.split("## Units\n", 1)[1].split("\n\n", 1)[0]
            selected = [self.units[json.loads(row)[0]] for row in table.splitlines() if row.startswith("[")]
            emit_candidate = (
                self.candidate_mode and "New candidate allowance this block: 0." not in messages[-1].content
            )
            body = json.dumps(
                {
                    "hypotheses": [candidate(selected[0])] if emit_candidate else [],
                    "no_issue_units": [clean(u) for u in selected[int(emit_candidate) :]],
                }
            )
        message = AIMessage(
            content=body,
            usage_metadata={"input_tokens": self.usage - 1, "output_tokens": 1, "total_tokens": self.usage},
        )
        return ChatResult(generations=[ChatGeneration(message=message)])

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        return self._generate(messages, stop, run_manager, **kwargs)

    @property
    def _llm_type(self):
        return "offline-discovery-budget"


def fixture(count=1, after_size=0, common_rule=""):
    units = [unit(f"file{i}.py") for i in range(count)]
    state = StateStore(file_diffs={u.path: diff(after="return new_value" + " # " + "x" * after_size) for u in units})
    responsibilities = {u.id: ["domain"] for u in units} if common_rule else {}
    kwargs = {"unit_responsibilities": responsibilities, "responsibility_rules": {"domain": common_rule}}
    return units, state, kwargs


def final_messages(generator, units, state, ledger, pack=None):
    pack = pack or ContextPack()
    block = plan_discovery(changeset(*units), pack, state.file_diffs, limits=generator._discovery_limits)[0]
    rules = list(dict.fromkeys(name for u in units for name in generator._unit_responsibilities.get(u.id, [])))
    allowance = min(generator._discovery_max_hypotheses_per_block, generator._max_hypotheses)
    user = generator._render_discovery_message(
        block,
        ledger,
        allowance,
        generator._verification_guidance(units, state.file_diffs, pack, delivered_rules=set(rules)),
    )
    return block, [
        SystemMessage(content=generator._discovery_system_prompt(allowance, rules)),
        HumanMessage(content=user),
    ]


def test_floor_varies_with_units_and_allowed_candidates_and_preserves_nontrivial_output():
    assert 512 < _discovery_output_floor(1, 1) < _discovery_output_floor(9, 6) <= 8192
    assert _discovery_output_floor(9, 0) < _discovery_output_floor(9, 6)
    assert _discovery_output_floor(12, 6) <= 8192


def test_fallback_accounts_for_stage_pr_pending_and_unknown_receipts_without_mutation():
    ledger = HypothesisLedger("run", "abc", "digest", protocol_version=3)
    saved = {
        "limit": 30_000,
        "discovery_limit": 20_000,
        "editor_reserve": 5_000,
        "reported_tokens": 10_000,
        "unreported_reserved_tokens": 2_000,
        "charged_by_phase": {"discovery": 11_000},
        "pending": {"request1": {"phase": "discovery", "reserved_tokens": 3_000}},
    }
    ledger.request_budget = saved
    assert _discovery_available(ledger) == 6_000 and ledger.request_budget == saved


@pytest.mark.asyncio
async def test_final_messages_and_bound_provider_options_determine_dynamic_cap():
    units, state, kwargs = fixture(common_rule="actual rule " * 80)
    ledger = HypothesisLedger("run", "abc", "digest", protocol_version=3)
    model = CoverageModel(units={u.id: u for u in units})
    options = {"extra_body": {"thinking": {"type": "disabled"}}, "response_format": {"type": "json_object"}}
    plain = HypothesisGenerator(model, **kwargs)
    _, messages = final_messages(plain, units, state, ledger)
    expected_input = estimate_request(messages, options | {"max_tokens": 8192})
    budget = RequestBudget(250_000, discovery_limit=expected_input + 1_400)

    async def changed(snapshot):
        ledger.request_budget = snapshot

    wrapped = TrackedChatLLM(
        BudgetedChatLLM(model, budget, phase="discovery", on_change=changed),
        RunContext(),
        "generator",
        request_options=options,
    )
    generator = HypothesisGenerator(wrapped, **kwargs)
    result = await generator.run(state, ContextPack(), changeset(*units), ledger)
    record = next(iter(ledger.discovery_blocks.values()))
    plan = record["request_plan"]
    assert plan["input_tokens"] == expected_input == estimate_request(model.calls[0][0], model.calls[0][1])
    assert plan["available_tokens"] == expected_input + 1400
    assert plan["useful_output_floor"] == _discovery_output_floor(1, 6)
    assert model.calls[0][1]["max_tokens"] == plan["max_output_tokens"] == 1400
    assert model.calls[0][1]["extra_body"] == options["extra_body"]
    assert result.blocks == 1 and not ledger.unresolved_units and budget.snapshot()["denied_requests"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("pending_tokens", [0, 30_000])
async def test_live_config_and_pending_capacity_take_precedence_over_empty_ledger_defaults(pending_tokens):
    units, state, _ = fixture()
    ledger = HypothesisLedger("run", "abc", "digest", protocol_version=3)
    model = CoverageModel(units={u.id: u for u in units})
    budget = RequestBudget(250_000, discovery_limit=120_000)
    if pending_tokens:
        budget.reserve("discovery", pending_tokens)
    wrapped = BudgetedChatLLM(model, budget, phase="discovery").bind(extra_body={"opaque_test_option": "x" * 230_000})
    result = await HypothesisGenerator(wrapped).run(state, ContextPack(), changeset(*units), ledger)
    plan = next(iter(ledger.discovery_blocks.values()))["request_plan"]
    assert result.blocks == 1 and len(model.calls) == 1
    assert plan["available_tokens"] == 120_000 - pending_tokens
    assert plan["max_output_tokens"] == min(8192, plan["available_tokens"] - plan["input_tokens"])
    assert plan["max_output_tokens"] == model.calls[0][1]["max_tokens"]
    assert budget.snapshot()["denied_requests"] == 0


@pytest.mark.asyncio
async def test_9_unit_tail_with_input_above_remaining_budget_has_no_full_block_dispatch():
    units, state, kwargs = fixture(9, after_size=900, common_rule="common domain rule " * 240)
    ledger = HypothesisLedger("run", "abc", "digest", protocol_version=3)
    model = CoverageModel(units={u.id: u for u in units})
    probe = HypothesisGenerator(model, **kwargs)
    parent, messages = final_messages(probe, units, state, ledger)
    whole_input = estimate_request(messages, {"max_tokens": 8192})
    available = whole_input - 100
    budget = RequestBudget(250_000, discovery_limit=available)
    generator = HypothesisGenerator(BudgetedChatLLM(model, budget, phase="discovery"), **kwargs)
    result = await generator.run(state, ContextPack(), changeset(*units), ledger)
    record = ledger.discovery_blocks[f"generator:{parent.id}"]
    assert record["status"] == "partitioned" and "request_attempted" not in record
    assert record["request_plan"]["input_tokens"] == whole_input > record["request_plan"]["available_tokens"]
    assert record["request_plan"]["max_output_tokens"] == 0
    assert 0 < result.blocks <= 8 and len(model.calls) == result.blocks
    assert all("## Required assessments (9)" not in messages[-1].content for messages, _ in model.calls)
    assert budget.snapshot()["denied_requests"] == 0
    assert budget.snapshot()["reported_tokens"] <= available
    assert set(ledger.no_issue_units) | set(ledger.unresolved_units) == {u.id for u in units}


def test_partition_preserves_complete_hunks_actual_context_and_cross_file_navigation():
    units, state, _ = fixture(4)
    shared = ContextSlice(
        "caller", "caller.py", 7, 9, "read", "first();\nsecond();\nthird();", "cross-file caller", "abc"
    )
    pack = ContextPack(units={u.id: UnitContext(u.id, [shared]) for u in units})
    limits = DiscoveryLimits()
    parent = plan_discovery(changeset(*units), pack, state.file_diffs, limits=limits)[0]
    children = split_discovery(parent, limits)
    assert len(children) == 2 and sorted(u.id for child in children for u in child.units) == sorted(u.id for u in units)
    for child in children:
        assert child.overview == parent.overview and child.navigation == parent.navigation
        assert child.context_text.count("second();") == 1
        for u in child.units:
            original = {(f.side, f.path, f.line, f.text, f.kind) for f in parent.facts if f.unit_id == u.id}
            actual = {(f.side, f.path, f.line, f.text, f.kind) for f in child.facts if f.unit_id == u.id}
            assert original <= actual
            assert child.diff_text.count(f"### Shared before/after diff: {u.path}") == 1
            assert "return old_value" in child.diff_text and "return new_value" in child.diff_text
        assert parent.resolve_source_fact(next(iter(child.source_refs))) is None


def test_partition_preserves_delivered_blank_context_and_paths_with_spaces():
    units = [unit("first file.py"), unit("second file.py")]
    slice_ = ContextSlice(
        "caller",
        "folder with spaces/caller.py",
        7,
        8,
        "read",
        "supported_call(argument);\n\n",
        "cross-file caller",
        "abc",
    )
    pack = ContextPack(units={u.id: UnitContext(u.id, [slice_]) for u in units})
    limits = DiscoveryLimits()
    parent = plan_discovery(changeset(*units), pack, {u.path: diff() for u in units}, limits=limits)[0]
    children = split_discovery(parent, limits)
    assert len(children) == 2
    for block in [parent, *children]:
        assert all(ref_id in block.diff_text or ref_id in block.context_text for ref_id in block.source_refs)
        assert any(f.kind == "caller" and f.line == 8 and f.text == "" for f in block.facts)
        assert all(block.source_matches(block.resolve_source_fact(ref_id)) for ref_id in block.source_refs)


@pytest.mark.asyncio
async def test_indivisible_shared_hunk_recurses_only_to_single_units_and_stays_unresolved():
    units = [unit("shared.py", f"method{i}") for i in range(4)]
    patch = diff(after="return new_value # " + "x" * 15_000)
    state, ledger = (
        StateStore(file_diffs={units[0].path: patch}),
        HypothesisLedger("run", "abc", "digest", protocol_version=3),
    )
    model = CoverageModel(units={u.id: u for u in units})
    budget = RequestBudget(250_000, discovery_limit=5_000)
    result = await HypothesisGenerator(BudgetedChatLLM(model, budget, phase="discovery")).run(
        state, ContextPack(), changeset(*units), ledger
    )
    assert result.blocks == 0 and not model.calls and len(ledger.discovery_blocks) == 7
    assert len(ledger.unresolved_units) == 4 and not ledger.no_issue_units
    assert all("budget-exhausted" in reason for reason in ledger.unresolved_units.values())
    assert all(record["diff_chars"] > len(patch) for record in ledger.discovery_blocks.values())
    assert budget.snapshot()["request_count"] == budget.snapshot()["denied_requests"] == 0


@pytest.mark.asyncio
async def test_below_useful_floor_never_sends_a_tiny_schema_lottery():
    units, state, kwargs = fixture()
    ledger, model = (
        HypothesisLedger("run", "abc", "digest", protocol_version=3),
        CoverageModel(units={u.id: u for u in units}),
    )
    probe = HypothesisGenerator(model)
    _, messages = final_messages(probe, units, state, ledger)
    budget = RequestBudget(250_000, discovery_limit=estimate_request(messages, {"max_tokens": 8192}) + 512)
    result = await HypothesisGenerator(BudgetedChatLLM(model, budget, phase="discovery"), **kwargs).run(
        state, ContextPack(), changeset(*units), ledger
    )
    record = next(iter(ledger.discovery_blocks.values()))
    assert result.blocks == 0 and not model.calls and record["request_plan"]["max_output_tokens"] == 512
    assert record["request_plan"]["useful_output_floor"] > 512 and "request_attempted" not in record
    assert units[0].id in ledger.unresolved_units


@pytest.mark.asyncio
async def test_partition_cannot_bypass_original_actual_discovery_call_cap():
    units, state, kwargs = fixture(9, after_size=900, common_rule="common domain rule " * 240)
    ledger, model = (
        HypothesisLedger("run", "abc", "digest", protocol_version=3),
        CoverageModel(units={u.id: u for u in units}),
    )
    _, messages = final_messages(HypothesisGenerator(model, **kwargs), units, state, ledger)
    budget = RequestBudget(250_000, discovery_limit=estimate_request(messages, {"max_tokens": 8192}) - 100)
    generator = HypothesisGenerator(BudgetedChatLLM(model, budget, phase="discovery"), discovery_max_blocks=1, **kwargs)
    result = await generator.run(state, ContextPack(), changeset(*units), ledger)
    assert result.blocks == 1 and len(model.calls) == 1 and ledger.unresolved_units
    assert any("discovery block limit" in reason for reason in ledger.unresolved_units.values())
    await generator.run(state, ContextPack(), changeset(*units), ledger, resume=True)
    assert len(model.calls) == 1


@pytest.mark.asyncio
async def test_retries_keep_selected_generator_output_cap_without_replanning():
    units, state, kwargs = fixture()
    model = CoverageModel(units={u.id: u for u in units}, fail=True)
    budget = RequestBudget(250_000, discovery_limit=30_000)
    wrapped = RetryChatLLM(
        BudgetedChatLLM(model, budget, phase="discovery"), RetryPolicy(max_attempts=3, base_delay_seconds=0)
    )
    ledger = HypothesisLedger("run", "abc", "digest", protocol_version=3)
    result = await HypothesisGenerator(wrapped, **kwargs).run(state, ContextPack(), changeset(*units), ledger)
    record = next(iter(ledger.discovery_blocks.values()))
    assert result.blocks == 1 and len(model.calls) >= 2
    assert all(kwargs["max_tokens"] == record["request_plan"]["max_output_tokens"] for _, kwargs in model.calls)
    assert all(messages == model.calls[0][0] for messages, _ in model.calls)
    assert budget.snapshot()["unreported_reserved_tokens"] > 0 and units[0].id in ledger.unresolved_units


@pytest.mark.asyncio
async def test_child_allowance_and_floor_use_latest_global_candidate_count():
    units, state, kwargs = fixture(4, after_size=900, common_rule="common domain rule " * 100)
    ledger = HypothesisLedger("run", "abc", "digest", protocol_version=3)
    model = CoverageModel(units={u.id: u for u in units}, candidate_mode=True)
    probe = HypothesisGenerator(model, max_hypotheses=1, **kwargs)
    _, messages = final_messages(probe, units, state, ledger)
    # Refuse the complete parent but admit useful child output. Derive this
    # boundary from the policy floor, not incidental prompt padding bytes.
    budget = RequestBudget(
        250_000,
        discovery_limit=estimate_request(messages, {"max_tokens": 8192}) + _discovery_output_floor(4, 1) - 100,
    )
    result = await HypothesisGenerator(
        BudgetedChatLLM(model, budget, phase="discovery"), max_hypotheses=1, **kwargs
    ).run(state, ContextPack(), changeset(*units), ledger)
    assert result.accepted == 1 and len(ledger.items) == 1 and 2 <= len(model.calls) <= 4
    assert "New candidate allowance this block: 1." in model.calls[0][0][-1].content
    assert "New candidate allowance this block: 0." in model.calls[1][0][-1].content
    assert any(len(row["unit_ids"]) == 4 and row["status"] == "partitioned" for row in ledger.discovery_blocks.values())
    delivered = [row for row in ledger.discovery_blocks.values() if row.get("request_attempted")]
    assert delivered[0]["request_plan"]["useful_output_floor"] == _discovery_output_floor(
        len(delivered[0]["unit_ids"]), 1
    )
    assert all(
        row["request_plan"]["useful_output_floor"] == _discovery_output_floor(len(row["unit_ids"]), 0)
        for row in delivered[1:]
    )


@pytest.mark.asyncio
async def test_format_repair_is_planned_separately_and_does_not_reset_budget():
    units, state, _ = fixture()
    ledger = HypothesisLedger("run", "abc", "digest", protocol_version=3)
    model = CoverageModel(units={u.id: u for u in units}, raw="{ not valid JSON")
    probe = HypothesisGenerator(model)
    _, messages = final_messages(probe, units, state, ledger)
    available = estimate_request(messages, {"max_tokens": 8192}) + _discovery_output_floor(1, 6) + 200
    model.usage = available - 800
    budget = RequestBudget(250_000, discovery_limit=available)

    async def changed(snapshot):
        ledger.request_budget = snapshot

    wrapped = BudgetedChatLLM(model, budget, phase="discovery", on_change=changed)
    result = await HypothesisGenerator(wrapped).run(state, ContextPack(), changeset(*units), ledger)
    record = next(iter(ledger.discovery_blocks.values()))
    assert result.blocks == 1 and len(model.calls) == 1 and record["repair_plan"]["available_tokens"] == 800
    assert record["repair_plan"]["max_output_tokens"] < record["repair_plan"]["useful_output_floor"]
    assert "budget-exhausted" in ledger.unresolved_units[units[0].id]
    assert budget.snapshot()["request_count"] == 1 and budget.snapshot()["denied_requests"] == 0
    assert ledger.request_budget["reported_tokens"] == model.usage
