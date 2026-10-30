"""Independent knowledge contracts require complete pinned controller receipts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from test_proof_gate import GateLLM, candidate, ledger
from test_source_audit import audit, verdict

from reviewforge.core.state import StateStore
from reviewforge.engine.context_pack import ContextPack
from reviewforge.engine.hypothesis import HypothesisLedger, Observation
from reviewforge.engine.investigator import Investigator, build_workspace_executor
from reviewforge.engine.knowledge_receipt import validate_knowledge_receipt
from reviewforge.engine.proof_gate import ProofGate, accepted
from reviewforge.engine.publication_contracts import contract_catalog
from reviewforge.engine.review_session import EvidenceStore
from reviewforge.engine.source_audit import consume, knowledge_sources, messages, validate
from reviewforge.engine.source_navigation import _pieces


def receipt(kind="documentation"):
    content = "An absent value must return null.\n" * 100
    result = {
        "status": "success",
        "head_sha": "head",
        "workspace_digest": "workspace",
        "kind": kind,
        "path": "docs/interface.md",
        "revision": "head",
        "content": content,
        "content_sha256": hashlib.sha256(content.encode()).hexdigest(),
    }
    if kind == "issue":
        result.update(url="https://github.com/owner/repo/issues/1", revision="2026-10-30T01:00:00Z")
        result["path"] = "issue:" + result["url"]
    return result


def knowledge_case(kind="documentation", *, original=None):
    item = candidate()
    book = ledger(item, protocol=11)
    raw_receipt = original if original is not None else receipt(kind)
    text = json.dumps(raw_receipt, ensure_ascii=False, sort_keys=True)
    digest = hashlib.sha256(text.encode()).hexdigest()
    query = 'read_review_knowledge {"path": "' + raw_receipt["path"] + '"}'
    ids = []
    for index, (part, _span) in enumerate(_pieces(text, None)):
        identity = f"obs_{index + 2}"
        ids.append(identity)
        observation = Observation(
            identity,
            "read_review_knowledge",
            query,
            raw_receipt["path"],
            None,
            "head",
            digest[:16],
            part,
            "success",
        )
        item.observations.append(observation)
        EvidenceStore(book).observation(observation)
    item.investigation_progress = [
        {
            "phase": "source-window-v9",
            "query": query,
            "observation_ids": ids[:],
            "saved_range": None,
            "saved_chars": len(text),
            "truncated": False,
        },
        {
            "phase": "review-context-receipt",
            "tool": "read_review_knowledge",
            "query": query,
            "receipt_sha256": digest,
            "observation_ids": ids[:],
            "head_sha": "head",
            "workspace_digest": "workspace",
        },
    ]
    raw_audit = audit(item.id)
    raw_audit["contracts"] = [
        {"kind": kind, "statement": "An absent value must return null.", "evidence": [f"{item.id}/{ids[0]}:e1"]}
    ]
    return item, book, raw_audit


@pytest.mark.parametrize("kind", ["documentation", "issue"])
def test_complete_split_original_receipt_supplies_only_its_own_kind_of_contract(kind):
    item, book, raw = knowledge_case(kind)
    assert len(item.investigation_progress[-1]["observation_ids"]) > 1
    assert validate(raw, item, protocol_version=11, ledger=book)["contracts"] == raw["contracts"]
    sources = knowledge_sources(item, book)
    assert len(sources) == len(item.observations) - 2
    assert {value["kind"] for value in sources.values()} == {kind}
    assert all("content" not in value for value in sources.values())
    saved = HypothesisLedger.from_dict(json.loads(json.dumps(book.to_dict(), sort_keys=True)))
    restored = saved.items[item.identity]
    assert validate(raw, restored, protocol_version=11, ledger=saved) == validate(
        raw,
        item,
        protocol_version=11,
        ledger=book,
    )
    raw["contracts"][0]["kind"] = "issue" if kind == "documentation" else "documentation"
    with pytest.raises(ValueError, match="knowledge-contract-provenance"):
        validate(raw, item, protocol_version=11, ledger=book)


@pytest.mark.parametrize(
    "damage",
    [
        "read_file",
        "failed",
        "wrong_head",
        "wrong_workspace",
        "missing_metadata",
        "note_metadata",
        "missing_piece",
        "changed_piece",
        "changed_raw_hash",
        "missing_window",
        "partial_window",
        "mixed_reference",
    ],
)
def test_forged_failed_partial_or_unrelated_observations_cannot_establish_knowledge_contracts(damage):
    item, book, raw = knowledge_case()
    if damage == "read_file":
        item.observations[2] = replace(item.observations[2], tool="read_file")
    elif damage == "failed":
        item.observations[2] = replace(item.observations[2], status="error")
    elif damage == "wrong_head":
        item.observations[2] = replace(item.observations[2], sha="other")
    elif damage == "wrong_workspace":
        item.investigation_progress[-1]["workspace_digest"] = "other"
    elif damage == "missing_metadata":
        item.investigation_progress.pop()
    elif damage == "note_metadata":
        item.investigation_progress[-1]["phase"] = "shared-checkpoint"
    elif damage == "missing_piece":
        item.observations.pop()
    elif damage == "changed_piece":
        item.observations[-1] = replace(item.observations[-1], excerpt="changed source")
    elif damage == "changed_raw_hash":
        item.investigation_progress[-1]["receipt_sha256"] = "other"
    elif damage == "missing_window":
        item.investigation_progress.pop(0)
    elif damage == "partial_window":
        item.investigation_progress[0]["truncated"] = True
    else:
        raw["contracts"][0]["evidence"].append(f"{item.id}/obs_1:e1")
    with pytest.raises(ValueError):
        validate(raw, item, protocol_version=11, ledger=book)


@pytest.mark.parametrize("damage", ["kind", "head", "workspace", "hash", "revision", "path", "extra", "status"])
def test_complete_rehashed_receipt_still_requires_valid_source_identity(damage):
    raw = receipt()
    if damage == "kind":
        raw["kind"] = "agent-note"
    elif damage == "head":
        raw["head_sha"] = "other"
    elif damage == "workspace":
        raw["workspace_digest"] = "other"
    elif damage == "hash":
        raw["content_sha256"] = "0" * 64
    elif damage == "revision":
        raw["revision"] = "branch-main"
    elif damage == "path":
        raw["path"] = "../outside.md"
    elif damage == "extra":
        raw["model_summary"] = "expected behavior"
    else:
        raw["status"] = "unavailable"
    with pytest.raises(ValueError):
        validate_knowledge_receipt(raw, head_sha="head", workspace_digest="workspace")
    item, book, audit_raw = knowledge_case(original=raw)
    assert not knowledge_sources(item, book)
    with pytest.raises(ValueError, match="knowledge-contract-provenance"):
        validate(audit_raw, item, protocol_version=11, ledger=book)


@pytest.mark.parametrize("damage", ["url", "path", "revision"])
def test_issue_receipt_requires_original_url_path_and_revision(damage):
    raw = receipt("issue")
    if damage == "url":
        raw["url"] = "http://github.com/owner/repo/issues/1"
        raw["path"] = "issue:" + raw["url"]
    elif damage == "path":
        raw["path"] = "docs/local-note.md"
    else:
        raw["revision"] = ""
    with pytest.raises(ValueError):
        validate_knowledge_receipt(raw, head_sha="head", workspace_digest="workspace")


def test_bad_knowledge_contract_is_rejected_without_losing_independent_source_execution():
    item, book, raw = knowledge_case()
    raw["contracts"].append({"kind": "issue", "statement": "Claimed issue contract", "evidence": ["h_bug/obs_1:e1"]})
    saved, rejected = consume(raw, item, protocol_version=11, ledger=book)
    assert saved["execution"] == raw["execution"]
    assert saved["contracts"] == raw["contracts"][:1]
    assert rejected == [{"array": "contracts", "index": 1, "reason": "invalid-knowledge-contract-provenance"}]
    assert saved["gaps"]


def test_blind_v11_prompt_and_metadata_do_not_expose_upstream_claims_or_notes():
    item, book, _raw = knowledge_case("issue")
    item.claim = "PRIVATE_CANDIDATE"
    item.discovery_basis = {"notes": "PRIVATE_DISCOVERY_NOTE"}
    item.investigation_progress.append({"phase": "shared-checkpoint", "summary": "PRIVATE_INVESTIGATION_NOTE"})
    chat = messages([item], book)
    text = "\n".join(message.content for message in chat)
    assert "PRIVATE_" not in text
    assert "documentation|issue" in chat[0].content
    packet = json.loads(chat[1].content)["items"][0]
    assert len(packet["knowledge_sources"]) == 1
    assert packet["knowledge_sources"][0]["url"] == "https://github.com/owner/repo/issues/1"


@pytest.mark.parametrize("protocol", [4, 8, 9, 10])
def test_historical_protocols_do_not_gain_new_contract_kinds(protocol):
    item, book, raw = knowledge_case()
    with pytest.raises(ValueError, match="contract-kind"):
        validate(raw, item, protocol_version=protocol, ledger=book)
    original = audit(item.id)
    assert validate(original, item, protocol_version=protocol)["contracts"] == original["contracts"]
    old_book = ledger(item, protocol=protocol)
    assert "knowledge_sources" not in json.loads(messages([item], old_book)[1].content)["items"][0]


def test_knowledge_contract_without_expected_ledger_binding_fails_closed():
    item, _book, raw = knowledge_case()
    with pytest.raises(ValueError, match="knowledge-contract-provenance"):
        validate(raw, item, protocol_version=11)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["documentation", "issue"])
async def test_real_context_tool_and_save_window_metadata_make_split_original_contract_citable(kind):
    content = "An absent value must return null.\n" * 100

    async def read_async(_path):
        return content

    url = "https://github.com/owner/repo/issues/1"
    workspace = SimpleNamespace(
        head_sha="head",
        digest="workspace",
        source="tarball",
        truncated=False,
        list_paths=lambda: ["docs/interface.md"],
        read_async=read_async,
    )
    state = StateStore(
        head_sha="head",
        repo="owner/repo",
        linked_issues=[
            {
                "html_url": url,
                "updated_at": "2026-10-30T01:00:00Z",
                "title": "Optional inputs",
                "body": content,
            }
        ],
    )
    item = candidate()
    book = ledger(item, protocol=11)
    worker = Investigator(
        None,
        build_workspace_executor(workspace, state, protocol_version=11),
        protocol_version=11,
        evidence_store=EvidenceStore(book),
    )
    worker._prepare_investigation(item, state, ContextPack(workspace_digest="workspace"))
    reply = await worker._run_tool(
        "read_review_knowledge",
        {"path": "issue:" + url if kind == "issue" else "docs/interface.md"},
    )
    assert "Saved evidence" in reply
    item.observations, item.investigation_progress = list(worker._observations), list(worker._progress)
    entry = next(row for row in item.investigation_progress if row.get("phase") == "review-context-receipt")
    assert len(entry["observation_ids"]) > 1
    raw = audit(item.id)
    raw["contracts"] = [
        {
            "kind": kind,
            "statement": "An absent value must return null.",
            "evidence": [f"{item.id}/{entry['observation_ids'][0]}:e1"],
        }
    ]
    assert validate(raw, item, protocol_version=11, ledger=book)["contracts"] == raw["contracts"]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["documentation", "issue"])
async def test_new_contract_still_requires_independent_source_audit_and_final_proof(kind):
    item, book, raw = knowledge_case(kind)
    book.source_audits[item.id] = {"facts": raw}
    contract_id = next(iter(contract_catalog(item, book)))
    book.source_audits = {}
    final = verdict()
    final["contract_refs"] = [contract_id]
    final["obligations"]["required_behavior"]["evidence"] = raw["contracts"][0]["evidence"]
    model = GateLLM(replies=[{"audits": [raw]}, {"reviews": [final]}])
    await ProofGate(model).run(book)
    assert len(model.calls) == 2 and accepted(item, book)
    blind_packet = json.loads(model.calls[0][0][-1].content)
    assert "candidate" not in blind_packet["items"][0]
    final_packet = json.loads(model.calls[1][0][-1].content)
    assert final_packet["items"][0]["independent_source_audit"]["contracts"][0]["kind"] == kind
