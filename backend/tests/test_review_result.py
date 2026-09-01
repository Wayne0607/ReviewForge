"""Tests for the fail-safe three-valued review result protocol."""

import json

import pytest

from reviewforge.core.state import Finding
from reviewforge.engine.review_result import (
    ReviewConclusion,
    ReviewResult,
    ReviewResultParseError,
    ReviewResultValidationError,
    parse_review_result,
)


def make_finding() -> Finding:
    return Finding(id="finding_stable", file="app.py", line=7, message="A real issue")


def test_finding_requires_at_least_one_finding():
    with pytest.raises(ReviewResultValidationError, match="at least one"):
        ReviewResult.finding([])


def test_no_issue_requires_evidence_and_forbids_findings():
    with pytest.raises(ReviewResultValidationError, match="non-empty evidence"):
        ReviewResult.no_issue(evidence=[])

    with pytest.raises(ReviewResultValidationError, match="cannot contain findings"):
        ReviewResult(
            outcome=ReviewConclusion.NO_ISSUE,
            findings=(make_finding(),),
            evidence=("Inspected the changed branch",),
        )


def test_failure_metadata_is_reserved_for_unknown():
    with pytest.raises(ReviewResultValidationError, match="only for UNKNOWN"):
        ReviewResult(
            outcome=ReviewConclusion.FINDING,
            findings=(make_finding(),),
            reason="conflicting state",
        )


def test_unknown_carries_failure_metadata_but_not_findings():
    result = ReviewResult.unknown(
        reason="provider timed out",
        retryable=True,
        failure_kind="timeout",
    )

    assert result.outcome is ReviewConclusion.UNKNOWN
    assert result.reason == "provider timed out"
    assert result.retryable is True
    assert result.failure_kind == "timeout"

    with pytest.raises(ReviewResultValidationError, match="cannot contain findings"):
        ReviewResult(outcome=ReviewConclusion.UNKNOWN, findings=(make_finding(),))


def test_legacy_non_empty_findings_becomes_finding():
    result = parse_review_result(
        {
            "findings": [
                {
                    "id": "finding_stable",
                    "file": "app.py",
                    "line": 7,
                    "message": "A real issue",
                }
            ]
        }
    )

    assert result.outcome is ReviewConclusion.FINDING
    assert result.findings == (make_finding(),)


@pytest.mark.parametrize("payload", [{"findings": []}, []])
def test_legacy_empty_findings_is_unknown_never_no_issue(payload):
    result = parse_review_result(payload)

    assert result.outcome is ReviewConclusion.UNKNOWN
    assert result.findings == ()


def test_new_no_issue_envelope_requires_explicit_evidence():
    result = parse_review_result(
        {
            "outcome": "no_issue",
            "evidence": ["Read every changed hunk", "Checked both call sites"],
            "findings": [],
        }
    )

    assert result == ReviewResult.no_issue(evidence=["Read every changed hunk", "Checked both call sites"])

    with pytest.raises(ReviewResultParseError, match="non-empty evidence"):
        parse_review_result({"outcome": "no_issue", "findings": []})


def test_new_finding_envelope_parses_existing_finding_model():
    existing = make_finding()
    result = parse_review_result(
        {
            "outcome": "finding",
            "evidence": ["app.py:7"],
            "findings": [existing],
        }
    )

    assert result.findings == (existing,)
    assert result.evidence == ("app.py:7",)


def test_new_unknown_envelope_parses_json_text():
    result = ReviewResult.from_payload(
        json.dumps(
            {
                "outcome": "unknown",
                "findings": [],
                "evidence": [],
                "reason": "malformed provider output",
                "retryable": False,
                "failure_kind": "parse_error",
            }
        )
    )

    assert result == ReviewResult.unknown(
        reason="malformed provider output",
        retryable=False,
        failure_kind="parse_error",
    )


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({}, "must contain findings"),
        ({"findings": {}}, "findings must be a list"),
        ({"findings": ["bad"]}, r"findings\[0\] must be an object"),
        ({"outcome": "CLEAN", "findings": []}, "outcome must be one of"),
        ({"outcome": "unknown", "findings": [], "retryable": "yes"}, "retryable must be"),
        ({"outcome": "unknown", "findings": [], "evidence": [""]}, r"evidence\[0\]"),
        ({"outcome": "unknown", "findings": [], "retryabl": True}, "unsupported review result field"),
    ],
)
def test_malformed_payloads_raise_explicit_parse_error(payload, message):
    with pytest.raises(ReviewResultParseError, match=message):
        parse_review_result(payload)


def test_serialization_is_deterministic_and_round_trips():
    result = ReviewResult.finding([make_finding()], evidence=["app.py:7"])

    first = result.to_json()
    second = result.to_json()

    assert first == second
    assert parse_review_result(first) == result
    assert list(result.to_dict()) == ["outcome", "evidence", "findings"]
