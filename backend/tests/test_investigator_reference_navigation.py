"""Literal references guide a real read; ranking paths/lookup bounds are not evidence."""

import json
from types import SimpleNamespace

import pytest
from test_investigator_typed_closure import candidate

from reviewforge.core.state import StateStore
from reviewforge.engine.context_pack import ContextPack
from reviewforge.engine.hypothesis import EvidenceCitation
from reviewforge.engine.investigation_evidence import EvidenceScope, SharedEvidenceCache
from reviewforge.engine.investigator import Investigator, _evidence_segments, build_workspace_executor


class Workspace:
    def __init__(self, *, empty=False):
        self.empty = empty
        self.calls = []

    def find_references(self, symbol, **kwargs):
        self.calls.append((symbol, kwargs))
        hits = () if self.empty else (SimpleNamespace(path="consumer.tsx", line=42, text="<Widget id={entry.id} />"),)
        return SimpleNamespace(
            hits=hits,
            truncated=True,
            scanned_files=17,
            max_hits=10,
            per_file_limit=2,
            skipped_files=3,
            skipped_large_files=1,
            source_truncated=False,
            unavailable=False,
        )


def worker(workspace, *, protocol=4):
    state = StateStore(repo="o/r", head_sha="pinned", file_diffs={"declaration.ts": ""})
    actor = Investigator(None, build_workspace_executor(workspace, state), protocol_version=protocol)
    actor._state = state
    return actor


@pytest.mark.parametrize("protocol", [1, 2, 3, 4])
def test_reference_navigation_tool_is_only_added_to_v4(protocol):
    actor = worker(Workspace(), protocol=protocol)
    tools = actor._build_tools()
    assert ("find_references" in {tool.name for tool in tools}) is (protocol >= 4)
    if protocol >= 4:
        description = next(tool.description for tool in tools if tool.name == "find_references")
        assert "only glob filters" in description and "Aliases and full dataflow are not resolved" in description


@pytest.mark.asyncio
async def test_ranked_declaration_path_does_not_become_observation_source():
    workspace = Workspace()
    actor = worker(workspace)
    args = {"symbol": "Widget", "path": "declaration.ts", "glob": "*.tsx", "max_hits": 10}
    reply = await actor._run_tool("find_references", args)
    observation = actor._observations[0]
    assert observation.status == "success" and observation.path == "" and observation.sha == "pinned"
    assert observation.excerpt == "- consumer.tsx:42: <Widget id={entry.id} />"
    assert workspace.calls == [("Widget", {"path": "declaration.ts", "globs": ["*.tsx"], "max_hits": 10})]
    assert actor._saved_read_focus("consumer.tsx") == 42
    assert actor._saved_read_focus("declaration.ts") is None
    assert actor._focus_read_args({"path": "consumer.tsx"}) == {"path": "consumer.tsx", "start": 39, "end": 50}
    # Search bounds remain metadata in BOTH the rebuilt exploration and closure.
    packet = actor._evidence_packet(candidate())
    scope = packet["observations"][0]["lookup_scope"]
    assert scope["navigation_only"] and not scope["execution_resolved"] and not scope["aliases_resolved"]
    assert scope["truncated"] and scope["skipped_large_files"] == 1 and scope["head_sha"] == "pinned"
    for chat in [
        actor._exploration_chat(candidate(), ContextPack()),
        actor._closing_chat([], 24_000, hypothesis=candidate()),
    ]:
        assert json.loads(chat[-1].content)["observations"][0]["lookup_scope"] == scope
    quotes = _evidence_segments(observation)
    assert quotes == {"obs_0:e1": EvidenceCitation("obs_0", observation.excerpt)}
    assert "scanned_files" not in observation.excerpt
    assert "consumer.tsx:42" in reply


@pytest.mark.asyncio
async def test_negative_reference_lookup_is_not_successful_or_citable_source():
    actor = worker(Workspace(empty=True))
    await actor._run_tool("find_references", {"symbol": "missing", "path": "declaration.ts"})
    observation = actor._observations[0]
    assert observation.status == "not_found" and observation.path == ""
    assert not _evidence_segments(observation)
    packet = actor._evidence_packet(candidate())
    assert "evidence" not in packet["observations"][0]
    assert packet["observations"][0]["lookup_scope"]["truncated"]


@pytest.mark.asyncio
async def test_reference_citation_outside_diff_uses_actual_hit_location_not_ranking_path():
    actor = worker(Workspace())
    await actor._run_tool("find_references", {"symbol": "Widget", "path": "declaration.ts"})
    observation = actor._observations[0]
    assert actor._quote_outside_diff(observation, observation.excerpt, {"declaration.ts"})
    assert not actor._quote_outside_diff(observation, observation.excerpt, {"consumer.tsx"})


@pytest.mark.asyncio
@pytest.mark.parametrize("empty", [False, True])
async def test_real_shared_cache_allows_reference_navigation_but_never_caches_absence(empty):
    workspace = Workspace(empty=empty)
    state = StateStore(repo="o/r", head_sha="pinned")
    executor = build_workspace_executor(workspace, state)
    cache = SharedEvidenceCache(EvidenceScope("run", "pinned", "digest"))
    actors = [Investigator(None, executor, protocol_version=4, evidence_cache=cache) for _ in range(2)]
    args = {"symbol": "Widget", "path": "declaration.ts"}
    try:
        for actor in actors:
            actor._state = state
            await actor._run_tool("find_references", args)
            observation = actor._observations[0]
            assert observation.status == ("not_found" if empty else "success")
            scope = actor._evidence_packet(candidate())["observations"][0]["lookup_scope"]
            assert scope["scanned_files"] == 17 and scope["navigation_only"]
            assert "scanned_files" not in observation.excerpt
        assert len(workspace.calls) == (2 if empty else 1)
        assert cache.entry_count == (0 if empty else 1)
        if not empty:
            assert actors[0]._observations[0].shared_result_id == actors[1]._observations[0].shared_result_id
    finally:
        await cache.close()
