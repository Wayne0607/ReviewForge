from __future__ import annotations

import copy

import pytest

from reviewforge.core.state import StateStore
from reviewforge.engine.context_pack import ContextPack
from reviewforge.engine.editor import Editor, cluster_confirmed, confirmed_fact_digest, fallback_comment
from reviewforge.engine.hypothesis import Hypothesis, HypothesisLedger, Mechanism, Observation, Site
from reviewforge.engine.investigator import Investigator


def _worker():
    worker = Investigator(None, None)
    worker._observations = [
        Observation(
            "obs_0",
            "read_file",
            "contract",
            "tests/contract.py",
            (1, 1),
            "head",
            "a",
            "assert transform('valid') == 'ready'",
            "success",
        ),
        Observation(
            "obs_1",
            "read_file",
            "actual",
            "a.py",
            (1, 2),
            "head",
            "b",
            "def transform(value):\n    return None",
            "success",
        ),
    ]
    return worker


def _hypothesis():
    return Hypothesis(
        "h",
        "unit::contract-mismatch::transform",
        "unit",
        Mechanism.CONTRACT_MISMATCH,
        "Valid input no longer produces the required string",
        "transform('valid')",
        "The caller receives None",
        "What does the valid-input contract require?",
        "A caller explicitly accepts None",
        [Site("a.py", 2, "    return None")],
        "error",
        "generator",
    )


def _verdict():
    return {
        "verdict": "confirmed",
        "evidence_ids": ["obs_1"],
        "evidence_quote": "return None",
        "severity": "error",
        "answer": "The valid-input test requires 'ready'.",
        "reason": "The changed return value violates that test's contract.",
        "assessment": {
            "expected": "Valid input produces 'ready'.",
            "actual": "The changed implementation returns None.",
            "comparison": "conflict",
            "expected_evidence": [{"observation_id": "obs_0", "quote": "assert transform('valid') == 'ready'"}],
            "actual_evidence": [{"observation_id": "obs_1", "quote": "return None"}],
        },
    }


def test_answering_a_fact_cannot_confirm_without_a_contract_comparison():
    raw = _verdict()
    raw.pop("assessment")
    result = _worker()._finalize(raw, _hypothesis(), {"a.py"}, steps=2)
    assert result.verdict == "unknown"
    assert result.reason == "incomplete-assessment"


@pytest.mark.parametrize(
    "change",
    ["missing-contract", "invented-quote", "unknown-observation", "not-found", "unsaved-quote", "wrong-verdict"],
)
def test_each_premise_requires_its_own_successful_exact_citation(change):
    raw, worker = _verdict(), _worker()
    if change == "missing-contract":
        raw["assessment"]["expected_evidence"] = []
    elif change == "invented-quote":
        raw["assessment"]["expected_evidence"][0]["quote"] = "assert transform('invalid') == 'ready'"
    elif change == "unknown-observation":
        raw["assessment"]["expected_evidence"][0]["observation_id"] = "obs_99"
    elif change == "not-found":
        worker._observations[0] = Observation("obs_0", "grep", "q", "", None, "head", "x", "No results", "not_found")
        raw["assessment"]["expected_evidence"][0]["quote"] = "No results"
    elif change == "unsaved-quote":
        raw["assessment"]["expected_evidence"][0]["quote"] = "proof_in_unsaved_context()"
    else:
        raw["assessment"]["comparison"] = "compatible"
    result = worker._finalize(raw, _hypothesis(), {"a.py"}, steps=2)
    assert result.verdict == "unknown"
    assert result.strength == "none"


def test_a_proved_contract_and_changed_behavior_can_confirm():
    raw = _verdict()
    result = _worker()._finalize(raw, _hypothesis(), {"a.py"}, steps=2)
    assert result.verdict == "confirmed"
    assert result.strength == "strong"
    assert result.assessment.expected == "Valid input produces 'ready'."
    raw["assessment"]["actual_evidence"].clear()
    assert len(result.assessment.actual_evidence) == 1


def test_refutation_cannot_use_a_successful_unrelated_read_to_mask_missing_contract():
    raw = _verdict()
    raw["verdict"] = "refuted"
    raw["assessment"]["comparison"] = "compatible"
    raw["assessment"]["expected_evidence"] = []
    result = _worker()._finalize(raw, _hypothesis(), {"a.py"}, steps=2)
    assert result.verdict == "unknown"


def test_unknown_does_not_require_or_get_promoted_by_an_assessment():
    raw = _verdict()
    raw["verdict"] = "unknown"
    complete = copy.deepcopy(raw)
    raw.pop("assessment")
    for item in (raw, complete):
        result = _worker()._finalize(item, _hypothesis(), {"a.py"}, steps=2)
        assert result.verdict == "unknown" and result.strength == "none"


@pytest.mark.asyncio
async def test_assessment_reaches_persisted_ledger_editor_and_fallback(tmp_path, monkeypatch):
    from reviewforge.core.database import Database

    worker = _worker()
    result = worker._finalize(_verdict(), _hypothesis(), {"a.py"}, steps=2)

    async def investigate(*args, **kwargs):
        return result

    monkeypatch.setattr(Investigator, "investigate", investigate)
    ledger = HypothesisLedger("run", "head", "digest")
    ledger.upsert(_hypothesis())
    db = Database(tmp_path / "ledger.db")
    await db.connect()
    try:
        await db.create_run("run", "owner/repo", 1, "head")
        await worker.run(
            ledger,
            StateStore(repo="owner/repo", pr_number=1, head_sha="head", files_changed=["a.py"]),
            ContextPack(),
            on_update=db.checkpoint_hypothesis_ledger,
        )
        restored = await db.load_hypothesis_ledger("run")
        assert restored is not None and restored.to_dict() == ledger.to_dict()
        hypothesis = restored.items[_hypothesis().identity]
        assert hypothesis.assessment == result.assessment
        result.assessment.actual_evidence.clear()
        assert len(hypothesis.assessment.actual_evidence) == 1
        clusters = cluster_confirmed(restored)
        rendered = Editor(None)._render(clusters, clusters, [], ContextPack(), restored)
        for text in (rendered, fallback_comment(clusters[0]).body):
            assert "Valid input produces 'ready'." in text
            assert "assert transform('valid') == 'ready'" in text
        changed = copy.deepcopy(hypothesis)
        changed.assessment = None
        assert confirmed_fact_digest(changed) != confirmed_fact_digest(hypothesis)
    finally:
        await db.close()


def test_old_closed_checkpoint_can_load_without_reopening_or_inventing_a_proof():
    raw = _hypothesis().to_dict()
    raw.pop("assessment")
    raw["status"] = "confirmed"
    restored = Hypothesis.from_dict(raw)
    assert restored.status == "confirmed" and restored.assessment is None


def test_author_intent_is_bounded_context_and_retained_for_closure():
    from langchain_core.messages import HumanMessage

    worker = _worker()
    intent = "Remove unsafe markup; preserve supported behavior. " + "x" * 2_050
    state = StateStore(repo="owner/repo", pr_number=1, head_sha="head")
    user = worker._render_user(_hypothesis(), state, ContextPack(pr_intent=intent))
    assert "author context, not defect evidence" in user
    assert intent[:2_000] in user and intent not in user
    assert intent[:2_000] in str(worker._closing_chat([HumanMessage(content=user)], 24_000))
    assert "## PR intent" not in worker._render_user(_hypothesis(), state, ContextPack())
