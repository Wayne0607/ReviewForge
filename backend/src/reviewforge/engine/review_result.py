"""Three-valued result protocol for reviewer output.

An empty legacy ``findings`` list is ambiguous: it can mean a clean review,
an incomplete review, or a failed parser.  This module preserves that
ambiguity as ``UNKNOWN``.  A reviewer may report ``NO_ISSUE`` only through the
explicit envelope and only when it supplies evidence.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from reviewforge.core.state import Finding


class ReviewResultError(ValueError):
    """Base class for review-result protocol errors."""


class ReviewResultValidationError(ReviewResultError):
    """Raised when a result violates the three-valued protocol."""


class ReviewResultParseError(ReviewResultError):
    """Raised when an external payload cannot be parsed as a review result."""


class ReviewConclusion(StrEnum):
    """The only valid conclusions of a review attempt."""

    # Wire values intentionally match the existing CoverageStatus vocabulary
    # and the lowercase JSON conventions used by reviewer prompts.
    FINDING = "finding"
    NO_ISSUE = "no_issue"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ReviewResult:
    """A validated review result.

    ``FINDING`` proves that one or more findings exist. ``NO_ISSUE`` proves
    only an explicitly evidenced clean review. ``UNKNOWN`` is the fail-safe
    state for incomplete, failed, or ambiguous attempts.
    """

    outcome: ReviewConclusion
    findings: tuple[Finding, ...] = ()
    evidence: tuple[str, ...] = ()
    reason: str | None = None
    retryable: bool | None = None
    failure_kind: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, ReviewConclusion):
            raise ReviewResultValidationError("outcome must be a ReviewConclusion")

        findings = _validate_finding_instances(self.findings)
        evidence = _validate_evidence(self.evidence)
        reason = _validate_optional_text("reason", self.reason)
        failure_kind = _validate_optional_text("failure_kind", self.failure_kind)
        if self.retryable is not None and not isinstance(self.retryable, bool):
            raise ReviewResultValidationError("retryable must be a bool or None")

        object.__setattr__(self, "findings", findings)
        object.__setattr__(self, "evidence", evidence)
        object.__setattr__(self, "reason", reason)
        object.__setattr__(self, "failure_kind", failure_kind)

        if self.outcome is ReviewConclusion.FINDING:
            if not findings:
                raise ReviewResultValidationError("FINDING requires at least one finding")
            self._reject_failure_metadata()
        elif self.outcome is ReviewConclusion.NO_ISSUE:
            if findings:
                raise ReviewResultValidationError("NO_ISSUE cannot contain findings")
            if not evidence:
                raise ReviewResultValidationError("NO_ISSUE requires non-empty evidence")
            self._reject_failure_metadata()
        elif findings:
            raise ReviewResultValidationError("UNKNOWN cannot contain findings")

    def _reject_failure_metadata(self) -> None:
        if self.reason is not None or self.retryable is not None or self.failure_kind is not None:
            raise ReviewResultValidationError("failure metadata is valid only for UNKNOWN")

    @classmethod
    def finding(
        cls,
        findings: Iterable[Finding],
        *,
        evidence: Iterable[str] = (),
    ) -> ReviewResult:
        """Construct an explicitly positive review result."""

        return cls(
            outcome=ReviewConclusion.FINDING,
            findings=tuple(findings),
            evidence=tuple(evidence),
        )

    @classmethod
    def no_issue(cls, *, evidence: Iterable[str]) -> ReviewResult:
        """Construct an explicitly evidenced clean review result."""

        return cls(outcome=ReviewConclusion.NO_ISSUE, evidence=tuple(evidence))

    @classmethod
    def unknown(
        cls,
        *,
        evidence: Iterable[str] = (),
        reason: str | None = None,
        retryable: bool | None = None,
        failure_kind: str | None = None,
    ) -> ReviewResult:
        """Construct an incomplete, failed, or otherwise ambiguous result."""

        return cls(
            outcome=ReviewConclusion.UNKNOWN,
            evidence=tuple(evidence),
            reason=reason,
            retryable=retryable,
            failure_kind=failure_kind,
        )

    @classmethod
    def from_payload(cls, payload: object) -> ReviewResult:
        """Parse a new result envelope or a legacy findings payload.

        New envelopes contain ``outcome``. Legacy objects contain only a
        ``findings`` collection, and legacy top-level finding lists are also
        accepted. Legacy empty collections are always ``UNKNOWN``.
        """

        return parse_review_result(payload)

    def to_dict(self) -> dict[str, Any]:
        """Return the deterministic wire envelope."""

        envelope: dict[str, Any] = {
            "outcome": self.outcome.value,
            "evidence": list(self.evidence),
            "findings": [finding.to_dict() for finding in self.findings],
        }
        if self.reason is not None:
            envelope["reason"] = self.reason
        if self.retryable is not None:
            envelope["retryable"] = self.retryable
        if self.failure_kind is not None:
            envelope["failure_kind"] = self.failure_kind
        return envelope

    def to_json(self) -> str:
        """Serialize the envelope with stable key ordering and separators."""

        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def parse_review_result(payload: object) -> ReviewResult:
    """Strictly parse JSON text, a wire mapping, or a legacy finding list."""

    value = _decode_payload(payload)
    if isinstance(value, list):
        findings = _parse_findings(value)
        return ReviewResult.finding(findings) if findings else ReviewResult.unknown()
    if not isinstance(value, Mapping):
        raise ReviewResultParseError("review result must be an object or a legacy findings list")

    if "outcome" not in value:
        if "findings" not in value:
            raise ReviewResultParseError("legacy review result must contain findings")
        findings = _parse_findings_field(value["findings"])
        return ReviewResult.finding(findings) if findings else ReviewResult.unknown()

    allowed_fields = {"outcome", "findings", "evidence", "reason", "retryable", "failure_kind"}
    unsupported_fields = set(value) - allowed_fields
    if unsupported_fields:
        names = ", ".join(sorted(repr(field) for field in unsupported_fields))
        raise ReviewResultParseError(f"unsupported review result field(s): {names}")

    outcome = _parse_outcome(value["outcome"])
    findings = _parse_findings_field(value.get("findings", []))
    evidence = _parse_evidence_field(value.get("evidence", []))
    reason = _parse_optional_text_field(value, "reason")
    retryable = value.get("retryable")
    if retryable is not None and not isinstance(retryable, bool):
        raise ReviewResultParseError("retryable must be a bool or null")
    failure_kind = _parse_optional_text_field(value, "failure_kind")

    try:
        return ReviewResult(
            outcome=outcome,
            findings=findings,
            evidence=evidence,
            reason=reason,
            retryable=retryable,
            failure_kind=failure_kind,
        )
    except ReviewResultValidationError as exc:
        raise ReviewResultParseError(str(exc)) from exc


def _decode_payload(payload: object) -> object:
    if isinstance(payload, (str, bytes, bytearray)):
        try:
            return json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ReviewResultParseError("review result is not valid JSON") from exc
    return payload


def _parse_outcome(value: object) -> ReviewConclusion:
    if not isinstance(value, str):
        raise ReviewResultParseError("outcome must be a string")
    try:
        return ReviewConclusion(value)
    except ValueError as exc:
        allowed = ", ".join(item.value for item in ReviewConclusion)
        raise ReviewResultParseError(f"outcome must be one of: {allowed}") from exc


def _parse_findings_field(value: object) -> tuple[Finding, ...]:
    if not isinstance(value, list):
        raise ReviewResultParseError("findings must be a list")
    return _parse_findings(value)


def _parse_findings(values: list[object]) -> tuple[Finding, ...]:
    parsed: list[Finding] = []
    for index, item in enumerate(values):
        if isinstance(item, Finding):
            parsed.append(item)
            continue
        if not isinstance(item, Mapping):
            raise ReviewResultParseError(f"findings[{index}] must be an object")
        try:
            parsed.append(Finding(**dict(item)))
        except (TypeError, ValueError) as exc:
            raise ReviewResultParseError(f"findings[{index}] is invalid: {exc}") from exc
    return tuple(parsed)


def _parse_evidence_field(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ReviewResultParseError("evidence must be a list of non-empty strings")
    try:
        return _validate_evidence(value)
    except ReviewResultValidationError as exc:
        raise ReviewResultParseError(str(exc)) from exc


def _parse_optional_text_field(value: Mapping[object, object], key: str) -> str | None:
    raw = value.get(key)
    try:
        return _validate_optional_text(key, raw)
    except ReviewResultValidationError as exc:
        raise ReviewResultParseError(str(exc)) from exc


def _validate_finding_instances(values: Iterable[Finding]) -> tuple[Finding, ...]:
    try:
        normalized = tuple(values)
    except TypeError as exc:
        raise ReviewResultValidationError("findings must be an iterable of Finding objects") from exc
    for index, item in enumerate(normalized):
        if not isinstance(item, Finding):
            raise ReviewResultValidationError(f"findings[{index}] must be a Finding")
    return normalized


def _validate_evidence(values: Iterable[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes, bytearray)):
        raise ReviewResultValidationError("evidence must be an iterable of non-empty strings")
    try:
        normalized = tuple(values)
    except TypeError as exc:
        raise ReviewResultValidationError("evidence must be an iterable of non-empty strings") from exc
    for index, item in enumerate(normalized):
        if not isinstance(item, str) or not item.strip():
            raise ReviewResultValidationError(f"evidence[{index}] must be a non-empty string")
    return normalized


def _validate_optional_text(name: str, value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ReviewResultValidationError(f"{name} must be a non-empty string or None")
    return value
