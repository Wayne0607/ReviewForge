"""Frozen v4 publication binds final proof, while old delivery stays idempotent."""

from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_proof_gate import GateLLM, candidate, ledger
from test_source_audit import audit, verdict

from reviewforge.core.config import PipelineV4Config
from reviewforge.core.events import EventBus
from reviewforge.core.state import StateStore
from reviewforge.engine import editor, pipeline_v4
from reviewforge.engine.editor import Publication, confirmed_fact_digest, resumed_publication
from reviewforge.engine.proof_gate import ProofGate, accepted, proof_fingerprint
from reviewforge.engine.semantic_diff import SemanticChangeSet
from reviewforge.tools.workspace import WorkspaceInfo


async def approved(protocol=4):
    item = candidate()
    book = ledger(item, protocol=protocol)
    llm = GateLLM(replies=[{"audits": [audit()]}, {"reviews": [verdict()]}]) if protocol >= 4 else GateLLM()
    await ProofGate(llm).run(book)
    assert accepted(item, book)
    return item, book


def frozen(book, *, legacy=False, status="pending"):
    publication = resumed_publication(
        book,
        covered_ids=set(),
        published_clusters=set(),
        inline_used=0,
        max_inline=5,
        max_inline_overflow=2,
        output_language="en",
    )
    payload = {
        "comments": [{"file_path": row.path, "line": row.line, "body": row.body} for row in publication.comments],
        "body": "summary",
    }
    coverage = pipeline_v4._publication_coverage(book, publication, payload)
    if legacy:
        coverage.pop("publication_renderer_version", None)
        coverage["publication_proof_version"] = 1
        coverage["fact_hashes"] = {item.id: confirmed_fact_digest(item) for item in book.items.values()}
        payload["comments"][0]["body"] = "OLD DRAFT CLAIM"
    return {"batch_id": "", "status": status, "payload": {**payload, "coverage": coverage}}


async def resume(book, record, tmp_path, monkeypatch, *, allow_stages=False):
    gateway = SimpleNamespace(
        workspace_for=AsyncMock(
            return_value=SimpleNamespace(
                info=WorkspaceInfo("o/r", "o/r", "head", tmp_path, 0, 0, "workspace", False, "api-fallback"),
                digest="workspace",
            )
        )
    )
    db = SimpleNamespace(
        get_v4_publication=AsyncMock(return_value=record),
        list_v4_publications=AsyncMock(return_value=[record]),
    )
    fake = SimpleNamespace(
        _gateway=gateway,
        _db=db,
        _events=EventBus(),
        _model_router=object(),
        _pipeline_v4_config=PipelineV4Config(mode="hypothesis", protocol_version=book.protocol_version),
    )
    state = StateStore(repo="o/r", pr_number=1, head_sha="head", ledger=book)
    changes = SemanticChangeSet(repo="o/r", pr_number=1, head_sha="head", units=[SimpleNamespace(id="unit")])
    monkeypatch.setattr(pipeline_v4, "compile_changeset_v4", lambda state: changes)
    monkeypatch.setattr(
        pipeline_v4.ContextPack,
        "build",
        lambda *args, **kwargs: SimpleNamespace(
            workspace_digest="workspace",
            units={},
            render_all=lambda **kwargs: "",
        ),
    )
    monkeypatch.setattr(pipeline_v4, "_persist_ledger", AsyncMock())
    stages = AsyncMock(side_effect=AssertionError("Must not restart model stages"))
    if allow_stages:
        stages = AsyncMock(return_value=Publication(comments=[], summary_items=[], merged=[]))
    delivery = AsyncMock(return_value=pipeline_v4.DeliveryOutcome(delivered=1))
    monkeypatch.setattr(pipeline_v4, "_run_llm_stages", stages)
    monkeypatch.setattr(pipeline_v4, "deliver_saved_publication", delivery)
    # The default stage stub cannot generate a supplemental payload; any actual
    # resume we allow below is inspected at the renderer boundary separately.
    monkeypatch.setattr(pipeline_v4, "render_review_body", lambda *args, **kwargs: "summary")
    health = await pipeline_v4.run_hypothesis_pipeline(fake, state)
    return health, stages, delivery


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["reason", "obligations", "contract_refs", "source_audit_hash", "renderer"])
async def test_v4_fact_identity_binds_final_proof_and_renderer(field, monkeypatch):
    item, book = await approved()
    before = confirmed_fact_digest(item, ledger=book)
    original_fingerprint = proof_fingerprint(item, book.head_sha, book.workspace_digest)
    record = book.publication_reviews[item.id]
    if field == "reason":
        record["reason"] += " A precise explanation."
    elif field == "obligations":
        record["obligations"]["bad_consequence"]["statement"] += " A precise consequence."
    elif field == "contract_refs":
        record["contract_refs"] = ["different-contract"]
    elif field == "source_audit_hash":
        record["source_audit_hash"] = "different-sources"
    else:
        monkeypatch.setattr(editor, "V4_PUBLICATION_RENDERER_VERSION", 999)
    assert proof_fingerprint(item, book.head_sha, book.workspace_digest) == original_fingerprint
    assert confirmed_fact_digest(item, ledger=book) != before


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", [1, 2, 3])
async def test_old_protocol_fact_identity_is_unchanged(protocol):
    item = candidate()
    book = ledger(item, protocol=protocol)
    before = confirmed_fact_digest(item)
    book.publication_reviews[item.id] = {"reason": "new final prose", "obligations": {}}
    assert confirmed_fact_digest(item, ledger=book) == before


@pytest.mark.asyncio
async def test_current_v4_coverage_records_actual_proof_version_and_final_digest():
    item, book = await approved()
    coverage = frozen(book)["payload"]["coverage"]
    assert coverage["publication_proof_version"] == book.publication_reviews[item.id]["proof_version"] == 2
    assert coverage["publication_renderer_version"] == editor.V4_PUBLICATION_RENDERER_VERSION
    assert coverage["fact_hashes"] == {item.id: confirmed_fact_digest(item, ledger=book)}


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["pending", "sending"])
async def test_old_v4_frozen_draft_is_blocked_without_rewrite(tmp_path, monkeypatch, status):
    _, book = await approved()
    record = frozen(book, legacy=True, status=status)
    before = deepcopy(record)
    health, stages, delivery = await resume(book, record, tmp_path, monkeypatch)
    assert "isolated" in str(health)
    stages.assert_not_called()
    delivery.assert_not_called()
    assert record == before


@pytest.mark.asyncio
async def test_changed_final_reason_rejects_previously_frozen_payload(tmp_path, monkeypatch):
    item, book = await approved()
    record = frozen(book)
    book.publication_reviews[item.id]["reason"] += " The final accepted explanation changed."
    assert accepted(item, book)
    health, stages, delivery = await resume(book, record, tmp_path, monkeypatch)
    assert "isolated" in str(health)
    stages.assert_not_called()
    delivery.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True])
async def test_delivered_unchanged_fact_does_not_repeat_for_renderer_migration(tmp_path, monkeypatch, legacy):
    _, book = await approved()
    record = frozen(book, legacy=legacy, status="delivered")
    _, stages, delivery = await resume(book, record, tmp_path, monkeypatch)
    stages.assert_not_called()
    assert delivery.await_count == 1  # Existing delivery path only reconciles an already delivered receipt.


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["claim", "assessment", "sites"])
async def test_delivered_old_fact_change_keeps_summary_supplement(tmp_path, monkeypatch, change):
    item, book = await approved()
    record = frozen(book, legacy=True, status="delivered")
    if change == "claim":
        item.claim += " A distinct supported input."
    elif change == "assessment":
        item.assessment = replace(item.assessment, expected=item.assessment.expected + " for this supported input")
    else:
        item.sites[0] = replace(item.sites[0], excerpt="value.id")
    # Changed candidate/source facts need a new independent proof before they
    # may become a supplement; the test uses only the deterministic mock gate.
    await ProofGate(GateLLM(replies=[{"reviews": [verdict()]}])).run(book)
    assert accepted(item, book)
    _, stages, _ = await resume(book, record, tmp_path, monkeypatch, allow_stages=True)
    assert stages.await_args.kwargs["covered_ids"] == set()
    clusters = stages.await_args.kwargs["published_clusters"]
    assert clusters
    supplemental = resumed_publication(
        book,
        covered_ids=set(),
        published_clusters=clusters,
        inline_used=1,
        max_inline=5,
        max_inline_overflow=2,
        output_language="en",
    )
    assert not supplemental.comments and supplemental.summary_items


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["fact_hashes", "confirmed_ids", "cluster_keys", "coverage"])
async def test_delivered_old_missing_fact_identity_cannot_guess_coverage(tmp_path, monkeypatch, missing):
    _, book = await approved()
    record = frozen(book, legacy=True, status="delivered")
    if missing == "coverage":
        record["payload"].pop("coverage")
    else:
        record["payload"]["coverage"].pop(missing)
    before = deepcopy(record)
    health, stages, _ = await resume(book, record, tmp_path, monkeypatch)
    assert "isolated" in str(health.delivery.errors)
    stages.assert_not_called()
    assert record == before


@pytest.mark.asyncio
async def test_current_frozen_v4_can_resume_delivery(tmp_path, monkeypatch):
    _, book = await approved()
    _, stages, delivery = await resume(book, frozen(book), tmp_path, monkeypatch)
    stages.assert_not_called()
    delivery.assert_awaited_once()


@pytest.mark.asyncio
async def test_v3_frozen_receipt_keeps_original_contract(tmp_path, monkeypatch):
    _, book = await approved(protocol=3)
    record = frozen(book)
    assert record["payload"]["coverage"]["publication_proof_version"] == 1
    assert "publication_renderer_version" not in record["payload"]["coverage"]
    _, stages, delivery = await resume(book, record, tmp_path, monkeypatch)
    stages.assert_not_called()
    delivery.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["budget-exhausted", "provider-error", "output-error"])
async def test_v4_current_capped_receipt_does_not_restart_for_v3_version_constant(tmp_path, monkeypatch, outcome):
    item, book = await approved()
    record = frozen(book, status="delivered")
    book.publication_reviews[item.id].update(
        decision="unknown",
        outcome=outcome,
        validated=False,
        attempts=2,
        model_attempts=2,
    )
    _, stages, _ = await resume(book, record, tmp_path, monkeypatch)
    stages.assert_not_called()
