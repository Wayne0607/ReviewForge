import copy
import json
from dataclasses import replace

import pytest
from test_proof_gate import GateLLM, candidate, ledger, review

from reviewforge.engine.hypothesis import HypothesisLedger
from reviewforge.engine.proof_gate import ProofGate, accepted, completed
from reviewforge.engine.publication_contracts import contract_catalog
from reviewforge.engine.source_audit import messages, validate


def verdict(decision="accepted"):
    original = review(decision=decision)
    fields = {"accepted": ("obligations",), "refuted": ("counterevidence",), "unknown": ("missing_obligations",)}
    book = ledger(candidate(), protocol=4)
    book.source_audits["h_bug"] = {"facts": audit()}
    return {
        key: value
        for key, value in original.items()
        if key in {"hypothesis_id", "decision", "reason", *fields[decision]}
    } | ({"contract_refs": [next(iter(contract_catalog(candidate(), book)))]} if decision == "accepted" else {})


def audit(identity="h_bug"):
    return {
        "hypothesis_id": identity,
        "execution": [{"statement": "Unconditional value.id access.", "evidence": [f"{identity}/obs_0:e1"]}],
        "contracts": [
            {
                "kind": "caller",
                "statement": "The supplied interface allows an absent value.",
                "evidence": [f"{identity}/obs_1:e1"],
            }
        ],
        "gaps": [],
    }


def test_blind_input_contains_sources_without_candidate_or_assessment():
    item = candidate()
    item.claim = "PRIVATE_CLAIM"
    item.trigger = "PRIVATE_TRIGGER"
    item.assessment = replace(item.assessment, expected="PRIVATE_EXPECTED")
    item.discovery_basis = {"anything_future": "PRIVATE_BASIS"}
    text = messages([item], ledger(item))[-1].content
    assert "PRIVATE_" not in text
    assert "return value.id" in text and "value is optional" in text


@pytest.mark.parametrize("change", ["unknown_ref", "empty", "invented_kind"])
def test_reconstruction_cannot_invent_sources_or_contract_kinds(change):
    raw = audit()
    if change == "unknown_ref":
        raw["execution"][0]["evidence"] = ["other/obs_0:e1"]
    elif change == "empty":
        raw.update(execution=[], contracts=[], gaps=[])
    else:
        raw["contracts"][0]["kind"] = "variable-name"
    with pytest.raises(ValueError):
        validate(raw, candidate())


@pytest.mark.asyncio
async def test_v4_requires_blind_reconstruction_before_claim_review_and_persists_it():
    item = candidate()
    book = ledger(item, protocol=4)
    llm = GateLLM(replies=[{"audits": [audit()]}, {"reviews": [verdict()]}])
    await ProofGate(llm).run(book)
    assert len(llm.calls) == 2 and accepted(item, book)
    first = llm.calls[0][0][-1].content
    assert item.claim not in first and item.assessment.expected not in first
    final = json.loads(llm.calls[1][0][-1].content)["items"][0]
    assert final["independent_source_audit"] == audit()
    restored = HypothesisLedger.from_dict(book.to_dict())
    assert restored.source_audits == book.source_audits
    await ProofGate(llm).run(book)
    assert len(llm.calls) == 2
    book.source_audits[item.id]["facts"]["execution"][0]["statement"] = "Changed interpretation"
    assert not accepted(item, book)


@pytest.mark.asyncio
async def test_failed_source_audit_does_not_fall_through_to_claim_confirmation():
    item = candidate()
    book = ledger(item, protocol=4)
    llm = GateLLM(replies=['{"audits": ['])
    await ProofGate(llm).run(book)
    assert len(llm.calls) == 1 and not accepted(item, book) and not completed(item, book)
    assert book.source_audits[item.id]["outcome"] == "output-error"
    assert book.publication_reviews[item.id]["outcome"] == "output-error"


@pytest.mark.asyncio
async def test_source_audit_refutation_is_not_silently_promoted_or_retried():
    item = candidate()
    book = ledger(item, protocol=4)
    raw = copy.deepcopy(audit())
    raw["contracts"] = []
    raw["gaps"] = ["No caller requirement is supplied."]
    llm = GateLLM(replies=[{"audits": [raw]}])
    await ProofGate(llm).run(book)
    assert completed(item, book) and not accepted(item, book)
    await ProofGate(llm).run(book)
    assert len(llm.calls) == 1
    assert book.publication_reviews[item.id]["reason"] == "no-independent-contract-or-execution"


@pytest.mark.asyncio
async def test_invalid_contract_record_does_not_destroy_independent_valid_execution():
    item = candidate()
    book = ledger(item, protocol=4)
    raw = audit()
    raw["contracts"][0]["evidence"] = []
    llm = GateLLM(replies=[{"audits": [raw]}])
    await ProofGate(llm).run(book)
    saved = book.source_audits[item.id]
    assert saved["facts"]["execution"] == raw["execution"]
    assert not saved["facts"]["contracts"] and saved["rejected_records"]
    assert not accepted(item, book) and completed(item, book)
    assert len(llm.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["refuted", "unknown"])
async def test_non_acceptance_does_not_require_irrelevant_four_obligations(decision):
    item = candidate()
    book = ledger(item, protocol=4)
    llm = GateLLM(replies=[{"audits": [audit()]}, {"reviews": [verdict(decision)]}])
    await ProofGate(llm).run(book)
    assert completed(item, book) and not accepted(item, book)
    assert book.publication_reviews[item.id]["decision"] == decision


@pytest.mark.asyncio
@pytest.mark.parametrize("indices", [[], [1], [True], [0, 0]])
async def test_accepted_requires_an_actual_independent_contract_reference(indices):
    item = candidate()
    book = ledger(item, protocol=4)
    raw = verdict()
    raw["contract_refs"] = indices
    llm = GateLLM(replies=[{"audits": [audit()]}, {"reviews": [raw]}])
    await ProofGate(llm).run(book)
    assert not accepted(item, book) and not completed(item, book)
    assert book.publication_reviews[item.id]["outcome"] == "output-error"


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["closers", "missing-proof", "string", "extra-root", "duplicate"])
async def test_v4_delimiter_recovery_still_requires_complete_bound_proof(damage):
    item = candidate()
    book = ledger(item, protocol=4)
    value = verdict()
    if damage == "missing-proof":
        del value["obligations"]["bad_consequence"]
    raw = json.dumps({"reviews": [value]})
    if damage in {"closers", "missing-proof"}:
        raw = raw[:-3]  # Observed failure: closed obligations, missing verdict/root closers.
    elif damage == "string":
        raw = raw[: raw.rfind('"')]
    elif damage == "extra-root":
        raw += ' {"reviews":[]}'
    else:
        raw = raw.replace('"decision":', '"decision":"refuted","decision":', 1)[:-3]
    llm = GateLLM(replies=[{"audits": [audit()]}, raw])
    await ProofGate(llm).run(book)
    assert len(llm.calls) == 2
    assert accepted(item, book) == (damage == "closers")
    if damage == "closers":
        assert book.publication_reviews[item.id]["output_framing"]["recovery"] == "missing-container-closers"
    if damage != "closers":
        assert book.publication_reviews[item.id]["outcome"] == "output-error"
