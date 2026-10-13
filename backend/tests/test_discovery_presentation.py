"""Presentation savings preserve source bodies, coordinates and output contracts."""

from __future__ import annotations

import copy
import json
import re

import pytest
from test_discovery_v3 import changeset, diff, unit

from reviewforge.engine.context_pack import ContextPack, ContextSlice, UnitContext
from reviewforge.engine.discovery import DiscoveryLimits, compact_overview, compact_source_coordinates, plan_discovery
from reviewforge.engine.hypothesis import HypothesisLedger
from reviewforge.engine.hypothesis_generator import HypothesisGenerator
from reviewforge.engine.prompts_v4 import load_prompt
from reviewforge.engine.verification_guidance import defect_scope_guidance, investigation_capabilities


def _v4_shared_block(
    text="return a_really_long_shared_source_value + another_very_long_source_value" * 3,
):
    first, second = unit("path with spaces/shared.py", "first"), unit("path with spaces/shared.py", "second")
    slices = [ContextSlice("caller", first.path, 1, 1, "invoke", text, "actual caller", "abc")]
    pack = ContextPack(units={item.id: UnitContext(item.id, slices) for item in (first, second)})
    return plan_discovery(changeset(first, second), pack, {first.path: diff(after=text)}, limits=DiscoveryLimits())[0]


def test_v4_context_shares_exact_right_body_without_changing_any_source_identity():
    from reviewforge.engine.discovery_presentation import present_discovery_block

    block = _v4_shared_block()
    saved = copy.deepcopy(block)
    shown = present_discovery_block(block)
    text = "return a_really_long_shared_source_value + another_very_long_source_value"
    assert text in shown.diff_text and text not in shown.context_text
    assert "= RIGHT" in shown.context_text
    assert "never RIGHT sites" in shown.context_text
    assert block == saved
    assert set(re.findall(r"sr_[a-f0-9]+", shown.diff_text + shown.context_text)) == set(block.source_refs)
    for ref_id, fact in block.source_refs.items():
        assert block.resolve_source_fact(ref_id)["side"] == fact.side
    context_ref = next(ref for ref, fact in block.source_refs.items() if fact.side == "context")
    assert not block.unit_source_matches(block.units[0].id, block.resolve_source_fact(context_ref), changed=True)


def test_v4_context_keeps_different_path_coordinate_or_text_and_all_long_line_segments():
    from reviewforge.engine.discovery_presentation import present_discovery_block

    text = "語言" * 180 + "  exact trailing whitespace  "
    block = _v4_shared_block(text)
    saved = copy.deepcopy(block)
    shown = present_discovery_block(block)
    assert shown.diff_text.count(text) == 1 and text not in shown.context_text
    context_refs = {ref for ref, fact in block.source_refs.items() if fact.side == "context"}
    assert len(context_refs) > 1 and all(ref in shown.context_text for ref in context_refs)
    assert block == saved

    for replacement in (text + "different", " " + text):
        separate = _v4_shared_block(replacement)
        # Only the real Context line changes; it no longer equals the delivered RIGHT line.
        separate.diff_text = separate.diff_text.replace(replacement, text)
        result = present_discovery_block(separate)
        assert replacement in result.context_text and "= RIGHT" not in result.context_text


def test_v4_context_does_not_alias_undelivered_source_even_if_registered():
    from reviewforge.engine.discovery_presentation import present_discovery_block

    block = _v4_shared_block()
    block.diff_text = "(source omitted in a deliberately inconsistent fixture)"
    shown = present_discovery_block(block)
    assert "return a_really_long_shared_source_value" in shown.context_text
    assert "= RIGHT" not in shown.context_text


@pytest.mark.parametrize("path,line", [("different.py", 1), ("changed.py", 2)])
def test_v4_context_never_shares_just_equal_text_at_a_different_source_location(path, line):
    from reviewforge.engine.discovery_presentation import present_discovery_block

    item = unit("changed.py")
    body = "return " + "same_text_different_origin_" * 12
    context = ContextSlice("caller", path, line, line, "invoke", body, "actual caller", "abc")
    block = plan_discovery(
        changeset(item),
        ContextPack(units={item.id: UnitContext(item.id, [context])}),
        {item.path: diff(after=body)},
        limits=DiscoveryLimits(),
    )[0]
    shown = present_discovery_block(block)
    assert body in shown.diff_text and body in shown.context_text
    assert "= RIGHT" not in shown.context_text


def test_v4_context_does_not_trust_altered_reference_labels():
    from reviewforge.engine.discovery_presentation import present_discovery_block

    block = _v4_shared_block()
    after_ref = next(ref for ref, fact in block.source_refs.items() if fact.side == "after")
    block.diff_text = block.diff_text.replace(after_ref, "sr_unregistered")
    shown = present_discovery_block(block)
    assert "= RIGHT" not in shown.context_text


def test_v4_context_shares_overlapping_rows_but_keeps_context_provenance():
    from reviewforge.engine.discovery_presentation import present_discovery_block

    first, second = unit("changed.py", "first"), unit("changed.py", "second")
    body = "return " + "unchanged_actual_caller_value_" * 5
    slices = [ContextSlice("caller", "caller.py", 10, 11, "invoke", body + "\nreturn end", "caller", "abc")]
    other = [ContextSlice("callee", "caller.py", 10, 10, "invoke", body, "callee", "abc")]
    block = plan_discovery(
        changeset(first, second),
        ContextPack(units={first.id: UnitContext(first.id, slices), second.id: UnitContext(second.id, other)}),
        {first.path: diff()},
        limits=DiscoveryLimits(),
    )[0]
    saved = copy.deepcopy(block)
    shown = present_discovery_block(block)
    assert shown.context_text.count(body) == 1
    assert "= earlier Context" in shown.context_text
    assert "callee" in shown.context_text and "caller" in shown.context_text
    assert block == saved


def test_v4_child_navigation_projects_only_proven_sibling_ids_and_keeps_unknown_rows():
    from reviewforge.engine.discovery_presentation import present_discovery_block

    block = _v4_shared_block()
    own = block.units[0].id
    sibling = "su_0123456789abcdef"
    block.navigation = (
        f"- {own} -> caller long/path.py:4-8 invoke: calls invoke at line 6\n"
        f"- {sibling} -> caller other.py:4-8 other: calls other at line 6\n"
        "- unrecognized ownership -> caller unknown.py:1-3 x: uncertain\n"
        "(4 source relationships omitted from this bounded navigation; not assessed here.)"
    )
    shown = present_discovery_block(block)
    assert own in shown.navigation and sibling not in shown.navigation
    assert "unrecognized ownership" in shown.navigation
    assert "(4 source relationships omitted" in shown.navigation


def test_v4_navigation_table_preserves_every_field_order_and_unknown_text():
    from reviewforge.engine.discovery_presentation import compact_navigation

    path = "long/repository/path/with repeated source.py"
    rows = [
        f"- su_{index:016x} -> caller {path}:{index}-{index + 2} same: calls same at line {index + 1}"
        for index in range(8)
    ]
    original = "\n".join(rows + ["(2 source relationships omitted; not assessed here.)"])
    shown = compact_navigation(original)
    assert len(shown) < len(original)
    assert shown.endswith("(2 source relationships omitted; not assessed here.)")
    lines = shown.splitlines()
    paths = json.loads(
        next(line.removeprefix("Navigation paths ") for line in lines if line.startswith("Navigation paths "))
    )
    decoded = [json.loads(line) for line in lines if line.startswith("[")]
    reconstructed = [f"- {row[0]} -> {row[1]} {paths[row[2]]}:{row[3]}-{row[4]} {row[5]}: {row[6]}" for row in decoded]
    assert reconstructed == rows


def test_v4_short_context_and_unrecognized_navigation_are_not_expanded():
    from reviewforge.engine.discovery_presentation import compact_navigation, present_discovery_block

    block = _v4_shared_block("x")
    shown = present_discovery_block(block)
    assert "= RIGHT" not in shown.context_text
    navigation = "- ambiguous data: do not guess fields\n(unknown relationships)"
    assert compact_navigation(navigation) == navigation


def source_rows(text):
    """Read coordinates/labels/body independently of their alignment padding."""
    rows = []
    for line in text.splitlines():
        both = re.fullmatch(r" before=\s*(\d+)( \[[^]]*\])? +RIGHT=\s*(\d+)( \[[^]]*\])? +\| (.*)", line)
        single = re.fullmatch(r"([-+])\s*(\d+)( \[[^]]*\])? +\| (.*)", line)
        context = re.fullmatch(r"\s*(\d+)( \[[^]]*\])? +\| (.*)", line)
        if both:
            rows.extend([("before", int(both[1]), both[2], both[5]), ("after", int(both[3]), both[4], both[5])])
        elif single:
            rows.append(("before" if single[1] == "-" else "after", int(single[2]), single[3], single[4]))
        elif context:
            rows.append(("context", int(context[1]), context[2], context[3]))
    return rows


def test_coordinate_compaction_changes_only_padding_not_body_refs_or_sides():
    text = (
        "### Shared before/after diff: path with spaces.py\n@@ -4,2 +7,2 @@\n"
        "-    4 [sr_before] |   old | value  \n"
        "+    7 [sr_after] |   +    1 [sr_fake] | code  \n"
        " before=    5 [sr_left] RIGHT=    8 [sr_right] | same | expression\n"
        " before=    6  RIGHT=    9  | \n"
        "+   10  | \n"
        "    9 [sr_context:1-14] |     return caller\n"
        "12345 [sr_large] | unchanged padding"
    )
    compact = compact_source_coordinates(text)
    assert source_rows(compact) == source_rows(text)
    assert "   +    1 [sr_fake] | code  " in compact
    assert " before=5 [sr_left] RIGHT=8 [sr_right]" in compact
    assert "12345 [sr_large] | unchanged padding" in compact
    assert compact.count("@@ -4,2 +7,2 @@") == 1 and len(compact) < len(text)
    assert compact_source_coordinates(compact) == compact


def test_overview_groups_only_contiguous_repeated_paths_and_keeps_all_rows_in_order():
    path = "directory/long-subdirectory/another-subdirectory/source.py"
    original = (
        "PR head=abc; 5 semantic units.\nIntent: Preserve the supplied intent.\n"
        + "\n".join(f"- u{i} {path}:{i}-{i + 1} symbol{i}" for i in range(3))
        + "\n- u3 other.py:7-8 symbol3\n"
        + f"- u4 {path}:9-10 symbol4\n"
        + "(2 PR units omitted from this bounded navigation; not assessed here.)"
    )
    compact = compact_overview(original)
    assert len(compact) < len(original)
    assert compact.count(path) == 2
    assert "Intent: Preserve the supplied intent." in compact
    assert compact.endswith("(2 PR units omitted from this bounded navigation; not assessed here.)")
    recovered, current_path = [], ""
    for line in compact.splitlines():
        if line.startswith("File "):
            current_path = line[5:].split(" (inventory:", 1)[0]
        elif match := re.fullmatch(r"(u\d+) \| (\d+-\d+) \| (symbol\d+)", line):
            recovered.append((match[1], current_path, match[2], match[3]))
        elif match := re.fullmatch(r"- (u\d+) (.+):(\d+-\d+) (symbol\d+)", line):
            recovered.append(tuple(match.groups()))
    assert recovered == [(f"u{i}", path, f"{i}-{i + 1}", f"symbol{i}") for i in range(3)] + [
        ("u3", "other.py", "7-8", "symbol3"),
        ("u4", path, "9-10", "symbol4"),
    ]
    assert compact_overview(compact) == compact


def test_render_keeps_every_delivered_fact_ref_and_scope_without_mutating_the_block():
    first, second = unit("shared.py", "first"), unit("shared.py", "second")
    context = ContextSlice("caller", "caller.py", 42, 42, "invoke", "return invoke(arg | flag)", "real caller", "abc")
    pack = ContextPack(units={first.id: UnitContext(first.id, [context])}, pr_intent="Specific intent")
    block = plan_discovery(
        changeset(first, second),
        pack,
        {first.path: diff(after="return new_value # " + "x" * 700)},
        limits=DiscoveryLimits(),
    )[0]
    saved = copy.deepcopy(block)
    generator = HypothesisGenerator(None, unit_responsibilities={first.id: ["domain"]})
    message = generator._render_discovery_message(
        block, HypothesisLedger("run", "abc", "digest", protocol_version=3), 6
    )
    assert block == saved
    assert source_rows(message) == source_rows(block.diff_text + "\n" + block.context_text)
    assert set(re.findall(r"sr_[a-f0-9]+", message)) == set(block.source_refs)
    for ref_id, fact in block.source_refs.items():
        assert block.resolve_source_fact({"source_ref": ref_id}) == {
            "side": fact.side,
            "path": fact.path,
            "line": fact.line,
            "excerpt": fact.text,
        }
    table = message.split("## Units\n", 1)[1].split("\n\n", 1)[0]
    rows = [json.loads(line) for line in table.splitlines() if line.startswith("[")]
    assert [row[0] for row in rows] == [first.id, second.id]
    assert rows[0][1:] == [first.path, first.symbol, block.unit_before_states[first.id], "1", "1", ["domain"], ""]
    assert rows[1][6] == []
    assert "Allowed unit_id values" not in message
    assert "shared.py:first" not in message.split("## Required assessments", 1)[1]
    assert message.count("return new_value # " + "x" * 700) == 1


def test_v3_scope_and_capabilities_are_single_contract_with_domain_body_intact():
    rule = "---\nname: fixture-domain\n---\nFull domain contract: supported inputs retain persisted values."
    generator = HypothesisGenerator(None, responsibility_rules={"first": rule, "alias": rule})
    prompt = generator._discovery_system_prompt(6, ["first", "alias"])
    assert prompt.count(rule) == 1
    assert defect_scope_guidance() not in prompt and investigation_capabilities() not in prompt
    assert "caller/test/config contracts" in prompt and "exit-code API" in prompt
    assert "read_file/read_diff/grep/" in prompt and "concrete supplied advisory" in prompt
    assert "without internet/advisory lookup or execution" in prompt
    assert len(generator._discovery_system_prompt(6).encode()) < 9_000
    assert generator._system_prompt() == (
        load_prompt("generator", output_language="English", lens="generator", max_hypotheses=12)
        + "\n\n"
        + defect_scope_guidance()
        + "\n\n"
        + investigation_capabilities()
    )


def test_compact_schema_example_has_object_behavior_and_all_violation_fields():
    prompt = HypothesisGenerator(None)._discovery_system_prompt(6)
    example = json.loads(prompt.split("```json\n", 1)[1].split("\n```", 1)[0])
    item = example["hypotheses"][0]
    assert set(item["behavior"]) == {"before", "after", "observable", "source_facts"}
    assert set(item["violation"]) == {
        "required_behavior",
        "requirement_basis",
        "actual_incompatible_behavior",
        "negative_consequence",
        "supported_input",
        "input_basis",
    }
    assert "no_issue_units entries are {unit_id,checked:P,behavior:B}" in prompt
    assert "different IDs are not required" in prompt and "no overrides or other-block IDs" in prompt
