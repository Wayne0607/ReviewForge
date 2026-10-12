"""Presentation savings preserve source bodies, coordinates and output contracts."""

from __future__ import annotations

import copy
import json
import re

from test_discovery_v3 import changeset, diff, unit

from reviewforge.engine.context_pack import ContextPack, ContextSlice, UnitContext
from reviewforge.engine.discovery import DiscoveryLimits, compact_overview, compact_source_coordinates, plan_discovery
from reviewforge.engine.hypothesis import HypothesisLedger
from reviewforge.engine.hypothesis_generator import HypothesisGenerator
from reviewforge.engine.prompts_v4 import load_prompt
from reviewforge.engine.verification_guidance import defect_scope_guidance, investigation_capabilities


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
