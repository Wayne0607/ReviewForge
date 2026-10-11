from __future__ import annotations

import pytest

from reviewforge.core.state import StateStore
from reviewforge.engine.context_pack import ContextPack, ContextSlice, UnitContext
from reviewforge.engine.hypothesis import Hypothesis, Mechanism, Observation, Site
from reviewforge.engine.investigator import Investigator, build_workspace_executor
from reviewforge.engine.semantic_diff import SemanticChangeSet, SemanticUnit, UnitKind
from reviewforge.tools.workspace import PRHeadWorkspace, WorkspaceInfo


def _worker(tmp_path, *, changeset=True):
    source = "# A long copyright header repeated before the implementation.\n" * 79
    source += "def transform(value):\n    return None\n"
    for path in ("a.py", "caller.py"):
        (tmp_path / path).write_text(source, encoding="utf-8")
    info = WorkspaceInfo("o/r", "o/r", "head", tmp_path, 2, len(source) * 2, "digest", False, "tarball")
    workspace = PRHeadWorkspace(info, None, fallback_repo="o/r", temp_dir=tmp_path)
    state = StateStore(repo="o/r", pr_number=1, head_sha="head", files_changed=["a.py"])
    unit = SemanticUnit(id="unit", path="a.py", kind=UnitKind.SYMBOL, symbol="transform", start_line=80, end_line=81)
    hypothesis = Hypothesis(
        "h",
        "unit::null-path::transform",
        "unit",
        Mechanism.NULL_PATH,
        "Returns None",
        "Valid input",
        "Wrong value",
        "What is returned?",
        "A guard prevents it",
        [Site("a.py", 81, "    return None")],
        "error",
        "generator",
    )
    worker = Investigator(
        None,
        build_workspace_executor(workspace, state),
        changeset=SemanticChangeSet(units=[unit]) if changeset else None,
    )
    worker._state = state
    return worker, hypothesis


@pytest.mark.asyncio
async def test_known_site_default_read_saves_the_relevant_source_after_a_long_header(tmp_path):
    worker, hypothesis = _worker(tmp_path)
    worker._prepare_read_focus(hypothesis, ContextPack())
    args = {"path": "a.py"}
    output = await worker._run_tool("read_file", args)
    observation = worker._observations[-1]
    assert args == {"path": "a.py"}
    assert observation.line_range == (78, 89)
    assert "def transform(value):\n    return None" in observation.excerpt
    assert "requested lines 78-89" in output
    assert '"start": 78' in observation.query and observation.sha == "head"


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["grep", "find_callers", "find_definition"])
async def test_saved_positive_search_hit_focuses_the_next_default_read(tmp_path, tool):
    worker, _ = _worker(tmp_path, changeset=False)
    hit = (
        "- transform [function] caller.py:80\n  def transform(value):"
        if tool == "find_definition"
        else "- caller.py:80: def transform(value):"
    )
    worker._observations = [Observation("obs_0", tool, "q", "", None, "head", "d", hit, "success")]
    await worker._run_tool("read_file", {"path": "caller.py"})
    assert worker._observations[-1].line_range == (77, 88)
    assert "return None" in worker._observations[-1].excerpt


@pytest.mark.asyncio
async def test_only_this_units_related_context_locations_are_used(tmp_path):
    worker, hypothesis = _worker(tmp_path)
    slice = ContextSlice("caller", "caller.py", 80, 81, "transform", "", "calls the method", "head")
    pack = ContextPack(units={"unit": UnitContext("unit", slices=[slice])})
    worker._prepare_read_focus(hypothesis, pack)
    await worker._run_tool("read_file", {"path": "caller.py"})
    assert worker._observations[-1].line_range == (77, 88)
    worker._prepare_read_focus(hypothesis, ContextPack(units={"unrelated": UnitContext("unrelated", slices=[slice])}))
    await worker._run_tool("read_file", {"path": "caller.py"})
    assert worker._observations[-1].line_range is None
    assert "return None" not in worker._observations[-1].excerpt


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["not_found", "error"])
async def test_negative_search_record_cannot_invent_a_focus_location(tmp_path, status):
    worker, _ = _worker(tmp_path, changeset=False)
    worker._observations = [
        Observation("obs_0", "grep", "q", "", None, "head", "d", "- caller.py:80: def transform(value):", status)
    ]
    await worker._run_tool("read_file", {"path": "caller.py"})
    assert worker._observations[-1].line_range is None
    assert "return None" not in worker._observations[-1].excerpt


@pytest.mark.asyncio
async def test_explicit_window_and_unknown_path_keep_existing_behavior(tmp_path):
    worker, hypothesis = _worker(tmp_path)
    worker._prepare_read_focus(hypothesis, ContextPack())
    await worker._run_tool("read_file", {"path": "a.py", "start": 1, "end": 2})
    assert worker._observations[-1].line_range == (1, 2)
    assert worker._observations[-1].excerpt.count("copyright") == 2
    await worker._run_tool("read_file", {"path": "caller.py"})
    assert worker._observations[-1].line_range is None
    await worker._run_tool("read_file", {"path": "does-not-exist.py"})
    assert worker._observations[-1].status == "not_found"


@pytest.mark.asyncio
async def test_unfocused_long_read_does_not_silently_grow_saved_evidence(tmp_path):
    worker, _ = _worker(tmp_path, changeset=False)
    output = await worker._run_tool("read_file", {"path": "a.py"})
    assert "return None" not in output
    assert len(worker._observations[-1].excerpt) == 1200
    assert "return None" not in worker._observations[-1].excerpt


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["grep", "find_callers", "find_definition"])
async def test_explicit_wide_read_saves_a_known_hit_without_changing_the_request(tmp_path, tool):
    worker, _ = _worker(tmp_path, changeset=False)
    hit = (
        "- transform [function] caller.py:80\n  def transform(value):"
        if tool == "find_definition"
        else "- caller.py:80: def transform(value):"
    )
    worker._observations = [Observation("obs_0", tool, "q", "", None, "head", "d", hit, "success")]
    output = await worker._run_tool("read_file", {"path": "caller.py", "start": 1, "end": 100})
    observation = worker._observations[-1]
    assert observation.line_range == (1, 100)
    assert '"start": 1' in observation.query and '"end": 100' in observation.query
    assert observation.excerpt_line_range == (77, 81)
    assert "def transform(value):\n    return None" in observation.excerpt
    assert "Saved source lines 77-81" in output
    assert "Saved search location" not in output
    assert len(observation.excerpt) <= 1200
    # Closure must preserve the saved location as well as the original query.
    closing = worker._closing_chat([], 24000)[-1].content
    assert '"excerpt_line_range": [77, 81]' in closing


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["not_found", "error"])
async def test_negative_search_cannot_redirect_an_explicit_wide_read(tmp_path, status):
    worker, _ = _worker(tmp_path, changeset=False)
    worker._observations = [
        Observation("obs_0", "grep", "q", "", None, "head", "d", "- caller.py:80: def transform(value):", status)
    ]
    output = await worker._run_tool("read_file", {"path": "caller.py", "start": 1, "end": 100})
    assert "Saved search location" not in output
    assert worker._observations[-1].line_range == (1, 100)


@pytest.mark.asyncio
async def test_saved_location_outside_the_requested_window_does_not_redirect(tmp_path):
    worker, _ = _worker(tmp_path, changeset=False)
    worker._observations = [
        Observation("obs_0", "grep", "q", "", None, "head", "d", "- caller.py:80: def transform(value):", "success")
    ]
    output = await worker._run_tool("read_file", {"path": "caller.py", "start": 1, "end": 40})
    assert "Saved search location" not in output


@pytest.mark.asyncio
async def test_navigation_hint_cannot_support_a_confirmed_verdict(tmp_path):
    from test_investigator import _ScriptedToolLLM, _verdict

    worker, hypothesis = _worker(tmp_path, changeset=False)
    hypothesis.observations = [
        Observation("obs_0", "grep", "q", "", None, "head", "d", "- caller.py:80: def transform(value):", "success")
    ]
    worker._llm = _ScriptedToolLLM(
        turns=[
            {"name": "read_file", "args": {"path": "caller.py", "start": 1, "end": 100}, "id": "wide"},
            _verdict(quote="Saved source lines 77-81", ids=["obs_1"]),
        ]
    )
    result = await worker.investigate(hypothesis, worker._state, ContextPack())
    assert result.verdict == "unknown" and result.reason == "ungrounded"


@pytest.mark.asyncio
async def test_known_hit_outside_the_returned_tool_cap_remains_navigation_only(tmp_path):
    worker, _ = _worker(tmp_path, changeset=False)
    (tmp_path / "caller.py").write_text("# Header\n" * 800 + "return None\n", encoding="utf-8")
    worker._observations = [
        Observation("obs_0", "grep", "q", "", None, "head", "d", "- caller.py:801: return None", "success")
    ]
    output = await worker._run_tool("read_file", {"path": "caller.py", "start": 1, "end": 801})
    observation = worker._observations[-1]
    assert "return None" not in observation.excerpt
    assert "Saved search location caller.py:801 is outside this excerpt" in output
    assert observation.excerpt_line_range[1] < 801


@pytest.mark.asyncio
async def test_long_line_before_known_hit_does_not_consume_its_saved_window(tmp_path):
    worker, _ = _worker(tmp_path, changeset=False)
    (tmp_path / "caller.py").write_text("#" + "x" * 1300 + "\nreturn None\n", encoding="utf-8")
    worker._observations = [
        Observation("obs_0", "grep", "q", "", None, "head", "d", "- caller.py:2: return None", "success")
    ]
    await worker._run_tool("read_file", {"path": "caller.py", "start": 1, "end": 2})
    observation = worker._observations[-1]
    assert observation.excerpt == "return None\n" and observation.excerpt_line_range == (2, 2)


@pytest.mark.asyncio
async def test_narrow_saved_window_does_not_request_another_read(tmp_path):
    worker, _ = _worker(tmp_path, changeset=False)
    worker._observations = [
        Observation("obs_0", "grep", "q", "", None, "head", "d", "- caller.py:80: def transform(value):", "success")
    ]
    output = await worker._run_tool("read_file", {"path": "caller.py", "start": 77, "end": 81})
    assert "return None" in output
    assert "Saved search location" not in output


@pytest.mark.asyncio
async def test_latest_saved_definition_can_change_the_same_files_focus(tmp_path):
    worker, hypothesis = _worker(tmp_path)
    with (tmp_path / "a.py").open("a", encoding="utf-8") as source:
        source.write("\n" * 18 + "def other():\n    return 'other'\n")
    worker._prepare_read_focus(hypothesis, ContextPack())
    await worker._run_tool("read_file", {"path": "a.py"})
    worker._observations.append(
        Observation(
            "obs_10",
            "find_definition",
            "q",
            "",
            None,
            "head",
            "d",
            "- other [function] a.py:100\n  def other():",
            "success",
        )
    )
    await worker._run_tool("read_file", {"path": "a.py"})
    assert worker._observations[-1].line_range == (97, 108)
    assert "return 'other'" in worker._observations[-1].excerpt
    assert worker._observations[0].query != worker._observations[-1].query


@pytest.mark.asyncio
async def test_unsaved_search_hit_is_not_used_as_a_default_location(tmp_path):
    worker, _ = _worker(tmp_path, changeset=False)
    read = worker._executor

    async def execute(name, args):
        if name == "grep":
            return "- irrelevant.py:1: " + "x" * 1_200 + "\n- caller.py:80: def transform(value):"
        return await read(name, args)

    worker._executor = execute
    output = await worker._run_tool("grep", {"pattern": "transform"})
    assert "caller.py:80" not in output and "caller.py:80" not in worker._observations[0].excerpt
    await worker._run_tool("read_file", {"path": "caller.py"})
    assert worker._observations[-1].line_range is None
