"""The final accepted proof is the v4 publication contract, including resume."""

from dataclasses import replace

import pytest
from test_proof_gate import GateLLM, candidate, ledger, structural_candidate
from test_source_audit import audit, verdict

from reviewforge.engine.editor import Editor, cluster_confirmed, proof_comment, resumed_publication
from reviewforge.engine.proof_gate import OBLIGATIONS, ProofGate, accepted


def narrow_verdict():
    result = verdict()
    result["reason"] = "Only the supported absent input fails"
    for name in OBLIGATIONS:
        result["obligations"][name]["statement"] = f"Final scoped {name}"
    return result


def draft_item():
    item = candidate()
    item.claim = "DRAFT_CLAIM: all inputs fail"
    item.trigger = "DRAFT_TRIGGER: any input"
    item.impact = "DRAFT_IMPACT: every request fails"
    item.assessment = replace(item.assessment, expected="DRAFT_EXPECTED", actual="DRAFT_ACTUAL")
    return item


@pytest.mark.asyncio
@pytest.mark.parametrize("placement", ["inline", "overflow", "resume"])
@pytest.mark.parametrize("language", ["en", "zh-CN"])
async def test_v4_formats_final_scoped_proof_in_every_publication_path(placement, language):
    item = draft_item()
    book = ledger(item, protocol=4)
    gate = GateLLM(replies=[{"audits": [audit()]}, {"reviews": [narrow_verdict()]}])
    await ProofGate(gate).run(book)
    assert accepted(item, book)
    original = item.to_dict()
    unused_prose_model = GateLLM()
    if placement == "resume":
        publication = resumed_publication(
            book,
            covered_ids=set(),
            published_clusters={cluster_confirmed(book)[0].key},
            inline_used=1,
            max_inline=5,
            max_inline_overflow=8,
            output_language=language,
        )
    else:
        limit = 0 if placement == "overflow" else 5
        publication = await Editor(
            unused_prose_model, output_language=language, max_inline=limit, max_inline_overflow=limit
        ).run(book, None)
    text = "\n".join(
        [comment.body for comment in publication.comments] + [body for _, body in publication.summary_items]
    )
    assert "DRAFT_" not in text
    assert "Only the supported absent input fails" in text
    assert all(f"Final scoped {name}" in text for name in OBLIGATIONS)
    for observation in item.observations:
        assert observation.excerpt.strip() in text
    assert text.count("> return value.id") == 1  # Shared obligation evidence is quoted once.
    assert "app.ts:1" in text
    if publication.comments:
        assert publication.comments[0].title == "Only the supported absent input fails"
    assert item.to_dict() == original and not unused_prose_model.calls


@pytest.mark.asyncio
async def test_v3_keeps_original_proof_presentation():
    item = draft_item()
    book = ledger(item, protocol=3)
    # V3 uses its original review shape, which has no independent contract refs.
    result = narrow_verdict()
    result.pop("contract_refs")
    result.update(counterevidence=None, missing_obligations=[])
    await ProofGate(GateLLM(replies=[{"reviews": [result]}])).run(book)
    assert accepted(item, book)
    publication = await Editor(GateLLM()).run(book, None)
    text = publication.comments[0].body
    assert "DRAFT_CLAIM" in text and "DRAFT_EXPECTED" in text
    assert "Final scoped" not in text


@pytest.mark.asyncio
async def test_v4_stale_proof_cannot_be_formatted_directly_or_through_editor():
    item = draft_item()
    book = ledger(item, protocol=4)
    await ProofGate(GateLLM(replies=[{"audits": [audit()]}, {"reviews": [narrow_verdict()]}])).run(book)
    cluster = cluster_confirmed(book)[0]
    item.observations[0] = replace(item.observations[0], excerpt="changed source")
    assert not accepted(item, book)
    with pytest.raises(ValueError, match="current accepted proof"):
        proof_comment(cluster, ledger=book)
    publication = await Editor(GateLLM()).run(book, None)
    assert not publication.comments and not publication.summary_items


@pytest.mark.asyncio
async def test_v4_structural_summary_keeps_reviewed_proof_and_verified_before_after_links():
    item, book = structural_candidate()
    book.protocol_version = 4
    # The real loader is the independent filename contract, not rename metadata.
    raw_audit = audit()
    raw_audit["contracts"][0]["statement"] = "Loader requires filename and id to match"
    raw = narrow_verdict()
    from reviewforge.engine.publication_contracts import contract_catalog

    book.source_audits[item.id] = {"facts": raw_audit}
    raw["contract_refs"] = [next(iter(contract_catalog(item, book)))]
    book.source_audits = {}
    item.claim = "DRAFT_CLAIM"
    await ProofGate(GateLLM(replies=[{"audits": [raw_audit]}, {"reviews": [raw]}])).run(book)
    assert accepted(item, book)
    publication = await Editor(GateLLM()).run(book, None)
    assert not publication.comments and len(publication.summary_items) == 1
    text = publication.summary_items[0][1]
    assert "DRAFT_CLAIM" not in text and "Final scoped introduced_regression" in text
    change = item.change_anchor
    assert f"/blob/{change.comparison_base_sha}/{change.previous_path}" in text
    assert f"/blob/{change.head_sha}/{change.path}" in text
