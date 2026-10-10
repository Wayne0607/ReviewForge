from __future__ import annotations

import json
from typing import Any

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import ConfigDict, Field

from reviewforge.core.state import StateStore
from reviewforge.engine.context_pack import ContextPack, ContextSlice, UnitContext
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
    output_limits: list[int | None] = Field(default_factory=list)

    model_config = ConfigDict(arbitrary_types_allowed=True)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls.append(list(messages))
        self.output_limits.append(kwargs.get("max_tokens"))
        content = self.responses.pop(0) if self.responses else "{}"
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=content))])

    @property
    def _llm_type(self):
        return "scripted"

    @property
    def _identifying_params(self):
        return {}


@pytest.mark.asyncio
async def test_oversized_unit_is_unresolved_instead_of_exceeding_input_cap():
    llm = _ScriptedLLM(responses=['{"hypotheses": [], "no_issue_units": []}'])
    unit = _unit("service.py", "get_or_create_resource")
    ledger = HypothesisLedger("run", "abc", "digest")
    result = await HypothesisGenerator(llm, max_input_chars=200).run(
        StateStore(file_diffs=_server_diff()), ContextPack(), _changeset(unit), ledger
    )
    assert not llm.calls
    assert result.failed_blocks == 1
    assert ledger.unresolved_units[unit.id] == "generator input-too-large"
    assert not ledger.no_issue_units


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
async def test_model_anchor_variants_cannot_split_the_same_code_hypothesis() -> None:
    first = _hypothesis("wrong-argument")
    second = {**first, "anchor_symbol": "invented_other_function"}
    llm = _ScriptedLLM(responses=[json.dumps({"hypotheses": [first, second], "no_issue_units": []})])
    ledger = HypothesisLedger("run", "abc", "digest")
    await HypothesisGenerator(llm).run(
        StateStore(file_diffs=_server_diff()),
        ContextPack(),
        _changeset(_unit("service.py", "get_or_create_resource")),
        ledger,
    )
    assert list(ledger.items) == ["service.py:get_or_create_resource::wrong-argument::get_or_create_resource"]


@pytest.mark.asyncio
async def test_anchor_uses_innermost_head_function_and_caches_source() -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from reviewforge.engine.hypothesis_generator import build_anchor_resolver

    source = (
        "class Outer:\n"
        "    def method(self):\n"
        "        def nested(owner_id):\n"
        "            return create_resource(owner_id)\n"
        "        return nested(self.owner_id)\n"
    )
    unit = _unit("service.py", "Outer")
    changeset = _changeset(unit)
    workspace = SimpleNamespace(read_async=AsyncMock(return_value=source))
    resolver = build_anchor_resolver(workspace, changeset)
    assert await resolver(unit.id, unit.path, 4) == "nested"
    assert await resolver(unit.id, unit.path, 5) == "method"
    hypothesis = {**_hypothesis("wrong-argument", line=4), "unit_id": unit.id, "anchor_symbol": "invented"}
    llm = _ScriptedLLM(responses=[json.dumps({"hypotheses": [hypothesis], "no_issue_units": []})])
    ledger = HypothesisLedger("run", "abc", "digest")
    await HypothesisGenerator(llm, anchor_resolver=resolver).run(
        StateStore(file_diffs={unit.path: "@@ -0,0 +1,5 @@\n" + "\n".join("+" + line for line in source.splitlines())}),
        ContextPack(),
        changeset,
        ledger,
    )
    assert list(ledger.items) == [f"{unit.id}::wrong-argument::nested"]
    workspace.read_async.assert_awaited_once_with(unit.path)


@pytest.mark.asyncio
async def test_anchor_falls_back_to_unit_symbol_for_unavailable_or_resource_source() -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from reviewforge.engine.hypothesis_generator import build_anchor_resolver
    from reviewforge.tools.workspace import WorkspaceUnavailable

    unit = _unit("service.py", "get_or_create_resource")
    workspace = SimpleNamespace(read_async=AsyncMock(side_effect=WorkspaceUnavailable()))
    resolver = build_anchor_resolver(workspace, _changeset(unit))
    assert await resolver(unit.id, unit.path, 2) == unit.symbol
    resource = _unit("messages.properties", "")
    workspace = SimpleNamespace(read_async=AsyncMock(return_value="translation = Hello\n"))
    resolver = build_anchor_resolver(workspace, _changeset(resource))
    assert await resolver(resource.id, resource.path, 1) == ""


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
    assert llm.output_limits == [8192, 8192]


@pytest.mark.asyncio
@pytest.mark.parametrize("template", ["generator", "lens"])
async def test_generation_prompt_delivers_configured_output_bound(template: str) -> None:
    llm = _ScriptedLLM(responses=['{"hypotheses": [], "no_issue_units": []}'])
    await HypothesisGenerator(llm, max_hypotheses=2, prompt_template=template).run(
        StateStore(file_diffs=_server_diff()),
        ContextPack(),
        _changeset(_unit("service.py", "get_or_create_resource")),
        HypothesisLedger("run", "abc", "digest"),
    )
    prompt = llm.calls[0][0].content
    assert "2 条不同的假设" in prompt
    assert "{{max_hypotheses}}" not in prompt


@pytest.mark.asyncio
async def test_format_repair_receives_original_instead_of_repeating_code_review() -> None:
    payload = {"hypotheses": [_hypothesis("wrong-argument")], "no_issue_units": []}
    malformed = json.dumps(payload)[:-1] + ",}"
    llm = _ScriptedLLM(responses=[malformed, json.dumps(payload)])
    result = await HypothesisGenerator(llm).run(
        StateStore(file_diffs=_server_diff()),
        ContextPack(),
        _changeset(_unit("service.py", "get_or_create_resource")),
        HypothesisLedger("run", "abc", "digest"),
    )
    assert result.accepted == 1
    repair = llm.calls[1]
    assert repair[1].type == "ai" and repair[1].content == malformed
    assert "return null" in repair[0].content
    assert all("## Changes" not in message.content for message in repair)


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
    generator = HypothesisGenerator(llm, max_input_chars=1000)
    ledger = HypothesisLedger("run", "abc", "digest")

    result = await generator.run(StateStore(file_diffs=diffs), ContextPack(), _changeset(first, second), ledger)

    assert result.blocks == 2
    assert len(ledger.items) == 1
    assert all(len(call[1].content) <= 1000 for call in llm.calls)
    second_block_text = "\n".join(getattr(message, "content", "") for message in llm.calls[1])
    assert "wrong-argument" in second_block_text
    assert "## Existing hypotheses" in second_block_text
    for call, unit in zip(llm.calls, (first, second), strict=True):
        checklist = call[1].content.split("## Required assessments (1)\n")[1]
        assert unit.id in checklist
        other = second if unit is first else first
        assert other.id not in checklist


@pytest.mark.asyncio
async def test_generator_sees_removed_guard_and_patch_markers() -> None:
    patch = (
        "@@ -1,4 +1,2 @@\n"
        " def get_or_create_resource(owner_id):\n"
        "-    if owner_id is None:\n"
        "-        raise ValueError('owner required')\n"
        "     return create_resource(owner_id)\n"
    )
    llm = _ScriptedLLM(responses=['{"hypotheses": [], "no_issue_units": []}'])
    await HypothesisGenerator(llm).run(
        StateStore(file_diffs={"service.py": patch}),
        ContextPack(),
        _changeset(_unit("service.py", "get_or_create_resource")),
        HypothesisLedger("run", "abc", "digest"),
    )

    prompt = str(llm.calls[0][1].content)
    assert "-    if owner_id is None:" in prompt
    assert "@@ -1,4 +1,2 @@" in prompt
    assert "2 |     return create_resource(owner_id)" in prompt


@pytest.mark.asyncio
async def test_shared_hunk_is_rendered_once_and_budgeted_once_for_distinct_units():
    first = _unit("big.py", "first", end_line=2)
    last = _unit("big.py", "last", end_line=150)
    last.start_line = 149
    source = ["def first():", *["    # " + "padding" * 42 for _ in range(148)], "def last():"]
    patch = _diff("big.py", *source)
    response = json.dumps(
        {"hypotheses": [], "no_issue_units": [{"unit_id": unit.id, "checked": "checked"} for unit in (first, last)]}
    )
    llm = _ScriptedLLM(responses=[response])
    ledger = HypothesisLedger("run", "abc", "digest")
    result = await HypothesisGenerator(llm, max_input_chars=60000).run(
        StateStore(file_diffs={"big.py": patch}), ContextPack(), _changeset(first, last), ledger
    )
    assert result.blocks == 1 and len(llm.calls) == 1
    prompt = llm.calls[0][1].content
    assert len(prompt) <= 60000
    assert prompt.count("@@ -0,0 +1,150 @@") == 1
    assert "1 | def first():" in prompt and "150 | def last():" in prompt
    assert prompt.count("def first():") == 1 and prompt.count("def last():") == 1
    assert set(ledger.no_issue_units) == {first.id, last.id}
    assert not ledger.unresolved_units


@pytest.mark.asyncio
async def test_shared_diff_preserves_removed_guards_without_unrelated_hunks():
    first = _unit("service.py", "first", end_line=2)
    last = _unit("service.py", "last", end_line=20)
    last.start_line = 20
    patch = (
        "@@ -1,3 +1,2 @@\n def first():\n-    validate(user_input)\n+    return user_input\n"
        "@@ -20,2 +20,1 @@\n-    check_owner(owner)\n+    return create_resource(owner_id)\n"
        "@@ -900 +900 @@\n+UNRELATED_CHANGE\n"
    )
    llm = _ScriptedLLM(responses=['{"hypotheses":[],"no_issue_units":[]}'])
    await HypothesisGenerator(llm).run(
        StateStore(file_diffs={"service.py": patch}),
        ContextPack(),
        _changeset(first, last),
        HypothesisLedger("run", "abc", "digest"),
    )
    prompt = llm.calls[0][1].content
    assert prompt.count("-    validate(user_input)") == 1
    assert prompt.count("-    check_owner(owner)") == 1
    assert "UNRELATED_CHANGE" not in prompt
    assert "2 |     return user_input" in prompt
    assert "20 |     return create_resource(owner_id)" in prompt


@pytest.mark.asyncio
async def test_shared_diff_retains_code_in_every_block_that_needs_it():
    first = _unit("big.py", "first", end_line=1)
    last = _unit("big.py", "last", end_line=300)
    last.start_line = 300
    patch = "@@ -0,0 +1,1 @@\n+" + "x" * 1500 + "\n@@ -299,0 +300,1 @@\n+" + "y" * 1500
    llm = _ScriptedLLM(responses=['{"hypotheses":[],"no_issue_units":[]}'] * 2)
    await HypothesisGenerator(llm, max_input_chars=2800).run(
        StateStore(file_diffs={"big.py": patch}),
        ContextPack(),
        _changeset(first, last),
        HypothesisLedger("run", "abc", "digest"),
    )
    assert len(llm.calls) == 2
    for call, code in zip(llm.calls, ("x" * 1500, "y" * 1500), strict=True):
        assert call[1].content.count(code) == 1
        assert len(call[1].content) <= 2800


@pytest.mark.asyncio
async def test_block_budget_counts_shared_context_once_without_restoring_omissions():
    first = _unit("a.py", "first")
    second = _unit("b.py", "second")
    shared = ContextSlice("caller", "caller.py", 1, 60, "run", "source_fact\n" * 900, "caller", "head")
    pack = ContextPack(units={unit.id: UnitContext(unit.id, [shared], ["schema"]) for unit in (first, second)})
    llm = _ScriptedLLM(
        responses=[
            json.dumps(
                {
                    "hypotheses": [],
                    "no_issue_units": [{"unit_id": u.id, "checked": "caller checked"} for u in (first, second)],
                }
            )
        ]
    )
    result = await HypothesisGenerator(llm, max_input_chars=18000).run(
        StateStore(file_diffs={"a.py": _diff("a.py", "def first():"), "b.py": _diff("b.py", "def second():")}),
        pack,
        _changeset(first, second),
        HypothesisLedger("run", "abc", "digest"),
    )
    assert result.blocks == 1 and len(llm.calls) == 1
    assert llm.calls[0][1].content.count(shared.text) == 1
    assert len(llm.calls[0][1].content) <= 18000
    assert "Same source as Unit a.py:first" in llm.calls[0][1].content
    assert all(context.truncated_kinds == ["schema"] for context in pack.units.values())


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [{"hypotheses": "invalid"}, {"hypotheses": [], "no_issue_units": 7}])
async def test_malformed_arrays_are_repaired_then_marked_unresolved(bad: dict) -> None:
    llm = _ScriptedLLM(responses=[json.dumps(bad), json.dumps(bad)])
    unit = _unit("service.py", "get_or_create_resource")
    ledger = HypothesisLedger("run", "abc", "digest")
    result = await HypothesisGenerator(llm).run(
        StateStore(file_diffs=_server_diff()), ContextPack(), _changeset(unit), ledger
    )

    assert len(llm.calls) == 2
    assert result.failed_blocks == 1
    assert unit.id in ledger.unresolved_units
    assert not ledger.no_issue_units


@pytest.mark.asyncio
async def test_generator_rejects_unknown_unit_even_with_real_site() -> None:
    hypothesis = _hypothesis("wrong-argument")
    hypothesis["unit_id"] = "invented.py:missing"
    llm = _ScriptedLLM(responses=[json.dumps({"hypotheses": [hypothesis], "no_issue_units": []})])
    ledger = HypothesisLedger("run", "abc", "digest")
    result = await HypothesisGenerator(llm).run(
        StateStore(file_diffs=_server_diff()),
        ContextPack(),
        _changeset(_unit("service.py", "get_or_create_resource")),
        ledger,
    )

    assert result.accepted == 0
    assert result.dropped_invalid == 1
    assert not ledger.items


@pytest.mark.asyncio
async def test_generator_shows_opaque_ids_and_full_tail_of_changed_unit():
    unit = _unit("service.py", "get_or_create_resource", end_line=601)
    unit.id = "su_0123456789abcdef"
    llm = _ScriptedLLM(responses=['{"hypotheses": [], "no_issue_units": []}'])
    source = ["def get_or_create_resource(owner_id):", *["    # unchanged padding" for _ in range(599)], _EXCERPT_LINE]
    await HypothesisGenerator(llm).run(
        StateStore(file_diffs={"service.py": _diff("service.py", *source)}),
        ContextPack(),
        _changeset(unit),
        HypothesisLedger("run", "abc", "digest"),
    )
    assert "Allowed unit_id values" in llm.calls[0][1].content
    assert "su_0123456789abcdef" in llm.calls[0][1].content
    assert "601 |" in llm.calls[0][1].content
    assert _EXCERPT_LINE in llm.calls[0][1].content


@pytest.mark.asyncio
async def test_lens_no_issue_does_not_erase_unresolved_generator_boundary():
    unit = _unit("service.py", "get_or_create_resource")
    ledger = HypothesisLedger("run", "abc", "digest")
    ledger.unresolved_units[unit.id] = "generator parse failure"
    llm = _ScriptedLLM(
        responses=[json.dumps({"hypotheses": [], "no_issue_units": [{"unit_id": unit.id, "checked": "lens clean"}]})]
    )
    await HypothesisGenerator(llm, source="lens:security").run(
        StateStore(file_diffs=_server_diff()), ContextPack(), _changeset(unit), ledger
    )
    assert unit.id in ledger.unresolved_units
    assert unit.id not in ledger.no_issue_units


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["generator", "lens:localization"])
async def test_valid_json_cannot_hide_units_omitted_from_the_response(source: str):
    # The real Keycloak response omitted these four changed test methods while
    # returning a valid JSON object for the other units in the same block.
    first = _unit("service.py", "get_or_create_resource")
    omitted = [
        _unit("tests.py", symbol)
        for symbol in (
            "verifyNoChangedAnchors",
            "verifyIllegalHtmlTagDetected",
            "verifyNoHtmlAllowed",
            "verifyDuplicateKeysDetected",
        )
    ]
    llm = _ScriptedLLM(responses=[json.dumps({"hypotheses": [_hypothesis("wrong-argument")], "no_issue_units": []})])
    ledger = HypothesisLedger("run", "abc", "digest")
    ledger.no_issue_units[omitted[0].id] = "earlier pass was clean"
    saved = []

    async def checkpoint(current):
        saved.append(current.to_dict())

    result = await HypothesisGenerator(llm, source=source, on_update=checkpoint).run(
        StateStore(file_diffs=_server_diff()), ContextPack(), _changeset(first, *omitted), ledger
    )

    assert result.accepted == 1
    assert result.failed_blocks == 1
    assert set(result.unresolved_units) == {unit.id for unit in omitted}
    assert ledger.unresolved_units == {unit.id: f"{source} missing unit assessment" for unit in omitted}
    assert not ledger.no_issue_units
    assert len(llm.calls) == 1  # No "look harder" or formatting-repair call.
    assert saved[-1]["unresolved_units"] == ledger.unresolved_units


@pytest.mark.asyncio
async def test_explicit_clean_assessments_complete_a_block_without_hypotheses():
    units = [_unit("service.py", "get_or_create_resource"), _unit("helper.py", "normalize")]
    llm = _ScriptedLLM(
        responses=[
            json.dumps(
                {
                    "hypotheses": [],
                    "no_issue_units": [{"unit_id": unit.id, "checked": "changed lines inspected"} for unit in units],
                }
            )
        ]
    )
    ledger = HypothesisLedger("run", "abc", "digest")
    result = await HypothesisGenerator(llm).run(
        StateStore(file_diffs=_server_diff()), ContextPack(), _changeset(*units), ledger
    )
    assert result.failed_blocks == 0
    assert not ledger.unresolved_units
    assert set(ledger.no_issue_units) == {unit.id for unit in units}
    assert len(llm.calls) == 1


@pytest.mark.asyncio
async def test_partial_assessment_does_not_clear_a_previous_failure_for_an_omitted_unit():
    first = _unit("service.py", "get_or_create_resource")
    second = _unit("helper.py", "normalize")
    ledger = HypothesisLedger("run", "abc", "digest")
    ledger.unresolved_units = {unit.id: "generator parse failure" for unit in (first, second)}
    llm = _ScriptedLLM(responses=[json.dumps({"hypotheses": [_hypothesis("wrong-argument")], "no_issue_units": []})])
    await HypothesisGenerator(llm).run(
        StateStore(file_diffs=_server_diff()), ContextPack(), _changeset(first, second), ledger
    )
    assert ledger.unresolved_units == {second.id: "generator missing unit assessment"}


@pytest.mark.asyncio
async def test_empty_or_unknown_clean_assessments_do_not_acknowledge_a_real_unit():
    unit = _unit("service.py", "get_or_create_resource")
    llm = _ScriptedLLM(
        responses=[
            json.dumps(
                {
                    "hypotheses": [],
                    "no_issue_units": [
                        {"unit_id": unit.id, "checked": " "},
                        {"unit_id": "invented", "checked": "looks clean"},
                    ],
                }
            )
        ]
    )
    ledger = HypothesisLedger("run", "abc", "digest")
    await HypothesisGenerator(llm).run(StateStore(file_diffs=_server_diff()), ContextPack(), _changeset(unit), ledger)
    assert ledger.unresolved_units == {unit.id: "generator missing unit assessment"}
    assert not ledger.no_issue_units
