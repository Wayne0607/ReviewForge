"""Offline reductions of long exact quotes and candidate/deferred real outputs."""

from __future__ import annotations

import copy
import json
import re

import pytest
from test_discovery_v3 import ScriptedLLM, candidate, changeset, clean, diff, unit

from reviewforge.core.state import StateStore
from reviewforge.engine.context_pack import ContextPack, ContextSlice, UnitContext
from reviewforge.engine.discovery import DiscoveryLimits, plan_discovery
from reviewforge.engine.hypothesis import HypothesisLedger
from reviewforge.engine.hypothesis_generator import HypothesisGenerationResult, HypothesisGenerator


def reference(block, side, path=None, line=1):
    return next(
        {"source_ref": ref_id}
        for ref_id, fact in block.source_refs.items()
        if fact.side == side and fact.line == line and (path is None or fact.path == path)
    )


def referenced_candidate(u, block):
    item = candidate(u)
    item["behavior"]["source_facts"] = [reference(block, "before", u.path), reference(block, "after", u.path)]
    item["trigger_basis"] = reference(block, "before", u.path)
    item["sites"] = [reference(block, "after", u.path)]
    return item


@pytest.mark.asyncio
@pytest.mark.parametrize("length", [241, 290, 420, 700, 1100, 1700])
async def test_six_long_exact_source_quotes_are_accepted_without_undeclared_length_gate(length):
    u = unit("long.properties")
    before, after = "message=old_value " + "x" * length, "message=new_value " + "y" * length
    item = candidate(u)
    item["behavior"]["source_facts"] = [
        {"side": "before", "path": u.path, "line": 1, "excerpt": before},
        {"side": "after", "path": u.path, "line": 1, "excerpt": after},
    ]
    item["trigger_basis"] = item["behavior"]["source_facts"][0]
    item["sites"] = [{"path": u.path, "line": 1, "excerpt": after}]
    ledger = HypothesisLedger("run", "abc", "digest", protocol_version=3)
    result = await HypothesisGenerator(ScriptedLLM(responses=[json.dumps({"hypotheses": [item]})])).run(
        StateStore(file_diffs={u.path: diff(before, after)}), ContextPack(), changeset(u), ledger
    )
    assert result.accepted == 1 and not ledger.unresolved_units
    assert next(iter(ledger.items.values())).discovery_basis["trigger_basis"]["excerpt"] == before


@pytest.mark.asyncio
async def test_short_ids_resolve_before_schema_and_store_only_exact_source_navigation():
    u = unit("source.py")
    after = "return new_value # " + "x" * 1000
    patch = diff(after=after)
    block = plan_discovery(changeset(u), ContextPack(), {u.path: patch}, limits=DiscoveryLimits())[0]
    item = referenced_candidate(u, block)
    # The compact string form is compatible too; the documented object is default.
    item["trigger_basis"] = item["trigger_basis"]["source_ref"]
    ledger = HypothesisLedger("run", "abc", "digest", protocol_version=3)
    llm = ScriptedLLM(responses=[json.dumps({"hypotheses": [item]})])
    result = await HypothesisGenerator(llm).run(
        StateStore(file_diffs={u.path: patch}), ContextPack(), changeset(u), ledger
    )
    assert result.accepted == 1 and not ledger.unresolved_units
    stored = next(iter(ledger.items.values()))
    assert stored.sites[0].line == 1 and stored.sites[0].path == u.path
    assert len(stored.sites[0].excerpt) <= 240
    assert stored.discovery_basis["trigger_basis"] == {
        "side": "before",
        "path": u.path,
        "line": 1,
        "excerpt": "return old_value",
    }
    assert not stored.observations
    assert llm.calls[0][1].content.count(after) == 1
    assert "sr_" in llm.calls[0][0].content


def test_references_are_stable_and_bind_block_side_path_line_and_excerpt():
    first, second = unit("first.py"), unit("second.py")
    plan = plan_discovery(
        changeset(first, second),
        ContextPack(),
        {u.path: diff() for u in (first, second)},
        limits=DiscoveryLimits(max_units=1),
    )
    same = plan_discovery(changeset(first), ContextPack(), {first.path: diff()}, limits=DiscoveryLimits())[0]
    assert plan[0].source_refs == same.source_refs
    before = reference(same, "before")
    after = reference(same, "after")
    assert before != after
    assert same.resolve_source_fact(reference(plan[1], "after")) is None
    assert same.resolve_source_fact({**after, "line": 9}) is None
    assert same.resolve_source_fact({"source_ref": []}) is None
    assert same.resolve_source_fact({"source_ref": None}) is None
    assert same.resolve_source_fact("sr_unknown") is None
    modified = plan_discovery(
        changeset(first), ContextPack(), {first.path: diff(after="return third_value")}, limits=DiscoveryLimits()
    )[0]
    assert modified.id != same.id and modified.resolve_source_fact(after) is None
    grouped = plan_discovery(
        changeset(first, second), ContextPack(), {u.path: diff() for u in (first, second)}, limits=DiscoveryLimits()
    )[0]
    assert grouped.id != same.id and grouped.resolve_source_fact(after) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_field", ["unknown", "other_block", "override", "before_site", "context_site"])
async def test_invalid_id_or_wrong_site_side_remains_unresolved(bad_field):
    u, other = unit("source.py"), unit("other.py")
    slice_ = ContextSlice("caller", "source.py", 1, 1, "update", "return new_value", "same text", "abc")
    pack = ContextPack(units={u.id: UnitContext(u.id, [slice_])})
    block = plan_discovery(changeset(u), pack, {u.path: diff()}, limits=DiscoveryLimits())[0]
    item = referenced_candidate(u, block)
    if bad_field == "unknown":
        item["behavior"]["source_facts"][1] = {"source_ref": "sr_unknown"}
    elif bad_field == "other_block":
        other_block = plan_discovery(changeset(other), ContextPack(), {other.path: diff()}, limits=DiscoveryLimits())[0]
        item["trigger_basis"] = reference(other_block, "after")
    elif bad_field == "override":
        item["behavior"]["source_facts"][1]["side"] = "after"
    else:
        item["sites"] = [reference(block, "before" if bad_field == "before_site" else "context")]
    ledger = HypothesisLedger("run", "abc", "digest", protocol_version=3)
    result = await HypothesisGenerator(ScriptedLLM(responses=[json.dumps({"hypotheses": [item]})])).run(
        StateStore(file_diffs={u.path: diff()}), pack, changeset(u), ledger
    )
    assert result.failed_blocks == 1 and u.id in ledger.unresolved_units and not ledger.items
    assert not ledger.no_issue_units


@pytest.mark.parametrize("foreign", [False, True])
def test_short_ids_do_not_bypass_own_unit_or_changed_operation_binding(foreign):
    first, second = unit("source.py", "first"), unit("source.py", "second")
    second.start_line, second.end_line = 10, 12
    patch = "@@ -1,2 +1,2 @@\n keep_existing();\n-return old_value\n+return new_value"
    patch += "\n@@ -10,1 +10,1 @@\n-return other_old\n+return other_new"
    block = plan_discovery(changeset(first, second), ContextPack(), {first.path: patch}, limits=DiscoveryLimits())[0]
    item = clean(first)
    item["behavior"]["source_facts"] = [
        reference(block, side, first.path, 10 if foreign else 1) for side in ("before", "after")
    ]
    resolved = HypothesisGenerator._resolve_discovery_sources(item, block)
    assert not HypothesisGenerator._valid_behavior(resolved, first, block)


def test_legacy_exact_quotes_keep_side_path_line_and_text_validation():
    u = unit("source.py")
    text = "return new_value # " + "x" * 1700
    block = plan_discovery(changeset(u), ContextPack(), {u.path: diff(after=text)}, limits=DiscoveryLimits())[0]
    good = {"side": "after", "path": u.path, "line": 1, "excerpt": text}
    assert block.source_matches(good)
    for field, value in [("side", "before"), ("path", "other.py"), ("line", 2), ("excerpt", text + "invented")]:
        assert not block.source_matches({**good, field: value})


def test_source_reference_input_overhead_is_bounded_without_copying_source():
    u = unit("long.py")
    u.start_line, u.end_line = 1, 401
    common = [f"    source_{i:04} = receive_supported_value_from_existing_contract({i:04})" for i in range(400)]
    long = "return new_value # " + "x" * 10000
    patch = "@@ -1,401 +1,401 @@\n" + "\n".join(" " + line for line in common)
    patch += "\n-return old_value\n+" + long
    block = plan_discovery(changeset(u), ContextPack(), {u.path: patch}, limits=DiscoveryLimits())[0]
    unmarked = re.sub(r" \[sr_[^\]]+\]", "", block.diff_text)
    assert len(block.diff_text) <= len(unmarked) * 1.5 + 500
    assert block.diff_text.count(long) == 1
    assert all(block.diff_text.count(line) == 1 for line in common)
    assert all(
        len(source.text) <= 240 and block.source_matches(block.resolve_source_fact(ref_id))
        for ref_id, source in block.source_refs.items()
    )
    # Each ID appears in the existing coordinate labels, without a body index.
    assert all(ref_id in block.diff_text for ref_id in block.source_refs)


def test_context_reference_overhead_is_in_per_block_and_global_context_budgets():
    units = [unit(f"file{i}.py") for i in range(5)]
    slices = [
        ContextSlice(
            "caller",
            "caller.py",
            1,
            30,
            "caller",
            "\n".join("supported_call(argument)" for _ in range(30)),
            "actual caller",
            "abc",
        )
    ]
    pack = ContextPack(units={u.id: UnitContext(u.id, slices) for u in units})
    limits = DiscoveryLimits(max_units=1, context_max_chars=500, global_context_max_chars=1300)
    blocks = plan_discovery(changeset(*units), pack, {u.path: diff() for u in units}, limits=limits)
    assert all(len(block.context_text) <= 500 for block in blocks)
    assert sum(len(block.context_text) for block in blocks) <= 1300
    assert any(block.unchecked for block in blocks)


def test_reference_annotation_preserves_sharing_one_oversize_hunk():
    units = [unit("large.py", "parent"), unit("large.py", "child")]
    text = "return new_value # " + "x" * 35000
    blocks = plan_discovery(
        changeset(*units), ContextPack(), {units[0].path: diff(after=text)}, limits=DiscoveryLimits()
    )
    assert len(blocks) == 1 and len(blocks[0].units) == 2 and blocks[0].oversize
    assert blocks[0].diff_text.count(text) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reason",
    [
        "The supported input contract still requires investigation.",
        "The persisted-value caller contract has not been read.",
        "The invocation lifecycle condition requires another source.",
        "The error-path caller contract is not available.",
        "The second hunk's external consumer remains unchecked.",
    ],
)
async def test_five_candidate_plus_deferred_shapes_keep_candidate_and_uncovered_boundary(reason):
    u = unit("source.py")
    patch = diff() + "\n@@ -3,1 +3,1 @@\n-return extra_old\n+return extra_new"
    body = {"hypotheses": [candidate(u)], "deferred_units": [{"unit_id": u.id, "reason": reason}]}
    ledger = HypothesisLedger("run", "abc", "digest", protocol_version=3)
    result = await HypothesisGenerator(ScriptedLLM(responses=[json.dumps(body)])).run(
        StateStore(file_diffs={u.path: patch}), ContextPack(), changeset(u), ledger
    )
    assert result.accepted == 1 and result.failed_blocks == 1 and len(ledger.items) == 1
    assert u.id in ledger.unresolved_units and not ledger.no_issue_units
    row = next(iter(ledger.discovery_blocks.values()))["units"][u.id]
    assert row["status"] == "unresolved" and reason in row["reason"] and len(row["candidates"]) == 1


@pytest.mark.asyncio
async def test_candidate_and_deferred_consumption_is_order_independent():
    u = unit("source.py")
    block = plan_discovery(changeset(u), ContextPack(), {u.path: diff()}, limits=DiscoveryLimits())[0]
    payload = {
        "hypotheses": [referenced_candidate(u, block)],
        "deferred_units": [{"unit_id": u.id, "reason": "Another supported input is not checked."}],
    }
    outputs = []
    for body in (payload, dict(reversed(list(payload.items())))):
        ledger = HypothesisLedger("run", "abc", "digest", protocol_version=3)
        record, result = {"units": {}}, HypothesisGenerationResult()
        await HypothesisGenerator(ScriptedLLM())._consume_discovery(
            copy.deepcopy(body), block, ledger, result, {u.path: {1: "return new_value"}}, 6, record
        )
        outputs.append((ledger.unresolved_units, list(ledger.items), record, result.accepted))
    assert outputs[0] == outputs[1] and outputs[0][-1] == 1


@pytest.mark.asyncio
async def test_no_issue_and_candidate_are_still_contradictory():
    u = unit("source.py")
    body = {
        "hypotheses": [candidate(u)],
        "no_issue_units": [clean(u)],
        "deferred_units": [{"unit_id": u.id, "reason": "Another contract remains unchecked."}],
    }
    ledger = HypothesisLedger("run", "abc", "digest", protocol_version=3)
    result = await HypothesisGenerator(ScriptedLLM(responses=[json.dumps(body)])).run(
        StateStore(file_diffs={u.path: diff()}), ContextPack(), changeset(u), ledger
    )
    assert result.accepted == 0 and not ledger.items and not ledger.no_issue_units
    assert "contradictory" in ledger.unresolved_units[u.id]


@pytest.mark.asyncio
async def test_model_deferred_can_resume_with_candidate_without_resetting_budget_or_forcing_clean():
    u = unit("source.py")
    body = {"hypotheses": [candidate(u)], "deferred_units": [{"unit_id": u.id, "reason": "Caller not yet checked."}]}
    llm = ScriptedLLM(responses=[json.dumps(body), json.dumps({"hypotheses": [candidate(u)]})])
    ledger = HypothesisLedger("run", "abc", "digest", protocol_version=3)
    ledger.request_budget = {"sentinel": "shared request accounting"}
    state, generator = StateStore(file_diffs={u.path: diff()}), HypothesisGenerator(llm)
    await generator.run(state, ContextPack(), changeset(u), ledger)
    assert u.id in ledger.unresolved_units
    await generator.run(state, ContextPack(), changeset(u), ledger, resume=True)
    assert len(llm.calls) == 2 and len(ledger.items) == 1 and not ledger.unresolved_units
    assert not ledger.no_issue_units and ledger.request_budget == {"sentinel": "shared request accounting"}
    rows = [record["units"][u.id] for record in ledger.discovery_blocks.values()]
    assert rows[0]["status"] == "recovered" and "Caller not yet checked" in rows[0]["reason"]
    row = rows[-1]
    assert row["status"] == "assessed" and row["assessment"] == "hypothesis"


@pytest.mark.asyncio
async def test_resuming_model_deferred_does_not_expand_original_model_block_limit():
    u = unit("source.py")
    llm = ScriptedLLM(
        responses=[
            json.dumps({"hypotheses": [], "deferred_units": [{"unit_id": u.id, "reason": "Caller is not delivered."}]})
        ]
    )
    ledger = HypothesisLedger("run", "abc", "digest", protocol_version=3)
    generator, state = HypothesisGenerator(llm, discovery_max_blocks=1), StateStore(file_diffs={u.path: diff()})
    await generator.run(state, ContextPack(), changeset(u), ledger)
    await generator.run(state, ContextPack(), changeset(u), ledger, resume=True)
    assert len(llm.calls) == 1 and not ledger.items and u.id in ledger.unresolved_units
    records = list(ledger.discovery_blocks.values())
    assert records[0]["units"][u.id]["reason"] == "deferred: Caller is not delivered."
    assert records[1]["units"][u.id]["status"] == "capacity-deferred"
    await generator.run(state, ContextPack(), changeset(u), ledger, resume=True)
    assert len(llm.calls) == 1 and len(ledger.discovery_blocks) == 2
