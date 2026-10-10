from __future__ import annotations

import io
import tarfile

import pytest
from test_workspace import _state, _TarballGitHub

from reviewforge.core.specs import build_registry
from reviewforge.engine.context_engine import ContextEngine
from reviewforge.engine.context_pack import ContextPack
from reviewforge.engine.declarations_v4 import compile_changeset_v4
from reviewforge.engine.java_navigation import JavaSource, matches_qualified
from reviewforge.engine.semantic_diff import SemanticChangeSet, SemanticUnit, UnitKind
from reviewforge.tools.gateway import ToolGateway
from reviewforge.tools.workspace import PRHeadWorkspace

SOURCES = {
    "core/Gate.java": """package core;
public class Gate {
    private static volatile Gate CURRENT;
    private final boolean enabled;
    public static Gate configure(boolean enabled) {
        CURRENT = new Gate(enabled);
        return CURRENT;
    }
    public static Gate init(boolean enabled) {
        CURRENT = new Gate(enabled);
        return CURRENT;
    }
    private Gate(boolean enabled) { this.enabled = enabled; }
    public static Gate getInstance() { return CURRENT; }
    public static void reset() { CURRENT = null; }
    public static boolean isEnabled() { return getInstance().enabled; }
    public static class Nested {
        public static boolean isEnabled() { return false; }
    }
}
""",
    "other/Gate.java": """package other;
public class Gate {
    public static boolean isEnabled() { return false; }
}
""",
    "cli/Command.java": """package cli;
import core.Gate;
public class Command {
    public void run() {
        if (!Gate.isEnabled()) { throw new IllegalStateException(); }
    }
}
""",
    "cli/Entry.java": """package cli;
public class Entry {
    public void execute(Command command, Runnable other) {
        command.run();
        other.run();
        // command.run();
        String example = "command.run()";
    }
}
""",
    "other/Calls.java": """package other;
public class Calls {
    public void execute(Gate gate) { gate.isEnabled(); }
}
""",
}


def _archive(sources):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for path, source in sources.items():
            content = source.encode()
            info = tarfile.TarInfo("fixture/" + path)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    return output.getvalue()


@pytest.fixture
async def java_workspace():
    workspace = await PRHeadWorkspace.build(
        _state(files_changed=["cli/Command.java"]), _TarballGitHub(_archive(SOURCES))
    )
    try:
        yield workspace
    finally:
        workspace.cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize("symbol", ["core.Gate.isEnabled", "Gate.isEnabled"])
async def test_qualified_definition_excludes_other_owners(java_workspace, symbol):
    hits = java_workspace.find_symbol_definitions(symbol, language="java")
    expected = ["core/Gate.java"] if symbol.startswith("core.") else ["core/Gate.java", "other/Gate.java"]
    assert [hit.path for hit in hits] == expected
    assert all(
        hit.line
        == SOURCES[hit.path]
        .splitlines()
        .index("    public static boolean isEnabled() { return getInstance().enabled; }")
        + 1
        for hit in hits
        if hit.path == "core/Gate.java"
    )


@pytest.mark.asyncio
async def test_nested_owner_and_missing_qualified_definition_never_fall_back(java_workspace):
    hits = java_workspace.find_symbol_definitions("core.Gate.Nested.isEnabled", language="java")
    assert len(hits) == 1 and "return false" in hits[0].excerpt
    assert java_workspace.find_symbol_definitions("Missing.isEnabled", language="java") == []
    assert len(java_workspace.find_symbol_definitions("isEnabled", language="java")) == 3


@pytest.mark.asyncio
async def test_qualified_callers_bind_imports_and_declared_receiver_types(java_workspace):
    hits = java_workspace.find_callers("core.Gate.isEnabled", language="java", max_hits=10)
    assert [(hit.path, hit.line) for hit in hits] == [("cli/Command.java", 5)]
    hits = java_workspace.find_callers("cli.Command.run", language="java", max_hits=10)
    assert [(hit.path, hit.line) for hit in hits] == [("cli/Entry.java", 4)]
    assert java_workspace.find_callers("Missing.run", language="java", max_hits=10) == []


def _changeset(call):
    return SemanticChangeSet(
        repo="owner/repo",
        pr_number=42,
        head_sha="head-sha",
        units=[
            SemanticUnit(
                id="cmd",
                path="cli/Command.java",
                language="java",
                kind=UnitKind.SYMBOL,
                symbol="run",
                start_line=4,
                end_line=6,
                added_lines=[5],
                calls=[call],
            )
        ],
    )


@pytest.mark.asyncio
async def test_context_retains_receiver_identity_and_state_producer_navigation(java_workspace):
    call = {"callee": "isEnabled", "receiver": "Gate", "caller": "run", "line": 5}
    pack = ContextPack.build(_changeset(call), java_workspace)
    slices = pack.units["cmd"].slices
    assert [s.path for s in slices if s.kind == "caller"] == ["cli/Entry.java"]
    callees = [s for s in slices if s.kind == "callee"]
    assert len(callees) == 1 and callees[0].path == "core/Gate.java"
    assert "State navigation" in callees[0].reason
    assert "CURRENT" in callees[0].reason
    assert "configure" in callees[0].reason and "init" in callees[0].reason and "reset" in callees[0].reason
    assert "not evidence" in callees[0].reason
    assert len(slices) <= 12
    assert len(pack.render_all(max_chars=40_000)) <= 40_000
    assert pack.render_all(max_chars=40_000) == ContextPack.build(_changeset(call), java_workspace).render_all(
        max_chars=40_000
    )


@pytest.mark.asyncio
async def test_unresolved_receiver_is_reported_without_unrelated_callees(java_workspace):
    pack = ContextPack.build(
        _changeset({"callee": "isEnabled", "receiver": "unknown", "caller": "run", "line": 5}), java_workspace
    )
    assert not any(s.kind == "callee" for s in pack.units["cmd"].slices)
    assert "callee" in pack.units["cmd"].truncated_kinds


@pytest.mark.asyncio
async def test_inferred_context_metadata_cannot_create_an_observation(java_workspace):
    pack = ContextPack.build(_changeset({"callee": "isEnabled", "receiver": "Gate", "line": 5}), java_workspace)
    assert "obs_" not in pack.render_all(max_chars=40_000)


@pytest.mark.asyncio
@pytest.mark.parametrize("v4", [False, True])
async def test_manifest_to_context_preserves_receivers_only_for_v4(java_workspace, v4):
    class GitHub:
        async def get_file_content(self, repo, ref, path):
            assert ref == "head-sha"
            return SOURCES.get(path, "")

        async def search_code(self, repo, pattern, file_glob=""):
            return "No results"

    state = _state(
        files_changed=["cli/Command.java"],
        file_diffs={
            "cli/Command.java": (
                "@@ -4,3 +4,3 @@\n     public void run() {\n-        return;\n"
                "+        if (!Gate.isEnabled()) { throw new IllegalStateException(); }\n     }\n"
            )
        },
    )
    manifest = await ContextEngine(ToolGateway(build_registry(), GitHub()), v4_declarations=v4).build(state)
    call = next(c for c in manifest["files"][0]["calls"] if c["callee"] == "isEnabled")
    if not v4:
        assert call == {"caller": "run", "callee": "isEnabled", "line": 5}
        return
    assert call["receiver"] == "Gate"
    pack = ContextPack.build(compile_changeset_v4(state), java_workspace)
    slices = [s for context in pack.units.values() for s in context.slices]
    assert any(s.kind == "callee" and "configure" in s.reason for s in slices)
    assert not any(s.kind == "callee" and s.path == "other/Gate.java" for s in slices)


@pytest.mark.parametrize(
    "source,expected",
    [
        ("import core.Gate;\nclass C { void f() { Gate.check(); } }", "core.Gate.check"),
        ("import static core.Gate.check;\nclass C { void f() { check(); } }", "core.Gate.check"),
        ("import core.Gate;\nclass C { void f(Object Gate) { Gate.check(); } }", "Object.check"),
        ("import core.Gate;\nclass C { void f() { var Gate = factory(); Gate.check(); } }", None),
        ("class C { void f() { unknown.check(); } }", None),
        ("class C { void f() { super.check(); } }", None),
        ("class C { void f() { this.check(); } void check() {} }", "C.check"),
    ],
)
def test_source_bindings_never_use_an_unrelated_receiver_hint(source, expected):
    navigation = JavaSource(source, "C.java")
    call = next(c for c in navigation.calls if c.callee == "check")
    call.receiver_type = "UnrelatedHint"
    assert navigation.call_target(call) == expected


def test_receiver_names_in_sibling_methods_and_fields_are_scoped():
    source = """package app;
import core.Gate;
import other.Other;
class C {
    Gate value;
    void first(Gate value) { value.check(); }
    void second(Other value) { value.check(); this.value.check(); }
}
"""
    navigation = JavaSource(source, "C.java")
    targets = [navigation.call_target(c) for c in navigation.calls if c.callee == "check"]
    assert targets == ["core.Gate.check", "other.Other.check", "core.Gate.check"]


def test_qualification_uses_the_package_declaration_not_capitalization():
    navigation = JavaSource("package Capital;\nclass lower {\n    void check() {}\n}", "lower.java")
    definition = next(d for d in navigation.definitions if d.name == "check")
    qualified = navigation.qualified_definition(definition)
    assert qualified == "Capital.lower.check"
    assert matches_qualified(qualified, "lower.check", package=navigation.package)
    assert not matches_qualified("Capital.Outer.lower.check", "lower.check", package=navigation.package)


@pytest.mark.parametrize("limit", [1, 2, 3])
@pytest.mark.asyncio
async def test_navigation_obeys_the_existing_slice_budget(java_workspace, limit):
    pack = ContextPack.build(
        _changeset({"callee": "isEnabled", "receiver": "Gate", "line": 5}), java_workspace, max_slices=limit
    )
    assert len(pack.units["cmd"].slices) <= limit
    assert len(pack.render_all(max_chars=500)) <= 500


def test_state_navigation_masks_literals_and_excludes_nested_class_state():
    source = (
        SOURCES["core/Gate.java"]
        .replace(
            "    private final boolean enabled;",
            "    private final boolean enabled;\n    private static String UNRELATED;\n"
            '    public static void example() { String literal = "CURRENT = null;"; }',
        )
        .replace(
            "    public static class Nested {",
            "    public static class Nested {\n        private static Nested CURRENT;\n"
            "        public static void nestedConfigure() { CURRENT = null; }",
        )
    )
    navigation = JavaSource(source, "Gate.java")
    definition = next(d for d in navigation.definitions if d.name == "isEnabled")
    result = navigation.state_navigation(definition)
    assert "configure" in result and "init" in result and "reset" in result
    assert "UNRELATED" not in result and "example" not in result and "nestedConfigure" not in result
    assert len(navigation.state_navigation(definition, max_chars=110)) <= 110
