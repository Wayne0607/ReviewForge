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
    states = [s for s in slices if s.kind == "field_usage"]
    assert states and all(s.symbol == "core.Gate.CURRENT" and s.path == "core/Gate.java" for s in states)
    assert any("core.Gate.configure" in s.reason and "CURRENT = new Gate(enabled);" in s.text for s in states)
    assert any("core.Gate.init" in s.reason for s in states)
    assert any("core.Gate.reset" in s.reason and "CURRENT = null" in s.text for s in states)
    assert all("order unproved" in s.reason and s.end_line - s.start_line < 7 for s in states)
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
    assert "field_usage" in pack.units["cmd"].truncated_kinds
    assert len(pack.render_all(max_chars=500)) <= 500


@pytest.mark.asyncio
async def test_callee_state_source_is_isolated_by_unit_and_never_an_observation(java_workspace):
    from reviewforge.engine.hypothesis import Hypothesis, Mechanism, Site
    from reviewforge.engine.investigator import Investigator, build_workspace_executor

    changeset = _changeset({"callee": "isEnabled", "receiver": "Gate", "line": 5})
    changeset.units.append(
        SemanticUnit("other", "other/Calls.java", "java", UnitKind.SYMBOL, "execute", 3, 3, [3], calls=[])
    )
    pack = ContextPack.build(changeset, java_workspace)
    assert any(s.kind == "field_usage" for s in pack.units["cmd"].slices)
    assert not [s for s in pack.units["other"].slices if s.kind == "field_usage" and s.path == "core/Gate.java"]
    worker = Investigator(None, build_workspace_executor(java_workspace, _state()), changeset=changeset)
    hypothesis = Hypothesis(
        "h",
        "cmd::contract-mismatch::run",
        "cmd",
        Mechanism.CONTRACT_MISMATCH,
        "State is not initialized",
        "The command runs",
        "Incorrect feature flag",
        "Is it initialized?",
        "A caller configures it",
        [Site("cli/Command.java", 5, "Gate.isEnabled()")],
        "error",
        "generator",
    )
    assert "CURRENT = new Gate(enabled)" in worker._render_user(hypothesis, _state(), pack)
    result = worker._finalize(
        {
            "answer": "A configuration path exists",
            "assessment": {
                "expected": "Configured flag",
                "actual": "Configured flag",
                "expected_evidence": ["obs_0:e1"],
                "actual_evidence": ["obs_0:e1"],
                "comparison": "compatible",
            },
        },
        hypothesis,
        {"cli/Command.java"},
        steps=1,
    )
    assert result.verdict == "unknown" and result.reason == "ungrounded-assessment"


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


def test_java_fields_require_a_declaration_at_class_scope():
    source = """package core;
class Fields {
    private static volatile Fields CURRENT;
    private boolean ready;
    // private int COMMENT;
    private String example = "private int LITERAL;";
    static { int temporary = 1; }
    void run(boolean argument) {
        int local = 1;
        ready = true;
        if (ready) { return; }
        throw new IllegalStateException();
    }
    class Nested {
        private int nested;
    }
}
"""
    navigation = JavaSource(source, "Fields.java")
    fields = navigation.field_declarations
    assert {field.name for field in fields} == {"CURRENT", "ready", "example", "nested"}
    assert {field.name for field in fields if field.mutable_static} == {"CURRENT"}
    assert (
        navigation.class_name(next(field.owner for field in fields if field.name == "nested")) == "core.Fields.Nested"
    )


def test_inline_field_modifiers_do_not_leak_from_a_sibling_declaration():
    navigation = JavaSource(
        "class Fields { static int first; int second; static final int CONSTANT = 1; }", "Fields.java"
    )
    assert {field.name for field in navigation.field_declarations} == {"first", "second", "CONSTANT"}
    assert {field.name for field in navigation.field_declarations if field.mutable_static} == {"first"}


@pytest.mark.asyncio
async def test_java_context_does_not_treat_return_throw_or_local_assignments_as_fields(tmp_path):
    from reviewforge.core.state import StateStore
    from reviewforge.tools.workspace import WorkspaceInfo

    source = """class Command {
    void run() {
        int local = 1;
        local = 2;
        if (local == 2) { return; }
        throw failure;
    }
}
"""
    (tmp_path / "Command.java").write_text(source, encoding="utf-8")
    workspace = PRHeadWorkspace(
        WorkspaceInfo("o/r", "o/r", "head", tmp_path, 1, len(source), "d", False, "tarball"),
        None,
        fallback_repo="o/r",
        temp_dir=tmp_path,
    )
    state = StateStore(repo="o/r", pr_number=1, head_sha="head")
    changeset = SemanticChangeSet(
        repo=state.repo,
        head_sha=state.head_sha,
        units=[SemanticUnit("run", "Command.java", "java", UnitKind.SYMBOL, "run", 2, 7, [3, 4, 5, 6])],
    )
    pack = ContextPack.build(changeset, workspace)
    assert not [s for s in pack.units["run"].slices if s.kind == "field_usage"]


@pytest.mark.asyncio
async def test_java_context_keeps_real_field_uses_with_exact_case_and_owner(tmp_path):
    from reviewforge.tools.workspace import WorkspaceInfo

    source = """class Command {
    private boolean ready;
    void unrelated() {
        String example = "ready";
        // ready
        int Ready = 1;
    }
    void run() {
        if (ready) { return; }
    }
    class Nested {
        private boolean ready;
        void run() { if (ready) { return; } }
    }
}
"""
    (tmp_path / "Command.java").write_text(source, encoding="utf-8")
    workspace = PRHeadWorkspace(
        WorkspaceInfo("o/r", "o/r", "head", tmp_path, 1, len(source), "d", False, "tarball"),
        None,
        fallback_repo="o/r",
        temp_dir=tmp_path,
    )
    changeset = SemanticChangeSet(
        units=[SemanticUnit("run", "Command.java", "java", UnitKind.SYMBOL, "run", 8, 10, [9])]
    )
    pack = ContextPack.build(changeset, workspace)
    fields = [s for s in pack.units["run"].slices if s.kind == "field_usage"]
    assert [s.reason for s in fields] == ["uses ready at line 2", "uses ready at line 9"]
    assert all(s.symbol == "ready" for s in fields)


@pytest.mark.asyncio
async def test_java_context_literals_cannot_select_a_field(tmp_path):
    from reviewforge.tools.workspace import WorkspaceInfo

    source = 'class Command {\n    private int ready;\n    void run() { String text = "ready"; }\n}\n'
    (tmp_path / "Command.java").write_text(source, encoding="utf-8")
    workspace = PRHeadWorkspace(
        WorkspaceInfo("o/r", "o/r", "head", tmp_path, 1, len(source), "d", False, "tarball"),
        None,
        fallback_repo="o/r",
        temp_dir=tmp_path,
    )
    changeset = SemanticChangeSet(
        units=[SemanticUnit("run", "Command.java", "java", UnitKind.SYMBOL, "run", 3, 3, [3])]
    )
    assert not [s for s in ContextPack.build(changeset, workspace).units["run"].slices if s.kind == "field_usage"]


@pytest.mark.asyncio
async def test_annotated_unit_start_still_resolves_the_declared_caller(java_workspace):
    sources = {
        **SOURCES,
        "cli/Command.java": SOURCES["cli/Command.java"].replace(
            "    public void run() {", "    @Deprecated\n    public void run() {"
        ),
    }
    workspace = await PRHeadWorkspace.build(
        _state(files_changed=["cli/Command.java"]), _TarballGitHub(_archive(sources))
    )
    try:
        changeset = _changeset({"callee": "isEnabled", "receiver": "Gate", "line": 6})
        changeset.units[0].added_lines = [6]
        changeset.units[0].end_line = 7
        pack = ContextPack.build(changeset, workspace)
        assert [s.path for s in pack.units["cmd"].slices if s.kind == "caller"] == ["cli/Entry.java"]
    finally:
        workspace.cleanup()


@pytest.mark.asyncio
async def test_java_definition_queries_keep_filename_independence_and_query_history():
    sources = {
        "odd-name.java": "package app;\nclass Holder {\n    void run() {}\n}\n",
        "another-name.java": "package other;\nclass Holder {\n    void run() {}\n}\n",
        "noise.java": """package app;
class Noise {
    String text = "app.Holder.run";
    // class Holder { void run() {} }
}
""",
    }
    workspace = await PRHeadWorkspace.build(_state(), _TarballGitHub(_archive(sources)))
    try:
        for _ in range(2):
            assert workspace.find_symbol_definitions("app.Absent.run", language="java") == []
            exact = workspace.find_symbol_definitions("app.Holder.run", language="java")
            assert [(hit.path, hit.symbol, hit.line) for hit in exact] == [("odd-name.java", "run", 3)]
            short = workspace.find_symbol_definitions("Holder.run", language="java")
            assert {hit.path for hit in short} == {"odd-name.java", "another-name.java"}
            assert {hit.path for hit in workspace.find_symbol_definitions("run", language="java")} == {
                "odd-name.java",
                "another-name.java",
            }
            assert [
                (hit.path, hit.symbol) for hit in workspace.find_symbol_definitions("app.Holder", language="java")
            ] == [
                ("odd-name.java", "Holder"),
            ]
    finally:
        workspace.cleanup()
