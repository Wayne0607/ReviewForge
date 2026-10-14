"""Use the same validated tool arguments for identity, execution and recovery."""

import json
from dataclasses import asdict, replace

import pytest
from test_investigation_v3 import RecordingLLM, candidate, source_executor, state

from reviewforge.engine.context_pack import ContextPack
from reviewforge.engine.investigator import Investigator


@pytest.mark.asyncio
async def test_numeric_string_read_reuses_integer_bootstrap_and_records_real_line_metadata():
    calls = []
    inv = Investigator(RecordingLLM(), source_executor(calls), protocol_version=4)
    inv._prepare_investigation(candidate(), state(), ContextPack())
    first = await inv._run_tool("read_file", {"path": "a.ts", "start": 9, "end": 20})
    second = await inv._run_tool("read_file", {"path": "a.ts", "start": "9", "end": "20"})
    assert first in second and "Already recorded" in second
    assert calls == [("read_file", {"path": "a.ts", "start": 9, "end": 20})]
    assert len(inv._observations) == 1 and inv._observations[0].line_range == (9, 20)
    assert inv._observations[0].query == 'read_file {"end": 20, "path": "a.ts", "start": 9}'


@pytest.mark.asyncio
async def test_legacy_saved_string_query_reuses_without_rewriting_history_or_adding_an_observation():
    calls = []
    inv = Investigator(RecordingLLM(), source_executor(calls), protocol_version=4)
    item = candidate()
    inv._prepare_investigation(item, state(), ContextPack())
    await inv._run_tool("read_file", {"path": "a.ts", "start": 9, "end": 20})
    saved = inv._observations[0]
    saved = replace(saved, query='read_file {"path":"a.ts","start":"9","end":"20"}', line_range=None)
    original = asdict(saved)
    item.observations = [saved]
    restored = Investigator(RecordingLLM(), source_executor(calls), protocol_version=4)
    restored._prepare_investigation(item, state(), ContextPack())
    assert "Already recorded" in await restored._run_tool("read_file", {"path": "a.ts", "start": 9, "end": 20})
    assert len(calls) == 1 and restored._observations == [saved]
    assert asdict(saved) == original


@pytest.mark.asyncio
async def test_default_arguments_share_one_identity_in_direct_calls_and_collection():
    calls = []
    inv = Investigator(RecordingLLM(), source_executor(calls), protocol_version=4)
    inv._prepare_investigation(candidate(), state(), ContextPack())
    await inv._run_tool("grep", {"pattern": "calendar"})
    repeated = {"pattern": "calendar", "glob": "", "max_hits": "10"}
    assert "Already recorded" in await inv._run_tool("grep", repeated)
    tool = next(tool for tool in inv._build_tools() if tool.name == "grep")
    assert inv._collection_query_exists("grep", repeated, tool)
    assert calls == [("grep", {"pattern": "calendar", "glob": "", "max_hits": 10})]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "args",
    [
        {"path": "a.ts", "start": True},
        {"path": "a.ts", "start": 1.5},
        {"path": "a.ts", "start": "1.0"},
        {"path": "a.ts", "start": "not-an-integer"},
        {"path": "a.ts", "start": {}},
        {"path": "a.ts", "undeclared": "ignored before validation"},
        {"start": 1},
    ],
)
async def test_invalid_arguments_never_execute_or_become_evidence(args):
    calls = []
    inv = Investigator(RecordingLLM(), source_executor(calls), protocol_version=4)
    inv._prepare_investigation(candidate(), state(), ContextPack())
    assert "Invalid tool arguments" in await inv._run_tool("read_file", args)
    assert not calls and not inv._observations


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["native", "progress"])
async def test_investigation_repeat_closes_using_bootstrap_without_an_extra_round(shape):
    calls = []
    repeated = [{"name": "read_file", "args": {"path": "a.ts", "start": "9", "end": "20"}, "id": "repeat"}]
    if shape == "progress":
        repeated = json.dumps(
            {
                "progress": {
                    "missing_premise": "contract",
                    "question": "What contract permits absent calendars?",
                    "next_evidence": {"tool": "read_file", "args": repeated[0]["args"]},
                }
            }
        )
    llm = RecordingLLM(turns=[repeated, json.dumps({"reason": "independent input contract is unavailable"})])
    result = await Investigator(llm, source_executor(calls), protocol_version=4).investigate(
        candidate(),
        state(),
        ContextPack(),
    )
    assert len(llm.calls) == 2
    assert len(calls) == len(result.observations) == 2
    assert any(event.get("event") == "close-after-repeated-evidence" for event in result.progress)


@pytest.mark.asyncio
@pytest.mark.parametrize("args", [{"path": "consumer.ts", "start": True}, {"path": "consumer.ts", "undeclared": 1}])
async def test_collection_does_not_coerce_or_discard_invalid_fields_before_the_shared_boundary(args):
    from test_evidence_collection import Model, budgeted, call
    from test_evidence_collection import candidate as collection_candidate
    from test_evidence_collection import state as collection_state

    model = Model(responses=[call(**args)])
    llm, _ = budgeted(model)
    calls = []
    item = collection_candidate()
    result = await Investigator(llm, source_executor(calls), protocol_version=4).collect_evidence(
        item,
        collection_state(),
        ContextPack(),
        token_limit=12_000,
        max_steps=3,
        gaps=["independent contract"],
    )
    assert not calls and result.observations == item.observations
    assert result.outcome == "output-error" and result.reason == "invalid-evidence-tool"


@pytest.mark.asyncio
async def test_collection_recognizes_numeric_string_as_a_saved_integer_read():
    from test_evidence_collection import Model, budgeted, call
    from test_evidence_collection import candidate as collection_candidate
    from test_evidence_collection import state as collection_state

    model = Model(responses=[call(path="check.ts", start="1", end="2")])
    llm, _ = budgeted(model)
    calls = []
    item = collection_candidate()
    result = await Investigator(llm, source_executor(calls), protocol_version=4).collect_evidence(
        item,
        collection_state(),
        ContextPack(),
        token_limit=12_000,
        max_steps=3,
        gaps=["independent contract"],
    )
    assert not calls and result.observations == item.observations
    assert result.reason == "repeated-evidence" and len(model.calls) == 1
