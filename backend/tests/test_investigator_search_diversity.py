from __future__ import annotations

import pytest

from reviewforge.core.state import StateStore
from reviewforge.engine.investigator import Investigator, build_workspace_executor
from reviewforge.tools.workspace import PRHeadWorkspace, WorkspaceInfo


@pytest.fixture
def workspace(tmp_path):
    calls = "        Gate.configure(true); // " + "repeated test setup " * 6 + "\n"
    sources = {
        "a/ManyTests.java": "package tests;\nimport core.Gate;\npublic class ManyTests {\n"
        "    public void check() {\n" + calls * 15 + "    }\n}\n",
        "core/Gate.java": "package core;\npublic class Gate {\n"
        "    public static void configure(boolean enabled) {}\n}\n",
        "z/Entry.java": "package cli;\nimport core.Gate;\npublic class Entry {\n"
        "    public void run() {\n        Gate.configure(true);\n    }\n}\n",
        "z/Wrapper.java": "package cli;\nimport core.Gate;\npublic class Wrapper {\n"
        "    public void run() {\n        Gate.configure(false);\n    }\n}\n",
    }
    for name, source in sources.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
    info = WorkspaceInfo("o/r", "o/r", "head", tmp_path, len(sources), 0, "digest", False, "tarball")
    yield PRHeadWorkspace(info, None, fallback_repo="o/r", temp_dir=tmp_path)


@pytest.mark.parametrize("tool", ["grep", "qualified_callers", "callers"])
def test_diverse_search_keeps_later_files_with_the_same_hit_limit(workspace, tool):
    def search(diverse):
        if tool == "grep":
            return workspace.grep(r"Gate\.configure", globs=None, max_hits=10, context=1, diverse=diverse)
        return workspace.find_callers(
            "core.Gate.configure" if tool == "qualified_callers" else "configure",
            language="java",
            max_hits=10,
            diverse=diverse,
        )

    original = search(False)
    assert len(original) == 10 and {hit.path for hit in original} == {"a/ManyTests.java"}
    balanced = search(True)
    assert len(balanced) == 10
    assert [(hit.path, hit.line) for hit in balanced[:3]] == [
        ("a/ManyTests.java", 5),
        ("z/Entry.java", 5),
        ("z/Wrapper.java", 5),
    ]
    assert [(hit.path, hit.line) for hit in balanced[3:]] == [("a/ManyTests.java", number) for number in range(6, 13)]
    assert "Gate.configure(true)" in balanced[1].text
    if tool == "grep":
        assert "public void run" in balanced[1].context
    balanced.clear()
    assert search(True) and search(False) == original


def test_diverse_grep_preserves_explicit_scope_and_single_file_results(workspace):
    old = workspace.grep("configure", globs=["a/*.java"], max_hits=5)
    assert workspace.grep("configure", globs=["a/*.java"], max_hits=5, diverse=True) == old
    assert workspace.grep("configure", globs=None, max_hits=0, diverse=True) == []
    assert workspace.find_callers("configure", language="java", max_hits=0, diverse=True) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["grep", "find_callers"])
async def test_investigator_saves_distinct_paths_before_repeated_hits(workspace, tool):
    state = StateStore(repo="o/r", pr_number=1, head_sha="head")
    worker = Investigator(None, build_workspace_executor(workspace, state))
    worker._state = state
    args = {"pattern": r"Gate\.configure"} if tool == "grep" else {"symbol": "core.Gate.configure"}
    view = await worker._run_tool(tool, args)
    observation = worker._observations[-1]
    assert observation.sha == "head" and observation.status == "success"
    assert len(observation.excerpt) == 1200
    assert "z/Entry.java:5:         Gate.configure(true);" in observation.excerpt
    assert "z/Wrapper.java:5:         Gate.configure(false);" in view
    assert "omitted source is not shown" in view
    # A normal follow-up read is focused by an actual saved hit, not by a
    # guessed production/test classifier or text outside the saved excerpt.
    await worker._run_tool("read_file", {"path": "z/Entry.java"})
    assert worker._observations[-1].line_range == (2, 13)
    assert "public void run()" in worker._observations[-1].excerpt


def test_diverse_search_stops_after_the_first_hit_in_enough_distinct_files(workspace, monkeypatch):
    read = workspace._read_local
    paths = []

    def counted(path, candidate):
        paths.append(path)
        return read(path, candidate)

    monkeypatch.setattr(workspace, "_read_local", counted)
    hits = workspace.grep(r"Gate\.configure", globs=None, max_hits=2, diverse=True)
    assert [hit.path for hit in hits] == ["a/ManyTests.java", "z/Entry.java"]
    assert "z/Wrapper.java" not in paths
