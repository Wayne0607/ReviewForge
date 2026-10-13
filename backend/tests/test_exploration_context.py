from __future__ import annotations

import copy
import json

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import Field

from reviewforge.core.state import StateStore
from reviewforge.engine.context_pack import ContextPack, ContextSlice, UnitContext
from reviewforge.engine.exploration_context import context_navigation
from reviewforge.engine.hypothesis import Hypothesis, Mechanism, Observation, Site
from reviewforge.engine.investigator import Investigator
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
