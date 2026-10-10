from __future__ import annotations

import copy
import hashlib
import json

import pytest

from reviewforge.core.state import StateStore
from reviewforge.engine.editor import confirmed_fact_digest
from reviewforge.engine.hypothesis import Hypothesis, Mechanism, Observation, Site
from reviewforge.engine.investigator import Investigator, _evidence_segments


def _hypothesis():
    return Hypothesis(
        "h",
        "unit::contract-mismatch::transform",
        "unit",
        Mechanism.CONTRACT_MISMATCH,
        "The return violates the valid-input contract",
        "A valid input",
        "Wrong result",
        "What does valid input return?",
        "A caller permits None",
        [Site("a.py", 2, "    return None")],
        "error",
        "generator",
    )


def _observation(identity, path, source, *, tool="read_file", status="success"):
    return Observation(identity, tool, "q", path, (1, 3), "head", "digest", source, status)


def _worker():
    worker = Investigator(None, None)
    worker._observations = [
        _observation("obs_0", "tests/test_contract.py", "    assert transform('有效输入') == 'ready'\r\n"),
        _observation("obs_1", "a.py", "def transform(value):\n    return None\n"),
    ]
    return worker


def _verdict():
    return {
        "verdict": "confirmed",
        "assessment": {
            "expected": "Valid input returns ready",
            "actual": "The implementation returns None",
            "comparison": "conflict",
            "expected_evidence": ["obs_0:e1"],
            "actual_evidence": ["obs_1:e1"],
        },
    }


def test_refs_compile_original_source_with_unicode_indentation_and_newlines():
    worker = _worker()
    result = worker._finalize(_verdict(), _hypothesis(), {"a.py"}, steps=2)
    assert result.verdict == "confirmed" and result.strength == "strong"
    assert result.assessment.expected_evidence[0].quote == worker._observations[0].excerpt
    assert result.assessment.actual_evidence[0].quote == worker._observations[1].excerpt
    assert result.evidence_ids == ["obs_0", "obs_1"]


@pytest.mark.parametrize("reference", ["obs_99:e1", "obs_0:e99", "obs_0:e1 ", "obs_0:e1+obs_1:e1"])
def test_unknown_or_constructed_references_cannot_close(reference):
    raw = _verdict()
    raw["assessment"]["actual_evidence"] = ["obs_1:e1", reference]
    result = _worker()._finalize(raw, _hypothesis(), {"a.py"}, steps=2)
    assert result.verdict == "unknown" and result.reason == "ungrounded-assessment"
    assert result.strength == "none"


@pytest.mark.parametrize("status", ["not_found", "error"])
def test_negative_observation_cannot_mint_a_reference(status):
    worker = _worker()
    worker._observations[1] = _observation("obs_1", "a.py", "No results", status=status)
    result = worker._finalize(_verdict(), _hypothesis(), {"a.py"}, steps=2)
    assert not _evidence_segments(worker._observations[1])
    assert result.verdict == "unknown" and result.reason == "ungrounded-assessment"


@pytest.mark.parametrize("placement", ["premise", "legacy-top-level"])
def test_valid_refs_do_not_hide_explicit_invalid_legacy_quotes(placement):
    raw = _verdict()
    collapsed = "def transform(value): return None"
    if placement == "premise":
        raw["assessment"]["actual_evidence"].append({"observation_id": "obs_1", "quote": collapsed})
    else:
        raw.update(evidence_ids=["obs_1"], evidence_quote=collapsed)
    result = _worker()._finalize(raw, _hypothesis(), {"a.py"}, steps=2)
    assert result.verdict == "unknown" and result.reason.startswith("ungrounded")


@pytest.mark.parametrize("verdict", ["refuted", "unknown"])
def test_refs_preserve_verdict_relation_and_never_promote_unknown(verdict):
    raw = _verdict()
    raw["verdict"] = verdict
    result = _worker()._finalize(raw, _hypothesis(), {"a.py"}, steps=2)
    assert result.verdict == "unknown" and result.strength == "none"
    if verdict == "refuted":
        raw["assessment"]["comparison"] = "compatible"
        assert _worker()._finalize(raw, _hypothesis(), {"a.py"}, steps=2).verdict == "refuted"


@pytest.mark.asyncio
async def test_tool_and_closure_refs_resolve_only_the_saved_body():
    source = "  2024: literal prefix\n" + "x" * 1178 + "unsaved_fact()"

    async def execute(name, args):
        return source

    worker = Investigator(None, execute)
    worker._state = StateStore(repo="o/r", pr_number=1, head_sha="head")
    view = await worker._run_tool("read_file", {"path": "a.py", "start": 1, "end": 20})
    observation = worker._observations[0]
    assert observation.excerpt == source[:1200]
    assert observation.result_digest == hashlib.sha256(source.encode()).hexdigest()[:16]
    assert "unsaved_fact()" not in view and "[obs_0:e1]" in view
    assert "  2024: literal prefix\n" in view
    cards = _evidence_segments(observation)
    assert "".join(citation.quote for citation in cards.values()) == observation.excerpt
    closure = worker._closing_chat([], 24000)
    assert "unsaved_fact()" not in str(closure)
    for reference in cards:
        assert reference in str(closure)


@pytest.mark.parametrize("tool", ["grep", "find_callers", "find_definition"])
def test_search_refs_keep_file_boundaries_and_only_cited_outside_source_is_strong(tool):
    source = (
        "- transform [function] a.py:1\n  return None\n- unrelated [function] other.py:2\n  return False\n"
        if tool == "find_definition"
        else "- a.py:1: return None\n- other.py:2: return False\n"
    )
    worker = _worker()
    worker._observations = [_observation("obs_0", "", source, tool=tool)]
    cards = _evidence_segments(worker._observations[0])
    assert len(cards) == 2
    assert not worker._quote_outside_diff(worker._observations[0], cards["obs_0:e1"].quote, {"a.py"})
    assert worker._quote_outside_diff(worker._observations[0], cards["obs_0:e2"].quote, {"a.py"})


def test_compiled_quotes_and_fact_digest_survive_checkpoint_round_trip():
    worker = _worker()
    result = worker._finalize(_verdict(), _hypothesis(), {"a.py"}, steps=2)
    hypothesis = _hypothesis()
    hypothesis.assessment = result.assessment
    hypothesis.observations = copy.deepcopy(result.observations)
    encoded = json.loads(json.dumps(hypothesis.to_dict()))
    assert isinstance(encoded["assessment"]["actual_evidence"][0], dict)
    restored = Hypothesis.from_dict(encoded)
    assert restored.assessment == result.assessment
    assert confirmed_fact_digest(restored) == confirmed_fact_digest(hypothesis)


@pytest.mark.parametrize(
    ("comparison", "verdict"), [("conflict", "confirmed"), ("compatible", "refuted"), ("unresolved", "unknown")]
)
def test_new_assessment_output_has_one_decision(comparison, verdict):
    raw = _verdict()
    raw.pop("verdict")
    raw["assessment"]["comparison"] = comparison
    assert Investigator._parse_verdict(json.dumps(raw)) == raw
    result = _worker()._finalize(raw, _hypothesis(), {"a.py"}, steps=2)
    assert result.verdict == verdict
    if verdict != "unknown":
        assert result.assessment and result.strength == "strong"
        assert result.evidence_ids == ["obs_0", "obs_1"]
    else:
        assert result.strength == "none"


@pytest.mark.parametrize("failure", ["unknown-ref", "negative-result", "missing-premise", "invalid-legacy"])
def test_derived_decision_still_requires_both_grounded_premises(failure):
    raw = _verdict()
    raw.pop("verdict")
    worker = _worker()
    if failure == "unknown-ref":
        raw["assessment"]["actual_evidence"] = ["obs_99:e1"]
    elif failure == "negative-result":
        worker._observations[1] = _observation("obs_1", "a.py", "No results", status="not_found")
    elif failure == "missing-premise":
        raw["assessment"].pop("expected")
    else:
        raw.update(evidence_ids=["obs_1"], evidence_quote="invented quote")
    result = worker._finalize(raw, _hypothesis(), {"a.py"}, steps=2)
    assert result.verdict == "unknown" and result.strength == "none"
    assert result.reason == (
        "incomplete-assessment"
        if failure == "missing-premise"
        else "ungrounded"
        if failure == "invalid-legacy"
        else "ungrounded-assessment"
    )


@pytest.mark.parametrize("comparison", ["conflict|compatible", "Compatible", True, [], {}, None])
def test_invalid_or_ambiguous_relation_cannot_create_a_verdict(comparison):
    raw = _verdict()
    raw.pop("verdict")
    raw["assessment"]["comparison"] = comparison
    result = _worker()._finalize(raw, _hypothesis(), {"a.py"}, steps=2)
    assert result.verdict == "unknown" and result.strength == "none"


@pytest.mark.parametrize(
    ("verdict", "comparison"), [("confirmed", "compatible"), ("refuted", "conflict"), ("unknown", "conflict")]
)
def test_explicit_legacy_verdict_is_never_rewritten(verdict, comparison):
    raw = _verdict()
    raw["verdict"] = verdict
    raw["assessment"]["comparison"] = comparison
    result = _worker()._finalize(raw, _hypothesis(), {"a.py"}, steps=2)
    assert result.verdict == "unknown" and result.strength == "none"
    if verdict != "unknown":
        assert result.reason == "inconsistent-assessment"


@pytest.mark.parametrize(
    "raw", [{"assessment": None, "answer": "No proof"}, {"verdict": "unknown"}, {"answer": "No proof"}]
)
def test_decision_parser_preserves_unknown_and_legacy_shapes(raw):
    parsed = Investigator._parse_verdict(json.dumps(raw))
    assert parsed == (raw if "assessment" in raw or "verdict" in raw else None)
