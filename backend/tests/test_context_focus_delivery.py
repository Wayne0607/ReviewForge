"""V4 keeps verified navigation hits inside the original Context source budget."""

from copy import deepcopy
from dataclasses import replace

import pytest
from test_context_pack import _changeset, _Workspace
from test_discovery_v3 import changeset, diff, unit
from test_structural_discovery_v3 import Workspace, inputs

from reviewforge.engine.context_pack import ContextPack, ContextSlice, UnitContext
from reviewforge.engine.discovery import DiscoveryLimits, _context, plan_discovery, split_discovery


def focused_slice(*, focus=32, text=None):
    lines = [f"// source line {number}" for number in range(20, 45)]
    lines[12] = '    load("resources/new.json");'
    return ContextSlice(
        "reference",
        "consumer.py",
        20,
        44,
        "",
        text or "\n".join(lines),
        "verified literal resource reference at line 32; navigation only",
        "pinned-head",
        focus_line=focus,
    )


def render(slice_, *, protocol=4, budget=700):
    target = unit("resource.json")
    pack = ContextPack(units={target.id: UnitContext(target.id, [slice_])}, protocol_version=protocol)
    before = deepcopy(pack)
    result = _context([target], pack, budget)
    assert pack == before
    return result


def test_v4_budgeted_context_keeps_the_actual_middle_hit_not_import_prefix():
    source = focused_slice()
    text, facts, unchecked, delivered = render(source)
    assert any(fact.line == 32 and 'load("resources/new.json")' in fact.text for fact in facts)
    assert facts[0].line > 20 and facts[-1].line < 44
    assert [fact.line for fact in facts] == list(range(facts[0].line, facts[-1].line + 1))
    saved = next(iter(delivered.units.values())).slices[0]
    assert (saved.start_line, saved.end_line) == (facts[0].line, facts[-1].line)
    assert saved.focus_line == 32 and saved.sha == source.sha
    assert saved.text.splitlines() == source.text.splitlines()[saved.start_line - 20 : saved.end_line - 19]
    assert f"consumer.py:{saved.start_line}-{saved.end_line}" in text
    assert any("before focused window" in row for row in unchecked)
    assert any("after focused window" in row for row in unchecked)
    assert len(text) <= 700 and delivered.protocol_version == 4


@pytest.mark.parametrize("protocol", [1, 2, 3])
def test_older_protocols_preserve_the_exact_prefix_behavior(protocol):
    focused = render(focused_slice(), protocol=protocol)
    original = render(focused_slice(focus=None), protocol=protocol)
    assert focused[:3] == original[:3]
    assert focused[1][0].line == 20 and focused[1][-1].line < 32


@pytest.mark.parametrize("focus", [None, True, 0, 19, 45, "32"])
def test_missing_or_invalid_focus_cannot_select_uncollected_source(focus):
    actual = render(focused_slice(focus=focus))
    original = render(focused_slice(focus=None))
    assert actual[:3] == original[:3]


def test_oversize_focus_line_is_omitted_whole_without_falling_back_to_unrelated_prefix():
    source = focused_slice()
    lines = source.text.splitlines()
    lines[12] = 'load("' + "x" * 1000 + '");'
    text, facts, unchecked, delivered = render(replace(source, text="\n".join(lines)), budget=500)
    assert text == "" and not facts
    assert not next(iter(delivered.units.values())).slices
    assert any("focus line 32" in row for row in unchecked)


def test_focus_does_not_change_source_when_the_whole_slice_fits():
    source = focused_slice()
    focused = render(source, budget=10000)
    prefix = render(replace(source, focus_line=None), budget=10000)
    assert focused[:3] == prefix[:3]


def test_distinct_focus_in_identical_source_never_reuses_a_cropped_window():
    first, second = unit("first.py"), unit("second.py")
    source = focused_slice(focus=23)
    pack = ContextPack(
        units={
            first.id: UnitContext(first.id, [source]),
            second.id: UnitContext(second.id, [replace(source, focus_line=41)]),
        },
        protocol_version=4,
    )
    text, facts, unchecked, delivered = _context([first, second], pack, 1400)
    first_lines = {fact.line for fact in facts if fact.unit_id == first.id}
    second_lines = {fact.line for fact in facts if fact.unit_id == second.id}
    assert 23 in first_lines and 41 in second_lines
    assert 41 not in first_lines and 23 not in second_lines
    assert "Same delivered source as" not in text and len(text) <= 1400
    assert delivered.units[first.id].slices[0].focus_line == 23
    assert delivered.units[second.id].slices[0].focus_line == 41
    assert any(first.id in row and "after focused window" in row for row in unchecked)
    assert any(second.id in row and "before focused window" in row for row in unchecked)


def test_distinct_focus_can_share_only_fully_delivered_identical_source():
    first, second = unit("first.py"), unit("second.py")
    source = focused_slice(focus=23)
    pack = ContextPack(
        units={
            first.id: UnitContext(first.id, [source]),
            second.id: UnitContext(second.id, [replace(source, focus_line=41)]),
        },
        protocol_version=4,
    )
    text, facts, unchecked, delivered = _context([first, second], pack, 20000)
    for target in (first, second):
        assert {fact.line for fact in facts if fact.unit_id == target.id} == set(range(20, 45))
        assert delivered.units[target.id].slices[0].text == source.text
    assert "Same delivered source as" in text and not unchecked


def test_structural_grep_hit_is_marked_only_inside_saved_source():
    workspace = Workspace(updated=True)
    workspace.files["app.py"] = "\n".join(["# import preamble"] * 31 + ['load("resources/new.json")'] + ["# tail"] * 12)
    state, changes, pack, target = inputs(workspace=workspace, protocol=4)
    reference = next(source for source in pack.units[target.id].slices if source.kind == "reference")
    assert pack.protocol_version == 4 and reference.focus_line == 32
    block = plan_discovery(
        changes,
        pack,
        {},
        limits=DiscoveryLimits(context_max_chars=700),
        file_changes={
            target.path: state.file_changes[target.path],
        },
    )[0]
    consumer = [source for source in block.facts if source.kind == "reference" and source.line == 32]
    assert len(consumer) == 1
    fact = {"side": "context", "path": consumer[0].path, "line": 32, "excerpt": consumer[0].text}
    assert block.structural_source_matches(target.id, fact, consumer=True)
    assert len(block.context_text) <= 700


def test_caller_lookup_focus_is_a_collected_source_line_and_old_slice_shape_still_loads():
    pack = ContextPack.build(_changeset(), _Workspace(), protocol_version=4)
    caller = next(source for source in pack.units["unit-service"].slices if source.kind == "caller")
    assert caller.focus_line == 2 and caller.start_line <= 2 <= caller.end_line
    assert "service.get_user" in caller.text.splitlines()[2 - caller.start_line]
    old = ContextSlice("caller", "old.py", 1, 1, "run", "run()", "caller", "head")
    assert old.focus_line is None and ContextPack().protocol_version == 1


def test_split_preserves_focused_delivered_lines_without_restoring_omitted_source():
    first, second = unit("first.py"), unit("second.py")
    source = focused_slice()
    pack = ContextPack(
        units={target.id: UnitContext(target.id, [source]) for target in (first, second)},
        protocol_version=4,
    )
    limits = DiscoveryLimits(context_max_chars=1400, global_context_max_chars=1400)
    block = plan_discovery(changeset(first, second), pack, {first.path: diff(), second.path: diff()}, limits=limits)[0]
    assert any(fact.line == 32 and fact.side == "context" for fact in block.facts)
    children = split_discovery(block, limits)
    assert len(children) == 2
    for child in children:
        ids = {target.id for target in child.units}
        expected = {
            (f.path, f.line, f.text, f.unit_id) for f in block.facts if f.side == "context" and f.unit_id in ids
        }
        actual = {(f.path, f.line, f.text, f.unit_id) for f in child.facts if f.side == "context"}
        assert actual == expected and child.delivered_context.protocol_version == 4
        assert any("before focused window" in row for row in child.unchecked)
