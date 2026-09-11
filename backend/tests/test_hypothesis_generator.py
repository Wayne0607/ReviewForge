from __future__ import annotations

import json
from typing import Any

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import ConfigDict, Field

from reviewforge.core.state import StateStore
from reviewforge.engine.context_pack import ContextPack
from reviewforge.engine.hypothesis import HypothesisLedger, HypothesisStatus, Mechanism
from reviewforge.engine.hypothesis_generator import HypothesisGenerator
from reviewforge.engine.semantic_diff import SemanticChangeSet, SemanticUnit, UnitKind

_EXCERPT_LINE = "    return create_resource(owner_id)"


def _diff(path: str, *lines: str) -> str:
    body = "\n".join(f"+{line}" for line in lines)
    return (
        f"diff --git a/{path} b/{path}\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        f"+++ b/{path}\n"
        f"@@ -0,0 +1,{len(lines)} @@\n"
        f"{body}\n"
    )


def _unit(path: str, symbol: str, *, risk: float = 1.0, end_line: int = 4) -> SemanticUnit:
    return SemanticUnit(
        id=f"{path}:{symbol}",
        path=path,
        language="python",
        kind=UnitKind.SYMBOL,
        symbol=symbol,
        start_line=1,
        end_line=end_line,
        added_lines=list(range(1, end_line + 1)),
        risk_score=risk,
    )


def _server_diff() -> dict[str, str]:
    return {
        "service.py": _diff(
            "service.py",
            "def get_or_create_resource(owner_id):",
            _EXCERPT_LINE,
            "    if owner is None: return None",
            "def process_request(req):",
        )
    }


class _ScriptedLLM(BaseChatModel):
    """Returns one fixed JSON body per call; records every message list."""

    responses: list[str] = Field(default_factory=list)
    calls: list[list[BaseMessage]] = Field(default_factory=list)

    model_config = ConfigDict(arbitrary_types_allowed=True)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls.append(list(messages))
        content = self.responses.pop(0) if self.responses else "{}"
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=content))])

    @property
    def _llm_type(self):
        return "scripted"

    @property
    def _identifying_params(self):
        return {}


def _hypothesis(
    mechanism: str, severity: str = "error", excerpt: str = "return create_resource(owner_id)", line: int = 2
) -> dict[str, Any]:
    return {
        "unit_id": "service.py:get_or_create_resource",
        "mechanism": mechanism,
        "anchor_symbol": "get_or_create_resource",
        "claim": "owner id is not the client id",
        "trigger": "resource created with the wrong owner",
        "impact": "the new resource points at the wrong client",
        "open_question": "does getOrCreateResource use resourceServer.getClientId()?",
        "refutation": "the callee overwrites the owner",
        "severity": severity,
        "sites": [{"path": "service.py", "line": line, "excerpt": excerpt}],
    }


def _changeset(*units: SemanticUnit) -> SemanticChangeSet:
    return SemanticChangeSet(repo="owner/repo", pr_number=1, head_sha="abc", units=list(units))


@pytest.mark.asyncio
async def test_generator_accepts_valid_output_and_upserts() -> None:
    llm = _ScriptedLLM(responses=[json.dumps({"hypotheses": [_hypothesis("wrong-argument")], "no_issue_units": []})])
    generator = HypothesisGenerator(llm, output_language="en")
    unit = _unit("service.py", "get_or_create_resource")
    ledger = HypothesisLedger("run", "abc", "digest")

    result = await generator.run(
        StateStore(file_diffs=_server_diff()), ContextPack(pr_intent="fix owner"), _changeset(unit), ledger
    )

    assert result.accepted == 1
    assert result.dropped_unanchored == 0
    identity = "service.py:get_or_create_resource::wrong-argument::get_or_create_resource"
    stored = ledger.items[identity]
    assert stored.id.startswith("h_") and len(stored.id) == 10
    assert stored.mechanism is Mechanism.WRONG_ARGUMENT
    assert stored.status is HypothesisStatus.OPEN
    assert stored.source == "generator"
    assert [site.line for site in stored.sites] == [2]


@pytest.mark.asyncio
async def test_unanchored_excerpt_drops_hypothesis() -> None:
    llm = _ScriptedLLM(
        responses=[
            json.dumps(
                {
                    "hypotheses": [_hypothesis("wrong-argument", excerpt="this text is not in the diff at all")],
                    "no_issue_units": [],
                }
            )
        ]
    )
    generator = HypothesisGenerator(llm)
    ledger = HypothesisLedger("run", "abc", "digest")

    result = await generator.run(
        StateStore(file_diffs=_server_diff()),
        ContextPack(),
        _changeset(_unit("service.py", "get_or_create_resource")),
        ledger,
    )

    assert result.accepted == 0
    assert result.dropped_unanchored == 1
    assert not ledger.items


@pytest.mark.asyncio
async def test_excerpt_on_wrong_line_is_unanchored() -> None:
    # Excerpt exists in the file but is reported against line 1, whose content
    # does not contain it.
    llm = _ScriptedLLM(
        responses=[json.dumps({"hypotheses": [_hypothesis("wrong-argument", line=1)], "no_issue_units": []})]
    )
    generator = HypothesisGenerator(llm)
    ledger = HypothesisLedger("run", "abc", "digest")

    result = await generator.run(
        StateStore(file_diffs=_server_diff()),
        ContextPack(),
        _changeset(_unit("service.py", "get_or_create_resource")),
        ledger,
    )

    assert result.accepted == 0
    assert result.dropped_unanchored == 1


@pytest.mark.asyncio
async def test_overflow_keeps_highest_severity() -> None:
    hypotheses = [
        _hypothesis("wrong-argument", severity="info"),
        _hypothesis("null-path", severity="warning"),
        _hypothesis("security-sink", severity="error"),
    ]
    llm = _ScriptedLLM(responses=[json.dumps({"hypotheses": hypotheses, "no_issue_units": []})])
    generator = HypothesisGenerator(llm, max_hypotheses=2)
    ledger = HypothesisLedger("run", "abc", "digest")

    result = await generator.run(
        StateStore(file_diffs=_server_diff()),
        ContextPack(),
        _changeset(_unit("service.py", "get_or_create_resource")),
        ledger,
    )

    assert result.accepted == 2
    assert result.dropped_overflow == 1
    mechanisms = {item.mechanism for item in ledger.items.values()}
    assert mechanisms == {Mechanism.NULL_PATH, Mechanism.SECURITY_SINK}


@pytest.mark.asyncio
async def test_parse_failure_marks_the_units_unresolved() -> None:
    llm = _ScriptedLLM(responses=["this is not json", "still not json"])
    generator = HypothesisGenerator(llm)
    unit = _unit("service.py", "get_or_create_resource")
    ledger = HypothesisLedger("run", "abc", "digest")

    result = await generator.run(StateStore(file_diffs=_server_diff()), ContextPack(), _changeset(unit), ledger)

    assert result.accepted == 0
    assert result.failed_blocks == 1
    assert result.unresolved_units == [unit.id]
    assert ledger.unresolved_units[unit.id] == "generator parse failure"


@pytest.mark.asyncio
async def test_invalid_mechanism_is_dropped() -> None:
    llm = _ScriptedLLM(responses=[json.dumps({"hypotheses": [_hypothesis("banana")], "no_issue_units": []})])
    generator = HypothesisGenerator(llm)
    ledger = HypothesisLedger("run", "abc", "digest")

    result = await generator.run(
        StateStore(file_diffs=_server_diff()),
        ContextPack(),
        _changeset(_unit("service.py", "get_or_create_resource")),
        ledger,
    )

    assert result.accepted == 0
    assert result.dropped_invalid == 1
    assert not ledger.items


@pytest.mark.asyncio
async def test_chunking_shares_the_ledger_across_blocks() -> None:
    first = _unit("service.py", "get_or_create_resource", risk=2.0)
    second = _unit("helper.py", "normalize", risk=1.0, end_line=2)
    diffs = {
        "service.py": _diff("service.py", "def get_or_create_resource(owner_id):", _EXCERPT_LINE),
        "helper.py": _diff("helper.py", "def normalize(x):", "    return x.strip()"),
    }
    responses = [
        json.dumps({"hypotheses": [_hypothesis("wrong-argument")], "no_issue_units": []}),
        json.dumps({"hypotheses": [], "no_issue_units": []}),
    ]
    llm = _ScriptedLLM(responses=responses)
    # A tiny budget forces each unit into its own block.
    generator = HypothesisGenerator(llm, max_input_chars=200)
    ledger = HypothesisLedger("run", "abc", "digest")

    result = await generator.run(StateStore(file_diffs=diffs), ContextPack(), _changeset(first, second), ledger)

    assert result.blocks == 2
    assert len(ledger.items) == 1
    second_block_text = "\n".join(getattr(message, "content", "") for message in llm.calls[1])
    assert "wrong-argument" in second_block_text
    assert "## Existing hypotheses" in second_block_text
