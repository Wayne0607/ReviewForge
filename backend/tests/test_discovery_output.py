import json

import pytest

from reviewforge.engine.discovery_output import frame_discovery_output


def test_missing_only_outer_brace_preserves_every_complete_value():
    value = {"hypotheses": [{"unit_id": "u", "claim": "Italian {text}"}], "no_issue_units": []}
    raw = json.dumps(value)[:-1]
    result = frame_discovery_output(raw)
    assert result.payload == value
    assert result.complete and result.recovery == "missing-root-closer"
    assert raw.endswith("]")  # Caller owns the immutable original.


def test_truncated_item_never_becomes_a_completed_assessment():
    raw = '{"hypotheses":[{"unit_id":"u","claim":"fact"},{"unit_id":"v","claim":"cut'
    result = frame_discovery_output(raw)
    assert result.payload == {"hypotheses": [{"unit_id": "u", "claim": "fact"}]}
    assert not result.complete
    assert result.recovery == "complete-record-prefix"


@pytest.mark.parametrize(
    "raw",
    [
        '{"hypotheses":[],"hypotheses":[{}]}',
        '{"hypotheses":[{"unit_id":"u","unit_id":"v"}]}',
        'prefix {"hypotheses":[]} suffix',
        '{"hypotheses":[]} {"hypotheses":[{}]}',
        '{"hypotheses":null}',
        '{"hypotheses":[{"unit_id":"unfinished',
    ],
)
def test_ambiguous_envelopes_and_invented_values_are_not_salvaged(raw):
    assert frame_discovery_output(raw) is None


def test_exact_fence_and_array_members_with_json_delimiters():
    value = {"hypotheses": [{"claim": 'text ]} \\" and \\n'}], "deferred_units": []}
    result = frame_discovery_output("```json\n" + json.dumps(value) + "\n```")
    assert result.payload == value and result.complete


def test_complete_record_array_preserves_records_and_explicit_categories():
    records = [
        {"unit_id": "u", "claim": "Italian text", "behavior": {"after": "unchanged"}},
        {"unit_id": "v", "checked": "literal"},
        {"unit_id": "w", "reason": "missing source"},
    ]
    result = frame_discovery_output(json.dumps(records))
    assert result.complete and result.recovery == "record-array-envelope"
    assert result.payload == {"hypotheses": records[:1], "no_issue_units": records[1:2], "deferred_units": records[2:]}


@pytest.mark.parametrize(
    "raw",
    [
        "[]",
        "[{}]",
        '[{"unit_id":"u"}]',
        '[{"unit_id":"u","claim":"x","checked":"y"}]',
        '[{"unit_id":"u","claim":"x","claim":"y"}]',
        '[{"unit_id":"u","claim":"x"}] {}',
        '[{"unit_id":"u","claim":"x"},',
        '[{"unit_id":null,"claim":"x"}]',
    ],
)
def test_record_array_never_guesses_ambiguous_or_incomplete_records(raw):
    assert frame_discovery_output(raw) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["missing-closer", "truncated", "record-array"])
async def test_generator_keeps_complete_candidates_without_model_repair(shape):
    from test_discovery_v3 import ScriptedLLM, candidate, changeset, diff, unit

    from reviewforge.core.state import StateStore
    from reviewforge.engine.context_pack import ContextPack
    from reviewforge.engine.hypothesis import HypothesisLedger
    from reviewforge.engine.hypothesis_generator import HypothesisGenerator

    u = unit("app.py")
    raw = json.dumps({"hypotheses": [candidate(u)], "no_issue_units": []})[:-1]
    if shape == "truncated":
        raw += ',"deferred_units":[{"unit_id":"cut'
    elif shape == "record-array":
        raw = json.dumps([candidate(u)])
    model = ScriptedLLM(responses=[raw])
    ledger = HypothesisLedger("run", "abc", "digest", protocol_version=3)
    result = await HypothesisGenerator(model).run(
        StateStore(file_diffs={u.path: diff()}), ContextPack(), changeset(u), ledger
    )
    assert result.accepted == 1 and len(model.calls) == 1
    assert bool(ledger.unresolved_units) == (shape == "truncated")
    assert not ledger.no_issue_units
    assert next(iter(ledger.items.values())).status.value == "open"


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol_version", [3, 4])
@pytest.mark.parametrize("shape", ["duplicate-root", "duplicate-reference", "truncated-record"])
async def test_repair_cannot_turn_ambiguous_or_incomplete_output_into_clean_coverage(protocol_version, shape):
    from test_discovery_v3 import ScriptedLLM, changeset, clean, diff, unit

    from reviewforge.core.state import StateStore
    from reviewforge.engine.context_pack import ContextPack
    from reviewforge.engine.discovery import DiscoveryLimits, plan_discovery
    from reviewforge.engine.discovery_aliases import source_aliases
    from reviewforge.engine.hypothesis import HypothesisLedger
    from reviewforge.engine.hypothesis_generator import HypothesisGenerator

    u = unit("app.py")
    block = plan_discovery(changeset(u), ContextPack(), {u.path: diff()}, limits=DiscoveryLimits())[0]
    aliases = source_aliases(block.source_refs)
    references = [alias if protocol_version >= 4 else ref for alias, ref in aliases.items()]
    assessment = clean(u)
    assessment["behavior"]["source_facts"] = [{"source_ref": ref} for ref in references]
    raw = json.dumps({"hypotheses": [], "no_issue_units": [assessment]})
    if shape == "duplicate-root":
        raw = raw.replace('"no_issue_units":', '"no_issue_units": [], "no_issue_units":', 1)
    elif shape == "duplicate-reference":
        token = json.dumps(references[0])
        raw = raw.replace(f'"source_ref": {token}', f'"source_ref": "invalid", "source_ref": {token}', 1)
    else:
        raw = raw[:-1] + ',"deferred_units":[{"unit_id":"cut'
    model = ScriptedLLM(responses=["unparseable original", raw])
    ledger = HypothesisLedger("run", "abc", "digest", protocol_version=protocol_version)
    result = await HypothesisGenerator(model).run(
        StateStore(file_diffs={u.path: diff()}), ContextPack(), changeset(u), ledger
    )
    assert len(model.calls) == 2 and result.accepted == 0
    assert not ledger.no_issue_units and u.id in ledger.unresolved_units
    record = next(iter(ledger.discovery_blocks.values()))
    assert record["status"] == "unresolved"
    assert record["repair_output_framing"] == {
        "complete": False,
        "recovery": "complete-record-prefix" if shape == "truncated-record" else "invalid",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol_version", [3, 4])
@pytest.mark.parametrize("shape", ["complete", "missing-root-closer", "record-array"])
async def test_complete_repair_keeps_reference_identity_and_records_framing(protocol_version, shape):
    from test_discovery_v3 import ScriptedLLM, changeset, clean, diff, unit

    from reviewforge.core.state import StateStore
    from reviewforge.engine.context_pack import ContextPack
    from reviewforge.engine.discovery import DiscoveryLimits, plan_discovery
    from reviewforge.engine.discovery_aliases import source_aliases
    from reviewforge.engine.hypothesis import HypothesisLedger
    from reviewforge.engine.hypothesis_generator import HypothesisGenerator

    u = unit("app.py")
    block = plan_discovery(changeset(u), ContextPack(), {u.path: diff()}, limits=DiscoveryLimits())[0]
    aliases = source_aliases(block.source_refs)
    assessment = clean(u)
    assessment["behavior"]["source_facts"] = [
        {"source_ref": alias if protocol_version >= 4 else ref} for alias, ref in aliases.items()
    ]
    raw = json.dumps({"hypotheses": [], "no_issue_units": [assessment]})
    if shape == "missing-root-closer":
        raw = raw[:-1]
    elif shape == "record-array":
        raw = json.dumps([assessment])
    model = ScriptedLLM(responses=["unparseable original", raw])
    ledger = HypothesisLedger("run", "abc", "digest", protocol_version=protocol_version)
    await HypothesisGenerator(model).run(StateStore(file_diffs={u.path: diff()}), ContextPack(), changeset(u), ledger)
    assert len(model.calls) == 2 and not ledger.unresolved_units
    assert ledger.no_issue_units == {u.id: assessment["checked"]}
    record = next(iter(ledger.discovery_blocks.values()))
    assert record["status"] == "completed"
    assert record["repair_output_framing"] == {
        "complete": True,
        "recovery": {"complete": "none", "record-array": "record-array-envelope"}.get(shape, shape),
    }
    saved_refs = record["units"][u.id]["behavior"]["source_facts"]
    assert {item["source_ref"] for item in saved_refs} == set(block.source_refs)


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol_version", [1, 2])
async def test_legacy_repair_parser_contract_remains_unchanged(protocol_version):
    from test_discovery_v3 import ScriptedLLM

    from reviewforge.engine.hypothesis import HypothesisLedger
    from reviewforge.engine.hypothesis_generator import HypothesisGenerator

    raw = '{"hypotheses":[{}],"hypotheses":[],"no_issue_units":[]}'
    record = {}
    model = ScriptedLLM(responses=[raw])
    parsed = await HypothesisGenerator(model)._invoke_repair(
        "unparseable original",
        ledger=HypothesisLedger("run", "abc", "digest", protocol_version=protocol_version),
        record=record,
    )
    assert parsed == {"hypotheses": [], "no_issue_units": []}
    assert "repair_output_framing" not in record
