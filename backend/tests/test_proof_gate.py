"""Protocol mocks validate proof delivery and gates, not semantic model ability."""

from __future__ import annotations

import asyncio
import copy
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

from reviewforge.core.change_manifest import FileChange, file_change_id, render_change_fact
from reviewforge.engine.hypothesis import (
    ContractAssessment,
    EvidenceCitation,
    Hypothesis,
    HypothesisLedger,
    HypothesisStatus,
    Mechanism,
    Observation,
    Site,
)
from reviewforge.engine.proof_gate import OBLIGATIONS, ProofGate, accepted, completed, proof_fingerprint, retryable
from reviewforge.engine.request_budget import (
    BudgetedChatLLM,
    RequestBudget,
    RequestBudgetExceededError,
    RequestBudgetView,
    estimate_request,
)


class GateLLM(BaseChatModel):
    replies: list = Field(default_factory=list)
    calls: list = Field(default_factory=list)
    usage: dict | None = {"input_tokens": 100, "output_tokens": 100, "total_tokens": 200}

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls.append((messages, kwargs))
        packet = json.loads(messages[-1].content)
        reply = (
            self.replies.pop(0)
            if self.replies
            else {"reviews": [review(entry["candidate"]["id"]) for entry in packet["items"]]}
        )
        if isinstance(reply, BaseException):
            raise reply
        return ChatResult(
            generations=[
                ChatGeneration(
                    message=AIMessage(
                        content=reply if isinstance(reply, str) else json.dumps(reply),
                        usage_metadata=self.usage,
                        additional_kwargs={"reasoning_content": "HIDDEN_REASONING_SENTINEL"},
                    )
                )
            ]
        )

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        return self._generate(messages, stop=stop, run_manager=run_manager, **kwargs)

    @property
    def _llm_type(self):
        return "proof-gate-protocol-fixture"


def observation(identity="obs_0", text="return value.id\n", **kwargs):
    return Observation(
        identity, "read_file", "read_file app.ts", "app.ts", (1, 2), "head", "digest", text, "success", **kwargs
    )


def candidate(identity="h_bug"):
    item = Hypothesis(
        id=identity,
        identity=f"unit::{identity}::lookup",
        unit_id="unit",
        mechanism=Mechanism.NULL_PATH,
        claim="Supported missing values cause a dereference",
        trigger="The optional value is absent",
        impact="A supported request raises instead of returning null",
        open_question="Is the value required?",
        refutation="The value is checked before access",
        sites=[Site("app.ts", 1, "return value.id")],
        severity="warning",
        source="generator",
        status=HypothesisStatus.CONFIRMED,
        evidence_strength="strong",
        observations=[observation(), observation("obs_1", "// value is optional; previously return value?.id\n")],
    )
    item.assessment = ContractAssessment(
        "The supported absent input must return null",
        "Unconditional value.id raises for this input",
        "conflict",
        [EvidenceCitation("obs_1", item.observations[1].excerpt)],
        [EvidenceCitation("obs_0", item.observations[0].excerpt)],
    )
    return item


def ledger(*items, protocol=3, budget=None):
    result = HypothesisLedger("run", "head", "workspace", protocol_version=protocol)
    for item in items:
        # Use the current ledger object to test mutations, not upsert's copy.
        result.items[item.identity] = item
    result.request_budget = budget or {}
    return result


def review(identity="h_bug", decision="accepted"):
    refs = [f"{identity}/obs_0:e1", f"{identity}/obs_1:e1"]
    obligations = {
        name: {"statement": f"The original candidate's {name} is established", "evidence": refs} for name in OBLIGATIONS
    }
    result = {
        "hypothesis_id": identity,
        "decision": decision,
        "reason": "Original candidate proof",
        "obligations": obligations,
        "counterevidence": None,
        "missing_obligations": [],
    }
    if decision == "refuted":
        result.update(
            obligations={},
            counterevidence={
                "premise": "mechanism",
                "statement": "This is ordinary optional access, which cannot perform the claimed dereference",
                "evidence": [f"{identity}/obs_0:e1"],
            },
        )
    elif decision == "unknown":
        result.update(
            obligations={},
            reason="The saved sources do not show a supported missing-value caller",
            missing_obligations=["supported_input"],
        )
    return result


@pytest.mark.asyncio
async def test_original_proof_with_exact_citations_can_be_accepted_without_mutating_investigation():
    item = candidate()
    book, llm = ledger(item), GateLLM()
    original = item.to_dict()
    await ProofGate(llm).run(book)
    record = book.publication_reviews[item.id]
    assert accepted(item, book)
    assert record["decision"] == "accepted" and record["outcome"] == "resolved" and record["validated"]
    assert record["attempts"] == record["model_attempts"] == 1 and record["tokens"] == 200
    assert set(record["obligations"]) == set(OBLIGATIONS)
    assert item.to_dict() == original
    assert "HIDDEN_REASONING_SENTINEL" not in json.dumps(record)
    assert accepted(
        HypothesisLedger.from_dict(book.to_dict()).items[item.identity], HypothesisLedger.from_dict(book.to_dict())
    )


@pytest.mark.asyncio
async def test_full_source_quotes_and_refs_are_delivered_once_with_proposals_not_authority():
    item = candidate()
    item.discovery_basis = {"violation": {"required_behavior": "PROPOSED_REQUIREMENT_SENTINEL"}}
    item.observations.append(replace(observation("obs_failure", "FAILED_SOURCE_SENTINEL"), status="error"))
    llm, book = GateLLM(), ledger(item)
    await ProofGate(llm).run(book)
    messages, options = llm.calls[0]
    packet = json.loads(messages[1].content)
    delivered = packet["items"][0]
    assert options["max_tokens"] == 4096 and len(messages) == 2
    assert delivered["candidate"]["assessment"] == item.assessment.to_dict()
    assert delivered["candidate"]["discovery_basis"] == item.discovery_basis
    assert "status" not in delivered["candidate"] and "evidence_strength" not in delivered["candidate"]
    for source, saved in zip(delivered["observations"], item.observations, strict=True):
        if saved.status == "success":
            assert "excerpt" not in source
            assert source["references"][f"{item.id}/{saved.id}:e1"] == {
                "original_ref": f"{saved.id}:e1",
                "quote": saved.excerpt,
            }
    assert "FAILED_SOURCE_SENTINEL" not in messages[1].content
    assert "original claim, trigger, impact" in messages[0].content
    assert "substitute another defect" in messages[0].content
    assert "pre-existing" in messages[0].content
    assert "New\n  declarations or consumer paths" in messages[0].content


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["refuted", "unknown"])
async def test_ordinary_valid_before_after_change_or_wrong_operation_explanation_is_not_published(decision):
    item = candidate()
    # The mock explicitly supplies the independent model's refutation/uncertainty.
    # This test does not claim the mock recognizes optional access semantics.
    item.claim = "Optional access executes an unsafe dynamic operation"
    item.observations[0] = observation(text="return value?.id\n")
    item.assessment = replace(
        item.assessment, actual_evidence=[EvidenceCitation("obs_0", item.observations[0].excerpt)]
    )
    llm, book = GateLLM(replies=[{"reviews": [review(decision=decision)]}]), ledger(item)
    await ProofGate(llm).run(book)
    assert book.publication_reviews[item.id]["decision"] == decision
    assert not accepted(item, book) and item.status == HypothesisStatus.CONFIRMED
    await ProofGate(llm).run(book)
    assert len(llm.calls) == 1  # valid unknown/refuted is a completed review


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["missing", "empty", "unknown-ref", "cross-ref", "counter", "missing-list"])
async def test_incomplete_or_contradictory_acceptance_and_invalid_references_fail_closed(damage):
    item, other = candidate(), candidate("h_other")
    response = review()
    if damage == "missing":
        response["obligations"].pop("required_behavior")
    elif damage == "empty":
        response["obligations"]["bad_consequence"]["statement"] = " "
    elif damage in {"unknown-ref", "cross-ref"}:
        response["obligations"]["bad_consequence"]["evidence"] = [
            "h_other/obs_0:e1" if damage == "cross-ref" else "h_bug/obs_missing:e1"
        ]
    elif damage == "counter":
        response["counterevidence"] = review(decision="refuted")["counterevidence"]
    else:
        response["missing_obligations"] = ["supported_input"]
    llm = GateLLM(replies=[{"reviews": [response, review("h_other")]}])
    book = ledger(item, other)
    await ProofGate(llm).run(book)
    assert not accepted(item, book) and book.publication_reviews[item.id]["outcome"] == "output-error"
    assert accepted(other, book)  # errors local to an otherwise well-formed item


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        {"reviews": []},
        {"reviews": [review(), review()]},
        {"reviews": [review("h_unknown")]},
        {"reviews": [None]},
        '{"reviews":[],"reviews":[]}',
        '{"reviews":',
        "Here is an invented replacement comment",
    ],
)
async def test_bad_batch_envelopes_and_duplicate_output_ids_fail_closed(response):
    item, llm = candidate(), GateLLM(replies=[response])
    book = ledger(item)
    await ProofGate(llm).run(book)
    assert not accepted(item, book)
    assert book.publication_reviews[item.id]["outcome"] == "output-error"
    assert len(llm.calls) == 1  # no format or semantic repair requests


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("decision", {}),
        ("decision", []),
        ("hypothesis_id", []),
        ("obligations", []),
        ("missing_obligations", [{}]),
        ("counterevidence", {"premise": [], "statement": "x", "evidence": []}),
    ],
)
async def test_adversarial_json_shapes_cannot_escape_as_type_errors(field, value):
    response = review()
    response[field] = value
    item, llm = candidate(), GateLLM(replies=[{"reviews": [response]}])
    book = ledger(item)
    await ProofGate(llm).run(book)
    assert not accepted(item, book)
    assert book.publication_reviews[item.id]["outcome"] == "output-error"


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["wrong-head", "duplicate-observation", "bad-assessment", "failed-assessment"])
async def test_invalid_saved_evidence_or_assessment_is_rejected_before_call(damage):
    item = candidate()
    if damage == "wrong-head":
        item.observations[0] = replace(item.observations[0], sha="other-head")
    elif damage == "duplicate-observation":
        item.observations.append(item.observations[0])
    elif damage == "failed-assessment":
        item.observations[0] = replace(item.observations[0], status="not_found")
    else:
        item.assessment = replace(item.assessment, actual_evidence=[EvidenceCitation("obs_0", "not the source")])
    llm, book = GateLLM(), ledger(item)
    await ProofGate(llm).run(book)
    assert not llm.calls and not accepted(item, book)
    assert book.publication_reviews[item.id]["outcome"] == "output-error"


@pytest.mark.asyncio
async def test_duplicate_candidate_ids_and_detector_without_evidence_cannot_accept():
    one, two = candidate(), candidate()
    two.identity += "different"
    book, llm = ledger(one, two), GateLLM()
    await ProofGate(llm).run(book)
    assert not llm.calls and not accepted(one, book)
    assert book.publication_reviews[one.id]["reason"] == "duplicate-hypothesis-id"
    detector = candidate("h_detector")
    detector.source, detector.observations, detector.assessment = "detector:confidence", [], None
    book = ledger(detector)
    await ProofGate(llm).run(book)
    assert not llm.calls and not accepted(detector, book)
    assert book.publication_reviews[detector.id]["outcome"] == "insufficient-evidence"


@pytest.mark.asyncio
async def test_provider_errors_are_closed_sanitized_and_bounded_across_resumes():
    item, llm = candidate(), GateLLM(replies=[TimeoutError("SECRET_SENTINEL"), TimeoutError("SECRET_SENTINEL")])
    book = ledger(item)
    for _ in range(3):
        await ProofGate(llm).run(book)
    record = book.publication_reviews[item.id]
    assert len(llm.calls) == 2 and record["attempts"] == record["model_attempts"] == 2
    assert record["outcome"] == "provider-error" and not accepted(item, book)
    assert record["tokens"] == 0 and "SECRET_SENTINEL" not in json.dumps(record)


@pytest.mark.asyncio
async def test_real_admission_rejection_is_budget_exhausted_without_provider_call():
    item, llm = candidate(), GateLLM(replies=[RequestBudgetExceededError("budget")])
    book = ledger(item)
    await ProofGate(llm).run(book)
    assert book.publication_reviews[item.id]["outcome"] == "budget-exhausted"
    assert not accepted(item, book)


@pytest.mark.asyncio
async def test_cancelled_request_preserves_attempt_and_does_not_swallow_cancellation():
    item = candidate()
    llm = SimpleNamespace(ainvoke=AsyncMock(side_effect=asyncio.CancelledError()))
    book = ledger(item)
    with pytest.raises(asyncio.CancelledError):
        await ProofGate(llm).run(book)
    assert book.publication_reviews[item.id]["attempts"] == 1
    assert not accepted(item, book)


@pytest.mark.asyncio
async def test_cache_binding_changes_on_every_material_fact_and_revalidates_acceptance():
    item, llm = candidate(), GateLLM()
    book = ledger(item)
    await ProofGate(llm).run(book)
    original = proof_fingerprint(item, book.head_sha, book.workspace_digest)
    await ProofGate(llm).run(book)
    assert len(llm.calls) == 1
    variants = [
        replace(item, claim="new claim"),
        replace(item, trigger="new trigger"),
        replace(item, impact="new impact"),
        replace(item, sites=[Site("other.ts", 3, "different")]),
        replace(item, assessment=replace(item.assessment, expected="new contract")),
        replace(item, observations=[replace(item.observations[0], result_digest="newdigest"), item.observations[1]]),
        replace(item, discovery_basis={"violation": {"required_behavior": "new requirement"}}),
    ]
    assert all(proof_fingerprint(variant, book.head_sha, book.workspace_digest) != original for variant in variants)
    assert proof_fingerprint(item, "new-head", book.workspace_digest) != original
    assert proof_fingerprint(item, book.head_sha, "new-workspace") != original
    book.publication_reviews[item.id]["obligations"]["required_behavior"]["evidence"] = ["h_other/obs_0:e1"]
    assert not accepted(item, book)  # matching fingerprint alone does not authorize publication
    item.claim = "Changed original claim"
    await ProofGate(llm).run(book)
    assert len(llm.calls) == 2 and accepted(item, book)
    book.head_sha = "new-head"
    assert not accepted(item, book)


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["assessment", "validated", "run", "version", "head", "record"])
async def test_accepted_helper_is_fail_closed_for_missing_or_stale_receipts(damage):
    item, llm = candidate(), GateLLM()
    book = ledger(item)
    await ProofGate(llm).run(book)
    if damage == "assessment":
        item.assessment = None
    elif damage == "record":
        book.publication_reviews[item.id] = None
    else:
        key = {"validated": "validated", "run": "run_id", "version": "proof_version", "head": "head_sha"}[damage]
        book.publication_reviews[item.id][key] = False if damage == "validated" else "wrong"
    assert not accepted(item, book)


@pytest.mark.asyncio
async def test_batch_size_is_bounded_by_output_policy_and_usage_is_not_multiplied():
    items, llm = [candidate(f"h_{index}") for index in range(10)], GateLLM()
    book = ledger(*items, budget={"limit": 100_000})
    await ProofGate(llm).run(book)
    assert len(llm.calls) == 2
    counts = [len(json.loads(messages[1].content)["items"]) for messages, _ in llm.calls]
    assert counts == [9, 1]
    assert all(
        options["max_tokens"] <= 4096 and options["max_tokens"] >= 512 + 384 * count
        for (_, options), count in zip(llm.calls, counts, strict=True)
    )
    assert all(accepted(item, book) for item in items)
    assert sum(record["tokens"] for record in book.publication_reviews.values()) == 400


@pytest.mark.asyncio
async def test_complete_packet_size_and_four_logical_call_cap_preserve_unreviewed_candidates():
    items, llm = [candidate(f"h_{index}") for index in range(5)], GateLLM()
    for item in items:
        for index in range(2, 18):
            item.observations.append(observation(f"obs_{index}", "a" * 1200))
    book = ledger(*items, budget={"limit": 100_000})
    await ProofGate(llm).run(book)
    assert len(llm.calls) == 4
    assert all(sum(len(message.content) for message in messages) <= 32_000 for messages, _ in llm.calls)
    assert all(accepted(item, book) for item in items[:4])
    assert book.publication_reviews[items[4].id]["reason"] == "publication-call-cap"
    assert len(book.items) == 5 and all(item.status == HypothesisStatus.CONFIRMED for item in items)


@pytest.mark.asyncio
async def test_oversized_single_item_drops_no_evidence_and_does_not_use_a_model_call():
    large, small, llm = candidate("h_large"), candidate("h_small"), GateLLM()
    for index in range(2, 30):
        large.observations.append(observation(f"obs_{index}", "a" * 1200))
    original = copy.deepcopy(large.observations)
    book = ledger(large, small, budget={"limit": 100_000})
    await ProofGate(llm).run(book)
    assert len(llm.calls) == 1 and accepted(small, book) and not accepted(large, book)
    assert book.publication_reviews[large.id]["outcome"] == "budget-exhausted"
    assert large.observations == original


@pytest.mark.asyncio
async def test_physical_budget_estimate_and_pending_reservations_determine_useful_output():
    item, llm = candidate(), GateLLM()
    book = ledger(item)
    messages = ProofGate(llm)._messages(book, [item])
    estimate = estimate_request(messages, {"max_tokens": 4096})
    book.request_budget = {"limit": estimate + 896 + 1000, "pending": {"busy": {"reserved_tokens": 1000}}}
    await ProofGate(llm).run(book)
    assert llm.calls[0][1]["max_tokens"] == 896 and accepted(item, book)
    item, llm = candidate(), GateLLM()
    book = ledger(item, budget={"limit": estimate + 895})
    await ProofGate(llm).run(book)
    assert not llm.calls and book.publication_reviews[item.id]["outcome"] == "budget-exhausted"


@pytest.mark.asyncio
async def test_bound_real_budget_retains_unknown_cost_without_fabricating_usage():
    item, inner = candidate(), GateLLM(usage=None)
    book = ledger(item)
    budget = RequestBudget(16_000)

    async def changed(snapshot):
        book.request_budget = snapshot

    llm = BudgetedChatLLM(inner, budget, phase="editor", on_change=changed)
    await ProofGate(llm).run(book)
    assert accepted(item, book) and book.publication_reviews[item.id]["tokens"] == 0
    assert budget.reported == 0 and budget.unreported > 0


@pytest.mark.asyncio
async def test_live_budget_with_custom_large_limit_is_not_clipped_by_plain_fallback():
    class LiveLLM(GateLLM):
        def request_budget_view(self, messages, options):
            return RequestBudgetView(estimate_request(messages, options), 100_000)

    item, llm = candidate(), LiveLLM()
    for index in range(2, 19):
        item.observations.append(observation(f"obs_{index}", "证据" * 550))
    book = ledger(item)
    messages = ProofGate(llm)._messages(book, [item])
    assert sum(len(message.content) for message in messages) < 32_000
    assert estimate_request(messages, {"max_tokens": 4096}) > 16_000
    await ProofGate(llm.bind(response_format={"type": "json_object"})).run(book)
    assert len(llm.calls) == 1 and llm.calls[0][1]["max_tokens"] == 4096 and accepted(item, book)


@pytest.mark.asyncio
async def test_actual_usage_over_available_snapshot_cannot_publish_even_valid_proof():
    item, llm = candidate(), GateLLM(usage={"input_tokens": 10_000, "output_tokens": 10_000, "total_tokens": 20_000})
    book = ledger(item)
    await ProofGate(llm).run(book)
    receipt = book.publication_reviews[item.id]
    assert receipt["tokens"] == 20_000 and receipt["outcome"] == "budget-exhausted"
    assert not accepted(item, book) and not completed(item, book) and not retryable(item, book)


@pytest.mark.asyncio
async def test_callback_budget_overrun_cannot_publish_an_otherwise_valid_response():
    class OverrunLLM(GateLLM):
        def _generate(self, messages, **kwargs):
            book.request_budget = {"limit": 16_000, "reported_tokens": 15_900, "unreported_reserved_tokens": 200}
            return super()._generate(messages, **kwargs)

    item, llm = candidate(), OverrunLLM()
    book = ledger(item)
    await ProofGate(llm).run(book)
    assert book.publication_reviews[item.id]["outcome"] == "budget-exhausted"
    assert not accepted(item, book)


@pytest.mark.asyncio
async def test_public_completion_and_retry_helpers_follow_current_scope_and_attempt_cap():
    item, llm = candidate(), GateLLM(replies=[TimeoutError("failure"), {"reviews": [review()]}])
    book = ledger(item)
    assert not completed(item, book) and not retryable(item, book)
    await ProofGate(llm).run(book)
    assert retryable(item, book) and not completed(item, book)
    await ProofGate(llm).run(book)
    assert completed(item, book) and accepted(item, book) and not retryable(item, book)
    book.workspace_digest = "other"
    assert not completed(item, book) and not accepted(item, book) and not retryable(item, book)
    empty, llm = candidate("h_empty"), GateLLM()
    empty.observations, empty.assessment = [], None
    book = ledger(empty)
    await ProofGate(llm).run(book)
    assert completed(empty, book) and not accepted(empty, book) and not retryable(empty, book)
    await ProofGate(llm).run(book)
    assert book.publication_reviews[empty.id]["attempts"] == 1


@pytest.mark.asyncio
async def test_budget_refusal_is_not_reopened_by_resuming_same_original_fact_set():
    item, llm = candidate(), GateLLM()
    book = ledger(item, budget={"limit": 1})
    await ProofGate(llm).run(book)
    book.request_budget = {"limit": 100_000}
    await ProofGate(llm).run(book)
    assert not llm.calls and not accepted(item, book) and not retryable(item, book)
    assert book.publication_reviews[item.id]["attempts"] == 1


@pytest.mark.asyncio
async def test_unknown_with_partial_established_obligations_reuses_only_a_valid_receipt():
    response = review(decision="unknown")
    response["obligations"] = {"bad_consequence": review()["obligations"]["bad_consequence"]}
    item, llm = candidate(), GateLLM(replies=[{"reviews": [response]}])
    book = ledger(item)
    await ProofGate(llm).run(book)
    assert completed(item, book) and not accepted(item, book)
    book.publication_reviews[item.id]["obligations"]["bad_consequence"]["evidence"] = ["h_other/obs_0:e1"]
    assert not completed(item, book)


def structural_candidate(metadata_only=False):
    head = "a" * 40
    change = FileChange(
        repo="owner/repo",
        head_repo="fork/repo",
        path="data/new.json",
        previous_path="data/old.json",
        status="renamed",
        base_sha="b" * 40,
        comparison_base_sha="c" * 40,
        head_sha=head,
        base_blob_sha="d" * 40,
        head_blob_sha="d" * 40,
        base_mode="100644",
        head_mode="100644",
        base_type="blob",
        head_type="blob",
        patch_state="verified-no-text-change",
        source="github-pr-files+git-tree",
    )
    change = replace(change, id=file_change_id(change))
    item = candidate()
    item.sites, item.change_anchor = [], change
    item.claim = "The renamed resource filename no longer matches its unchanged id"
    item.trigger = "Loading data/new.json through the actual loader"
    item.impact = "Filename/id validation raises ValueError"
    item.observations = [
        replace(observation("obs_0", render_change_fact(change)), tool="read_change", sha=head),
        replace(
            observation(
                "obs_1",
                'if Path(path).stem != payload["id"]:\n    raise ValueError("resource id does not match filename")\n',
            ),
            path="loader.py",
            sha=head,
        ),
        replace(observation("obs_2", '{"id":"old"}\n'), path="data/new.json", sha=head),
    ]
    actual = item.observations[0 if metadata_only else 2]
    item.assessment = ContractAssessment(
        "The actual loader requires the resource id to equal the filename stem",
        "data/new.json retains id old and cannot satisfy the loader's validation",
        "conflict",
        [EvidenceCitation("obs_1", item.observations[1].excerpt)],
        [EvidenceCitation(actual.id, actual.excerpt)],
    )
    book = ledger(item)
    book.head_sha = head
    return item, book


@pytest.mark.asyncio
async def test_structural_actual_cannot_be_metadata_only_even_with_other_source_observations():
    item, book = structural_candidate(metadata_only=True)
    llm = GateLLM()
    await ProofGate(llm).run(book)
    assert not llm.calls and not accepted(item, book)
    assert book.publication_reviews[item.id]["reason"] == "structural-actual-needs-source-evidence"
    assert book.publication_reviews[item.id]["outcome"] == "output-error"


@pytest.mark.asyncio
async def test_structural_filename_contract_with_real_source_shape_can_pass_independent_protocol():
    item, book = structural_candidate()
    llm = GateLLM()
    await ProofGate(llm).run(book)
    assert accepted(item, book) and completed(item, book)
    original = proof_fingerprint(item, book.head_sha, book.workspace_digest)
    item.change_anchor = replace(item.change_anchor, previous_path="data/another.json")
    assert proof_fingerprint(item, book.head_sha, book.workspace_digest) != original
    assert not accepted(item, book)


@pytest.mark.asyncio
async def test_same_real_source_can_support_multiple_obligations_without_distinct_citation_rule():
    response = review()
    for proof in response["obligations"].values():
        proof["evidence"] = ["h_bug/obs_0:e1"]
    item, llm = candidate(), GateLLM(replies=[{"reviews": [response]}])
    book = ledger(item)
    await ProofGate(llm).run(book)
    assert accepted(item, book)


@pytest.mark.asyncio
async def test_actual_addition_can_introduce_a_violation_without_inventing_before_implementation():
    item = candidate()
    item.observations[0] = replace(
        observation(text="@@ -0,0 +1,2 @@\n+def lookup(value):\n+    return value.id\n"), tool="read_diff"
    )
    item.assessment = replace(
        item.assessment, actual_evidence=[EvidenceCitation("obs_0", item.observations[0].excerpt)]
    )
    response = review()
    response["obligations"]["introduced_regression"]["statement"] = (
        "The actual added declaration activates this violation for the existing supported caller"
    )
    llm, book = GateLLM(replies=[{"reviews": [response]}]), ledger(item)
    await ProofGate(llm).run(book)
    assert accepted(item, book)
    assert "never invent a before implementation" in llm.calls[0][0][0].content


@pytest.mark.asyncio
async def test_partial_diff_warning_and_full_original_long_line_quotes_are_preserved():
    item = candidate()
    item.observations.append(replace(observation("obs_long", "long source " * 95), tool="read_diff"))
    item.investigation_progress = [{"phase": "diff-navigation", "observation_id": "obs_long"}]
    llm, book = GateLLM(), ledger(item)
    await ProofGate(llm).run(book)
    packet = json.loads(llm.calls[0][0][1].content)
    delivered = packet["items"][0]["observations"][-1]
    assert delivered["references"]["h_bug/obs_long:e1"]["quote"] == item.observations[-1].excerpt
    assert delivered["source_view"] == "partial diff excerpt; not a complete hunk"


@pytest.mark.asyncio
async def test_usage_components_without_total_are_real_usage_not_unknown_or_estimated_charge():
    response = SimpleNamespace(
        content=json.dumps({"reviews": [review()]}),
        response_metadata={"token_usage": {"prompt_tokens": 123, "completion_tokens": 456}},
    )
    item = candidate()
    llm = SimpleNamespace(ainvoke=AsyncMock(return_value=response))
    book = ledger(item)
    await ProofGate(llm).run(book)
    assert accepted(item, book) and book.publication_reviews[item.id]["tokens"] == 579


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", [1, 2])
async def test_old_protocol_and_nonconfirmed_candidates_are_not_reviewed(protocol):
    item, llm = candidate(), GateLLM()
    book = ledger(item, protocol=protocol)
    await ProofGate(llm).run(book)
    assert not llm.calls and not book.publication_reviews and not accepted(item, book)
    book.protocol_version, item.status = 3, HypothesisStatus.OPEN
    await ProofGate(llm).run(book)
    assert not llm.calls and not book.publication_reviews
