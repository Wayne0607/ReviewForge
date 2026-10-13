"""Display samples cannot decide which real changed calls v4 navigates."""

import copy
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from reviewforge.core.state import StateStore
from reviewforge.engine.context_engine import ContextEngine, render_impact_manifest
from reviewforge.engine.context_pack import ContextPack
from reviewforge.engine.declarations_v4 import compile_changeset_v4
from reviewforge.engine.semantic_diff import compile_semantic_changeset


def state_for(patch, symbols, *, path="service.ts"):
    return StateStore(
        repo="example/repo",
        pr_number=1,
        head_sha="head",
        files_changed=[path],
        file_diffs={path: patch},
        impact_manifest={
            "version": 2,
            "files": [{"path": path, "language": "typescript", "changed_symbols": symbols}],
        },
    )


@pytest.mark.asyncio
async def test_late_callee_receives_navigation_without_expanding_display_or_slice_budgets():
    lines = ["export const perform = async () => {"]
    lines.extend(f"  const local{number} = {number};" for number in range(1, 44))
    lines.extend(["  return allowAdmin() && allowOwner();", "};"])
    source = "\n".join(lines) + "\n"
    patch = f"@@ -0,0 +1,{len(lines)} @@\n" + "".join("+" + line + "\n" for line in lines)
    state = StateStore(
        repo="example/repo",
        pr_number=1,
        head_sha="head",
        files_changed=["service.ts"],
        file_diffs={"service.ts": patch},
    )
    item = await ContextEngine(
        SimpleNamespace(invoke=AsyncMock(return_value=source)), v4_declarations=True
    )._inspect_file("service.ts", state)
    state.impact_manifest = {"version": 2, "files": [asdict(item)]}
    before = copy.deepcopy(state.impact_manifest)
    display = render_impact_manifest(before)
    assert item.changed_symbols[0]["added_lines"] == list(range(1, 13))
    assert item.added_lines == list(range(1, 41))
    assert {call["callee"] for call in item.calls} >= {"allowAdmin", "allowOwner"}

    helpers = {
        "allowAdmin": "export function allowAdmin() {\n  return true;\n}\n",
        "allowOwner": "export function allowOwner() {\n  return false;\n}\n",
    }
    contents = {"service.ts": source} | {f"{name}.ts": text for name, text in helpers.items()}
    workspace = SimpleNamespace(
        head_sha="head",
        source="tarball",
        read=contents.get,
        find_callers=lambda *args, **kwargs: [],
        grep=lambda *args, **kwargs: [],
        find_symbol_definitions=lambda name, **kwargs: (
            [SimpleNamespace(path=f"{name}.ts", line=1, start_line=1, end_line=3, symbol=name)]
            if name in helpers
            else []
        ),
    )
    legacy = compile_semantic_changeset(state)
    current = compile_changeset_v4(state)
    assert legacy.units[0].added_lines == list(range(1, 13))
    assert current.units[0].added_lines == list(range(1, len(lines) + 1))
    assert current.units[0].id == legacy.units[0].id
    assert not any(s.kind == "callee" for c in ContextPack.build(legacy, workspace).units.values() for s in c.slices)
    pack = ContextPack.build(current, workspace)
    assert {s.symbol for c in pack.units.values() for s in c.slices if s.kind == "callee"} == set(helpers)
    assert all(len(c.slices) <= 12 for c in pack.units.values())
    assert all(s.end_line - s.start_line + 1 <= 60 for c in pack.units.values() for s in c.slices)
    assert state.impact_manifest == before and render_impact_manifest(state.impact_manifest) == display


def test_right_coordinates_respect_each_overlapping_class_and_method_scope():
    patch = "@@ -10,2 +10,3 @@\n unchanged\n-old\n+first\n+second\n@@ -40 +41 @@\n-old\n+third\n"
    symbols = [
        {"name": "Container", "type": "class", "start_line": 1, "end_line": 80, "added_lines": [11]},
        {"name": "first", "start_line": 10, "end_line": 20, "added_lines": [11]},
        {"name": "second", "start_line": 40, "end_line": 50, "added_lines": [41]},
    ]
    state = state_for(patch, symbols)
    by_name = {unit.symbol: unit for unit in compile_changeset_v4(state).units}
    assert by_name["Container"].added_lines == [11, 12, 41]
    assert by_name["first"].added_lines == [11, 12]
    assert by_name["second"].added_lines == [41]


def test_hunk_count_never_expands_into_unsaved_rows_or_imports_context_lines():
    state = state_for(
        "@@ -1,0 +900000,1000000 @@\n+one\n+two\n+three\n",
        [{"name": "long", "start_line": 1, "end_line": 2000000, "added_lines": [900000]}],
    )
    assert compile_changeset_v4(state).units[0].added_lines == [900000, 900001, 900002]


@pytest.mark.parametrize("patch", ["@@ -5 +5,0 @@\n-old\n", "not a diff\n+unmapped"])
def test_present_diff_does_not_reuse_stale_or_unmapped_display_anchors(patch):
    state = state_for(patch, [{"name": "run", "start_line": 1, "end_line": 20, "added_lines": [5]}])
    assert compile_changeset_v4(state).units[0].added_lines == []
    assert compile_semantic_changeset(state).units[0].added_lines == [5]


def test_missing_diff_old_state_preserves_only_its_known_anchors():
    state = state_for("", [{"name": "run", "start_line": 1, "end_line": 20, "added_lines": [5]}])
    state.file_diffs = {}
    assert compile_changeset_v4(state).units[0].added_lines == [5]


def test_unknown_symbol_extent_never_claims_whole_file_additions():
    state = state_for(
        "@@ -0,0 +1,3 @@\n+one\n+two\n+three\n",
        [{"name": "unknown", "start_line": 2, "end_line": 0, "added_lines": [2]}],
    )
    assert compile_changeset_v4(state).units[0].added_lines == [2]


def test_resource_unit_uses_only_real_diff_additions_beyond_display_cap():
    patch = "@@ -0,0 +1,60 @@\n" + "".join(f"+key{number}=value\n" for number in range(60))
    state = state_for(patch, [], path="settings.properties")
    state.impact_manifest["files"][0]["added_lines"] = list(range(1, 13))
    assert compile_changeset_v4(state).units[0].added_lines == list(range(1, 61))
