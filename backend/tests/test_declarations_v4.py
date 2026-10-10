from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from reviewforge.engine.symbol_extractor import extract_definitions


def test_v4_declarations_exclude_comments_and_constructor_calls():
    from reviewforge.engine.declarations_v4 import extract_code_definitions

    source = """/** class Phantom { }
     * Common marker interface; protocols have a subclass of this interface.
     */
    public class Real {
        public Real() { }
        public String run() {
            Real value = new Real();
            return of(value);
        }
        public String of(Real value) { return "class Fake { }"; }
    }
    """
    assert any(item.name == "Phantom" for item in extract_definitions(source, "Real.java"))
    assert any(item.name == "of" and item.line == 2 for item in extract_definitions(source, "Real.java"))
    definitions = extract_code_definitions(source, "Real.java")
    assert [(item.name, item.line) for item in definitions] == [("Real", 4), ("Real", 5), ("run", 6), ("of", 10)]


def test_overloads_have_separate_v4_units_and_preserve_all_right_lines():
    from reviewforge.engine.context_pack import ContextPack
    from reviewforge.engine.declarations_v4 import compile_changeset_v4
    from reviewforge.engine.semantic_diff import compile_semantic_changeset

    state = SimpleNamespace(
        repo="owner/repo",
        pr_number=1,
        head_sha="head",
        files_changed=["Real.java"],
        file_diffs={},
        impact_manifest={
            "files": [
                {
                    "path": "Real.java",
                    "changed_symbols": [
                        {"name": "run", "start_line": 5, "end_line": 8, "added_lines": [6]},
                        {"name": "run", "start_line": 10, "end_line": 13, "added_lines": [11]},
                    ],
                }
            ]
        },
    )
    legacy = compile_semantic_changeset(state)
    assert legacy.units[0].id == legacy.units[1].id
    with pytest.raises(ValueError, match="unique semantic unit IDs"):
        ContextPack.build(legacy, SimpleNamespace(source="api-fallback"))
    v4 = compile_changeset_v4(state)
    assert len({unit.id for unit in v4.units}) == 2
    assert [unit.added_lines for unit in v4.units] == [[6], [11]]
    assert [unit.id for unit in v4.units] == [unit.id for unit in compile_changeset_v4(state).units]
    assert len(ContextPack.build(v4, SimpleNamespace(source="api-fallback")).units) == 2
    assert [unit.id for unit in compile_semantic_changeset(state).units] == [unit.id for unit in legacy.units]


def test_v4_collapses_duplicate_manifest_rows_without_dropping_lines():
    from reviewforge.engine.declarations_v4 import compile_changeset_v4

    state = SimpleNamespace(
        repo="owner/repo",
        pr_number=1,
        head_sha="head",
        files_changed=["Real.java"],
        file_diffs={},
        impact_manifest={
            "files": [
                {
                    "path": "Real.java",
                    "changed_symbols": [
                        {"name": "run", "start_line": 5, "end_line": 8, "added_lines": [6]},
                        {"name": "run", "start_line": 5, "end_line": 8, "added_lines": [7]},
                    ],
                }
            ]
        },
    )
    units = compile_changeset_v4(state).units
    assert len(units) == 1
    assert units[0].added_lines == [6, 7]


async def test_v4_manifest_preserves_real_calls_and_legacy_behavior():
    from reviewforge.core.state import StateStore
    from reviewforge.engine.context_engine import ContextEngine

    source = (
        "public class Real {\n"
        "    /** class Phantom {} */\n"
        "    public String run() {\n"
        "        Real value = new Real();\n"
        "        return of(value);\n"
        "    }\n"
        "    public String of(Real value) { return null; }\n"
        "}\n"
    )
    state = StateStore(file_diffs={"Real.java": "@@ -5 +5 @@\n-        return null;\n+        return of(value);"})
    gateway = SimpleNamespace(invoke=AsyncMock(return_value=source))
    legacy = await ContextEngine(gateway)._inspect_file("Real.java", state)
    assert {"caller": "run", "callee": "of", "line": 5} in legacy.calls
    v4 = await ContextEngine(gateway, v4_declarations=True)._inspect_file("Real.java", state)
    assert {item["name"] for item in v4.changed_symbols} == {"Real", "run"}
    assert {"caller": "run", "callee": "of", "line": 5} in v4.calls
    assert (await ContextEngine(gateway)._inspect_file("Real.java", state)).calls == legacy.calls
