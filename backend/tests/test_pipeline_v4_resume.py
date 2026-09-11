from __future__ import annotations

import json

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import ConfigDict, Field

from reviewforge.core.state import StateStore
from reviewforge.engine.context_pack import ContextPack
from reviewforge.engine.hypothesis import Hypothesis, HypothesisLedger, HypothesisStatus, Mechanism, Site
from reviewforge.engine.investigator import Investigator


def _hypothesis(identity: str, status: HypothesisStatus) -> Hypothesis:
    return Hypothesis(
        id=f"h_{identity}",
        identity=identity,
        unit_id=identity.split("::")[0],
        mechanism=Mechanism.NULL_PATH,
        claim="claim",
        trigger="trigger",
        impact="impact",
        open_question="q",
        refutation="r",
        sites=[Site(path="a.py", line=2, excerpt="return user_input")],
        severity="warning",
        source="generator",
        status=status,
    )


class _ScriptedLLM(BaseChatModel):
    responses: list = Field(default_factory=list)
    model_config = ConfigDict(arbitrary_types_allowed=True)

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        content = self.responses.pop(0) if self.responses else '{"verdict":"unknown","reason":"no turns"}'
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=content))])

    @property
    def _llm_type(self):
        return "scripted"

    @property
    def _identifying_params(self):
        return {}


@pytest.mark.asyncio
async def test_resume_reinvestigates_open_and_skips_confirmed() -> None:
    ledger = HypothesisLedger("run", "abc123", "digest")
    confirmed = _hypothesis("u1::null-path::f", HypothesisStatus.CONFIRMED)
    open_ = _hypothesis("u2::null-path::g", HypothesisStatus.OPEN)
    ledger.upsert(confirmed)
    ledger.upsert(open_)

    llm = _ScriptedLLM(responses=[json.dumps({"verdict": "unknown", "answer": "", "reason": "窄"})])
    state = StateStore(
        repo="owner/repo", pr_number=1, head_sha="abc123", files_changed=["a.py"], file_diffs={"a.py": "diff"}
    )
    await Investigator(llm, lambda name, args: "No results").run(ledger, state, ContextPack(), concurrency=1)

    assert ledger.items[confirmed.identity].status == HypothesisStatus.CONFIRMED
    assert ledger.items[confirmed.identity].attempts == 0  # CONFIRMED not re-investigated
    assert ledger.items[open_.identity].status == HypothesisStatus.UNKNOWN
    assert ledger.items[open_.identity].attempts == 1  # OPEN re-investigated on resume
