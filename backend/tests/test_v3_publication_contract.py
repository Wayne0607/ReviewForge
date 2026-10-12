"""Pattern matches and model-written prose cannot bypass independent proof."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from reviewforge.core.state import StateStore
from reviewforge.engine.detectors.base import DetectorFinding
from reviewforge.engine.detectors.security import detect_security_findings
from reviewforge.engine.editor import Editor, cluster_confirmed, render_review_body
from reviewforge.engine.hypothesis import Hypothesis, HypothesisLedger, HypothesisStatus, Mechanism, Site
from reviewforge.engine.pipeline_v4 import _seed_detector_hypotheses
from reviewforge.engine.semantic_diff import SemanticChangeSet, SemanticUnit, UnitKind


def proposed():
    return Hypothesis(
        id="h_claim",
        identity="unit::security-sink::entry",
        unit_id="unit",
        mechanism=Mechanism.SECURITY_SINK,
        claim="The rule reports a dynamic operation",
        trigger="An input reaches the operation",
        impact="Code may execute",
        open_question="Does this operation execute input as code?",
        refutation="It is a declaration",
        severity="warning",
        source="detector:code-injection",
        sites=[Site("app.js", 1, "(function () {})();")],
        status=HypothesisStatus.CONFIRMED,
        evidence_strength="strong",
    )


@pytest.mark.parametrize("confidence", [0.5, 0.99, 1.0])
def test_v3_pattern_match_never_becomes_confirmed_without_investigation(confidence):
    state = StateStore(head_sha="head", file_diffs={"app.js": "@@ -0,0 +1 @@\n+(function () {})();\n"})
    unit = SemanticUnit(
        id="unit",
        path="app.js",
        language="javascript",
        kind=UnitKind.SYMBOL,
        symbol="",
        start_line=1,
        end_line=1,
        added_lines=[1],
    )
    changes = SemanticChangeSet(repo="o/r", pr_number=1, head_sha="head", units=[unit])
    ledger = HypothesisLedger("run", "head", "workspace", protocol_version=3)
    finding = DetectorFinding("app.js", 1, "warning", "code-injection", "Dynamic operation", "Avoid it", confidence)
    assert _seed_detector_hypotheses(state, changes, ledger, [finding]) == 1
    item = next(iter(ledger.items.values()))
    assert item.status is HypothesisStatus.OPEN and item.evidence_strength == "none"
    assert item.open_question and item.refutation and not item.observations and item.assessment is None
    assert item.sites[0].excerpt == "(function () {})();"
    assert not cluster_confirmed(ledger)
    state.file_diffs = {}
    missing = replace(ledger, items={})
    assert _seed_detector_hypotheses(state, changes, missing, [finding]) == 0
    assert missing.unresolved_units and not missing.items


def test_javascript_keyword_is_not_dynamic_function_constructor():
    source = "(function () { return 1; })();\nconst fn = Function(userInput);\nconst upper = FUNCTION(value);"
    diff = "@@ -0,0 +1,3 @@\n" + "\n".join("+" + line for line in source.splitlines())
    findings = [item for item in detect_security_findings({"app.js": diff}) if item.category == "code-injection"]
    assert [item.line for item in findings] == [2]


@pytest.mark.asyncio
async def test_v3_editor_cannot_publish_unverified_confirmed_or_invent_prose():
    item = proposed()
    ledger = HypothesisLedger("run", "head", "workspace", protocol_version=3)
    ledger.upsert(item)
    llm = SimpleNamespace(ainvoke=AsyncMock(side_effect=AssertionError("Prose model must not run")))
    publication = await Editor(llm).run(ledger, None)
    assert not publication.comments and not publication.summary_items
    llm.ainvoke.assert_not_called()
    assert item.claim not in render_review_body(publication, ledger)
    assert not cluster_confirmed(ledger)


def test_proof_and_structure_receipts_round_trip_without_changing_old_protocol():
    ledger = HypothesisLedger("run", "head", "workspace", protocol_version=3)
    ledger.publication_reviews = {"h_claim": {"decision": "unknown", "outcome": "provider-error", "attempts": 1}}
    ledger.structural_checks = {"head_sha": "head", "changes": {"new.json": {"proof_error": "head-tree:timeout"}}}
    loaded = HypothesisLedger.from_dict(ledger.to_dict())
    assert loaded.to_dict() == ledger.to_dict()
    loaded.publication_reviews["h_claim"]["attempts"] = 2
    assert ledger.publication_reviews["h_claim"]["attempts"] == 1
    for protocol in (1, 2):
        old = HypothesisLedger("old", "head", "workspace", protocol_version=protocol).to_dict()
        assert "publication_reviews" not in old and "structural_checks" not in old


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("receipt", "failures", "retry"),
    [
        ("valid", 0, False),
        ("missing", 1, False),
        ("provider", 1, True),
        ("exhausted", 1, False),
        ("budget", 1, False),
        ("stale", 1, False),
    ],
)
async def test_publication_health_checks_current_receipt_and_retry_cap(tmp_path, receipt, failures, retry):
    from reviewforge.core.config import PipelineV4Config
    from reviewforge.core.events import EventBus
    from reviewforge.engine.pipeline_v4 import run_hypothesis_pipeline
    from reviewforge.engine.proof_gate import ProofGate
    from reviewforge.tools.workspace import WorkspaceInfo

    item = proposed()
    ledger = HypothesisLedger("run", "head", "workspace", protocol_version=3)
    ledger.upsert(item)
    llm = SimpleNamespace(ainvoke=AsyncMock(side_effect=AssertionError("No model call expected")))
    await ProofGate(llm).run(ledger)
    record = ledger.publication_reviews[item.id]
    if receipt == "missing":
        ledger.publication_reviews.clear()
    elif receipt in {"provider", "exhausted"}:
        record.update(outcome="provider-error", attempts=2 if receipt == "exhausted" else 1)
    elif receipt == "budget":
        record.update(outcome="budget-exhausted")
    elif receipt == "stale":
        record["fingerprint"] = "outdated"
    info = WorkspaceInfo("o/r", "o/r", "head", tmp_path, 0, 0, "workspace", False, "api-fallback")
    fake = SimpleNamespace(
        _gateway=SimpleNamespace(workspace_for=AsyncMock(return_value=SimpleNamespace(info=info, digest="workspace"))),
        _events=EventBus(),
        _pipeline_v4_config=PipelineV4Config(mode="shadow", protocol_version=3),
        _model_router=None,
        _db=None,
    )
    state = StateStore(repo="o/r", pr_number=1, head_sha="head", ledger=ledger)
    health = await run_hypothesis_pipeline(fake, state)
    assert health.publication.failures == failures
    assert health.publication.retryable is retry
    llm.ainvoke.assert_not_called()
