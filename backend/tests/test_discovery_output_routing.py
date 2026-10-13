"""Block cardinality chooses one wire contract before planning or dispatch."""

from __future__ import annotations

import json

import pytest
from langchain_core.messages import AIMessage
from pydantic import Field
from test_discovery_submission import call, fixture, hypothesis
from test_discovery_v3 import ScriptedLLM, candidate, clean

from reviewforge.engine.context_pack import ContextPack
from reviewforge.engine.hypothesis import HypothesisLedger
from reviewforge.engine.hypothesis_generator import HypothesisGenerator, discovery_uses_native


class RoutedModel(ScriptedLLM):
    options: list[dict] = Field(default_factory=list)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.options.append(kwargs)
        return super()._generate(messages, stop, run_manager, **kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol,count", [(4, 1), (4, 2), (3, 1), (3, 2)])
async def test_singleton_native_and_multiunit_json_are_selected_before_request(protocol, count):
    state, changes, block, aliases = fixture(count)
    native = protocol == 4 and count == 1
    body = {"hypotheses": [candidate(block.units[0])], "no_issue_units": [clean(item) for item in block.units[1:]]}
    response = (
        AIMessage(content="", tool_calls=[call("submit_hypothesis", hypothesis(block, aliases))])
        if native
        else json.dumps(body)
    )
    model = RoutedModel(responses=[response])
    ledger = HypothesisLedger("run", "abc", "digest", protocol_version=protocol)
    result = await HypothesisGenerator(model).run(state, ContextPack(), changes, ledger)
    assert result.accepted == 1 and not ledger.unresolved_units and len(model.calls) == 1
    assert discovery_uses_native(protocol, count) is native
    assert bool(model.bindings) is native and ("tools" in model.options[0]) is native
    assert ("tool_choice" in model.options[0]) is native
    if native:
        assert len(model.options[0]["tools"]) == 3
    record = next(iter(ledger.discovery_blocks.values()))
    if protocol >= 4:
        assert record["submission_mode"] == ("native" if native else "batch-text")
    assert "repair_plan" not in record
    assert next(iter(ledger.items.values())).status.value == "open"


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["missing-unit", "root-closer", "bad-tail", "unparseable"])
async def test_multiunit_batch_keeps_strict_coverage_and_never_switches_payload(shape):
    state, changes, block, _ = fixture(2)
    payload = {"hypotheses": [candidate(block.units[0])], "no_issue_units": [clean(block.units[1])]}
    if shape == "missing-unit":
        payload["no_issue_units"] = []
    raw = json.dumps(payload)
    if shape == "root-closer":
        raw = raw[:-1]
    elif shape == "bad-tail":
        raw = raw[:-1] + ',"deferred_units":[{"unit_id":"cut'
    elif shape == "unparseable":
        raw = "unparseable"
    model = RoutedModel(responses=[raw])
    ledger = HypothesisLedger("run", "abc", "digest", protocol_version=4)
    result = await HypothesisGenerator(model).run(state, ContextPack(), changes, ledger)
    assert len(model.calls) == 1 and not model.bindings and "tools" not in model.options[0]
    assert result.accepted == (shape != "unparseable")
    assert bool(ledger.unresolved_units) == (shape != "root-closer")
    if shape == "bad-tail":
        assert not ledger.no_issue_units and len(ledger.unresolved_units) == 2
    assert "repair_plan" not in next(iter(ledger.discovery_blocks.values()))


@pytest.mark.asyncio
async def test_unrequested_native_calls_cannot_override_multiunit_text_contract():
    state, changes, block, aliases = fixture(2)
    body = json.dumps({"hypotheses": [], "no_issue_units": [clean(item) for item in block.units]})
    model = RoutedModel(
        responses=[AIMessage(content=body, tool_calls=[call("submit_hypothesis", hypothesis(block, aliases))])]
    )
    ledger = HypothesisLedger("run", "abc", "digest", protocol_version=4)
    result = await HypothesisGenerator(model).run(state, ContextPack(), changes, ledger)
    assert not result.accepted and not ledger.no_issue_units and len(ledger.unresolved_units) == 2
    assert len(model.calls) == 1 and not model.bindings and "tools" not in model.options[0]


def test_system_rules_preserve_coverage_and_facts_in_both_preselected_modes():
    generator = HypothesisGenerator(None)
    native = generator._discovery_system_prompt(12, protocol_version=4, unit_count=1)
    batch = generator._discovery_system_prompt(12, protocol_version=4, unit_count=2)
    assert '"hypotheses"' in batch and '"no_issue_units"' in batch and '"deferred_units"' in batch
    assert "submit_hypothesis" in native and "submit_hypothesis" not in batch
    assert "source_facts" in batch and "change_facts" in batch
    assert "1–12 refs" in batch and "refutation" in batch
    assert "never clean" in batch and "never clean" in native
    assert discovery_uses_native(4, 0) is False
