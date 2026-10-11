from __future__ import annotations

import pytest
from test_java_navigation import _archive
from test_workspace import _state, _TarballGitHub

from reviewforge.core.specs import build_registry
from reviewforge.engine import symbol_extractor
from reviewforge.engine.context_engine import ContextEngine
from reviewforge.engine.context_pack import ContextPack
from reviewforge.engine.declarations_v4 import compile_changeset_v4, extract_code_definitions
from reviewforge.engine.java_navigation import JavaSource
from reviewforge.engine.semantic_diff import SemanticChangeSet, SemanticUnit, UnitKind
from reviewforge.tools.gateway import ToolGateway
from reviewforge.tools.workspace import PRHeadWorkspace

SOURCES = {
    "engine/Launcher.java": """package engine;
public class Launcher {
    public void terminate(int code) {
        if (code != 0) {
            System.exit(code);
        }
    }
}
""",
    "base/Base.java": """package base;
import engine.Launcher;
public abstract class Base {
    protected Launcher launcher;
}
""",
    "app/Middle.java": """package app;
import base.Base;
public abstract class Middle extends Base {
}
""",
    "app/Command.java": """package app;
public class Command extends Middle {
    public void run() {
        launcher.terminate(3);
    }
}
""",
    "other/Launcher.java": """package other;
public class Launcher {
    public void terminate(int code) { throw new AssertionError(code); }
}
""",
}


def test_v4_interface_declaration_is_real_code_and_keeps_legacy_extraction():
    source = """// interface Commented { }
public interface Extra {
    String example = "interface Literal { }";
    static int TOKEN = 1;
}
"""
    assert symbol_extractor.extract_definitions(source, "Extra.java") == []
    declarations = extract_code_definitions(source, "Extra.java")
    assert [(d.name, d.symbol_type, d.line, d.end_line) for d in declarations] == [("Extra", "class", 2, 5)]


def test_interface_fields_are_implicitly_public_final_and_parameters_are_not_members():
    source = """package app;
public interface Extra {
    static int TOKEN = 1;
    void process(Launcher launcher);
}
"""
    navigation = JavaSource(source, "app/Extra.java")
    assert [(f.name, f.access, f.mutable_static) for f in navigation.field_declarations] == [("TOKEN", "public", False)]


async def _pack(sources, *, max_slices=12, receiver_override=None):
    source = sources["app/Command.java"]
    number = next(i for i, line in enumerate(source.splitlines(), 1) if ".terminate(" in line)
    receiver = "this.launcher" if "this.launcher.terminate" in source else "launcher"
    receiver = receiver_override or receiver
    changeset = SemanticChangeSet(
        repo="owner/repo",
        pr_number=42,
        head_sha="head-sha",
        units=[
            SemanticUnit(
                id="command",
                path="app/Command.java",
                language="java",
                kind=UnitKind.SYMBOL,
                symbol="run",
                start_line=number - 1,
                end_line=number + 1,
                added_lines=[number],
                calls=[{"callee": "terminate", "receiver": receiver, "line": number}],
            )
        ],
    )
    workspace = await PRHeadWorkspace.build(
        _state(files_changed=["app/Command.java"]), _TarballGitHub(_archive(sources))
    )
    try:
        return ContextPack.build(changeset, workspace, max_slices=max_slices)
    finally:
        workspace.cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit_this", [False, True])
async def test_inherited_declared_receiver_delivers_exact_callee_and_binding(explicit_this):
    sources = dict(SOURCES)
    if explicit_this:
        sources["app/Command.java"] = sources["app/Command.java"].replace(
            "launcher.terminate", "this.launcher.terminate"
        )
    pack = await _pack(sources)
    slices = pack.units["command"].slices
    callees = [s for s in slices if s.kind == "callee"]
    assert len(callees) == 1 and callees[0].path == "engine/Launcher.java"
    assert "System.exit(code)" in callees[0].text
    assert "runtime dispatch unproved" in callees[0].reason
    bindings = [s for s in slices if s.kind == "field_usage" and s.symbol == "base.Base.launcher"]
    assert len(bindings) == 1 and "protected Launcher launcher" in bindings[0].text
    assert "not evidence" in bindings[0].reason
    assert not any(s.path == "other/Launcher.java" for s in slices)
    assert len(slices) <= 12 and len(pack.render_all(max_chars=40000)) <= 40000
    assert pack.render_all(max_chars=40000) == (await _pack(sources)).render_all(max_chars=40000)


@pytest.mark.asyncio
async def test_local_receiver_shadowing_keeps_its_actual_declared_type():
    sources = dict(SOURCES)
    sources["app/Command.java"] = """package app;
public class Command extends Middle {
    public void run(other.Launcher launcher) {
        launcher.terminate(3);
    }
}
"""
    pack = await _pack(sources)
    assert [s.path for s in pack.units["command"].slices if s.kind == "callee"] == ["other/Launcher.java"]
    assert not any(s.symbol == "base.Base.launcher" for s in pack.units["command"].slices)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "private",
        "package",
        "generic",
        "missing",
        "ambiguous",
        "cycle",
        "too_deep",
        "hidden",
        "local_unknown",
        "package_reentry",
        "interface",
        "raw_generic",
    ],
)
async def test_unproved_inherited_receiver_stays_unchecked(case):
    sources = dict(SOURCES)
    if case in {"private", "package"}:
        sources["base/Base.java"] = sources["base/Base.java"].replace(
            "protected Launcher", "private Launcher" if case == "private" else "Launcher"
        )
    elif case == "generic":
        sources["app/Middle.java"] = sources["app/Middle.java"].replace("Middle extends", "Middle<T> extends")
    elif case == "missing":
        del sources["base/Base.java"]
    elif case == "ambiguous":
        sources["duplicate/Base.java"] = sources["base/Base.java"]
    elif case == "cycle":
        sources["app/Middle.java"] = "package app;\npublic class Middle extends Command {}\n"
    elif case == "hidden":
        sources["app/Middle.java"] = sources["app/Middle.java"].replace(
            "extends Base {", "extends Base {\n    private other.Launcher launcher;"
        )
    elif case == "local_unknown":
        sources["app/Command.java"] = sources["app/Command.java"].replace("void run()", "void run(Unknown launcher)")
    elif case == "package_reentry":
        sources["base/Base.java"] = sources["base/Base.java"].replace("protected Launcher", "Launcher")
        sources["app/Command.java"] = sources["app/Command.java"].replace(
            "package app;", "package base;\nimport app.Middle;"
        )
    elif case == "interface":
        sources["app/Command.java"] = sources["app/Command.java"].replace(
            "extends Middle", "extends Middle implements Extra"
        )
        sources["app/Extra.java"] = "package app;\npublic interface Extra { other.Launcher launcher = null; }\n"
    elif case == "raw_generic":
        sources["base/Base.java"] = (
            sources["base/Base.java"]
            .replace("class Base", "class Base<T>")
            .replace("protected Launcher launcher", "protected T launcher")
        )
        sources["base/T.java"] = "package base;\npublic class T { public void terminate(int code) {} }\n"
    else:
        sources["app/Middle.java"] = "package app;\npublic class Middle extends Second {}\n"
        for name, parent in [("Second", "Third"), ("Third", "Fourth"), ("Fourth", "base.Base")]:
            sources[f"app/{name}.java"] = f"package app;\npublic class {name} extends {parent} {{}}\n"
    pack = await _pack(sources)
    assert not any(s.kind == "callee" for s in pack.units["command"].slices)
    assert "callee" in pack.units["command"].truncated_kinds


@pytest.mark.asyncio
async def test_inherited_binding_obeys_existing_slice_limit():
    pack = await _pack(SOURCES, max_slices=1)
    assert len(pack.units["command"].slices) == 1
    assert pack.units["command"].slices[0].kind == "callee"
    assert "field_usage" in pack.units["command"].truncated_kinds


@pytest.mark.asyncio
async def test_same_package_member_can_supply_the_declared_receiver():
    sources = dict(SOURCES)
    sources["app/Base.java"] = (
        sources.pop("base/Base.java").replace("package base;", "package app;").replace("protected Launcher", "Launcher")
    )
    sources["app/Middle.java"] = sources["app/Middle.java"].replace("import base.Base;\n", "")
    pack = await _pack(sources)
    assert [s.path for s in pack.units["command"].slices if s.kind == "callee"] == ["engine/Launcher.java"]
    assert any(s.symbol == "app.Base.launcher" for s in pack.units["command"].slices if s.kind == "field_usage")


@pytest.mark.asyncio
async def test_inherited_method_dispatch_is_not_resolved_as_a_field():
    sources = dict(SOURCES)
    sources["app/Command.java"] = sources["app/Command.java"].replace("launcher.terminate", "super.terminate")
    pack = await _pack(sources, receiver_override="super")
    assert not any(s.kind == "callee" for s in pack.units["command"].slices)
    assert "callee" in pack.units["command"].truncated_kinds


@pytest.mark.asyncio
async def test_unknown_interface_keeps_a_declared_candidate_without_claiming_resolution():
    sources = dict(SOURCES)
    sources["app/Middle.java"] = sources["app/Middle.java"].replace("extends Base", "extends Base implements Runnable")
    pack = await _pack(sources)
    callees = [s for s in pack.units["command"].slices if s.kind == "callee"]
    assert [s.path for s in callees] == ["engine/Launcher.java"]
    assert "candidate" in callees[0].reason and "interface members unchecked" in callees[0].reason
    assert "runtime dispatch unproved" in callees[0].reason
    assert "callee" in pack.units["command"].truncated_kinds
    assert any(s.symbol == "base.Base.launcher" for s in pack.units["command"].slices)


@pytest.mark.asyncio
async def test_manifest_delivers_the_inherited_receiver_through_to_context():
    patch = "@@ -3,3 +3,3 @@\n     public void run() {\n-        return;\n+        launcher.terminate(3);\n     }"
    state = _state(files_changed=["app/Command.java"])
    state.file_diffs = {"app/Command.java": patch}
    gateway = ToolGateway(build_registry(), _TarballGitHub(_archive(SOURCES)), pipeline_mode="hypothesis")
    try:
        await ContextEngine(gateway, v4_declarations=True).build(state)
        changeset = compile_changeset_v4(state)
        workspace = await gateway.workspace_for(state)
        pack = ContextPack.build(changeset, workspace)
        unit = next(unit for unit in changeset.units if unit.symbol == "run")
        assert any(call["receiver"] == "launcher" for call in unit.calls)
        assert [s.path for s in pack.units[unit.id].slices if s.kind == "callee"] == ["engine/Launcher.java"]
        assert not state.findings
    finally:
        await gateway.cleanup_workspace(state)
