from __future__ import annotations

import json
from dataclasses import replace

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

from reviewforge.core.state import StateStore
from reviewforge.engine.context_pack import ContextPack, ContextSlice, UnitContext
from reviewforge.engine.hypothesis import Hypothesis, Mechanism, Observation, Site
from reviewforge.engine.investigator import Investigator, _evidence_segments
from reviewforge.engine.verification_guidance import localization_guidance


class ClosingLLM(BaseChatModel):
    reply: dict = Field(default_factory=dict)
    calls: list = Field(default_factory=list)

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls.append((messages, kwargs))
        return ChatResult(
            generations=[
                ChatGeneration(
                    message=AIMessage(
                        content=json.dumps(self.reply),
                        usage_metadata={"input_tokens": 100, "output_tokens": 100, "total_tokens": 200},
                    )
                )
            ]
        )

    @property
    def _llm_type(self):
        return "closing-only-fixture"


def candidate():
    return Hypothesis(
        id="h_packet",
        identity="unit::null-path::lookup",
        unit_id="unit",
        mechanism=Mechanism.NULL_PATH,
        claim="Missing calendar causes a dereference",
        trigger="Calendar is absent",
        impact="The request raises",
        open_question="Does a distant caller always provide a calendar?",
        refutation="Local optional access prevents the dereference",
        severity="warning",
        source="generator",
        sites=[Site("a.ts", 1, "return calendar?.id")],
        discovery_basis={"discrepancy": "UNSAVED_DISCOVERY_SENTINEL"},
    )


def state():
    return StateStore(
        repo="owner/repo",
        head_sha="head",
        files_changed=["a.ts"],
        pr_body="UNSAVED_AUTHOR_SENTINEL",
        file_diffs={"a.ts": "@@ -1 +1 @@\n-x\n+return calendar?.id"},
    )


async def executor(name, args):
    if name == "read_diff":
        return "@@ -1 +1 @@\n-return calendar.id\n+return calendar?.id"
    return "return calendar?.id"


def counter():
    return {
        "assessment": None,
        "counterevidence": {
            "premise": "mechanism",
            "statement": "Optional access prevents the claimed dereference",
            "evidence": ["obs_1:e1"],
        },
        "answer": "The caller question is unnecessary after the local counterexample",
    }


@pytest.mark.asyncio
async def test_closure_can_refute_a_necessary_premise_without_answering_an_ancillary_question():
    llm = ClosingLLM(reply=counter())
    pack = ContextPack(
        units={
            "unit": UnitContext(
                "unit", slices=[ContextSlice("head", "a.ts", 1, 1, "", "UNSAVED_CONTEXT_SENTINEL", "", "head")]
            )
        },
        pr_intent="UNSAVED_AUTHOR_SENTINEL",
    )
    result = await Investigator(llm, executor, protocol_version=3, max_steps=1).investigate(candidate(), state(), pack)
    assert result.verdict == "refuted" and result.counterevidence is not None
    messages, options = llm.calls[0]
    assert len(messages) == 2 and options["max_tokens"] >= 512
    assert "counterevidence" in messages[0].content
    assert "unanswered open_question" in messages[0].content
    packet = json.loads(messages[1].content)
    assert packet["candidate"]["open_question"] == candidate().open_question
    assert not any("UNSAVED_" in message.content for message in messages)
    assert "next_evidence" not in messages[0].content
    assert [item["id"] for item in packet["observations"]] == ["obs_0", "obs_1"]
    for observation, rendered in zip(result.observations, packet["observations"], strict=True):
        assert rendered["evidence"] == {key: ref.quote for key, ref in _evidence_segments(observation).items()}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reply,reason,outcome",
    [
        (
            {
                "assessment": None,
                "counterevidence": None,
                "reason": "The local guard refutes the claim, but no proof object was emitted",
            },
            "The local guard refutes the claim, but no proof object was emitted",
            "insufficient-evidence",
        ),
        (
            {
                "assessment": None,
                "counterevidence": {
                    "premise": "mechanism",
                    "statement": "The guard protects access",
                    "evidence": ["obs_999:e1"],
                },
            },
            "ungrounded-assessment",
            "output-error",
        ),
    ],
)
async def test_compact_closure_does_not_promote_reason_text_or_unsaved_references(reply, reason, outcome):
    result = await Investigator(ClosingLLM(reply=reply), executor, protocol_version=3, max_steps=1).investigate(
        candidate(), state(), ContextPack()
    )
    assert result.verdict == "unknown" and result.reason == reason and result.outcome == outcome


def test_failed_observation_is_status_only_and_saved_source_ids_are_not_rewritten():
    value = Investigator(ClosingLLM(), executor, protocol_version=3)
    value._state = state()
    observations = [
        Observation("obs_7", "read_file", "query", "a.ts", (1, 1), "head", "digest", "return calendar?.id", "success"),
        Observation(
            "obs_12", "read_file", "query", "missing.ts", None, "head", "digest", "PRIVATE_ERROR_SENTINEL", "not_found"
        ),
    ]
    value._observations = observations
    packet = json.loads(value._closing_chat([], 16000, hypothesis=candidate())[1].content)
    assert packet["observations"][0]["evidence"] == {"obs_7:e1": "return calendar?.id"}
    assert packet["observations"][1]["status"] == "not_found"
    assert "evidence" not in packet["observations"][1]
    assert "PRIVATE_ERROR_SENTINEL" not in json.dumps(packet)
    assert value._observations == observations


@pytest.mark.parametrize("protocol", [1, 2])
def test_old_protocols_keep_their_original_closure_packet(protocol):
    value = Investigator(ClosingLLM(), executor, protocol_version=protocol)
    initial = [SystemMessage(content="old system"), HumanMessage(content="old context")]
    messages = value._closing_chat(initial, 16000, hypothesis=candidate())
    assert messages[:2] == initial
    assert "Answer the one open_question" in messages[-1].content
    assert "Assessment JSON only" in messages[-1].content


def test_closure_preserves_applicable_localization_contract_constraints():
    value = Investigator(ClosingLLM(), executor, protocol_version=3)
    item = replace(candidate(), mechanism=Mechanism.I18N)
    value._state = state()
    value._closure_constraints = value._closing_contract_constraints(item, state(), ContextPack())
    packet = json.loads(value._closing_chat([], 16000, hypothesis=item)[1].content)
    assert localization_guidance() in packet["contract_constraints"]
