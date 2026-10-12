"""A verified rename is an anchor, never a fabricated changed source line."""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

from reviewforge.core.change_manifest import FileChange, file_change_id, render_change_fact
from reviewforge.core.config import PipelineV4Config
from reviewforge.core.database import Database
from reviewforge.core.events import EventBus
from reviewforge.core.specs import build_registry
from reviewforge.core.state import StateStore
from reviewforge.engine.context_pack import ContextPack, ContextSlice, UnitContext
from reviewforge.engine.editor import Editor, cluster_confirmed, render_review_body, resumed_publication
from reviewforge.engine.hypothesis import Hypothesis, HypothesisLedger, HypothesisStatus, Mechanism, Site
from reviewforge.engine.investigator import Investigator, build_workspace_executor
from reviewforge.engine.pipeline_v4 import _publication_coverage, run_hypothesis_pipeline
from reviewforge.tools.gateway import ToolGateway
from reviewforge.tools.workspace import PRHeadWorkspace, WorkspaceInfo

HEAD = "a" * 40
BASE = "b" * 40
MERGE_BASE = "c" * 40
CONSUMER = 'if Path(path).stem != payload["id"]:'
PAYLOAD = '{"id": "old"}\n'
CONSUMER_SOURCE = """import json
from pathlib import Path
ROOT = Path(__file__).parent

def load_configuration():
    path = "data/new.json"
    payload = json.loads((ROOT / path).read_text(encoding="utf-8"))
    if Path(path).stem != payload["id"]:
        raise ValueError("resource id does not match filename")
    return payload
"""


def change():
    item = FileChange(
        repo="owner/repo",
        head_repo="fork/repo",
        path="data/new.json",
        previous_path="data/old.json",
        status="renamed",
        base_sha=BASE,
        comparison_base_sha=MERGE_BASE,
        head_sha=HEAD,
        base_blob_sha="d" * 40,
        head_blob_sha="d" * 40,
        base_mode="100644",
        head_mode="100644",
        base_type="blob",
        head_type="blob",
        patch_state="verified-no-text-change",
        source="github-pr-files+git-tree",
    )
    return replace(item, id=file_change_id(item))


def candidate():
    return Hypothesis(
        id="h_rename",
        identity="unit::contract-mismatch::rename",
        unit_id="unit",
        mechanism=Mechanism.CONTRACT_MISMATCH,
        claim="The renamed resource filename no longer agrees with its unchanged id",
        trigger="Loading data/new.json through load_configuration",
        impact="The concrete loader raises ValueError",
        open_question="Does the loader require the filename stem to equal the parsed resource id?",
        refutation="The loader accepts a filename different from the payload id",
        sites=[],
        severity="error",
        source="generator",
        change_anchor=change(),
        discovery_basis={
            "trigger_basis": {
                "side": "context",
                "path": "loader.py",
                "line": 3,
                "excerpt": "ROOT = Path(__file__).parent",
            }
        },
    )


def inputs(*, consumer_source=CONSUMER_SOURCE, extra_sources=None):
    state = StateStore(
        repo="owner/repo",
        head_repo="fork/repo",
        pr_number=1,
        head_sha=HEAD,
        base_sha=BASE,
        files_changed=[change().path],
        file_diffs={change().path: ""},
        file_changes={change().path: change()},
    )
    pack = ContextPack(
        units={
            "unit": UnitContext(
                "unit",
                slices=[
                    ContextSlice(
                        "reference",
                        "loader.py",
                        1,
                        len(consumer_source.splitlines()),
                        "",
                        consumer_source,
                        "concrete resource consumer",
                        HEAD,
                    ),
                ],
            )
        }
    )
    sources = {"loader.py": consumer_source, change().path: PAYLOAD, **(extra_sources or {})}
    return state, pack, build_workspace_executor(SimpleNamespace(read=sources.get), state)


class ScriptedLLM(BaseChatModel):
    turns: list = Field(default_factory=list)
    calls: list = Field(default_factory=list)

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls.append(messages)
        assert self.turns, "unexpected model call"
        response = AIMessage(
            content=json.dumps(self.turns.pop(0)),
            usage_metadata={
                "input_tokens": 100,
                "output_tokens": 100,
                "total_tokens": 200,
            },
        )
        return ChatResult(generations=[ChatGeneration(message=response)])

    @property
    def _llm_type(self):
        return "structural-fixture"


def confirmation():
    return {
        "assessment": {
            "expected": "The concrete loader requires the filename stem to equal the parsed resource id",
            "actual": "The new filename stem is new while its unchanged JSON id is old",
            "comparison": "conflict",
            "expected_evidence": [{"observation_id": "obs_1", "quote": CONSUMER}],
            "actual_evidence": [
                {"observation_id": "obs_0", "quote": "data/new.json"},
                {"observation_id": "obs_2", "quote": '"id": "old"'},
            ],
        }
    }


def test_filename_id_contract_fixture_is_an_executable_regression(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / change().path).write_text(PAYLOAD, encoding="utf-8")
    namespace = {"__file__": str(tmp_path / "loader.py")}
    exec(CONSUMER_SOURCE, namespace)
    with pytest.raises(ValueError, match="resource id does not match filename"):
        namespace["load_configuration"]()
    (tmp_path / change().previous_path).write_text(PAYLOAD, encoding="utf-8")
    exec(CONSUMER_SOURCE.replace("data/new.json", "data/old.json"), namespace)
    assert namespace["load_configuration"]() == {"id": "old"}


@pytest.mark.asyncio
async def test_metadata_only_structural_actual_cannot_confirm_lookup_failure():
    state, pack, executor = inputs()
    proof = confirmation()
    proof["assessment"]["actual"] = "The old path is unavailable because the change is a rename"
    proof["assessment"]["actual_evidence"] = [{"observation_id": "obs_0", "quote": "data/old.json"}]
    result = await Investigator(ScriptedLLM(turns=[proof]), executor, protocol_version=3).investigate(
        candidate(), state, pack
    )
    assert result.verdict == "unknown"
    assert result.outcome == "output-error"
    assert result.reason == "structural-actual-needs-source-evidence"


@pytest.mark.asyncio
async def test_unknown_loader_binding_remains_unknown_without_promoting_reason():
    source = 'ROOT = Path(__file__).parent\nreturn load("data/old.json")'
    state, pack, executor = inputs(consumer_source=source)
    item = replace(candidate(), discovery_basis={}, claim="Old resource lookup now fails")
    result = await Investigator(
        ScriptedLLM(
            turns=[
                {
                    "assessment": None,
                    "counterevidence": None,
                    "reason": "The loader implementation, aliases and fallback are not saved",
                }
            ]
        ),
        executor,
        protocol_version=3,
    ).investigate(item, state, pack)
    assert result.verdict == "unknown" and result.outcome == "insufficient-evidence"


@pytest.mark.asyncio
async def test_explicit_loader_alias_positively_refutes_old_name_failure():
    alias = 'ALIASES = {"data/old.json": "data/new.json"}'
    source = f"""from pathlib import Path
ROOT = Path(__file__).parent
{alias}
def load_configuration():
    return (ROOT / ALIASES["data/old.json"]).read_text(encoding="utf-8")
"""
    state, pack, executor = inputs(consumer_source=source)
    item = replace(
        candidate(),
        claim="The old resource name now causes lookup failure",
        discovery_basis={
            "trigger_basis": {
                "side": "context",
                "path": "loader.py",
                "line": 2,
                "excerpt": "ROOT = Path(__file__).parent",
            },
        },
    )
    proof = {
        "assessment": None,
        "counterevidence": {
            "premise": "mechanism",
            "statement": "The concrete loader resolves the old name to the present new file",
            "evidence": [
                {"observation_id": "obs_1", "quote": alias},
                {"observation_id": "obs_2", "quote": '"id": "old"'},
            ],
        },
    }
    result = await Investigator(ScriptedLLM(turns=[proof]), executor, protocol_version=3).investigate(item, state, pack)
    assert result.verdict == "refuted" and result.counterevidence is not None


@pytest.mark.asyncio
async def test_rebuilt_old_path_is_read_as_positive_counterevidence():
    source = """from pathlib import Path
ROOT = Path(__file__).parent
def load_configuration():
    return (ROOT / "data/old.json").read_text(encoding="utf-8")
"""
    state, pack, executor = inputs(consumer_source=source, extra_sources={change().previous_path: PAYLOAD})
    item = replace(
        candidate(),
        claim="The old resource path is no longer readable",
        discovery_basis={
            "trigger_basis": {
                "side": "context",
                "path": "loader.py",
                "line": 2,
                "excerpt": "ROOT = Path(__file__).parent",
            }
        },
    )
    llm = ScriptedLLM(
        turns=[
            {
                "progress": {
                    "missing_premise": "mechanism",
                    "question": "Is the old resource still present at head?",
                    "next_evidence": {
                        "tool": "read_file",
                        "args": {"path": change().previous_path, "start": 1, "end": 5},
                    },
                }
            },
            {
                "assessment": None,
                "counterevidence": {
                    "premise": "mechanism",
                    "statement": "The concrete old-path reader still has the intended head resource",
                    "evidence": [
                        {"observation_id": "obs_1", "quote": 'ROOT / "data/old.json"'},
                        {"observation_id": "obs_3", "quote": '"id": "old"'},
                    ],
                },
            },
        ]
    )
    result = await Investigator(llm, executor, protocol_version=3).investigate(item, state, pack)
    assert result.verdict == "refuted"
    assert result.observations[-1].path == change().previous_path
    assert result.observations[-1].status == "success"


def ledger(item):
    result = HypothesisLedger("run", HEAD, "workspace", protocol_version=3)
    result.upsert(item)
    return result


def test_structural_anchor_roundtrip_and_old_protocol_boundary():
    item = candidate()
    saved = ledger(item).to_dict()
    restored = HypothesisLedger.from_dict(saved)
    assert restored.items[item.identity].change_anchor == change()
    assert restored.items[item.identity].sites == []
    for protocol in (1, 2):
        with pytest.raises(ValueError, match="protocol and head"):
            HypothesisLedger("run", HEAD, "d", protocol_version=protocol).upsert(item)
    with pytest.raises(ValueError, match="protocol and head"):
        HypothesisLedger("run", BASE, "d", protocol_version=3).upsert(item)
    for anchor in ({}, replace(change(), patch_state="absent-unverified")):
        with pytest.raises(ValueError, match="verified rename"):
            replace(item, change_anchor=anchor)
    regular = replace(item, change_anchor=None, sites=[Site("a.py", 1, "value")])
    assert "change_anchor" not in regular.to_dict()


@pytest.mark.asyncio
async def test_read_change_uses_trusted_state_and_rejects_unverified_metadata():
    state, _pack, executor = inputs()
    assert await executor("read_change", {"path": change().path}) == render_change_fact(change())
    state.file_changes[change().path] = replace(change(), proof_error="head-entry-unavailable")
    with pytest.raises(ValueError, match="not been verified"):
        await executor("read_change", {"path": change().path})


@pytest.mark.asyncio
async def test_read_change_does_not_silently_truncate_authoritative_metadata():
    state, _pack, executor = inputs()
    item = replace(change(), path="data/" + "segment/" * 150 + "new.properties")
    item = replace(item, id=file_change_id(item))
    state.file_changes = {item.path: item}
    with pytest.raises(ValueError, match="Observation limit"):
        await executor("read_change", {"path": item.path})


@pytest.mark.asyncio
async def test_real_structural_reads_confirm_then_publish_summary_and_resume_without_duplicates():
    state, pack, executor = inputs()
    llm = ScriptedLLM(turns=[confirmation()])
    item = candidate()
    current = ledger(item)
    results = await Investigator(llm, executor, protocol_version=3).run(current, state, pack)
    stored = current.items[item.identity]
    assert stored.status is HypothesisStatus.CONFIRMED
    assert len(results) == 1 and stored.sites == []
    assert [obs.tool for obs in stored.observations] == ["read_change", "read_file", "read_file"]
    assert stored.observations[0].line_range is None
    assert [obs.path for obs in stored.observations[1:]] == ["loader.py", change().path]
    assert all(obs.sha == HEAD for obs in stored.observations)

    editor_llm = ScriptedLLM()
    publication = await Editor(editor_llm).run(current, pack)
    assert not editor_llm.calls and not publication.comments
    body = render_review_body(publication, current)
    assert CONSUMER in body and "Expected:" in body and "Actual:" in body
    assert f"/owner/repo/blob/{MERGE_BASE}/data/old.json" in body
    assert f"/fork/repo/blob/{HEAD}/data/new.json" in body
    assert f"/fork/repo/blob/{HEAD}/loader.py#L1" in body
    assert f"/blob/{BASE}/" not in body
    coverage = _publication_coverage(current, publication, {"comments": [], "body": body})
    assert coverage["confirmed_ids"] == [item.id]
    replay = resumed_publication(
        current,
        covered_ids=set(coverage["confirmed_ids"]),
        published_clusters={tuple(key) for key in coverage["cluster_keys"]},
        inline_used=0,
        max_inline=5,
        max_inline_overflow=8,
        output_language="en",
    )
    assert not replay.comments and not replay.summary_items


@pytest.mark.asyncio
async def test_stale_structural_anchor_stops_before_tool_or_model_calls():
    state, pack, executor = inputs()
    state.file_changes = {}
    llm = ScriptedLLM()
    result = await Investigator(llm, executor, protocol_version=3).investigate(candidate(), state, pack)
    assert result.verdict == "unknown" and result.outcome == "tool-error"
    assert not llm.calls and not result.observations


@pytest.mark.asyncio
async def test_structural_summary_cannot_be_forced_inline_or_use_up_inline_quota():
    structural = replace(candidate(), status=HypothesisStatus.CONFIRMED)
    regular = replace(
        structural,
        id="h_regular",
        identity="other::null-path::run",
        change_anchor=None,
        sites=[Site("a.py", 5, "return user.name")],
        claim="Missing user guard",
    )
    current = ledger(structural)
    current.upsert(regular)
    llm = ScriptedLLM(
        turns=[
            {
                "comments": [
                    {
                        "hypothesis_ids": [structural.id],
                        "path": change().path,
                        "line": 1,
                        "title": "forced",
                        "body": f"{change().path}:1",
                    }
                ],
                "summary_items": [{"hypothesis_id": structural.id, "one_line": "omit proof"}],
            }
        ]
    )
    publication = await Editor(llm, max_inline=1, max_inline_overflow=1).run(current, None)
    assert len(publication.comments) == 1
    assert publication.comments[0].hypothesis_ids == [regular.id]
    assert publication.summary_items[0][0] == structural.id
    assert "omit proof" not in publication.summary_items[0][1]
    assert "Before" in publication.summary_items[0][1]
    assert len(cluster_confirmed(current)) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("stale_metadata", [False, True])
async def test_restored_summary_delivery_requires_fresh_matching_change_proof(tmp_path, stale_metadata):
    state, pack, executor = inputs()
    current = ledger(candidate())
    await Investigator(ScriptedLLM(turns=[confirmation()]), executor, protocol_version=3).run(current, state, pack)
    publication = await Editor(ScriptedLLM()).run(current, pack)
    body = render_review_body(publication, current)
    payload = {"comments": [], "body": body}
    payload["coverage"] = _publication_coverage(current, publication, payload)
    state.ledger = HypothesisLedger.from_dict(current.to_dict())
    if stale_metadata:
        state.file_changes = {}
    source = tmp_path / "data"
    source.mkdir()
    (source / "new.json").write_text(PAYLOAD, encoding="utf-8")
    (tmp_path / "loader.py").write_text(CONSUMER_SOURCE, encoding="utf-8")
    info = WorkspaceInfo("owner/repo", "fork/repo", HEAD, tmp_path, 2, 50, "workspace", False, "tarball")
    workspace = PRHeadWorkspace(info, None, fallback_repo="owner/repo", temp_dir=tmp_path)
    gateway = ToolGateway(build_registry(), None, pipeline_mode="hypothesis")
    gateway._workspaces[id(state)] = workspace
    calls = []
    original = gateway.invoke

    async def invoke(name, params, state_, **kwargs):
        if name != "post_review":
            return await original(name, params, state_, **kwargs)
        calls.append(params)
        return {"delivered_indexes": [], "review": {"id": 42}, "compatibility": False}

    gateway.invoke = invoke
    database = Database(tmp_path / "outbox.sqlite")
    await database.connect()
    try:
        await database.create_run("run", "owner/repo", 1, HEAD)
        await database.prepare_v4_publication("run", HEAD, payload)
        fake = SimpleNamespace(
            _gateway=gateway,
            _events=EventBus(),
            _db=database,
            _pipeline_v4_config=PipelineV4Config(mode="hypothesis", protocol_version=3),
            _model_router=SimpleNamespace(get_llm=lambda *_: pytest.fail("frozen delivery must not invoke a model")),
        )
        if stale_metadata:
            with pytest.raises(ValueError, match="verified PR change"):
                await run_hypothesis_pipeline(fake, state)
            assert not calls
        else:
            await run_hypothesis_pipeline(fake, state)
            await run_hypothesis_pipeline(fake, state)
            assert len(calls) == 1
            assert calls[0]["body"] == body and calls[0]["comments"] == []
            assert (await database.get_v4_publication("run"))["status"] == "delivered"
    finally:
        await database.close()
