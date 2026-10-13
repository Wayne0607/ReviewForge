from __future__ import annotations

import copy
import json
from dataclasses import asdict, replace

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import Field, ValidationError

from reviewforge.core.state import StateStore
from reviewforge.engine.context_pack import ContextPack, ContextSlice, UnitContext
from reviewforge.engine.exploration_context import context_navigation
from reviewforge.engine.hypothesis import Hypothesis, Mechanism, Observation, Site
from reviewforge.engine.investigator import Investigator, _evidence_segments, build_workspace_executor
from reviewforge.engine.request_budget import estimate_request


def candidate():
    return Hypothesis(
        id="h_contract",
        identity="unit::wrong-operator::check",
        unit_id="unit",
        mechanism=Mechanism.WRONG_OPERATOR,
        claim="The permission predicate rejects a supported admin.",
        trigger="An admin calls the operation.",
        impact="The operation returns FORBIDDEN.",
        open_question="Does a real consumer require this admin operation?",
        refutation="The actual operation contract requires an owner.",
        sites=[Site("check.ts", 2, "return isAdmin && isOwner;")],
        severity="error",
        source="generator",
    )


def state():
    return StateStore(repo="o/r", pr_number=1, head_sha="head", file_diffs={"check.ts": "diff"})


def context(*, count=1):
    slices = [
        ContextSlice("caller", f"caller{i}.ts", 10, 20, "check", "UNSAVED CONTEXT " * 100, "caller", "head")
        for i in range(count)
    ]
    return ContextPack(units={"unit": UnitContext("unit", slices=slices)}, pr_intent="AUTHOR INTENT")


def observation(identity="obs_0", *, status="success", text="return isAdmin && isOwner;"):
    return Observation(
        id=identity,
        tool="read_file",
        query='read_file {"path":"check.ts","start":1,"end":10}',
        path="check.ts",
        line_range=(1, 10),
        sha="head",
        result_digest="digest",
        excerpt=text,
        status=status,
        excerpt_line_range=(1, 10),
    )


class RecordingLLM(BaseChatModel):
    calls: list = Field(default_factory=list)
    tools_seen: list = Field(default_factory=list)

    @property
    def _llm_type(self):
        return "compact-exploration-test"

    def bind_tools(self, tools, **kwargs):
        self.tools_seen.extend(tool.name for tool in tools)
        return self.bind(tools=[convert_to_openai_tool(tool) for tool in tools])

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls.append({"messages": list(messages), "options": kwargs})
        index = len(self.calls)
        if index < 3:
            message = AIMessage(
                content=json.dumps(
                    {
                        "progress": {
                            "missing_premise": "contract",
                            "question": "Read the actual consumer.",
                            "next_evidence": {"tool": "read_file", "args": {"path": f"caller{index}.ts"}},
                        }
                    }
                ),
                tool_calls=[{"name": "read_file", "args": {"path": f"caller{index}.ts"}, "id": f"call{index}"}],
            )
        else:
            message = AIMessage(
                content=json.dumps(
                    {
                        "answer": "The consumer rejects this input before the operation.",
                        "severity": "error",
                        "assessment": None,
                        "counterevidence": {
                            "premise": "trigger",
                            "statement": "The actual caller rejects admins.",
                            "evidence": ["obs_3:e1"],
                        },
                        "reason": "The caller guard contradicts the trigger.",
                        "additional_sites": [],
                    }
                )
            )
        usage = estimate_request(messages, kwargs)
        message.usage_metadata = {"input_tokens": usage, "output_tokens": 100, "total_tokens": usage + 100}
        return ChatResult(generations=[ChatGeneration(message=message)])


async def executor(name, args):
    if name == "read_diff":
        return "@@ -1 +1 @@\n-return isAdmin || isOwner;\n+return isAdmin && isOwner;"
    return "if (role !== OWNER) throw new Error('owner only');\ncheck(user);"


def test_navigation_deduplicates_metadata_without_promoting_context_source():
    pack = context()
    pack.units["unit"].slices *= 3
    pack.units["unit"].slices.append(
        ContextSlice("caller", "stale.ts", 1, 2, "check", "STALE SOURCE", "caller", "old-head")
    )
    pack.units["unit"].truncated_kinds = ["schema"]
    snapshot = copy.deepcopy(pack)
    navigation = context_navigation(candidate(), None, pack, head_sha="head")
    assert len(navigation["locations"]) == 1
    assert navigation["locations"][0] == {
        "kind": "caller",
        "path": "caller0.ts",
        "start": 10,
        "end": 20,
        "symbol": "check",
    }
    assert navigation["truncated_kinds"] == ["schema"]
    assert navigation["stale_locations"] == 1
    assert "UNSAVED CONTEXT" not in json.dumps(navigation)
    assert pack == snapshot


def test_navigation_has_a_valid_bounded_envelope_and_reports_omissions():
    navigation = context_navigation(candidate(), None, context(count=100), head_sha="head", max_chars=500)
    assert len(json.dumps(navigation, ensure_ascii=False, separators=(",", ":"))) <= 500
    assert 0 < len(navigation["locations"]) < 100
    assert navigation["omitted_locations"] + len(navigation["locations"]) == 100


@pytest.mark.parametrize("failure_status", ["error", "not_found"])
def test_exploration_and_closure_share_exact_evidence_and_preserve_failure_status(failure_status):
    worker = Investigator(RecordingLLM(), executor, protocol_version=4)
    worker._state = state()
    worker._observations = [
        observation(text="精确源码\n" * 200),
        observation("obs_1", status=failure_status, text=""),
        *(observation(f"obs_{i}", text=f"source_{i}\n" * 120) for i in range(2, 6)),
    ]
    worker._closure_constraints = ["Keep supported operation contracts."]
    worker._progress = [
        {"phase": "proof-duty", "missing_premise": "trigger", "question": "old question"},
        {"phase": "proof-duty", "missing_premise": "contract", "question": "actual consumer?"},
        {"phase": "budget", "event": "request-plan"},
    ]
    item = candidate()
    before = copy.deepcopy(item)
    exploration = json.loads(worker._exploration_chat(item, context())[-1].content)
    closure = json.loads(worker._closing_chat([], 24_000, hypothesis=item)[-1].content)
    assert exploration["candidate"] == closure["candidate"]
    assert exploration["observations"][0]["evidence"] == closure["observations"][0]["evidence"]
    assert "".join(exploration["observations"][0]["evidence"].values()) == worker._observations[0].excerpt
    assert sum(len(obs.excerpt) for obs in worker._observations) > 4096
    for raw, obs in zip(exploration["observations"], worker._observations, strict=True):
        if obs.status == "success":
            assert "".join(raw["evidence"].values()) == obs.excerpt
    assert exploration["observations"][1]["status"] == failure_status
    assert exploration["observations"][1]["query"] == worker._observations[1].query
    assert "evidence" not in exploration["observations"][1]
    assert exploration["proof_duty"]["question"] == "actual consumer?"
    assert exploration["contract_constraints"] == worker._closure_constraints
    assert "Missing test assertions" in exploration["review_scope"]
    assert "AUTHOR INTENT" not in json.dumps(exploration)
    assert "UNSAVED CONTEXT" not in json.dumps(exploration)
    assert item == before


@pytest.mark.asyncio
async def test_v4_rebuilds_each_tool_round_from_saved_state_inside_original_budget():
    llm = RecordingLLM()
    result = await Investigator(llm, executor, protocol_version=4).investigate(candidate(), state(), context(count=12))
    assert result.verdict == "refuted"
    assert result.steps <= 6
    assert result.tokens <= 24_000
    assert len(llm.calls) == 3
    assert llm.tools_seen == ["read_file", "grep", "find_definition", "find_callers", "read_diff"]
    for index, call in enumerate(llm.calls):
        assert [message.type for message in call["messages"]] == ["system", "human"]
        packet = json.loads(call["messages"][-1].content)
        assert len(packet["observations"]) == index + 2
        assert all("query" in row for row in packet["observations"])
        assert "UNSAVED CONTEXT" not in str(call["messages"])
        if index:
            assert packet["proof_duty"]["missing_premise"] == "contract"


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", [1, 2, 3])
async def test_old_protocols_do_not_use_compact_exploration(monkeypatch, protocol):
    def forbidden(*args, **kwargs):
        pytest.fail("old investigation protocol changed")

    monkeypatch.setattr(Investigator, "_exploration_chat", forbidden)
    await Investigator(RecordingLLM(), executor, protocol_version=protocol, max_steps=1).investigate(
        candidate(), state(), context()
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("symbol", "reference"),
    [
        ("handler", "const dispatch = handler; dispatch();"),
        ("addGuests", "const mutation = trpc.viewer.bookings.addGuests.useMutation({});"),
        ("AddGuestsDialog", "export const Screen = () => <AddGuestsDialog />;"),
    ],
)
async def test_direct_caller_miss_keeps_indirect_references_reachable_by_grep(tmp_path, symbol, reference):
    from reviewforge.tools.workspace import PRHeadWorkspace, WorkspaceInfo

    (tmp_path / "handler.ts").write_text(f"export function {symbol}() {{ return true; }}\n", encoding="utf-8")
    (tmp_path / "consumer.tsx").write_text(reference + "\n", encoding="utf-8")
    workspace = PRHeadWorkspace(
        WorkspaceInfo("o/r", "o/r", "head", tmp_path, 2, 0, "fixture", False, "tarball"),
        None,
        fallback_repo="o/r",
        temp_dir=tmp_path,
    )
    worker = Investigator(None, build_workspace_executor(workspace, state()), protocol_version=4)
    worker._state = state()
    await worker._run_tool("find_callers", {"symbol": symbol})
    missing = worker._observations[0]
    assert missing.status == "not_found"
    assert _evidence_segments(missing) == {}
    entry = json.loads(worker._exploration_chat(candidate(), ContextPack())[1].content)["observations"][0]
    assert entry["lookup_scope"]["kind"] == "direct-call-expression"
    assert entry["lookup_scope"]["indirect_references_resolved"] is False
    assert entry["lookup_scope"]["not_found_means"] == "No direct match in this lookup; consumers may exist."
    assert "evidence" not in entry

    await worker._run_tool("grep", {"pattern": symbol, "glob": "consumer.tsx", "max_hits": 10})
    hit = worker._observations[1]
    assert hit.status == "success"
    assert f"consumer.tsx:1: {reference}" in hit.excerpt
    await worker._run_tool("read_file", {"path": "consumer.tsx", "start": 1, "end": 1})
    assert worker._observations[2].excerpt == reference + "\n"
    assert len(worker._observations) == 3  # No automatic alias lookup or fabricated caller read.
    invalid = worker._finalize(
        {
            "assessment": None,
            "counterevidence": {"premise": "trigger", "statement": "No callers exist.", "evidence": ["obs_0:e1"]},
            "reason": "No callers exist.",
            "severity": "error",
        },
        candidate(),
        {"check.ts"},
        steps=1,
    )
    assert invalid.verdict == "unknown"


@pytest.mark.parametrize("protocol", [1, 2, 3, 4])
def test_caller_tool_scope_is_explicit_only_for_v4(protocol):
    worker = Investigator(None, executor, protocol_version=protocol)
    description = next(tool.description for tool in worker._build_tools() if tool.name == "find_callers")
    if protocol < 4:
        assert description == "查找某符号的调用位置"
    else:
        assert "direct textual" in description
        assert "qualified Java" in description
        assert "JSX" in description
        assert "grep" in description


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["grep", "find_definition", "read_file"])
async def test_compact_packets_preserve_recorded_truncation_without_unsaved_evidence(tool):
    original = "saved source\n" * 600 + "UNSAVED_FINAL_LINE\n"

    async def long_result(name, args):
        return original

    worker = Investigator(None, long_result, protocol_version=4)
    worker._state = state()
    args = {"path": "source.ts", "start": 1, "end": 601} if tool == "read_file" else {"symbol": "source"}
    await worker._run_tool(tool, args)
    source = worker._observations[0]
    snapshot = asdict(source)
    packet = json.loads(worker._exploration_chat(candidate(), ContextPack())[1].content)
    window = packet["observations"][0]["source_window"]
    assert window["truncated"] is True
    assert window["original_chars"] == len(original)
    assert window["saved_chars"] == len(source.excerpt)
    assert "narrower" in window["next_action"]
    assert "UNSAVED_FINAL_LINE" not in json.dumps(packet)
    assert asdict(source) == snapshot
    closing = json.loads(worker._closing_chat([], 24_000, hypothesis=candidate())[1].content)
    assert closing["observations"][0]["source_window"] == window

    # This is the same serialization boundary used by persisted hypotheses.
    saved = candidate()
    saved.observations = worker._observations
    saved.investigation_progress = worker._progress
    restored = Hypothesis.from_dict(saved.to_dict())
    resumed = Investigator(None, long_result, protocol_version=4)
    resumed._state = state()
    resumed._observations = restored.observations
    resumed._progress = restored.investigation_progress
    later = json.loads(resumed._exploration_chat(restored, ContextPack())[1].content)
    assert later["observations"][0]["source_window"] == window
    assert later["observations"][0]["evidence"] == packet["observations"][0]["evidence"]


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", [3, 4])
async def test_truncation_metadata_does_not_change_old_protocol_or_invent_historical_sizes(protocol):
    worker = Investigator(None, executor, protocol_version=protocol)
    worker._state = state()
    worker._observations = [observation(text="x" * 1200)]
    before = worker._evidence_packet(candidate())
    assert "source_window" not in before["observations"][0]
    if protocol == 3:

        async def long_result(name, args):
            return "x" * 2000

        worker._executor = long_result
        await worker._run_tool("find_definition", {"symbol": "target"})
        assert not any(entry.get("phase") == "observation-window" for entry in worker._progress)


def verified_rename(path):
    from reviewforge.core.change_manifest import FileChange, file_change_id

    change = FileChange(
        repo="o/r",
        head_repo="o/r",
        path=path,
        previous_path="old/" + path,
        status="renamed",
        base_sha="b" * 40,
        head_sha="a" * 40,
        comparison_base_sha="c" * 40,
        base_blob_sha="d" * 40,
        head_blob_sha="d" * 40,
        base_mode="100644",
        head_mode="100644",
        base_type="blob",
        head_type="blob",
        patch_state="verified-no-text-change",
        source="github-pr-files+git-tree",
    )
    return replace(change, id=file_change_id(change))


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [1, 2])
async def test_v4_read_change_schema_keeps_all_verified_paths_for_ordinary_candidates(count):
    from reviewforge.core.change_manifest import is_verified_rename

    current = state()
    current.head_sha = "a" * 40
    renames = [verified_rename(f"data/{i}.json") for i in range(count)]
    invalid = replace(verified_rename("unverified.json"), patch_state="absent-unverified")
    current.file_changes = {change.path: change for change in [*renames, invalid]}
    current.files_changed = ["check.ts", *current.file_changes]
    assert candidate().change_anchor is None
    assert all(is_verified_rename(change, current.head_sha) for change in renames)
    worker = Investigator(None, build_workspace_executor(None, current), protocol_version=4)
    worker._state = current
    tool = next(tool for tool in worker._build_tools() if tool.name == "read_change")
    property_schema = convert_to_openai_tool(tool)["function"]["parameters"]["properties"]["path"]
    allowed = property_schema.get("enum", [property_schema.get("const")])
    assert set(allowed) == {change.path for change in renames}
    assert "read_diff" in tool.description
    for change in renames:
        assert tool.args_schema.model_validate({"path": change.path}).path == change.path
    for bad_path in ("check.ts", invalid.path, renames[0].previous_path):
        with pytest.raises(ValidationError):
            tool.args_schema.model_validate({"path": bad_path})
    # Native execution still checks trusted state independently of model schema.
    with pytest.raises(ValueError, match="has not been verified"):
        await worker._executor("read_change", {"path": "check.ts"})
    assert renames[0].path in await worker._executor("read_change", {"path": renames[0].path})


def test_v3_read_change_schema_stays_unrestricted_and_v4_requires_verified_paths():
    current = state()
    current.head_sha = "a" * 40
    change = verified_rename("data/new.json")
    current.file_changes = {change.path: change}
    worker = Investigator(None, executor, protocol_version=3)
    worker._state = current
    tool = next(tool for tool in worker._build_tools() if tool.name == "read_change")
    assert tool.args_schema.model_validate({"path": "check.ts"}).path == "check.ts"
    assert "enum" not in tool.args_schema.model_json_schema()["properties"]["path"]
    worker._protocol_version = 4
    current.file_changes = {change.path: replace(change, patch_state="absent-unverified")}
    assert "read_change" not in [tool.name for tool in worker._build_tools()]
