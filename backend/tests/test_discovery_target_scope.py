"""Only block targets carry output identities; global navigation is not coverage."""

from __future__ import annotations

import copy
import json

import pytest
from test_discovery_v3 import ScriptedLLM, candidate, changeset, clean, diff, unit

from reviewforge.core.state import StateStore
from reviewforge.engine.context_pack import ContextPack, ContextSlice, UnitContext
from reviewforge.engine.discovery import DiscoveryLimits, compact_overview, plan_discovery
from reviewforge.engine.discovery_presentation import present_pr_overview
from reviewforge.engine.hypothesis import HypothesisLedger
from reviewforge.engine.hypothesis_generator import HypothesisGenerator
from reviewforge.engine.prompts_v4 import load_prompt


def fixture():
    units = [unit("first.py", risk=3), unit("second.py", risk=2), unit("elsewhere.py", risk=1)]
    for index, item in enumerate(units):
        item.id = f"su_{index + 1:016x}"
    diffs = {item.path: diff() for item in units}
    context = ContextSlice(
        "caller", "consumer.py", 17, 17, "caller", "return actual_consumer(value)", "real caller", "abc"
    )
    pack = ContextPack(units={units[0].id: UnitContext(units[0].id, [context])}, pr_intent="Keep all PR relationships")
    limits = DiscoveryLimits(max_units=2)
    block = plan_discovery(changeset(*units), pack, diffs, limits=limits)[0]
    return units, diffs, block


def overview(message):
    return message.split("## PR overview\n", 1)[1].split("\n\n## Source relationship navigation", 1)[0]


def test_overview_hides_only_inventory_ids_and_keeps_all_navigation_fields():
    first, second = "su_0000000000000001", "su_0000000000000002"
    text = (
        "PR head=abc; 2 semantic units.\nIntent: Keep exact intent.\n"
        f"- {first} directory with spaces/a.py:4-7 callable name\n"
        f"- {second} 日本語/config.json:0-0 (resource)\n"
        "(2 PR units omitted from this bounded navigation; not assessed here.)\n"
        "- unknown navigation syntax stays intact"
    )
    shown = present_pr_overview(text)
    assert first not in shown and second not in shown
    assert "- directory with spaces/a.py:4-7 callable name" in shown
    assert "- 日本語/config.json:0-0 (resource)" in shown
    assert shown.startswith("PR head=abc; 2 semantic units.\nIntent: Keep exact intent.\n")
    assert shown.endswith("- unknown navigation syntax stays intact")
    assert "(2 PR units omitted from this bounded navigation; not assessed here.)" in shown
    assert present_pr_overview(shown) == shown


def test_v4_targets_are_explicit_while_overview_has_no_output_identity():
    units, _, block = fixture()
    saved = copy.deepcopy(block)
    generator = HypothesisGenerator(None)
    ledger = HypothesisLedger("run", "abc", "digest", protocol_version=4)
    message = generator._render_discovery_message(block, ledger, 12)
    nav = overview(message)
    assert all(item.id not in nav for item in units)
    assert all(f"{item.path}:{item.start_line}-{item.end_line} {item.symbol}" in nav for item in units)
    final = message.split("## Required assessments", 1)[1]
    allowed = json.loads(final.split("Allowed unit_id values for this block (copy exactly; no others): ", 1)[1])
    assert allowed == [item.id for item in block.units]
    assert units[-1].id not in final
    assert "PR-wide navigation only" in nav
    assert block == saved  # Raw overview, facts, refs, scopes, hunks and ID retain identity.
    for ref_id, source in block.source_refs.items():
        assert block.resolve_source_fact(ref_id)["excerpt"] == source.text


def test_v3_keeps_the_existing_overview_and_assessment_presentation():
    _, _, block = fixture()
    ledger = HypothesisLedger("run", "abc", "digest", protocol_version=3)
    message = HypothesisGenerator(None)._render_discovery_message(block, ledger, 12)
    assert overview(message) == compact_overview(block.overview)
    assert "Allowed unit_id values for this block" not in message
    assert "PR-wide navigation only" not in message


@pytest.mark.asyncio
async def test_outside_unit_records_still_invalidate_coverage_and_keep_legal_candidates():
    units, diffs, _ = fixture()
    first, second, elsewhere = units
    payload = {
        "hypotheses": [candidate(first)],
        "no_issue_units": [clean(second), clean(elsewhere)],
        "deferred_units": [{"unit_id": elsewhere.id, "reason": "Overview-only unit is not delivered here."}],
    }
    llm = ScriptedLLM(
        responses=[json.dumps(payload), json.dumps({"hypotheses": [], "no_issue_units": [clean(elsewhere)]})]
    )
    ledger = HypothesisLedger("run", "abc", "digest", protocol_version=4)
    result = await HypothesisGenerator(llm, discovery_max_units=2).run(
        StateStore(file_diffs=diffs), ContextPack(), changeset(*units), ledger
    )
    assert result.accepted == 1
    assert next(iter(ledger.items.values())).unit_id == first.id
    assert (
        ledger.unresolved_units[first.id]
        == ledger.unresolved_units[second.id]
        == "generator invalid block output entries"
    )
    assert first.id not in ledger.no_issue_units and second.id not in ledger.no_issue_units
    assert elsewhere.id in ledger.no_issue_units  # Its own subsequent block can assess it.


def test_v4_prompt_distinguishes_output_targets_from_global_navigation():
    prompt = load_prompt("discovery_v4", lens="general correctness", output_language="English", max_hypotheses=12)
    assert "Only the final Allowed unit_id values list grants output permission" in prompt
    assert "every hypothesis, clean or deferred record must copy" in prompt
    assert "additional targets, even when their source or relationships are visible" in prompt
