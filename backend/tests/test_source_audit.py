import copy
import json
from dataclasses import replace

import pytest
from test_proof_gate import GateLLM, candidate, ledger, review

from reviewforge.engine.hypothesis import HypothesisLedger
from reviewforge.engine.proof_gate import ProofGate, accepted, completed
from reviewforge.engine.publication_contracts import contract_catalog
from reviewforge.engine.source_audit import consume, current, input_fingerprint, messages, run_source_audit, validate


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


def test_caveat_total_budget_preserves_long_individual_gap_without_changing_evidence():
    raw = audit()
    raw["gaps"] = ["The concrete binding remains unverified. " * 20]
    assert len(raw["gaps"][0]) > 600
    saved = validate(raw, candidate())
    assert saved["gaps"] == [raw["gaps"][0].strip()]
    assert saved["contracts"] == raw["contracts"]
    raw["gaps"] = ["x" * 4801]
    with pytest.raises(ValueError, match="unbounded-source-gap"):
        validate(raw, candidate())


def test_v9_preserves_large_audit_and_rejects_invalid_records_without_dropping_gaps():
    raw = audit()
    raw["execution"] = [copy.deepcopy(raw["execution"][0]) for _ in range(19)]
    raw["execution"][12]["evidence"] = ["invented/obs_99:e1"]
    raw["gaps"] = [f"Missing fact {index}" for index in range(9)]
    saved, rejected = consume(raw, candidate(), protocol_version=9)
    assert len(saved["execution"]) == 18
    assert rejected == [{"array": "execution", "index": 12, "reason": "invalid-proof-reference"}]
    assert saved["gaps"][:9] == raw["gaps"]
    assert len(saved["gaps"]) == 10
    with pytest.raises(ValueError, match="invalid-source-audit-envelope"):
        consume(raw, candidate(), protocol_version=8)


def test_v9_large_audit_still_enforces_whole_record_size():
    raw = audit()
    raw["gaps"] = ["x" * 33_000]
    with pytest.raises(ValueError, match="source-audit-transport-record-too-large"):
        consume(raw, candidate(), protocol_version=9)


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


@pytest.mark.asyncio
async def test_blind_sources_are_not_resampled_for_candidate_or_progress_only_changes():
    item = candidate()
    book = ledger(item, protocol=4)
    llm = GateLLM(replies=[{"audits": [audit()]}])
    await run_source_audit(llm, book)
    original = copy.deepcopy(book.source_audits[item.id])
    source_hash = input_fingerprint(item, book)
    item.claim = "A different candidate description with the same supplied sources."
    item.investigation_progress.append({"phase": "navigation", "note": "No new source."})
    assert input_fingerprint(item, book) == source_hash and current(item, book)
    await run_source_audit(llm, book)
    assert len(llm.calls) == 1 and book.source_audits[item.id] == original
    assert book.source_audit_batches == 1
    item.observations[0] = replace(item.observations[0], excerpt="return a_different_value.id")
    assert not current(item, book)


@pytest.mark.asyncio
async def test_source_audit_four_batch_limit_survives_new_facts_and_checkpoint_restore():
    item = candidate()
    book = ledger(item, protocol=4)
    llm = GateLLM(replies=[{"audits": [audit()]} for _ in range(4)])
    for index in range(4):
        item.observations.append(replace(item.observations[1], id=f"obs_{index + 2}"))
        await run_source_audit(llm, book)
        assert book.source_audit_batches == index + 1
        book = HypothesisLedger.from_dict(book.to_dict())
        item = next(iter(book.items.values()))
    assert len(llm.calls) == 4
    item.observations.append(replace(item.observations[1], id="obs_new"))
    await run_source_audit(llm, book)
    assert len(llm.calls) == 4 and book.source_audit_batches == 4
    assert book.source_audits[item.id]["outcome"] == "budget-exhausted"


def test_old_saved_source_audits_do_not_restore_a_fresh_batch_allowance():
    book = ledger(candidate(), protocol=4)
    value = book.to_dict()
    value.pop("source_audit_batches")
    value["source_audits"] = {"a": {"attempts": 2}, "b": {"attempts": 3}}
    assert HypothesisLedger.from_dict(value).source_audit_batches == 4
    value["source_audits"] = {}
    assert HypothesisLedger.from_dict(value).source_audit_batches == 0


@pytest.mark.asyncio
async def test_source_audit_dispatch_allowance_is_checkpointed_before_the_model_call():
    item = candidate()
    book = ledger(item, protocol=4)
    snapshots = []

    async def save(current_book):
        snapshots.append(current_book.to_dict())

    llm = GateLLM(replies=["malformed source response"])
    await run_source_audit(llm, book, save)
    assert snapshots[0]["source_audit_batches"] == 1
    assert snapshots[0]["source_audits"][item.id]["reason"] == "source-audit-interrupted"
    assert len(llm.calls) == 1 and book.source_audit_batches == 1


@pytest.mark.asyncio
async def test_v9_large_single_case_reaches_blind_audit_and_proof_with_all_sources():
    item = candidate()
    for index in range(50):
        item.observations.append(
            replace(item.observations[0], id=f"obs_extra_{index}", excerpt="source evidence line\n" * 50)
        )
    book = ledger(item, protocol=9)
    assert 32_000 < sum(len(message.content) for message in messages([item], book)) < 128_000
    llm = GateLLM(replies=[{"audits": [audit()]}, {"reviews": [verdict()]}])
    await ProofGate(llm).run(book)
    assert len(llm.calls) == 2
    assert current(item, book)
    for call, _ in llm.calls:
        assert "obs_extra_49" in call[-1].content
    assert item.claim not in llm.calls[0][0][-1].content
    assert accepted(item, book)


def test_v9_blind_and_final_requests_explicitly_identify_json_output():
    item = candidate()
    book = ledger(item, protocol=9)
    blind = messages([item], book)
    final = ProofGate(None)._messages(book, [item], forecast=True)
    assert "json" in blind[0].content.lower()
    assert "json" in final[0].content.lower()
    assert '{"reviews":[' in final[0].content


@pytest.mark.asyncio
async def test_v9_oversized_case_reports_packet_size_not_budget():
    item = candidate()
    for index in range(180):
        item.observations.append(
            replace(item.observations[0], id=f"obs_extra_{index}", excerpt="source evidence line\n" * 50)
        )
    book = ledger(item, protocol=9)
    llm = GateLLM(replies=[])
    await ProofGate(llm).run(book)
    assert not llm.calls
    assert book.source_audits[item.id]["outcome"] == "output-error"
    assert book.source_audits[item.id]["reason"] == "source-audit-packet-too-large"
    assert book.publication_reviews[item.id]["reason"] == "independent-source-audit-packet-too-large"
    assert not accepted(item, book)


@pytest.mark.asyncio
async def test_v9_oversized_final_proof_reports_packet_size_not_budget():
    item = candidate()
    for index in range(180):
        item.observations.append(
            replace(item.observations[0], id=f"obs_extra_{index}", excerpt="source evidence line\n" * 50)
        )
    book = ledger(item, protocol=9)
    book.source_audits[item.id] = {
        "version": 2,
        "run_id": book.run_id,
        "outcome": "resolved",
        "input_fingerprint": input_fingerprint(item, book),
        "facts": audit(),
    }
    assert current(item, book)
    llm = GateLLM(replies=[])
    await ProofGate(llm).run(book)
    assert not llm.calls
    assert book.publication_reviews[item.id]["outcome"] == "output-error"
    assert book.publication_reviews[item.id]["reason"] == "complete-proof-packet-too-large"
