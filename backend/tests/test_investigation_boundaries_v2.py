from __future__ import annotations

import copy
from dataclasses import replace

import pytest

from reviewforge.core.state import StateStore
from reviewforge.engine.context_pack import ContextPack, ContextSlice, UnitContext
from reviewforge.engine.editor import cluster_confirmed
from reviewforge.engine.hypothesis import Hypothesis, HypothesisLedger, Mechanism, Observation, Site
from reviewforge.engine.investigation_tasks import build_investigation_tasks
from reviewforge.engine.investigator import Investigator
from reviewforge.engine.semantic_diff import SemanticChangeSet, SemanticUnit, UnitKind


def candidate(unit="class", **changes):
    item = Hypothesis(
        f"h_{unit}",
        f"{unit}::null-path::run",
        unit,
        Mechanism.NULL_PATH,
        "Empty calendar causes TypeError",
        "calendar=[]",
        "Request fails",
        "Can the empty calendar reach this access?",
        "A guard prevents the access",
        [Site("service.ts", 12, "calendar?.externalId")],
        "error",
        "generator",
    )
    return replace(item, **changes)


def state():
    return StateStore(repo="owner/repo", pr_number=1, head_sha="head", file_diffs={"service.ts": "diff"})


def worker():
    value = Investigator(None, None, protocol_version=2)
    value._observations = [
        Observation(
            "obs_0",
            "read_file",
            "read",
            "service.ts",
            (10, 13),
            "head",
            "digest",
            "const [calendar] = event.destinationCalendar ?? [];\ncalendar?.externalId",
            "success",
        )
    ]
    return value


def counter():
    return {
        "answer": "Optional chaining prevents this exception",
        "assessment": None,
        "counterevidence": {
            "premise": "mechanism",
            "statement": "Undefined calendar is not dereferenced",
            "evidence": ["obs_0:e1"],
        },
        "severity": "error",
    }


def test_direct_source_counterevidence_refutes_without_inventing_an_expected_contract():
    result = worker()._finalize(counter(), candidate(), {"service.ts"}, steps=1)
    assert result.verdict == "refuted"
    assert result.counterevidence.premise == "mechanism"
    assert result.counterevidence.evidence[0].quote.endswith("calendar?.externalId")
    assert result.assessment is None
    assert result.outcome == "resolved"


@pytest.mark.parametrize("change", ["missing", "not-found", "context", "invalid-premise", "contradiction"])
def test_counterevidence_never_admits_missing_negative_or_contradictory_proof(change):
    item, value = counter(), worker()
    if change == "missing":
        item["counterevidence"]["evidence"] = []
    if change == "not-found":
        value._observations[0] = replace(value._observations[0], status="not_found")
    if change == "context":
        item["counterevidence"]["evidence"] = ["context:e1"]
    if change == "invalid-premise":
        item["counterevidence"]["premise"] = "preference"
    if change == "contradiction":
        item["verdict"] = "confirmed"
    result = value._finalize(item, candidate(), {"service.ts"}, steps=1)
    assert result.verdict == "unknown"
    assert result.outcome == "output-error"
    assert result.counterevidence is None


def test_explicit_legacy_unknown_is_not_upgraded_by_counterevidence():
    raw = counter() | {"verdict": "unknown"}
    result = worker()._finalize(raw, candidate(), {"service.ts"}, steps=1)
    assert result.verdict == "unknown"


def test_model_free_text_cannot_reclassify_a_valid_abstention_as_an_execution_error():
    result = worker()._finalize({"assessment": None, "reason": "token-exhausted"}, candidate(), {"service.ts"}, steps=1)
    assert result.verdict == "unknown"
    assert result.outcome == "insufficient-evidence"


def test_v2_coverage_identity_does_not_overwrite_a_distinct_trigger_in_the_same_function():
    ledger = HypothesisLedger("run", "head", "digest", protocol_version=2)
    first, _ = ledger.upsert(candidate())
    second, created = ledger.upsert(candidate(trigger="calendar is an expired remote object"))
    assert created and len(ledger.items) == 2
    assert first.identity == candidate().identity
    assert second.coverage_identity == first.identity
    assert first.unit_id == second.unit_id
    assert first.id != second.id
    duplicate, created = ledger.upsert(candidate(trigger="calendar is an expired remote object"))
    assert not created and duplicate.identity == second.identity
    assert len(ledger.items) == 2


def test_publication_keeps_distinct_function_risks_and_merges_only_their_exact_task_aliases():
    ledger = HypothesisLedger("run", "head", "digest", protocol_version=2)
    ledger.upsert(candidate())
    other = {
        "claim": "Missing owner causes TypeError",
        "trigger": "owner=None",
        "open_question": "Can the owner be absent?",
        "refutation": "Owner is validated",
        "sites": [Site("service.ts", 13, "return owner.email")],
    }
    ledger.upsert(candidate(**other))
    ledger.upsert(candidate("method", **other))
    for task in build_investigation_tasks(ledger.open(), head_sha="head", workspace_digest="digest"):
        ledger.apply_task_verdict(
            task.id,
            task.members,
            task.hypothesis.identity,
            status="confirmed",
            investigation_outcome="resolved",
            evidence_strength="strong",
        )
    clusters = cluster_confirmed(ledger)
    assert len(clusters) == 2
    assert sorted(len(cluster.hypothesis_ids) for cluster in clusters) == [1, 2]
    assert sum(len(cluster.hypothesis_ids) for cluster in clusters) == len(ledger.items)


def test_old_checkpoint_keeps_v1_completion_and_closed_decision():
    old = HypothesisLedger("run", "head", "digest")
    old.upsert(candidate())
    old.apply_verdict(candidate().identity, status="unknown", severity="error", verdict_reason="proof missing")
    raw = old.to_dict()
    raw.pop("protocol_version", None)
    restored = HypothesisLedger.from_dict(raw)
    assert restored.protocol_version == 1
    assert not restored.open()
    assert restored.items[candidate().identity].verdict_reason == "proof missing"


@pytest.mark.asyncio
async def test_equal_class_and_method_candidates_share_one_paid_investigation_and_keep_coverage(monkeypatch):
    calls = []

    async def investigate(value, item, *args, **kwargs):
        calls.append(item.identity)
        value._tokens = 1234
        return value._result(verdict="unknown", reason="supported-input contract missing", severity="error")

    monkeypatch.setattr(Investigator, "investigate", investigate)
    ledger = HypothesisLedger("run", "head", "digest", protocol_version=2)
    for unit in ("class", "method"):
        ledger.upsert(candidate(unit))
    await Investigator(None, None, protocol_version=2).run(ledger, state(), ContextPack(), concurrency=2)
    assert len(calls) == 1
    assert len(ledger.items) == 2
    assert {item.unit_id for item in ledger.items.values()} == {"class", "method"}
    assert len({item.investigation_task_id for item in ledger.items.values()}) == 1
    assert sum(item.investigation_tokens for item in ledger.items.values()) == 1234
    assert all(item.investigation_outcome == "insufficient-evidence" for item in ledger.items.values())
    assert len(ledger.investigation_tasks) == 1
    assert HypothesisLedger.from_dict(ledger.to_dict()).investigation_tasks == ledger.investigation_tasks


@pytest.mark.asyncio
async def test_different_trigger_or_file_never_shares_a_verdict(monkeypatch):
    calls = []

    async def investigate(value, item, *args, **kwargs):
        calls.append(item.identity)
        return value._result(verdict="unknown", reason="missing proof", severity="error")

    monkeypatch.setattr(Investigator, "investigate", investigate)
    ledger = HypothesisLedger("run", "head", "digest", protocol_version=2)
    ledger.upsert(candidate())
    ledger.upsert(candidate("different-input", trigger="calendar=None"))
    ledger.upsert(candidate("different-file", sites=[Site("other.ts", 12, "calendar?.externalId")]))
    await Investigator(None, None, protocol_version=2).run(ledger, state(), ContextPack())
    assert len(calls) == 3


def test_investigation_uses_innermost_site_context_without_rewriting_candidate_identity():
    item = candidate()
    before = copy.deepcopy(item.to_dict())
    changeset = SemanticChangeSet(
        "owner/repo",
        1,
        "head",
        units=[
            SemanticUnit("class", "service.ts", "typescript", UnitKind.SYMBOL, "Service", 1, 99),
            SemanticUnit("method", "service.ts", "typescript", UnitKind.SYMBOL, "run", 10, 18),
        ],
    )
    pack = ContextPack(
        units={
            "class": UnitContext(
                "class", [ContextSlice("config", "config.ts", 1, 2, "x", "class_config", "config", "head")]
            ),
            "method": UnitContext(
                "method",
                [ContextSlice("caller", "entry.ts", 1, 3, "main", "entry_caller_navigation", "caller", "head")],
            ),
        }
    )
    value = Investigator(None, None, changeset=changeset, protocol_version=2)
    rendered = value._render_user(item, state(), pack)
    assert "entry_caller_navigation" in rendered
    assert "class_config" in rendered
    assert "Investigation anchor" in rendered
    assert item.to_dict() == before


@pytest.mark.asyncio
async def test_paid_task_admission_reserves_budget_and_reports_uninvestigated_candidates(monkeypatch):
    calls = []

    async def investigate(value, item, *args, **kwargs):
        calls.append(item.identity)
        value._tokens = 12_000
        return value._result(verdict="unknown", reason="contract missing", severity="error")

    monkeypatch.setattr(Investigator, "investigate", investigate)
    ledger = HypothesisLedger("run", "head", "digest", protocol_version=2)
    ledger.upsert(candidate("a"))
    ledger.upsert(candidate("b", trigger="another supported input"))
    await Investigator(None, None, protocol_version=2).run(ledger, state(), ContextPack(), token_budget=24_000)
    assert len(calls) == 1
    assert sum(item.investigation_tokens for item in ledger.items.values()) == 12_000
    assert sum(item.investigation_outcome == "policy-deferred" for item in ledger.items.values()) == 1
    assert all(item.status.value == "unknown" for item in ledger.items.values())
