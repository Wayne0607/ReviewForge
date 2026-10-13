"""Discovery deltas and unconfirmed, source-bound regression obligations."""

from __future__ import annotations

import copy
import json

import pytest
from langchain_core.messages import AIMessage
from test_discovery_source_refs import reference
from test_discovery_v3 import ScriptedLLM, candidate, changeset, clean, diff, unit

from reviewforge.core.state import StateStore
from reviewforge.engine.context_pack import ContextPack, ContextSlice, UnitContext
from reviewforge.engine.discovery import DiscoveryLimits, plan_discovery
from reviewforge.engine.discovery_aliases import source_aliases
from reviewforge.engine.discovery_submission import discovery_submission_tools
from reviewforge.engine.hypothesis import HypothesisLedger
from reviewforge.engine.hypothesis_generator import HypothesisGenerator
from reviewforge.engine.prompts_v4 import load_prompt


def contracted_input():
    u = unit("service.py")
    # A real consumer assertion states the required result and supported call.
    caller = ContextSlice(
        "caller",
        "consumer.py",
        40,
        40,
        "load_existing_record",
        "assert fetch(existing_record_id) == persisted_value",
        "production caller contract",
        "abc",
    )
    pack = ContextPack(units={u.id: UnitContext(u.id, [caller])})
    state = StateStore(file_diffs={u.path: diff()})
    block = plan_discovery(changeset(u), pack, state.file_diffs, limits=DiscoveryLimits())[0]
    raw = candidate(u)
    source = reference(block, "context", "consumer.py", 40)
    raw["violation"] = {
        "required_behavior": "Fetching the consumer's existing record must return its persisted value.",
        "requirement_basis": [source],
        "actual_incompatible_behavior": "The changed operation returns the replacement value instead.",
        "negative_consequence": "The production consumer receives a value incompatible with its stored record.",
        "supported_input": "The delivered consumer requests an existing_record_id.",
        "input_basis": [copy.deepcopy(source)],
    }
    return u, state, pack, block, raw


@pytest.mark.asyncio
async def test_v4_discovers_a_source_grounded_lead_without_pretending_to_prove_the_contract():
    u, state, pack, block, raw = contracted_input()
    raw.pop("violation")
    aliases = source_aliases(block.source_refs)
    reverse = {reference: alias for alias, reference in aliases.items()}

    def source_alias(fact):
        return reverse[reference(block, fact["side"], fact["path"], fact["line"])["source_ref"]]

    behavior_refs = [source_alias(fact) for fact in raw["behavior"]["source_facts"]]
    wire = {
        **{
            field: raw[field]
            for field in (
                "unit_id",
                "mechanism",
                "severity",
                "claim",
                "trigger",
                "impact",
                "open_question",
                "refutation",
                "discrepancy",
            )
        },
        **{field: raw["behavior"][field] for field in ("before", "after", "observable")},
        "source_refs": [*behavior_refs, source_alias(raw["trigger_basis"])],
        "source_roles": ["behavior"] * len(behavior_refs) + ["trigger"],
        "site_refs": [source_alias({"side": "after", **raw["sites"][0]})],
        "change_fields": [],
    }
    schema = next(
        tool.args_schema for tool in discovery_submission_tools(block, aliases) if tool.name == "submit_hypothesis"
    )
    assert "violation" not in schema.model_fields
    schema.model_validate(wire)
    book = HypothesisLedger("run", "abc", "digest", protocol_version=4)
    model = ScriptedLLM(
        responses=[
            AIMessage(
                content="", tool_calls=[{"name": "submit_hypothesis", "args": wire, "id": "lead", "type": "tool_call"}]
            )
        ]
    )
    result = await HypothesisGenerator(model).run(state, pack, changeset(u), book)
    assert result.accepted == 1 and not book.unresolved_units
    item = next(iter(book.items.values()))
    assert item.status.value == "open" and item.assessment is None and not item.observations
    assert "violation" not in item.discovery_basis
    assert item.claim == raw["claim"]
    assert item.discovery_basis["behavior"]["source_facts"] == [
        block.resolve_source_fact(fact) for fact in raw["behavior"]["source_facts"]
    ]
    delivered = next(tool for tool in model.bindings[0]["tools"] if tool["function"]["name"] == "submit_hypothesis")
    assert "violation" not in delivered["function"]["parameters"]["properties"]
    assert len(model.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement", [False, True])
async def test_v4_requires_changed_before_not_unrelated_unchanged_context(replacement):
    u = unit("app.py")
    raw = clean(u)
    raw["behavior"]["source_facts"] = [{"side": "after", "path": u.path, "line": 2, "excerpt": "return new_value"}]
    patch = "@@ -1,1 +1,2 @@\n existing_line\n+return new_value"
    if replacement:
        patch = "@@ -1,2 +1,2 @@\n existing_line\n-return old_value\n+return new_value"
    book = HypothesisLedger("run", "abc", "digest", protocol_version=4)
    await HypothesisGenerator(ScriptedLLM(responses=[json.dumps({"hypotheses": [], "no_issue_units": [raw]})])).run(
        StateStore(file_diffs={u.path: patch}), ContextPack(), changeset(u), book
    )
    assert bool(book.unresolved_units) == replacement
    assert (u.id in book.no_issue_units) != replacement


@pytest.mark.asyncio
async def test_closing_markup_delta_without_violation_cannot_be_admitted():
    u = unit("messages.properties")
    before, after = "emailBody=<p>Welcome", "emailBody=<p>Welcome</p>"
    raw = candidate(u)
    raw.pop("violation")
    raw.update(
        claim="Adding a closing paragraph changes the HTML structure.",
        impact="The paragraph is now properly closed.",
        discrepancy="The after string has a closing paragraph tag.",
        trigger_basis={"side": "before", "path": u.path, "line": 1, "excerpt": before},
        sites=[{"path": u.path, "line": 1, "excerpt": after}],
    )
    raw["behavior"].update(
        before="The paragraph was open.",
        after="The paragraph is closed.",
        observable="The markup structure changes.",
        source_facts=[
            {"side": side, "path": u.path, "line": 1, "excerpt": text}
            for side, text in (("before", before), ("after", after))
        ],
    )
    ledger = HypothesisLedger("run", "abc", "digest", protocol_version=3)
    result = await HypothesisGenerator(ScriptedLLM(responses=[json.dumps({"hypotheses": [raw]})])).run(
        StateStore(file_diffs={u.path: diff(before, after)}), ContextPack(), changeset(u), ledger
    )
    assert result.accepted == 0 and result.dropped_invalid == 1 and not ledger.items
    assert "violation obligations" in ledger.unresolved_units[u.id] and not ledger.no_issue_units


@pytest.mark.asyncio
async def test_real_caller_requirement_and_input_may_reuse_one_source_ref():
    u, state, pack, block, raw = contracted_input()
    ledger = HypothesisLedger("run", "abc", "digest", protocol_version=3)
    result = await HypothesisGenerator(ScriptedLLM(responses=[json.dumps({"hypotheses": [raw]})])).run(
        state, pack, changeset(u), ledger
    )
    assert result.accepted == 1 and not ledger.unresolved_units
    stored = next(iter(ledger.items.values()))
    obligation = stored.discovery_basis["violation"]
    expected = block.resolve_source_fact(raw["violation"]["requirement_basis"][0])
    assert obligation["requirement_basis"] == obligation["input_basis"] == [expected]
    assert stored.status.value == "open" and not stored.observations
    saved = HypothesisLedger.from_dict(json.loads(json.dumps(ledger.to_dict())))
    assert next(iter(saved.items.values())).discovery_basis["violation"] == obligation


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field", ["required_behavior", "actual_incompatible_behavior", "negative_consequence", "supported_input"]
)
@pytest.mark.parametrize("invalid", [None, "", "x" * 601])
async def test_missing_or_unbounded_violation_prose_stays_unresolved(field, invalid):
    u, state, pack, _, raw = contracted_input()
    raw["violation"][field] = invalid
    ledger = HypothesisLedger("run", "abc", "digest", protocol_version=3)
    result = await HypothesisGenerator(ScriptedLLM(responses=[json.dumps({"hypotheses": [raw]})])).run(
        state, pack, changeset(u), ledger
    )
    assert result.accepted == 0 and not ledger.items and u.id in ledger.unresolved_units


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["requirement_basis", "input_basis"])
@pytest.mark.parametrize(
    "invalid",
    [None, [], ["sr_unknown"], ["sr_unknown"] * 5, [{"kind": "file-change", "change_id": "metadata-is-not-source"}]],
)
async def test_basis_requires_one_to_four_real_block_sources(field, invalid):
    u, state, pack, _, raw = contracted_input()
    raw["violation"][field] = invalid
    ledger = HypothesisLedger("run", "abc", "digest", protocol_version=3)
    result = await HypothesisGenerator(ScriptedLLM(responses=[json.dumps({"hypotheses": [raw]})])).run(
        state, pack, changeset(u), ledger
    )
    assert result.accepted == 0 and not ledger.items and u.id in ledger.unresolved_units


@pytest.mark.asyncio
@pytest.mark.parametrize("wrong", ["other_block", "wrong_side", "override", "navigation", "extra_field"])
async def test_violation_references_preserve_exact_side_block_and_source_constraints(wrong):
    u, state, pack, block, raw = contracted_input()
    if wrong == "other_block":
        other = unit("other.py")
        other_block = plan_discovery(changeset(other), ContextPack(), {other.path: diff()}, limits=DiscoveryLimits())[0]
        raw["violation"]["input_basis"] = [reference(other_block, "after")]
    elif wrong == "wrong_side":
        raw["violation"]["input_basis"] = [
            {
                "side": "before",
                "path": "consumer.py",
                "line": 40,
                "excerpt": "assert fetch(existing_record_id) == persisted_value",
            }
        ]
    elif wrong == "override":
        raw["violation"]["input_basis"][0]["side"] = "before"
    elif wrong == "navigation":
        raw["violation"]["requirement_basis"] = [
            {"side": "context", "path": "consumer.py", "line": 40, "excerpt": "production caller contract"}
        ]
    else:
        raw["violation"]["hidden_requirement"] = "not a schema field"
    ledger = HypothesisLedger("run", "abc", "digest", protocol_version=3)
    result = await HypothesisGenerator(ScriptedLLM(responses=[json.dumps({"hypotheses": [raw]})])).run(
        state, pack, changeset(u), ledger
    )
    assert result.accepted == 0 and not ledger.items and u.id in ledger.unresolved_units
    assert not ledger.no_issue_units


@pytest.mark.asyncio
async def test_invalid_obligations_do_not_consume_cap_or_discard_an_independent_valid_risk():
    u, state, pack, _, valid = contracted_input()
    invalid = candidate(u)
    invalid.pop("violation")
    invalid.update(severity="error", claim="A higher-ranked behavior change without a required contract.")
    ledger = HypothesisLedger("run", "abc", "digest", protocol_version=3)
    result = await HypothesisGenerator(
        ScriptedLLM(responses=[json.dumps({"hypotheses": [invalid, valid]})]), max_hypotheses=1
    ).run(state, pack, changeset(u), ledger)
    assert result.accepted == 1 and result.dropped_invalid == 1 and result.dropped_overflow == 0
    assert next(iter(ledger.items.values())).claim == valid["claim"]
    assert u.id in ledger.unresolved_units and not ledger.no_issue_units


@pytest.mark.asyncio
async def test_compatible_observable_change_can_have_a_concrete_contract_check():
    u = unit("api.py")
    before, after = "return 1_000", "return 1000"
    raw = clean(u)
    raw["checked"] = "Both literal forms return the required integer 1000 for the same supported call."
    raw["behavior"] = {
        "before": "Returns integer 1000.",
        "after": "Returns integer 1000.",
        "observable": "The caller's integer result remains 1000.",
        "source_facts": [
            {"side": side, "path": u.path, "line": 1, "excerpt": text}
            for side, text in (("before", before), ("after", after))
        ],
    }
    ledger = HypothesisLedger("run", "abc", "digest", protocol_version=3)
    await HypothesisGenerator(ScriptedLLM(responses=[json.dumps({"hypotheses": [], "no_issue_units": [raw]})])).run(
        StateStore(file_diffs={u.path: diff(before, after)}), ContextPack(), changeset(u), ledger
    )
    assert u.id in ledger.no_issue_units and not ledger.items and not ledger.unresolved_units


@pytest.mark.asyncio
@pytest.mark.parametrize("version", [1, 2])
async def test_saved_old_protocols_do_not_require_violation(version):
    u = unit("api.py")
    raw = candidate(u)
    for field in ("violation", "behavior", "trigger_basis", "discrepancy"):
        raw.pop(field)
    ledger = HypothesisLedger("run", "abc", "digest", protocol_version=version)
    result = await HypothesisGenerator(ScriptedLLM(responses=[json.dumps({"hypotheses": [raw]})])).run(
        StateStore(file_diffs={u.path: diff()}), ContextPack(), changeset(u), ledger
    )
    assert result.accepted == 1 and not ledger.unresolved_units


def test_prompt_requires_after_contract_violation_separately_from_delta():
    prompt = load_prompt("discovery_v3", lens="general correctness", output_language="English", max_hypotheses=6)
    assert "A real\nobservable change may be compatible with the contract" in prompt
    assert "required_behavior" in prompt and "requirement_basis" in prompt
    assert "same direction" in prompt and "different IDs are not required" in prompt
    assert "source resolution does not prove" in prompt.lower()
