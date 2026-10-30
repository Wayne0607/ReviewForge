"""Bound citation volume without discarding a multi-change unit's true sources."""

import copy
import json

import pytest
from test_discovery_v3 import ScriptedLLM
from test_structural_discovery_v3 import Workspace, change_facts, inputs

from reviewforge.core.state import StateStore
from reviewforge.engine.context_pack import ContextPack
from reviewforge.engine.discovery import DiscoveryLimits, plan_discovery
from reviewforge.engine.discovery_aliases import source_aliases
from reviewforge.engine.discovery_workflow_tools import assessment_tools
from reviewforge.engine.hypothesis import HypothesisLedger
from reviewforge.engine.hypothesis_generator import HypothesisGenerator
from reviewforge.engine.semantic_diff import SemanticChangeSet, SemanticUnit, UnitKind


def packet(count=4, *, addition=False, neighbor=False):
    unit = SemanticUnit(id="target", path="messages.properties", kind=UnitKind.RESOURCE)
    others = [SemanticUnit(id="neighbor", path="other.properties", kind=UnitKind.RESOURCE)] if neighbor else []
    changeset = SemanticChangeSet(head_sha="head", units=[unit, *others])
    patch = f"@@ -{0 if addition else 1},{0 if addition else count} +1,{count} @@\n"
    patch += "\n".join(
        row
        for index in range(count)
        for row in ([f"+key_{index}=new value"] if addition else [f"-key_{index}=old value", f"+key_{index}=new value"])
    )
    diffs = {item.path: patch for item in changeset.units}
    block = plan_discovery(changeset, ContextPack(), diffs, limits=DiscoveryLimits())[0]
    item = {
        "unit_id": unit.id,
        "checked": "Compared each changed resource value and its formatting boundary.",
        "behavior": {
            "before": "Original resource values." if not addition else "No removed source is supplied.",
            "after": "New resource values.",
            "observable": "Each changed resource supplies the updated value.",
            "source_facts": [
                {"source_ref": identity}
                for identity, fact in block.source_refs.items()
                if fact.path == unit.path and fact.changed
            ],
        },
    }
    return unit, changeset, diffs, block, item


def valid(item, unit, block, protocol=4, *, require_consumer=False):
    resolved = HypothesisGenerator._resolve_discovery_sources(item, block)
    return HypothesisGenerator._valid_behavior(
        resolved, unit, block, protocol_version=protocol, require_consumer=require_consumer
    )


@pytest.mark.parametrize("count,addition", [(4, False), (11, True), (12, True)])
def test_v4_keeps_all_true_references_for_multiple_changes(count, addition):
    unit, _, _, block, item = packet(count, addition=addition)
    original = copy.deepcopy(item)
    assert valid(item, unit, block)
    assert item == original  # No truncation, repair, or automatic before filling.


@pytest.mark.parametrize("protocol", [1, 2, 3])
def test_old_protocols_keep_the_four_reference_limit(protocol):
    unit, _, _, block, item = packet()
    assert len(item["behavior"]["source_facts"]) == 8
    assert not valid(item, unit, block, protocol)
    item["behavior"]["source_facts"] = item["behavior"]["source_facts"][:4]
    assert valid(item, unit, block, protocol)


@pytest.mark.parametrize("count", [13, 24])
def test_v4_still_rejects_over_capacity_even_when_every_reference_is_true(count):
    unit, _, _, block, item = packet(count, addition=True)
    assert not valid(item, unit, block)


@pytest.mark.parametrize("count", [7, 13])
def test_v10_keeps_all_required_source_pairs_without_a_record_quota(count):
    unit, _, _, block, item = packet(count)
    assert len(item["behavior"]["source_facts"]) == 2 * count
    aliases = source_aliases(block.source_refs, scoped=True)
    reverse = {reference: alias for alias, reference in aliases.items()}
    record = {
        "unit_id": unit.id,
        "kind": "no_issue",
        "checked": item["checked"],
        "source_refs": [reverse[fact["source_ref"]] for fact in item["behavior"]["source_facts"]],
    }
    _, schemas = assessment_tools(block, protocol_version=10)
    assert schemas["no_issue"].model_validate(record).source_refs == record["source_refs"]
    assert valid(item, unit, block, protocol=10)
    item["behavior"]["source_facts"].append({"source_ref": "sr_fabricated"})
    assert not valid(item, unit, block, protocol=10)
    assert schemas["no_issue"].model_validate({**record, "checked": "x" * 33_000}).checked == "x" * 33_000


@pytest.mark.parametrize("index", [0, 4, 7])
def test_any_fabricated_reference_invalidates_the_entire_extended_list(index):
    unit, _, _, block, item = packet()
    item["behavior"]["source_facts"][index] = {"source_ref": "sr_fabricated"}
    assert not valid(item, unit, block)


@pytest.mark.parametrize("side", ["before", "after"])
def test_true_neighbor_source_cannot_replace_a_required_target_side(side):
    unit, _, _, block, item = packet(neighbor=True)
    foreign = iter(
        {"source_ref": identity}
        for identity, fact in block.source_refs.items()
        if fact.path != unit.path and fact.side == side and fact.changed
    )
    item["behavior"]["source_facts"] = [
        next(foreign) if block.source_refs[fact["source_ref"]].side == side else fact
        for fact in item["behavior"]["source_facts"]
    ]
    assert not valid(item, unit, block)


def test_real_supplementary_source_remains_valid_alongside_all_required_target_facts():
    unit, _, _, block, item = packet(neighbor=True)
    foreign = next(identity for identity, fact in block.source_refs.items() if fact.path != unit.path)
    item["behavior"]["source_facts"].append({"source_ref": foreign})
    assert valid(item, unit, block)


def test_eight_after_references_cannot_hide_the_missing_changed_before_side():
    unit, _, _, block, item = packet(8)
    item["behavior"]["source_facts"] = [
        fact for fact in item["behavior"]["source_facts"] if block.source_refs[fact["source_ref"]].side == "after"
    ]
    assert len(item["behavior"]["source_facts"]) == 8
    assert not valid(item, unit, block)


@pytest.mark.parametrize("has_consumer", [False, True])
def test_extended_capacity_does_not_replace_structural_consumer_evidence(has_consumer):
    state, changeset, pack, unit = inputs(workspace=Workspace(updated=True), protocol=4)
    block = plan_discovery(
        changeset, pack, state.file_diffs, limits=DiscoveryLimits(), file_changes=state.file_changes
    )[0]
    facts = [
        {"source_ref": identity}
        for identity, fact in block.source_refs.items()
        if fact.kind == ("reference" if has_consumer else "head")
    ]
    item = {
        "unit_id": unit.id,
        "behavior": {
            "before": "Consumer refers to the previous resource name.",
            "after": "Resource is at the new name with identical bytes.",
            "observable": "Consumer reference must agree with the new path.",
            "change_facts": change_facts(state.file_changes[unit.path]),
            "source_facts": facts,
        },
    }
    assert valid(item, unit, block, require_consumer=True) is has_consumer


@pytest.mark.asyncio
async def test_generator_accepts_eight_reference_clean_without_dropping_any_fact():
    unit, changeset, diffs, block, item = packet()
    llm = ScriptedLLM(responses=[json.dumps({"hypotheses": [], "no_issue_units": [item]})])
    ledger = HypothesisLedger("run", "head", "workspace", protocol_version=4)
    result = await HypothesisGenerator(llm).run(StateStore(file_diffs=diffs), ContextPack(), changeset, ledger)
    assert not result.failed_blocks and unit.id in ledger.no_issue_units and not ledger.unresolved_units
    stored = next(iter(ledger.discovery_blocks.values()))["units"][unit.id]["behavior"]["source_facts"]
    assert stored == item["behavior"]["source_facts"]
    assert len(stored) == 8
    assert "1–12 refs" in llm.calls[0][0].content
    assert all(block.resolve_source_fact(fact) is not None for fact in stored)
