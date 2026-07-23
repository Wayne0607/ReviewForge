"""Tests for EvidenceVerifier — evidence-based verification of candidate findings.

Tests cover:
- Confirm (prover + refuter agree confirmed)
- Reject (prover + refuter agree rejected)
- Disagreement (arbiter resolves)
- Invalid JSON response
- Provider exception
- Provenance validation
- Serialization round-trip
- Deterministic evidence shortcut
- apply_evidence_to_finding compatibility
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from reviewforge.core.state import Finding
from reviewforge.engine.evidence_verifier import (
    EvidenceCapsule,
    EvidenceItem,
    EvidenceStatus,
    EvidenceVerdict,
    EvidenceVerifier,
    ProverVerdict,
    RefuterVerdict,
    apply_evidence_to_finding,
)

# ---------------------------------------------------------------------------
# Fake LLM helpers
# ---------------------------------------------------------------------------


class FakeLLM:
    """Fake LLM that returns a pre-configured response."""

    def __init__(self, response: dict | None = None, *, raise_exc: Exception | None = None):
        self._response = response
        self._raise_exc = raise_exc
        self.calls: list[list] = []

    async def ainvoke(self, messages: list) -> SimpleNamespace:
        self.calls.append(messages)
        if self._raise_exc is not None:
            raise self._raise_exc
        content = json.dumps(self._response, ensure_ascii=False) if self._response else "invalid json"
        return SimpleNamespace(content=content)


class InvalidJsonLLM:
    """Returns invalid JSON every time."""

    def __init__(self):
        self.calls: list[list] = []

    async def ainvoke(self, messages: list) -> SimpleNamespace:
        self.calls.append(messages)
        return SimpleNamespace(content="this is not json at all")


class RepairableLLM:
    """Returns invalid JSON first, then valid JSON on repair attempt."""

    def __init__(self, valid_response: dict):
        self._valid_response = valid_response
        self.calls: list[list] = []

    async def ainvoke(self, messages: list) -> SimpleNamespace:
        self.calls.append(messages)
        # First call: invalid. Repair call (3 messages): valid.
        if len(messages) <= 2:
            return SimpleNamespace(content="garbage")
        return SimpleNamespace(content=json.dumps(self._valid_response, ensure_ascii=False))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_finding(**overrides) -> Finding:
    defaults = {
        "id": "finding_001",
        "file": "src/auth.py",
        "line": 42,
        "severity": "error",
        "category": "sql-injection",
        "message": "SQL injection via unsanitized user input",
        "suggestion": "Use parameterized queries",
        "confidence": 0.8,
        "reviewer": "security_reviewer",
    }
    defaults.update(overrides)
    return Finding(**defaults)


def _make_evidence(**overrides) -> EvidenceItem:
    defaults = {
        "kind": "supporting",
        "path": "src/auth.py",
        "sha": "abc123def456",
        "line": 42,
        "snippet": 'cursor.execute(f"SELECT * FROM users WHERE id={user_id}")',
        "trigger": "user_id from request.params",
        "violated_contract": "SQL queries must use parameterized statements",
    }
    defaults.update(overrides)
    return EvidenceItem(**defaults)


def _confirmed_response() -> dict:
    return {
        "verdict": "confirmed",
        "confidence": 0.9,
        "rationale": "Clear SQL injection vulnerability in execute call",
    }


def _rejected_response() -> dict:
    return {
        "verdict": "rejected",
        "confidence": 0.85,
        "rationale": "Input is validated upstream via allow-list",
    }


def _abstain_response() -> dict:
    return {
        "verdict": "abstain",
        "confidence": 0.3,
        "rationale": "Insufficient evidence to determine",
    }


# ---------------------------------------------------------------------------
# EvidenceItem tests
# ---------------------------------------------------------------------------


class TestEvidenceItem:
    def test_serialization_round_trip(self):
        item = _make_evidence()
        d = item.to_dict()
        restored = EvidenceItem.from_dict(d)
        assert restored.kind == item.kind
        assert restored.path == item.path
        assert restored.sha == item.sha
        assert restored.line == item.line
        assert restored.snippet == item.snippet
        assert restored.trigger == item.trigger
        assert restored.violated_contract == item.violated_contract

    def test_from_dict_missing_key_raises(self):
        with pytest.raises(ValueError, match="missing required key"):
            EvidenceItem.from_dict({"kind": "supporting", "path": "x.py"})

    def test_from_dict_invalid_kind_raises(self):
        data = {"kind": "invalid", "path": "x.py", "sha": "abc", "line": 1, "snippet": "x"}
        with pytest.raises(ValueError, match="Invalid evidence kind"):
            EvidenceItem.from_dict(data)

    def test_from_dict_invalid_line_raises(self):
        data = {"kind": "supporting", "path": "x.py", "sha": "abc", "line": 0, "snippet": "x"}
        with pytest.raises(ValueError, match="Invalid line number"):
            EvidenceItem.from_dict(data)

    def test_frozen(self):
        item = _make_evidence()
        with pytest.raises(AttributeError):
            item.line = 99  # type: ignore[misc]


# ---------------------------------------------------------------------------
# EvidenceCapsule tests
# ---------------------------------------------------------------------------


class TestEvidenceCapsule:
    def test_final_verdict_no_verdicts_is_abstain(self):
        capsule = EvidenceCapsule(finding_id="f1")
        assert capsule.final_verdict == EvidenceVerdict.ABSTAIN

    def test_final_verdict_agreement_confirmed(self):
        capsule = EvidenceCapsule(finding_id="f1")
        capsule.prover = ProverVerdict(EvidenceVerdict.CONFIRMED, 0.9, "yes")
        capsule.refuter = RefuterVerdict(EvidenceVerdict.CONFIRMED, 0.85, "also yes")
        assert capsule.final_verdict == EvidenceVerdict.CONFIRMED

    def test_final_verdict_agreement_rejected(self):
        capsule = EvidenceCapsule(finding_id="f1")
        capsule.prover = ProverVerdict(EvidenceVerdict.REJECTED, 0.9, "no")
        capsule.refuter = RefuterVerdict(EvidenceVerdict.REJECTED, 0.85, "also no")
        assert capsule.final_verdict == EvidenceVerdict.REJECTED

    def test_final_verdict_disagreement_is_abstain(self):
        capsule = EvidenceCapsule(finding_id="f1")
        capsule.prover = ProverVerdict(EvidenceVerdict.CONFIRMED, 0.9, "yes")
        capsule.refuter = RefuterVerdict(EvidenceVerdict.REJECTED, 0.85, "no")
        assert capsule.final_verdict == EvidenceVerdict.ABSTAIN

    def test_final_verdict_arbiter_overrides(self):
        capsule = EvidenceCapsule(finding_id="f1")
        capsule.prover = ProverVerdict(EvidenceVerdict.CONFIRMED, 0.9, "yes")
        capsule.refuter = RefuterVerdict(EvidenceVerdict.REJECTED, 0.85, "no")
        capsule.arbiter = SimpleNamespace(
            verdict=EvidenceVerdict.REJECTED,
            confidence=0.7,
            rationale="arbiter says no",
        )
        assert capsule.final_verdict == EvidenceVerdict.REJECTED

    def test_serialization_round_trip(self):
        capsule = EvidenceCapsule(
            finding_id="f1",
            evidence=[_make_evidence()],
        )
        capsule.prover = ProverVerdict(EvidenceVerdict.CONFIRMED, 0.9, "yes")
        capsule.refuter = RefuterVerdict(EvidenceVerdict.CONFIRMED, 0.85, "also yes")
        capsule.status = EvidenceStatus.CONFIRMED

        d = capsule.to_dict()
        restored = EvidenceCapsule.from_dict(d)

        assert restored.finding_id == "f1"
        assert len(restored.evidence) == 1
        assert restored.prover is not None
        assert restored.prover.verdict == EvidenceVerdict.CONFIRMED
        assert restored.refuter is not None
        assert restored.refuter.verdict == EvidenceVerdict.CONFIRMED

    def test_from_dict_missing_finding_id_raises(self):
        with pytest.raises(ValueError, match="missing finding_id"):
            EvidenceCapsule.from_dict({"evidence": []})


# ---------------------------------------------------------------------------
# EvidenceVerifier — confirm
# ---------------------------------------------------------------------------


class TestVerifyConfirm:
    async def test_prover_and_refuter_agree_confirmed(self):
        """Both prover and refuter confirm → final is confirmed, no arbiter."""
        prover = FakeLLM(_confirmed_response())
        refuter = FakeLLM(_confirmed_response())
        arbiter = FakeLLM(_abstain_response())  # should not be called

        verifier = EvidenceVerifier(prover, refuter, arbiter)
        finding = _make_finding()
        evidence = [_make_evidence()]

        capsule = await verifier.verify_candidate(finding, evidence, "diff text")

        assert capsule.final_verdict == EvidenceVerdict.CONFIRMED
        assert capsule.status == EvidenceStatus.CONFIRMED
        assert capsule.confidence > 0.5
        assert len(arbiter.calls) == 0  # arbiter not needed
        assert capsule.retry_metadata == {}

    async def test_prover_and_refuter_agree_rejected(self):
        """Both prover and refuter reject → final is rejected, no arbiter."""
        prover = FakeLLM(_rejected_response())
        refuter = FakeLLM(_rejected_response())
        arbiter = FakeLLM(_confirmed_response())

        verifier = EvidenceVerifier(prover, refuter, arbiter)
        finding = _make_finding()

        capsule = await verifier.verify_candidate(finding, [], "diff text")

        assert capsule.final_verdict == EvidenceVerdict.REJECTED
        assert capsule.status == EvidenceStatus.REJECTED
        assert len(arbiter.calls) == 0


# ---------------------------------------------------------------------------
# EvidenceVerifier — disagreement → arbiter
# ---------------------------------------------------------------------------


class TestVerifyDisagreement:
    async def test_disagreement_calls_arbiter(self):
        """Prover confirms, refuter rejects → arbiter is called to resolve."""
        prover = FakeLLM(_confirmed_response())
        refuter = FakeLLM(_rejected_response())
        arbiter = FakeLLM(_rejected_response())

        verifier = EvidenceVerifier(prover, refuter, arbiter)
        finding = _make_finding()

        capsule = await verifier.verify_candidate(finding, [], "diff text")

        assert len(arbiter.calls) == 1
        assert capsule.final_verdict == EvidenceVerdict.REJECTED
        assert capsule.arbiter is not None

    async def test_arbiter_abstain_on_disagreement(self):
        """Arbiter abstains → final is abstain."""
        prover = FakeLLM(_confirmed_response())
        refuter = FakeLLM(_rejected_response())
        arbiter = FakeLLM(_abstain_response())

        verifier = EvidenceVerifier(prover, refuter, arbiter)
        finding = _make_finding()

        capsule = await verifier.verify_candidate(finding, [], "diff text")

        assert capsule.final_verdict == EvidenceVerdict.ABSTAIN
        assert capsule.status == EvidenceStatus.ABSTAIN


# ---------------------------------------------------------------------------
# EvidenceVerifier — invalid JSON
# ---------------------------------------------------------------------------


class TestVerifyInvalidJson:
    async def test_prover_invalid_json_produces_abstain(self):
        """Invalid JSON from prover → prover abstains, metadata recorded."""
        prover = InvalidJsonLLM()
        refuter = FakeLLM(_confirmed_response())
        arbiter = FakeLLM(_confirmed_response())

        verifier = EvidenceVerifier(prover, refuter, arbiter)
        finding = _make_finding()

        capsule = await verifier.verify_candidate(finding, [], "diff")

        assert capsule.prover.verdict == EvidenceVerdict.ABSTAIN
        assert capsule.retry_metadata.get("prover_failed") is True
        # arbiter called because prover abstained
        assert len(arbiter.calls) == 1

    async def test_refuter_invalid_json_produces_abstain(self):
        """Invalid JSON from refuter → refuter abstains, metadata recorded."""
        prover = FakeLLM(_confirmed_response())
        refuter = InvalidJsonLLM()
        arbiter = FakeLLM(_confirmed_response())

        verifier = EvidenceVerifier(prover, refuter, arbiter)
        finding = _make_finding()

        capsule = await verifier.verify_candidate(finding, [], "diff")

        assert capsule.refuter.verdict == EvidenceVerdict.ABSTAIN
        assert capsule.retry_metadata.get("refuter_failed") is True

    async def test_both_invalid_produces_abstain(self):
        """Both prover and refuter return invalid JSON → abstain with metadata."""
        prover = InvalidJsonLLM()
        refuter = InvalidJsonLLM()
        arbiter = InvalidJsonLLM()

        verifier = EvidenceVerifier(prover, refuter, arbiter)
        finding = _make_finding()

        capsule = await verifier.verify_candidate(finding, [], "diff")

        assert capsule.final_verdict == EvidenceVerdict.ABSTAIN
        assert capsule.retry_metadata.get("prover_failed") is True
        assert capsule.retry_metadata.get("refuter_failed") is True
        assert capsule.retry_metadata.get("arbiter_failed") is True

    async def test_repair_succeeds_on_second_attempt(self):
        """Invalid first response, valid repair → succeeds."""
        valid = _confirmed_response()
        prover = RepairableLLM(valid)
        refuter = RepairableLLM(valid)
        arbiter = FakeLLM(_confirmed_response())

        verifier = EvidenceVerifier(prover, refuter, arbiter)
        finding = _make_finding()

        capsule = await verifier.verify_candidate(finding, [], "diff")

        assert capsule.final_verdict == EvidenceVerdict.CONFIRMED
        # Each model called twice (initial + repair)
        assert len(prover.calls) == 2
        assert len(refuter.calls) == 2


# ---------------------------------------------------------------------------
# EvidenceVerifier — provider exception
# ---------------------------------------------------------------------------


class TestVerifyProviderException:
    async def test_prover_exception_produces_abstain(self):
        """Provider exception from prover → abstain, never false-positive."""
        prover = FakeLLM(raise_exc=RuntimeError("API timeout"))
        refuter = FakeLLM(_rejected_response())
        arbiter = FakeLLM(_rejected_response())

        verifier = EvidenceVerifier(prover, refuter, arbiter)
        finding = _make_finding()

        capsule = await verifier.verify_candidate(finding, [], "diff")

        assert capsule.prover.verdict == EvidenceVerdict.ABSTAIN
        assert capsule.retry_metadata.get("prover_failed") is True
        # P0 fix: prover failure → final verdict forced to ABSTAIN
        assert capsule.final_verdict == EvidenceVerdict.ABSTAIN
        assert capsule.status == EvidenceStatus.ABSTAIN

    async def test_refuter_exception_produces_abstain(self):
        """Provider exception from refuter → abstain with metadata."""
        prover = FakeLLM(_confirmed_response())
        refuter = FakeLLM(raise_exc=ConnectionError("network error"))
        arbiter = FakeLLM(_confirmed_response())

        verifier = EvidenceVerifier(prover, refuter, arbiter)
        finding = _make_finding()

        capsule = await verifier.verify_candidate(finding, [], "diff")

        assert capsule.refuter.verdict == EvidenceVerdict.ABSTAIN
        assert capsule.retry_metadata.get("refuter_failed") is True

    async def test_arbiter_exception_produces_abstain(self):
        """Provider exception from arbiter → abstain with metadata."""
        prover = FakeLLM(_confirmed_response())
        refuter = FakeLLM(_rejected_response())
        arbiter = FakeLLM(raise_exc=TimeoutError("timeout"))

        verifier = EvidenceVerifier(prover, refuter, arbiter)
        finding = _make_finding()

        capsule = await verifier.verify_candidate(finding, [], "diff")

        assert capsule.arbiter is not None
        assert capsule.arbiter.verdict == EvidenceVerdict.ABSTAIN
        assert capsule.retry_metadata.get("arbiter_failed") is True

    async def test_all_providers_fail_produces_abstain(self):
        """All providers fail → abstain with full metadata, never false-positive."""
        prover = FakeLLM(raise_exc=RuntimeError("fail"))
        refuter = FakeLLM(raise_exc=RuntimeError("fail"))
        arbiter = FakeLLM(raise_exc=RuntimeError("fail"))

        verifier = EvidenceVerifier(prover, refuter, arbiter)
        finding = _make_finding()

        capsule = await verifier.verify_candidate(finding, [], "diff")

        assert capsule.final_verdict == EvidenceVerdict.ABSTAIN
        assert capsule.status == EvidenceStatus.ABSTAIN
        assert capsule.retry_metadata.get("prover_failed") is True
        assert capsule.retry_metadata.get("refuter_failed") is True
        assert capsule.retry_metadata.get("arbiter_failed") is True


# ---------------------------------------------------------------------------
# EvidenceVerifier — provenance validation
# ---------------------------------------------------------------------------


class TestProvenanceValidation:
    async def test_evidence_with_valid_provenance_in_prompt(self):
        """Evidence items with valid provenance appear in the prompt."""
        prover = FakeLLM(_confirmed_response())
        refuter = FakeLLM(_confirmed_response())
        arbiter = FakeLLM(_confirmed_response())

        verifier = EvidenceVerifier(prover, refuter, arbiter)
        finding = _make_finding()
        # Use two different SHAs to avoid deterministic shortcut
        evidence = [
            _make_evidence(sha="abc123def456", trigger="request.params"),
            _make_evidence(sha="different_sha", trigger="other"),
        ]

        await verifier.verify_candidate(finding, evidence, "diff")

        # Check that evidence was included in the prover prompt
        assert len(prover.calls) == 1
        user_msg = str(prover.calls[0][1].content)
        assert "abc123de" in user_msg  # SHA truncated to 8 chars in summary
        assert "request.params" in user_msg
        assert "SQL queries must use parameterized statements" in user_msg

    async def test_deterministic_shortcut_requires_matching_sha(self):
        """Deterministic shortcut only works when all evidence has same SHA."""
        prover = FakeLLM(_confirmed_response())
        refuter = FakeLLM(_confirmed_response())
        arbiter = FakeLLM(_confirmed_response())

        verifier = EvidenceVerifier(prover, refuter, arbiter)
        finding = _make_finding()

        # Two evidence items with different SHAs → no shortcut
        evidence = [
            _make_evidence(sha="sha1"),
            _make_evidence(sha="sha2"),
        ]

        await verifier.verify_candidate(finding, evidence, "diff")

        # Should have called LLMs (no shortcut)
        assert len(prover.calls) == 1

    async def test_deterministic_shortcut_requires_trigger_and_contract(self):
        """Deterministic shortcut requires trigger and violated_contract."""
        prover = FakeLLM(_confirmed_response())
        refuter = FakeLLM(_confirmed_response())
        arbiter = FakeLLM(_confirmed_response())

        verifier = EvidenceVerifier(prover, refuter, arbiter)
        finding = _make_finding()

        # Missing trigger → no shortcut
        evidence_no_trigger = [
            EvidenceItem(
                kind="supporting",
                path="src/auth.py",
                sha="abc123",
                line=42,
                snippet="code",
                trigger="",
                violated_contract="contract",
            )
        ]
        await verifier.verify_candidate(finding, evidence_no_trigger, "diff")
        assert len(prover.calls) == 1

        prover.calls.clear()

        # Missing contract → no shortcut
        evidence_no_contract = [
            EvidenceItem(
                kind="supporting",
                path="src/auth.py",
                sha="abc123",
                line=42,
                snippet="code",
                trigger="trigger",
                violated_contract="",
            )
        ]
        await verifier.verify_candidate(finding, evidence_no_contract, "diff")
        assert len(prover.calls) == 1

    async def test_deterministic_shortcut_with_refuting_evidence_skipped(self):
        """Deterministic shortcut is skipped when refuting evidence exists."""
        prover = FakeLLM(_confirmed_response())
        refuter = FakeLLM(_confirmed_response())
        arbiter = FakeLLM(_confirmed_response())

        verifier = EvidenceVerifier(prover, refuter, arbiter)
        finding = _make_finding()

        evidence = [
            _make_evidence(kind="supporting"),
            _make_evidence(kind="refuting"),
        ]

        await verifier.verify_candidate(finding, evidence, "diff")

        # Should have called LLMs (no shortcut due to refuting evidence)
        assert len(prover.calls) == 1


# ---------------------------------------------------------------------------
# Deterministic evidence shortcut
# ---------------------------------------------------------------------------


class TestDeterministicShortcut:
    async def test_complete_evidence_bypasses_llm(self):
        """Complete code evidence with matching SHA → confirmed without LLM."""
        prover = FakeLLM(raise_exc=AssertionError("should not be called"))
        refuter = FakeLLM(raise_exc=AssertionError("should not be called"))
        arbiter = FakeLLM(raise_exc=AssertionError("should not be called"))

        verifier = EvidenceVerifier(prover, refuter, arbiter, expected_head_sha="abc123def456")
        finding = _make_finding(file="src/auth.py")
        evidence = [
            EvidenceItem(
                kind="supporting",
                path="src/auth.py",
                sha="abc123def456",
                line=42,
                snippet='cursor.execute(f"SELECT * FROM users WHERE id={user_id}")',
                trigger="user_id from request.params",
                violated_contract="SQL queries must use parameterized statements",
            )
        ]

        capsule = await verifier.verify_candidate(finding, evidence, "diff")

        assert capsule.final_verdict == EvidenceVerdict.CONFIRMED
        assert capsule.status == EvidenceStatus.CONFIRMED
        assert len(prover.calls) == 0
        assert len(refuter.calls) == 0
        assert len(arbiter.calls) == 0

    async def test_shortcut_with_multiple_same_sha_evidence(self):
        """Multiple evidence items with same SHA → deterministic shortcut."""
        prover = FakeLLM(raise_exc=AssertionError("should not be called"))
        refuter = FakeLLM(raise_exc=AssertionError("should not be called"))
        arbiter = FakeLLM(raise_exc=AssertionError("should not be called"))

        verifier = EvidenceVerifier(prover, refuter, arbiter, expected_head_sha="abc123")
        finding = _make_finding(file="src/auth.py")
        evidence = [
            EvidenceItem(
                kind="supporting",
                path="src/auth.py",
                sha="abc123",
                line=42,
                snippet="code1",
                trigger="t1",
                violated_contract="c1",
            ),
            EvidenceItem(
                kind="supporting",
                path="src/utils.py",
                sha="abc123",
                line=10,
                snippet="code2",
                trigger="t2",
                violated_contract="c2",
            ),
        ]

        capsule = await verifier.verify_candidate(finding, evidence, "diff")

        assert capsule.final_verdict == EvidenceVerdict.CONFIRMED
        assert len(prover.calls) == 0


# ---------------------------------------------------------------------------
# Batch verification
# ---------------------------------------------------------------------------


class TestVerifyBatch:
    async def test_batch_verifies_multiple_findings(self):
        """Batch verification processes multiple findings."""
        prover = FakeLLM(_confirmed_response())
        refuter = FakeLLM(_confirmed_response())
        arbiter = FakeLLM(_confirmed_response())

        verifier = EvidenceVerifier(prover, refuter, arbiter)
        findings = [_make_finding(id=f"f_{i}") for i in range(3)]

        capsules = await verifier.verify_batch(findings, {}, "diff")

        assert len(capsules) == 3
        for capsule in capsules:
            assert capsule.final_verdict == EvidenceVerdict.CONFIRMED

    async def test_batch_exception_produces_abstain(self):
        """Provider exception during batch → abstain with retry metadata."""
        call_count = 0

        class FailingOnSecondLLM:
            def __init__(self):
                self.calls = []

            async def ainvoke(self, messages):
                nonlocal call_count
                call_count += 1
                if call_count == 2:
                    raise RuntimeError("simulated failure")
                return SimpleNamespace(content=json.dumps(_confirmed_response()))

        prover = FailingOnSecondLLM()
        refuter = FailingOnSecondLLM()
        arbiter = FakeLLM(_confirmed_response())

        verifier = EvidenceVerifier(prover, refuter, arbiter)
        findings = [_make_finding(id=f"f_{i}") for i in range(3)]

        capsules = await verifier.verify_batch(findings, {}, "diff")

        assert len(capsules) == 3
        # The finding where prover failed should have retry metadata
        failed_capsules = [
            c for c in capsules if c.retry_metadata.get("prover_failed") or c.retry_metadata.get("refuter_failed")
        ]
        assert len(failed_capsules) >= 1


# ---------------------------------------------------------------------------
# apply_evidence_to_finding
# ---------------------------------------------------------------------------


class TestApplyEvidence:
    def test_confirmed_updates_finding(self):
        """Confirmed capsule updates finding status and confidence."""
        finding = _make_finding()
        capsule = EvidenceCapsule(finding_id=finding.id)
        capsule.prover = ProverVerdict(EvidenceVerdict.CONFIRMED, 0.9, "yes")
        capsule.refuter = RefuterVerdict(EvidenceVerdict.CONFIRMED, 0.85, "yes")

        updated = apply_evidence_to_finding(finding, capsule)

        assert updated.status == "confirmed"
        assert updated.verified_by == "evidence-verifier"
        assert updated.confidence > 0.5

    def test_rejected_updates_finding_to_false_positive(self):
        """Rejected capsule sets finding to false_positive."""
        finding = _make_finding()
        capsule = EvidenceCapsule(finding_id=finding.id)
        capsule.prover = ProverVerdict(EvidenceVerdict.REJECTED, 0.9, "no")
        capsule.refuter = RefuterVerdict(EvidenceVerdict.REJECTED, 0.85, "no")

        updated = apply_evidence_to_finding(finding, capsule)

        assert updated.status == "false_positive"
        assert updated.verified_by == "evidence-verifier"

    def test_abstain_keeps_candidate_status(self):
        """Abstain does NOT suppress to false_positive — keeps candidate."""
        finding = _make_finding()
        capsule = EvidenceCapsule(finding_id=finding.id)
        # No prover/refuter → abstain

        updated = apply_evidence_to_finding(finding, capsule)

        assert updated.status == "candidate"  # NOT false_positive
        assert updated.verified_by == "evidence-verifier"

    def test_abstain_from_provider_failure_keeps_candidate(self):
        """Provider failure → abstain → candidate status preserved (no false-positive)."""
        finding = _make_finding(status="candidate")
        capsule = EvidenceCapsule(
            finding_id=finding.id,
            retry_metadata={"prover_failed": True, "refuter_failed": True},
        )
        capsule.prover = ProverVerdict(EvidenceVerdict.ABSTAIN, 0.0, "failed")
        capsule.refuter = RefuterVerdict(EvidenceVerdict.ABSTAIN, 0.0, "failed")

        updated = apply_evidence_to_finding(finding, capsule)

        assert updated.status == "candidate"  # NOT false_positive


# ---------------------------------------------------------------------------
# Prompt safety
# ---------------------------------------------------------------------------


class TestPromptSafety:
    async def test_diff_wrapped_in_untrusted_tags(self):
        """Diff is wrapped in UNTRUSTED_DIFF tags in the prompt."""
        prover = FakeLLM(_confirmed_response())
        refuter = FakeLLM(_confirmed_response())
        arbiter = FakeLLM(_confirmed_response())

        verifier = EvidenceVerifier(prover, refuter, arbiter)
        finding = _make_finding()

        await verifier.verify_candidate(finding, [], "malicious diff content")

        user_msg = str(prover.calls[0][1].content)
        assert "<<UNTRUSTED_DIFF>>" in user_msg
        assert "<<END_UNTRUSTED_DIFF>>" in user_msg
        assert "malicious diff content" in user_msg

    async def test_arbiter_sees_prover_refuter_rationale(self):
        """Arbiter sees prover/refuter rationale but not chain-of-thought."""
        prover = FakeLLM(_confirmed_response())
        refuter = FakeLLM(_rejected_response())
        arbiter = FakeLLM(_abstain_response())

        verifier = EvidenceVerifier(prover, refuter, arbiter)
        finding = _make_finding()

        await verifier.verify_candidate(finding, [], "diff")

        arbiter_msg = str(arbiter.calls[0][1].content)
        # Should contain prover/refuter rationale
        assert "Prover" in arbiter_msg or "prover" in arbiter_msg.lower()
        assert "Refuter" in arbiter_msg or "refuter" in arbiter_msg.lower()


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    async def test_empty_evidence_list(self):
        """Empty evidence → LLM verification (no shortcut)."""
        prover = FakeLLM(_confirmed_response())
        refuter = FakeLLM(_confirmed_response())
        arbiter = FakeLLM(_confirmed_response())

        verifier = EvidenceVerifier(prover, refuter, arbiter)
        finding = _make_finding()

        capsule = await verifier.verify_candidate(finding, [], "diff")

        assert len(prover.calls) == 1
        assert capsule.finding_id == finding.id

    async def test_deterministic_shortcut_file_path_normalization(self):
        """File path normalization (backslash vs forward slash) works."""
        prover = FakeLLM(raise_exc=AssertionError("should not be called"))
        refuter = FakeLLM(raise_exc=AssertionError("should not be called"))
        arbiter = FakeLLM(raise_exc=AssertionError("should not be called"))

        verifier = EvidenceVerifier(prover, refuter, arbiter, expected_head_sha="abc123")
        finding = _make_finding(file="src\\auth.py")  # Windows path
        evidence = [
            EvidenceItem(
                kind="supporting",
                path="src/auth.py",  # Unix path
                sha="abc123",
                line=42,
                snippet="code",
                trigger="trigger",
                violated_contract="contract",
            )
        ]

        capsule = await verifier.verify_candidate(finding, evidence, "diff")

        assert capsule.final_verdict == EvidenceVerdict.CONFIRMED
        assert len(prover.calls) == 0

    async def test_max_candidates_limits_batch(self):
        """Batch verification respects max_candidates limit."""
        prover = FakeLLM(_confirmed_response())
        refuter = FakeLLM(_confirmed_response())
        arbiter = FakeLLM(_confirmed_response())

        verifier = EvidenceVerifier(prover, refuter, arbiter, max_candidates=2)
        findings = [_make_finding(id=f"f_{i}") for i in range(5)]

        capsules = await verifier.verify_batch(findings, {}, "diff")

        assert len(capsules) == 2  # limited by max_candidates


# ---------------------------------------------------------------------------
# P0 fix — ANY operational failure forces ABSTAIN
# ---------------------------------------------------------------------------


class TestAnyFailureForcesAbstain:
    """Regression tests for the P0 bug: provider failure suppressing findings.

    When ANY prover/refuter/arbiter fails (exception or invalid output) and
    retry_metadata is set, the effective final verdict/status MUST be ABSTAIN
    regardless of what the other models returned.  apply_evidence_to_finding
    must preserve candidate status in that case.
    """

    @pytest.mark.parametrize(
        "prover_llm,refuter_llm,arbiter_llm,scenario",
        [
            # Scenario 1: prover fails, refuter rejects, arbiter rejects
            pytest.param(
                FakeLLM(raise_exc=RuntimeError("prover down")),
                FakeLLM(_rejected_response()),
                FakeLLM(_rejected_response()),
                "prover_failure_refuter_reject_arbiter_reject",
                id="prover-fail+refuter-reject+arbiter-reject",
            ),
            # Scenario 2: refuter fails, prover rejects, arbiter rejects
            pytest.param(
                FakeLLM(_rejected_response()),
                FakeLLM(raise_exc=RuntimeError("refuter down")),
                FakeLLM(_rejected_response()),
                "refuter_failure_prover_reject_arbiter_reject",
                id="refuter-fail+prover-reject+arbiter-reject",
            ),
            # Scenario 3: arbiter fails on disagreement
            pytest.param(
                FakeLLM(_confirmed_response()),
                FakeLLM(_rejected_response()),
                FakeLLM(raise_exc=TimeoutError("arbiter timeout")),
                "arbiter_failure_on_disagreement",
                id="arbiter-fail-on-disagreement",
            ),
        ],
    )
    async def test_failure_forces_abstain(self, prover_llm, refuter_llm, arbiter_llm, scenario):
        """ANY operational failure → ABSTAIN, candidate preserved."""
        verifier = EvidenceVerifier(prover_llm, refuter_llm, arbiter_llm)
        finding = _make_finding(status="candidate")

        capsule = await verifier.verify_candidate(finding, [], "diff")

        # Core assertion: verdict and status must be ABSTAIN
        assert capsule.final_verdict == EvidenceVerdict.ABSTAIN, (
            f"[{scenario}] final_verdict should be ABSTAIN, got {capsule.final_verdict}"
        )
        assert capsule.status == EvidenceStatus.ABSTAIN, f"[{scenario}] status should be ABSTAIN, got {capsule.status}"
        # retry_metadata must record the failure
        assert len(capsule.retry_metadata) > 0, f"[{scenario}] retry_metadata must record the failure"

    @pytest.mark.parametrize(
        "prover_llm,refuter_llm,arbiter_llm,scenario",
        [
            pytest.param(
                FakeLLM(raise_exc=RuntimeError("prover down")),
                FakeLLM(_rejected_response()),
                FakeLLM(_rejected_response()),
                "prover_failure_refuter_reject_arbiter_reject",
                id="prover-fail+refuter-reject+arbiter-reject",
            ),
            pytest.param(
                FakeLLM(_rejected_response()),
                FakeLLM(raise_exc=RuntimeError("refuter down")),
                FakeLLM(_rejected_response()),
                "refuter_failure_prover_reject_arbiter_reject",
                id="refuter-fail+prover-reject+arbiter-reject",
            ),
            pytest.param(
                FakeLLM(_confirmed_response()),
                FakeLLM(_rejected_response()),
                FakeLLM(raise_exc=TimeoutError("arbiter timeout")),
                "arbiter_failure_on_disagreement",
                id="arbiter-fail-on-disagreement",
            ),
        ],
    )
    async def test_apply_evidence_preserves_candidate_on_failure(self, prover_llm, refuter_llm, arbiter_llm, scenario):
        """apply_evidence_to_finding must preserve candidate on ANY failure."""
        verifier = EvidenceVerifier(prover_llm, refuter_llm, arbiter_llm)
        finding = _make_finding(status="candidate")

        capsule = await verifier.verify_candidate(finding, [], "diff")
        updated = apply_evidence_to_finding(finding, capsule)

        assert updated.status == "candidate", f"[{scenario}] finding status must stay candidate, got {updated.status}"
        assert updated.verified_by == "evidence-verifier"


class TestInvalidOutputForcesAbstain:
    """Invalid JSON output (not exception) also counts as operational failure."""

    async def test_prover_invalid_json_refuter_reject_arbiter_reject(self):
        """Prover invalid JSON + refuter reject + arbiter reject → ABSTAIN."""
        prover = InvalidJsonLLM()
        refuter = FakeLLM(_rejected_response())
        arbiter = FakeLLM(_rejected_response())

        verifier = EvidenceVerifier(prover, refuter, arbiter)
        finding = _make_finding(status="candidate")

        capsule = await verifier.verify_candidate(finding, [], "diff")

        assert capsule.retry_metadata.get("prover_failed") is True
        assert capsule.final_verdict == EvidenceVerdict.ABSTAIN
        assert capsule.status == EvidenceStatus.ABSTAIN

        updated = apply_evidence_to_finding(finding, capsule)
        assert updated.status == "candidate"


# ---------------------------------------------------------------------------
# Deterministic shortcut — expected_head_sha guard
# ---------------------------------------------------------------------------


class TestExpectedHeadSha:
    """Strengthened deterministic shortcut: requires expected_head_sha match."""

    async def test_no_shortcut_without_expected_sha(self):
        """Without expected_head_sha, shortcut is disabled → LLM runs."""
        prover = FakeLLM(_confirmed_response())
        refuter = FakeLLM(_confirmed_response())
        arbiter = FakeLLM(_confirmed_response())

        verifier = EvidenceVerifier(prover, refuter, arbiter)  # no expected_head_sha
        finding = _make_finding(file="src/auth.py")
        evidence = [
            EvidenceItem(
                kind="supporting",
                path="src/auth.py",
                sha="abc123",
                line=42,
                snippet="code",
                trigger="trigger",
                violated_contract="contract",
            )
        ]

        capsule = await verifier.verify_candidate(finding, evidence, "diff")

        # LLMs should be called (shortcut disabled)
        assert len(prover.calls) == 1
        assert capsule.final_verdict == EvidenceVerdict.CONFIRMED

    async def test_no_shortcut_on_sha_mismatch(self):
        """Evidence SHA != expected_head_sha → shortcut disabled."""
        prover = FakeLLM(_confirmed_response())
        refuter = FakeLLM(_confirmed_response())
        arbiter = FakeLLM(_confirmed_response())

        verifier = EvidenceVerifier(prover, refuter, arbiter, expected_head_sha="correct_sha")
        finding = _make_finding(file="src/auth.py")
        evidence = [
            EvidenceItem(
                kind="supporting",
                path="src/auth.py",
                sha="stale_sha",  # does NOT match expected
                line=42,
                snippet="code",
                trigger="trigger",
                violated_contract="contract",
            )
        ]

        await verifier.verify_candidate(finding, evidence, "diff")

        # LLMs should be called (SHA mismatch blocks shortcut)
        assert len(prover.calls) == 1

    async def test_shortcut_with_instance_expected_sha(self):
        """Instance-level expected_head_sha enables shortcut."""
        prover = FakeLLM(raise_exc=AssertionError("should not be called"))
        refuter = FakeLLM(raise_exc=AssertionError("should not be called"))
        arbiter = FakeLLM(raise_exc=AssertionError("should not be called"))

        verifier = EvidenceVerifier(prover, refuter, arbiter, expected_head_sha="abc123")
        finding = _make_finding(file="src/auth.py")
        evidence = [
            EvidenceItem(
                kind="supporting",
                path="src/auth.py",
                sha="abc123",
                line=42,
                snippet="code",
                trigger="trigger",
                violated_contract="contract",
            )
        ]

        capsule = await verifier.verify_candidate(finding, evidence, "diff")

        assert capsule.final_verdict == EvidenceVerdict.CONFIRMED
        assert len(prover.calls) == 0

    async def test_method_sha_overrides_instance_sha(self):
        """Method-level expected_head_sha overrides instance-level."""
        prover = FakeLLM(raise_exc=AssertionError("should not be called"))
        refuter = FakeLLM(raise_exc=AssertionError("should not be called"))
        arbiter = FakeLLM(raise_exc=AssertionError("should not be called"))

        verifier = EvidenceVerifier(prover, refuter, arbiter, expected_head_sha="wrong_sha")
        finding = _make_finding(file="src/auth.py")
        evidence = [
            EvidenceItem(
                kind="supporting",
                path="src/auth.py",
                sha="correct_sha",
                line=42,
                snippet="code",
                trigger="trigger",
                violated_contract="contract",
            )
        ]

        # Method-level overrides instance → matches evidence SHA
        capsule = await verifier.verify_candidate(finding, evidence, "diff", expected_head_sha="correct_sha")

        assert capsule.final_verdict == EvidenceVerdict.CONFIRMED
        assert len(prover.calls) == 0

    async def test_method_sha_none_falls_back_to_instance(self):
        """Method-level None (default) falls back to instance SHA."""
        prover = FakeLLM(_confirmed_response())
        refuter = FakeLLM(_confirmed_response())
        arbiter = FakeLLM(_confirmed_response())

        verifier = EvidenceVerifier(prover, refuter, arbiter, expected_head_sha="abc123")
        finding = _make_finding(file="src/auth.py")
        evidence = [
            EvidenceItem(
                kind="supporting",
                path="src/auth.py",
                sha="abc123",
                line=42,
                snippet="code",
                trigger="trigger",
                violated_contract="contract",
            )
        ]

        # No method-level → uses instance expected_head_sha → shortcut fires
        capsule = await verifier.verify_candidate(finding, evidence, "diff")

        assert capsule.final_verdict == EvidenceVerdict.CONFIRMED
        assert len(prover.calls) == 0

    async def test_batch_forwards_expected_sha(self):
        """verify_batch forwards expected_head_sha to verify_candidate."""
        prover = FakeLLM(raise_exc=AssertionError("should not be called"))
        refuter = FakeLLM(raise_exc=AssertionError("should not be called"))
        arbiter = FakeLLM(raise_exc=AssertionError("should not be called"))

        verifier = EvidenceVerifier(prover, refuter, arbiter)
        finding = _make_finding(id="f1", file="src/auth.py")
        evidence_map = {
            "f1": [
                EvidenceItem(
                    kind="supporting",
                    path="src/auth.py",
                    sha="abc123",
                    line=42,
                    snippet="code",
                    trigger="trigger",
                    violated_contract="contract",
                )
            ]
        }

        capsules = await verifier.verify_batch([finding], evidence_map, "diff", expected_head_sha="abc123")

        assert len(capsules) == 1
        assert capsules[0].final_verdict == EvidenceVerdict.CONFIRMED
        assert len(prover.calls) == 0


# ---------------------------------------------------------------------------
# has_failure property
# ---------------------------------------------------------------------------


class TestHasFailure:
    def test_no_metadata_no_failure(self):
        """Empty retry_metadata → has_failure is False."""
        capsule = EvidenceCapsule(finding_id="f1")
        assert capsule.has_failure is False

    def test_prover_failed_true(self):
        """prover_failed=True → has_failure is True."""
        capsule = EvidenceCapsule(
            finding_id="f1",
            retry_metadata={"prover_failed": True},
        )
        assert capsule.has_failure is True

    def test_arbiter_failed_true(self):
        """arbiter_failed=True → has_failure is True."""
        capsule = EvidenceCapsule(
            finding_id="f1",
            retry_metadata={"arbiter_failed": True},
        )
        assert capsule.has_failure is True

    def test_unexpected_batch_error_is_a_failure(self):
        capsule = EvidenceCapsule(
            finding_id="f1",
            retry_metadata={"unexpected_error": "provider crashed"},
        )

        assert capsule.has_failure is True
        assert capsule.final_verdict == EvidenceVerdict.ABSTAIN

    def test_non_failed_metadata_ignored(self):
        """Metadata without _failed keys → has_failure is False."""
        capsule = EvidenceCapsule(
            finding_id="f1",
            retry_metadata={"prover_reason": "invalid_output"},
        )
        assert capsule.has_failure is False

    def test_failed_false_ignored(self):
        """_failed=False → has_failure is False."""
        capsule = EvidenceCapsule(
            finding_id="f1",
            retry_metadata={"prover_failed": False},
        )
        assert capsule.has_failure is False

    def test_failure_forces_abstain_in_final_verdict(self):
        """final_verdict returns ABSTAIN when has_failure is True."""
        capsule = EvidenceCapsule(
            finding_id="f1",
            retry_metadata={"prover_failed": True},
        )
        capsule.prover = ProverVerdict(EvidenceVerdict.CONFIRMED, 0.9, "yes")
        capsule.refuter = RefuterVerdict(EvidenceVerdict.CONFIRMED, 0.85, "yes")
        capsule.arbiter = SimpleNamespace(
            verdict=EvidenceVerdict.REJECTED,
            confidence=0.7,
            rationale="arbiter",
        )
        # Despite prover+refuter CONFIRMED and arbiter REJECTED,
        # failure forces ABSTAIN
        assert capsule.final_verdict == EvidenceVerdict.ABSTAIN
