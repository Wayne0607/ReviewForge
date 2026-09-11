from __future__ import annotations

import json
import threading

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import ConfigDict, Field

from reviewforge.core.state import StateStore
from reviewforge.engine.context_pack import ContextPack
from reviewforge.engine.hypothesis import Hypothesis, HypothesisLedger, Mechanism, Site
from reviewforge.engine.investigator import Investigator, budget_steps


def _hypothesis(*, severity: str = "warning", refutation: str = "", open_question: str = "") -> Hypothesis:
    return Hypothesis(
        id="h_test",
        identity="a.py:f::null-path::f",
        unit_id="a.py:f",
        mechanism=Mechanism.NULL_PATH,
        claim="未处理空值",
        trigger="输入为空时",
        impact="运行时崩溃",
        open_question=open_question or "输入是否可能为空？",
        refutation=refutation or "若上游已校验非空则不成立",
        sites=[Site(path="a.py", line=2, excerpt="return user_input")],
        severity=severity,
        source="generator",
    )


def _state(paths: list[str] | None = None, *, diffs: dict[str, str] | None = None) -> StateStore:
    return StateStore(
        repo="owner/repo",
        pr_number=1,
        head_sha="abc123",
        files_changed=paths or ["a.py"],
        file_diffs=diffs or {"a.py": "diff"},
    )


class _ScriptedToolLLM(BaseChatModel):
    turns: list = Field(default_factory=list)
    model_config = ConfigDict(arbitrary_types_allowed=True)

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        if not self.turns:
            content = json.dumps({"verdict": "unknown", "answer": "", "reason": "no turns left"})
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content=content))])
        turn = self.turns.pop(0)
        if isinstance(turn, str):
            message = AIMessage(content=turn)
        else:
            message = AIMessage(content="", tool_calls=[dict(turn)])
        return ChatResult(generations=[ChatGeneration(message=message)])

    @property
    def _llm_type(self):
        return "scripted-tools"

    @property
    def _identifying_params(self):
        return {}


def _executor(results: dict[tuple[str, tuple], str]):
    async def _exec(name: str, args: dict) -> str:
        key = (name, tuple(sorted(args.items())))
        return results.get(key, "No results")

    return _exec


def _read_file_call(path: str = "a.py"):
    return {"name": "read_file", "args": {"path": path}, "id": "call_1"}


def test_budget_by_severity() -> None:
    assert budget_steps(_hypothesis(severity="error")) == 6
    assert budget_steps(_hypothesis(severity="warning")) == 4
    assert budget_steps(_hypothesis(severity="info")) == 2
    # bonuses (caller -> +2, schema in question -> +2) still cap at 8
    boosted = _hypothesis(
        severity="error", refutation="caller already validates", open_question="does the schema allow null?"
    )
    assert budget_steps(boosted) == 8


@pytest.mark.asyncio
async def test_confirmed_downgraded_when_ungrounded() -> None:
    hypothesis = _hypothesis()
    llm = _ScriptedToolLLM(
        turns=[
            _read_file_call(),
            json.dumps(
                {
                    "verdict": "confirmed",
                    "answer": "输入确实可为空",
                    "evidence_ids": ["obs_0"],
                    "evidence_quote": "return user_input",
                    "severity": "error",
                    "additional_sites": [],
                    "reason": "未校验",
                }
            ),
        ]
    )
    investigator = Investigator(llm, _executor({("read_file", (("path", "a.py"),)): "No results"}))
    result = await investigator.investigate(hypothesis, _state(), ContextPack())

    assert result.verdict == "unknown"
    assert result.reason == "ungrounded"
    assert result.observations[0].status == "not_found"


@pytest.mark.asyncio
async def test_refuted_on_not_found_only_is_downgraded() -> None:
    hypothesis = _hypothesis()
    llm = _ScriptedToolLLM(
        turns=[
            _read_file_call(),
            json.dumps(
                {
                    "verdict": "refuted",
                    "answer": "找不到下游使用",
                    "evidence_ids": ["obs_0"],
                    "evidence_quote": "No results",
                    "severity": "warning",
                    "additional_sites": [],
                    "reason": "搜不到调用方",
                }
            ),
        ]
    )
    investigator = Investigator(llm, _executor({("read_file", (("path", "a.py"),)): "No results"}))
    result = await investigator.investigate(hypothesis, _state(), ContextPack())

    assert result.verdict == "unknown"
    assert result.strength == "none"


@pytest.mark.asyncio
async def test_grounded_confirmed_keeps_strength() -> None:
    hypothesis = _hypothesis()
    content = "def f():\n    return user_input"
    llm = _ScriptedToolLLM(
        turns=[
            _read_file_call(),
            json.dumps(
                {
                    "verdict": "confirmed",
                    "answer": "函数直接把 user_input 返回给用户",
                    "evidence_ids": ["obs_0"],
                    "evidence_quote": "return user_input",
                    "severity": "error",
                    "additional_sites": [],
                    "reason": "缺少校验",
                }
            ),
        ]
    )
    executor = _executor({("read_file", (("path", "a.py"),)): content})
    investigator = Investigator(llm, executor)

    result = await investigator.investigate(hypothesis, _state(), ContextPack(), changed_paths={"a.py"})
    assert result.verdict == "confirmed"
    assert result.strength == "weak"  # observation path is inside the diff
    assert result.observations[0].status == "success"
    assert result.observations[0].excerpt == content


@pytest.mark.asyncio
async def test_outside_diff_evidence_is_strong() -> None:
    hypothesis = _hypothesis()
    content = "def validate(x):\n    return x is not None"
    llm = _ScriptedToolLLM(
        turns=[
            {"name": "read_file", "args": {"path": "lib/validate.py"}, "id": "call_1"},
            json.dumps(
                {
                    "verdict": "refuted",
                    "answer": "上游已有非空校验",
                    "evidence_ids": ["obs_0"],
                    "evidence_quote": "return x is not None",
                    "severity": "warning",
                    "additional_sites": [],
                    "reason": "调用方已校验",
                }
            ),
        ]
    )
    executor = _executor({("read_file", (("path", "lib/validate.py"),)): content})
    investigator = Investigator(llm, executor)

    result = await investigator.investigate(hypothesis, _state(), ContextPack(), changed_paths={"a.py"})
    assert result.verdict == "refuted"
    assert result.strength == "strong"


def test_apply_verdict_transitions_and_merges_observations() -> None:
    ledger = HypothesisLedger("run", "abc123", "digest")
    hypothesis = _hypothesis()
    ledger.upsert(hypothesis)

    from reviewforge.engine.hypothesis import Observation

    first = Observation(
        id="obs_1",
        tool="read_file",
        query="q",
        path="a.py",
        line_range=None,
        sha="s",
        result_digest="d",
        excerpt="x",
        status="success",
    )
    second = Observation(
        id="obs_2",
        tool="grep",
        query="q2",
        path="a.py",
        line_range=None,
        sha="s",
        result_digest="d2",
        excerpt="y",
        status="success",
    )

    ledger.apply_verdict(
        hypothesis.identity, status="confirmed", evidence_strength="weak", verdict_reason="ok", observations=[first]
    )
    ledger.apply_verdict(
        hypothesis.identity,
        status="confirmed",
        evidence_strength="strong",
        verdict_reason="more",
        observations=[second],
    )

    item = ledger.items[hypothesis.identity]
    assert item.status.value == "confirmed"
    assert item.evidence_strength == "strong"
    assert item.attempts == 2
    assert {observation.id for observation in item.observations} == {"obs_1", "obs_2"}


def test_concurrent_upsert_never_loses_sites() -> None:
    ledger = HypothesisLedger("run", "abc123", "digest")

    def add_site(line: int) -> None:
        site = Site(path="a.py", line=line, excerpt="return user_input")
        ledger.upsert(
            Hypothesis(
                id="h_test",
                identity="a.py:f::null-path::f",
                unit_id="a.py:f",
                mechanism=Mechanism.NULL_PATH,
                claim="未处理空值",
                trigger="t",
                impact="i",
                open_question="q",
                refutation="r",
                sites=[site],
                severity="warning",
                source="generator",
            )
        )

    threads = [threading.Thread(target=add_site, args=(line,)) for line in range(1, 21)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(ledger.items["a.py:f::null-path::f"].sites) == 20
