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
