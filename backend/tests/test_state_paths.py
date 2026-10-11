from __future__ import annotations

import pytest
from test_java_navigation import SOURCES, _archive, _changeset
from test_workspace import _state, _TarballGitHub

from reviewforge.engine.context_pack import ContextPack
from reviewforge.engine.java_navigation import JavaSource
from reviewforge.engine.state_paths import MAX_PACK_QUERIES, MAX_PATH_CHARS, MAX_UNIT_QUERIES, StatePaths
from reviewforge.tools.workspace import PRHeadWorkspace

CHAIN = {
    "cli/Bootstrap.java": """package cli;
import core.Gate;
class Bootstrap {
    static void apply() { Gate.configure(true); }
}
""",
    "cli/Mappers.java": """package cli;
class Mappers {
    private static Inner WORK = new Inner();
    static void sanitize() { WORK.sanitize(); }
    static class Inner {
        void sanitize() { Bootstrap.apply(); }
    }
}
""",
    "cli/Parser.java": """package cli;
class Parser {
    static void parse() { Mappers.sanitize(); }
}
""",
    "cli/Launcher.java": """package cli;
class Launcher {
    public static void main(String[] args) { Parser.parse(); }
}
""",
    "runtime/Recorder.java": """package runtime;
import core.Gate;
class Recorder {
    void record() { Gate.init(true); }
}
""",
    "aaa/Setup.java": """package aaa;
import core.Gate;
class Setup {
    void setup() { Gate.configure(false); }
}
""",
}


@pytest.fixture
async def chain_workspace():
    workspace = await PRHeadWorkspace.build(
        _state(files_changed=["cli/Command.java"]), _TarballGitHub(_archive({**SOURCES, **CHAIN}))
    )
    try:
        yield workspace
    finally:
        workspace.cleanup()


@pytest.mark.asyncio
async def test_five_hop_state_context_keeps_nested_wrapper_and_alternative_producer(chain_workspace):
    pack = ContextPack.build(_changeset({"callee": "isEnabled", "receiver": "Gate", "line": 5}), chain_workspace)
    callee = next(s for s in pack.units["cmd"].slices if s.kind == "callee")
    assert "State caller navigation (not evidence;" in callee.reason
    for symbol in ("cli.Bootstrap.apply", "cli.Mappers.Inner.sanitize", "cli.Mappers.sanitize", "cli.Parser.parse"):
        assert symbol in callee.reason
    assert "cli/Launcher.java:3 (cli.Launcher.main)" in callee.reason
    assert "runtime/Recorder.java:4 (runtime.Recorder.record)" in callee.reason
    assert "aaa.Setup.setup" in callee.reason  # A second sampled path, not a claim of complete coverage.
    assert "other callers/mutations unchecked" in callee.reason and "order unproved" in callee.reason
    assert "caller" in pack.units["cmd"].truncated_kinds
    assert len(pack.units["cmd"].slices) <= 12
    assert all(s.end_line - s.start_line < 60 for s in pack.units["cmd"].slices)
    assert len(pack.render_all(max_chars=40_000)) <= 40_000
    assert "obs_" not in pack.render_all(max_chars=40_000)


@pytest.mark.asyncio
async def test_paths_recheck_source_hits_and_break_cycles_without_proving_no_callers(chain_workspace, monkeypatch):
    real = chain_workspace.find_callers

    def callers(symbol, **kwargs):
        if symbol == "cli.Parser.parse":
            # Unrelated same-name source must not become an edge even when a tool supplies it.
            return [{"path": "other/Calls.java", "line": 3}]
        return real(symbol, **kwargs)

    monkeypatch.setattr(chain_workspace, "find_callers", callers)
    pack = ContextPack.build(_changeset({"callee": "isEnabled", "receiver": "Gate", "line": 5}), chain_workspace)
    reason = next(s.reason for s in pack.units["cmd"].slices if s.kind == "callee")
    assert "cli.Parser.parse" in reason and "cli.Launcher.main" not in reason
    assert "other.Calls.execute" not in reason
    assert "unchecked" in reason and "caller" in pack.units["cmd"].truncated_kinds


@pytest.mark.asyncio
async def test_nearer_dead_end_does_not_hide_five_hop_entry_path():
    # The lexically earlier, equally near source is a valid call, but its
    # upstream path ends away from the command. A single greedy path loses
    # the entry even though the caller tool can resolve every wrapper.
    sources = {
        **SOURCES,
        **CHAIN,
        "cli/Aardvark.java": """package cli;
import core.Gate;
class Aardvark {
    static void setup() { Gate.configure(false); }
}
""",
    }
    workspace = await PRHeadWorkspace.build(
        _state(files_changed=["cli/Command.java"]), _TarballGitHub(_archive(sources))
    )
    try:
        pack = ContextPack.build(_changeset({"callee": "isEnabled", "receiver": "Gate", "line": 5}), workspace)
        reason = next(s.reason for s in pack.units["cmd"].slices if s.kind == "callee")
        assert "cli.Aardvark.setup" in reason
        assert "cli/Launcher.java:3 (cli.Launcher.main)" in reason
        assert "cli.Mappers.Inner.sanitize" in reason
        assert "runtime.Recorder.record" in reason
    finally:
        workspace.cleanup()


def _reader(sources):
    cache = {path: JavaSource(source, path) for path, source in sources.items()}
    return cache.get


def test_assignment_seeds_are_code_locations_not_init_names_or_shadowed_locals():
    source = """package core;
class Gate {
    static Gate CURRENT;
    static boolean read() { return CURRENT != null; }
    static void alpha() { CURRENT = new Gate(); }
    static void init() { System.out.println("CURRENT = null"); }
    static void beta() { Gate CURRENT = null; CURRENT = new Gate(); }
    static void gamma() { Gate.CURRENT = null; }
}
"""
    read_java = _reader({"core/Gate.java": source})
    navigation = read_java("core/Gate.java")
    queried = []

    class Workspace:
        def find_callers(self, symbol, **kwargs):
            queried.append(symbol)
            return []

    paths = StatePaths(Workspace(), read_java, 4)
    text, unchecked = paths.navigation(navigation, navigation.definition_at("read", 4), "cli/C.java")
    assert queried == ["core.Gate.alpha", "core.Gate.gamma"]
    assert text == "" and unchecked  # A miss is not a refutation.


@pytest.mark.asyncio
async def test_per_pack_cache_is_deterministic_and_unit_bound_applies_across_callees(chain_workspace, monkeypatch):
    queried = []
    real = chain_workspace.find_callers

    def callers(symbol, **kwargs):
        queried.append((symbol, kwargs))
        return real(symbol, **kwargs)

    monkeypatch.setattr(chain_workspace, "find_callers", callers)

    def reader(path):
        return JavaSource(chain_workspace.read(path), path)

    paths = StatePaths(chain_workspace, reader, 4)
    source = reader("core/Gate.java")
    first = paths.navigation(source, next(d for d in source.functions if d.name == "isEnabled"), "cli/Command.java")
    queries = len(queried)
    assert (
        paths.navigation(source, next(d for d in source.functions if d.name == "getInstance"), "cli/Command.java")
        == first
    )
    assert len(queried) == queries
    paths.begin_unit()
    assert (
        paths.navigation(source, next(d for d in source.functions if d.name == "isEnabled"), "cli/Command.java")
        == first
    )
    assert len(queried) == queries
    assert queries <= MAX_UNIT_QUERIES
    assert all(options == {"language": "java", "max_hits": 12, "diverse": True} for _, options in queried)
    # Distinct anchors/callees share the unit expansion ceiling too.
    for index in range(20):
        paths.navigation(source, next(d for d in source.functions if d.name == "isEnabled"), f"different{index}/C.java")
    assert paths._unit_queries <= MAX_UNIT_QUERIES
    assert len(queried) <= MAX_PACK_QUERIES


def test_query_and_metadata_limits_stop_long_or_cyclic_graphs():
    sources = {"core/Gate.java": SOURCES["core/Gate.java"]}
    for index in range(80):
        target = "Gate.configure" if index == 0 else f"C{index - 1}.call"
        sources[f"cli/C{index}.java"] = (
            f"package cli;\nimport core.Gate;\nclass C{index} {{\n    static void call() {{ {target}(true); }}\n}}\n"
        )
    reader = _reader(sources)
    queried = []

    class Workspace:
        def find_callers(self, symbol, **kwargs):
            queried.append(symbol)
            if symbol == "core.Gate.configure":
                return [{"path": "cli/C0.java", "line": 4}]
            if symbol.startswith("cli.C"):
                index = int(symbol.removeprefix("cli.C").removesuffix(".call")) + 1
                return [{"path": f"cli/C{index}.java", "line": 4}]
            return []

    source = reader("core/Gate.java")
    paths = StatePaths(Workspace(), reader, 4)
    text, unchecked = paths.navigation(
        source, next(d for d in source.functions if d.name == "isEnabled"), "cli/Command.java"
    )
    assert "cli.C4.call" in text and "cli.C5.call" not in text
    assert len(text) <= MAX_PATH_CHARS and unchecked
    assert len(queried) <= MAX_UNIT_QUERIES
    # Cache does not grow beyond the bound across many unrelated seeds.
    for index in range(40):
        paths.begin_unit()
        other = JavaSource(SOURCES["core/Gate.java"].replace("core", f"p{index}"), "X.java")
        paths.navigation(other, next(d for d in other.functions if d.name == "isEnabled"), "cli/Command.java")
    assert len(queried) <= MAX_PACK_QUERIES


def test_cycles_do_not_revisit_a_method_or_claim_an_entrypoint():
    gate = SOURCES["core/Gate.java"].replace(
        "CURRENT = new Gate(enabled);", "CURRENT = new Gate(enabled); cli.Cycle.call();", 1
    )
    reader = _reader(
        {
            "core/Gate.java": gate,
            "cli/Cycle.java": "package cli;\nimport core.Gate;\nclass Cycle {\n"
            "    static void call() { Gate.configure(true); }\n}\n",
        }
    )
    queried = []

    class Workspace:
        def find_callers(self, symbol, **kwargs):
            queried.append(symbol)
            if symbol == "core.Gate.configure":
                return [{"path": "cli/Cycle.java", "line": 4}]
            if symbol == "cli.Cycle.call":
                return [{"path": "core/Gate.java", "line": 6}]
            return []

    source = reader("core/Gate.java")
    paths = StatePaths(Workspace(), reader, 4)
    text, unchecked = paths.navigation(
        source, next(d for d in source.functions if d.name == "isEnabled"), "cli/Command.java"
    )
    assert text.count("core.Gate.configure") == 1 and text.count("cli.Cycle.call") == 1
    assert queried.count("core.Gate.configure") == 1 and unchecked
    assert "entrypoint" not in text and "order unproved" in text


@pytest.mark.asyncio
async def test_path_metadata_is_subject_to_original_render_budget_and_disabled_callers(chain_workspace):
    changeset = _changeset({"callee": "isEnabled", "receiver": "Gate", "line": 5})
    pack = ContextPack.build(changeset, chain_workspace, max_slices=1)
    assert len(pack.units["cmd"].slices) == 1
    assert "callee" in pack.units["cmd"].truncated_kinds
    assert len(pack.render_all(max_chars=500)) <= 500
    pack = ContextPack.build(changeset, chain_workspace, max_callers=0)
    assert "State caller navigation" not in pack.render_all(max_chars=40_000)
