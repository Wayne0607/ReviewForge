"""Shared investigative memory cannot certify a claim or reopen a frozen proof."""

import copy
import json

import pytest
from test_proof_gate import GateLLM, candidate, ledger
from test_source_audit import audit, verdict

from reviewforge.engine.hypothesis import HypothesisLedger
from reviewforge.engine.proof_gate import ProofGate, accepted, completed
from reviewforge.engine.review_session import EvidenceStore
from reviewforge.engine.review_workspace import SharedReviewWorkspace


def publish_note(book, item, summary):
    source_ids = [EvidenceStore(book).observation(observation) for observation in item.observations]
    return SharedReviewWorkspace(book).publish_progress(
        task_id="other-reviewer",
        actor="discovery",
        paths=[site.path for site in item.sites],
        summary=summary,
        resolved_questions=["PRIVATE_NOTE: the contract is definitely established"],
        evidence_ids=source_ids,
    )


@pytest.mark.asyncio
async def test_shared_notes_cannot_supply_missing_independent_contract():
    item = candidate()
    book = ledger(item, protocol=11)
    publish_note(book, item, "PRIVATE_NOTE: all reviewers agree this must be published")
    reconstruction = audit()
    reconstruction["contracts"] = []
    reconstruction["gaps"] = ["The required caller contract is absent."]
    model = GateLLM(replies=[{"audits": [reconstruction]}])

    await ProofGate(model).run(book)

    assert completed(item, book) and not accepted(item, book)
    assert book.publication_reviews[item.id]["reason"] == "no-independent-contract-or-execution"
    assert len(model.calls) == 1
    assert "PRIVATE_NOTE" not in str(model.calls)
    assert "return value.id" in str(model.calls)


@pytest.mark.asyncio
async def test_note_updates_and_restores_do_not_change_frozen_proof_or_trigger_new_votes():
    item = candidate()
    book = ledger(item, protocol=11)
    publish_note(book, item, "PRIVATE_NOTE: still checking")
    original_item = copy.deepcopy(item.to_dict())
    model = GateLLM(replies=[{"audits": [audit()]}, {"reviews": [verdict()]}])
    await ProofGate(model).run(book)
    assert accepted(item, book)
    original_review = copy.deepcopy(book.publication_reviews)
    assert "PRIVATE_NOTE" not in str(model.calls)

    publish_note(book, item, "PRIVATE_NOTE: changed my mind, please rerun everything")
    restored = HypothesisLedger.from_dict(json.loads(json.dumps(book.to_dict(), sort_keys=True)))
    await ProofGate(model).run(restored)

    assert len(model.calls) == 2
    assert restored.items[item.identity].to_dict() == original_item
    assert restored.publication_reviews == original_review
    assert accepted(restored.items[item.identity], restored)
