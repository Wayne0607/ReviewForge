"""Run-scoped hypothesis ledger for the hypothesis pipeline."""

from __future__ import annotations

import copy
import hashlib
import threading
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any

from reviewforge.core.change_manifest import FileChange, is_verified_rename


class Mechanism(StrEnum):
    WRONG_ARGUMENT = "wrong-argument"
    WRONG_OPERATOR = "wrong-operator"
    NULL_PATH = "null-path"
    CONTRACT_MISMATCH = "contract-mismatch"
    MISSING_AWAIT = "missing-await"
    LOCK_SCOPE = "lock-scope"
    STATE_LEAK = "state-leak"
    ERROR_PATH = "error-path"
    REGRESSION_REMOVED = "regression-removed"
    SECURITY_SINK = "security-sink"
    I18N = "i18n"
    A11Y = "a11y"
    PERF = "perf"
    TEST_GAP = "test-gap"
    DOC = "doc"


class HypothesisStatus(StrEnum):
    OPEN = "open"
    CONFIRMED = "confirmed"
    REFUTED = "refuted"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Site:
    path: str
    line: int
    excerpt: str


@dataclass(frozen=True)
class Observation:
    id: str
    tool: str
    query: str
    path: str
    line_range: tuple[int, int] | None
    sha: str
    result_digest: str
    excerpt: str
    status: str
    # Actual saved read_file source; line_range remains the requested window.
    # Missing in old checkpoints and in multi-location search observations.
    excerpt_line_range: tuple[int, int] | None = None
    shared_result_id: str = ""

    def __post_init__(self) -> None:
        if self.status not in {"success", "not_found", "error"}:
            raise ValueError(f"unsupported observation status: {self.status!r}")
        if len(self.excerpt) > 1_200:
            raise ValueError("observation excerpt exceeds 1200 characters")


@dataclass(frozen=True)
class EvidenceCitation:
    observation_id: str
    quote: str


@dataclass(frozen=True)
class ContractAssessment:
    """Keep the contract premise separate from the observed behavior.

    This is a model assessment, not a code-proved semantic conclusion. The
    investigator validates each citation against its own saved observations.
    """

    expected: str
    actual: str
    comparison: str
    expected_evidence: list[EvidenceCitation]
    actual_evidence: list[EvidenceCitation]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ContractAssessment:
        if not isinstance(raw, dict):
            raise ValueError("assessment must be an object")
        fields = {}
        for key in ("expected", "actual", "comparison"):
            value = raw.get(key)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"assessment requires {key}")
            fields[key] = value.strip()
        if fields["comparison"] not in {"conflict", "compatible", "unresolved"}:
            raise ValueError("unsupported assessment comparison")
        for key in ("expected_evidence", "actual_evidence"):
            values = raw.get(key)
            if not isinstance(values, list) or not values:
                raise ValueError(f"assessment requires {key}")
            citations = []
            for value in values:
                if not isinstance(value, dict):
                    raise ValueError("assessment citation must be an object")
                identity, quote = value.get("observation_id"), value.get("quote")
                if (
                    not isinstance(identity, str)
                    or not identity.strip()
                    or not isinstance(quote, str)
                    or not quote.strip()
                ):
                    raise ValueError("assessment citation requires an observation and quote")
                # Quotes are source, including its whitespace. Keep them stable
                # across persistence; normalization can change a fact digest.
                citations.append(EvidenceCitation(identity.strip(), quote))
            fields[key] = citations
        return cls(**fields)


@dataclass(frozen=True)
class CounterEvidence:
    """A cited contradiction of one necessary defect premise, not a preference."""

    premise: str
    statement: str
    evidence: list[EvidenceCitation]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> CounterEvidence:
        if not isinstance(raw, dict) or raw.get("premise") not in {"trigger", "mechanism", "impact", "regression"}:
            raise ValueError("counterevidence requires a necessary premise")
        statement = raw.get("statement")
        if not isinstance(statement, str) or not statement.strip():
            raise ValueError("counterevidence requires a statement")
        # Reuse exactly the existing citation parser; this adds a proof shape,
        # not a weaker source/quote validation rule.
        evidence = ContractAssessment.from_dict(
            {
                "expected": statement,
                "actual": statement,
                "comparison": "compatible",
                "expected_evidence": raw.get("evidence"),
                "actual_evidence": raw.get("evidence"),
            }
        ).actual_evidence
        return cls(raw["premise"], statement.strip(), evidence)


@dataclass
class Hypothesis:
    id: str
    identity: str
    unit_id: str
    mechanism: Mechanism
    claim: str
    trigger: str
    impact: str
    open_question: str
    refutation: str
    sites: list[Site]
    severity: str
    source: str
    status: HypothesisStatus = HypothesisStatus.OPEN
    evidence_strength: str = "none"
    observations: list[Observation] = field(default_factory=list)
    verdict_reason: str = ""
    attempts: int = 0
    investigation_steps: int = 0
    investigation_tokens: int = 0
    retryable: bool = False
    assessment: ContractAssessment | None = None
    counterevidence: CounterEvidence | None = None
    investigation_outcome: str = ""
    investigation_task_id: str = ""
    coverage_identity: str = ""
    investigation_progress: list[dict[str, Any]] = field(default_factory=list)
    investigation_unreported_tokens: int = 0
    discovery_basis: dict[str, Any] = field(default_factory=dict)
    change_anchor: FileChange | None = None

    def __post_init__(self) -> None:
        if self.severity not in _SEVERITY_RANK:
            raise ValueError(f"unsupported hypothesis severity: {self.severity!r}")
        if self.evidence_strength not in {"none", "weak", "strong"}:
            raise ValueError(f"unsupported evidence strength: {self.evidence_strength!r}")
        if self.change_anchor is not None and (
            not isinstance(self.change_anchor, FileChange)
            or not is_verified_rename(self.change_anchor, self.change_anchor.head_sha)
        ):
            raise ValueError("structural hypothesis requires a verified rename anchor")
        if not self.identity or not (self.sites or self.change_anchor):
            raise ValueError("hypothesis requires identity and a source site or verified change anchor")

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["mechanism"] = self.mechanism.value
        data["status"] = self.status.value
        # Empty v2 metadata must not inflate the v1 model prompt/budget or
        # change the shape of old candidate snapshots.
        for name in ("counterevidence", "investigation_outcome", "investigation_task_id", "coverage_identity"):
            if data[name] is None or data[name] == "":
                data.pop(name)
        if not data["investigation_progress"]:
            data.pop("investigation_progress")
        if not data["discovery_basis"]:
            data.pop("discovery_basis")
        if data["change_anchor"] is None:
            data.pop("change_anchor")
        if not data["investigation_unreported_tokens"]:
            data.pop("investigation_unreported_tokens")
        for observation in data["observations"]:
            if not observation["shared_result_id"]:
                observation.pop("shared_result_id")
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Hypothesis:
        observations = []
        for raw in data.get("observations", []):
            item = dict(raw)
            for key in ("line_range", "excerpt_line_range"):
                if item.get(key) is not None:
                    item[key] = tuple(item[key])
            observations.append(Observation(**item))
        return cls(
            id=str(data["id"]),
            identity=str(data["identity"]),
            unit_id=str(data["unit_id"]),
            mechanism=Mechanism(data["mechanism"]),
            claim=str(data["claim"]),
            trigger=str(data["trigger"]),
            impact=str(data["impact"]),
            open_question=str(data["open_question"]),
            refutation=str(data["refutation"]),
            sites=[Site(**site) for site in data.get("sites", [])],
            severity=str(data["severity"]),
            source=str(data["source"]),
            status=HypothesisStatus(data.get("status", "open")),
            evidence_strength=str(data.get("evidence_strength", "none")),
            observations=observations,
            verdict_reason=str(data.get("verdict_reason", "")),
            attempts=int(data.get("attempts", 0)),
            investigation_steps=int(data.get("investigation_steps", 0)),
            investigation_tokens=int(data.get("investigation_tokens", 0)),
            retryable=bool(data.get("retryable", False)),
            assessment=ContractAssessment.from_dict(data["assessment"]) if data.get("assessment") is not None else None,
            counterevidence=CounterEvidence.from_dict(data["counterevidence"])
            if data.get("counterevidence") is not None
            else None,
            investigation_outcome=str(data.get("investigation_outcome", "")),
            investigation_task_id=str(data.get("investigation_task_id", "")),
            coverage_identity=str(data.get("coverage_identity", "")),
            investigation_progress=copy.deepcopy(data.get("investigation_progress", [])),
            investigation_unreported_tokens=int(data.get("investigation_unreported_tokens", 0)),
            discovery_basis=copy.deepcopy(data.get("discovery_basis", {})),
            change_anchor=FileChange.from_dict(data["change_anchor"])
            if data.get("change_anchor") is not None
            else None,
        )


_SEVERITY_RANK = {"info": 0, "warning": 1, "error": 2}


@dataclass
class HypothesisLedger:
    run_id: str
    head_sha: str
    workspace_digest: str
    items: dict[str, Hypothesis] = field(default_factory=dict)
    no_issue_units: dict[str, str] = field(default_factory=dict)
    unresolved_units: dict[str, str] = field(default_factory=dict)
    protocol_version: int = 1
    investigation_tasks: dict[str, dict[str, Any]] = field(default_factory=dict)
    discovery_blocks: dict[str, dict[str, Any]] = field(default_factory=dict)
    request_budget: dict[str, Any] = field(default_factory=dict)
    publication_reviews: dict[str, dict[str, Any]] = field(default_factory=dict)
    source_audits: dict[str, dict[str, Any]] = field(default_factory=dict)
    source_audit_batches: int = 0
    structural_checks: dict[str, Any] = field(default_factory=dict)
    evidence_records: dict[str, dict[str, Any]] = field(default_factory=dict)
    _lock: threading.RLock = field(default_factory=threading.RLock, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.protocol_version not in {1, 2, 3, 4, 5, 6, 7, 8, 9, 10}:
            raise ValueError("unsupported investigation protocol")
        if type(self.source_audit_batches) is not int or self.source_audit_batches < 0:
            raise ValueError("source audit batch count must be a nonnegative integer")
        for item in self.items.values():
            self._validate_change_anchor(item)

    def _validate_change_anchor(self, hypothesis: Hypothesis) -> None:
        if hypothesis.change_anchor is not None and (
            self.protocol_version < 3 or not is_verified_rename(hypothesis.change_anchor, self.head_sha)
        ):
            raise ValueError("change anchor does not match the ledger protocol and head")

    def upsert(self, hypothesis: Hypothesis) -> tuple[Hypothesis, bool]:
        self._validate_change_anchor(hypothesis)
        if not hypothesis.identity or not (hypothesis.sites or hypothesis.change_anchor):
            raise ValueError("hypothesis requires identity and a source site or verified change anchor")
        with self._lock:
            current = self.items.get(hypothesis.identity)
            if current is not None and current.change_anchor != hypothesis.change_anchor:
                raise ValueError("cannot merge hypotheses bound to different structural changes")
            fact_fields = ("claim", "trigger", "impact", "open_question", "refutation")
            if (
                self.protocol_version >= 2
                and current is not None
                and any(getattr(current, name) != getattr(hypothesis, name) for name in fact_fields)
            ):
                # The old coverage identity cannot also identify distinct risks
                # in the same function. Keep its first row and original binding;
                # assign a stable variant only to the newly distinct candidate.
                base = hypothesis.identity
                facts = tuple(getattr(hypothesis, name) for name in fact_fields)
                digest = hashlib.sha256(repr((base, facts)).encode()).hexdigest()[:24]
                prefix, separator, anchor = base.rpartition("::")
                hypothesis = copy.deepcopy(hypothesis)
                hypothesis.coverage_identity = base
                hypothesis.identity = f"{prefix}::risk_{digest}::{anchor}" if separator else f"{base}::risk_{digest}"
                hypothesis.id = "h_" + hashlib.sha256(hypothesis.identity.encode()).hexdigest()[:8]
                current = self.items.get(hypothesis.identity)
            if current is None:
                current = copy.deepcopy(hypothesis)
                self.items[hypothesis.identity] = current
                return copy.deepcopy(current), True
            seen = {(site.path, site.line, site.excerpt) for site in current.sites}
            for site in hypothesis.sites:
                key = (site.path, site.line, site.excerpt)
                if key not in seen:
                    current.sites.append(copy.deepcopy(site))
                    seen.add(key)
            if _SEVERITY_RANK.get(hypothesis.severity, -1) > _SEVERITY_RANK.get(current.severity, -1):
                current.severity = hypothesis.severity
            known_observations = {observation.id for observation in current.observations}
            current.observations.extend(
                copy.deepcopy(observation)
                for observation in hypothesis.observations
                if observation.id not in known_observations
            )
            current.attempts = max(current.attempts, hypothesis.attempts)
            return copy.deepcopy(current), False

    def apply_verdict(
        self,
        identity: str,
        *,
        status: HypothesisStatus | str,
        evidence_strength: str = "none",
        verdict_reason: str = "",
        observations: list[Observation] | None = None,
        severity: str | None = None,
        additional_sites: list[Site] | None = None,
        investigation_steps: int = 0,
        investigation_tokens: int = 0,
        retryable: bool = False,
        assessment: ContractAssessment | None = None,
        counterevidence: CounterEvidence | None = None,
        investigation_outcome: str = "",
        investigation_task_id: str = "",
        investigation_progress: list[dict[str, Any]] | None = None,
        investigation_unreported_tokens: int = 0,
    ) -> Hypothesis:
        """Apply an investigation verdict to one hypothesis.

        The investigator is the only writer of status transitions: OPEN becomes
        CONFIRMED / REFUTED / UNKNOWN.  Severity may be corrected explicitly,
        and additional sites discovered through tool evidence are merged without
        ever dropping a previously recorded site.
        """

        with self._lock:
            current = self.items.get(identity)
            if current is None:
                raise KeyError(f"unknown hypothesis identity: {identity!r}")
            status_value = status if isinstance(status, HypothesisStatus) else HypothesisStatus(str(status))
            current.status = status_value
            current.evidence_strength = evidence_strength if evidence_strength in {"none", "weak", "strong"} else "none"
            current.verdict_reason = str(verdict_reason)
            if severity in _SEVERITY_RANK:
                current.severity = severity
            current.attempts += 1
            current.investigation_steps = investigation_steps
            current.investigation_tokens = investigation_tokens
            current.investigation_unreported_tokens = investigation_unreported_tokens
            current.retryable = retryable
            current.assessment = copy.deepcopy(assessment)
            current.counterevidence = copy.deepcopy(counterevidence)
            current.investigation_outcome = investigation_outcome
            current.investigation_task_id = investigation_task_id
            if investigation_progress is not None:
                current.investigation_progress = copy.deepcopy(investigation_progress)
            if observations:
                known = {observation.id for observation in current.observations}
                for observation in observations:
                    if observation.id not in known:
                        current.observations.append(copy.deepcopy(observation))
                        known.add(observation.id)
            if additional_sites:
                seen = {(site.path, site.line, site.excerpt) for site in current.sites}
                for site in additional_sites:
                    key = (site.path, site.line, site.excerpt)
                    if key not in seen:
                        current.sites.append(copy.deepcopy(site))
                        seen.add(key)
            return copy.deepcopy(current)

    def apply_task_verdict(self, task_id: str, members: tuple[str, ...], charged_to: str, **verdict: Any) -> None:
        """Atomically link one actual investigation to all equal OPEN candidates."""
        with self._lock:
            if charged_to not in members or any(key not in self.items for key in members):
                raise ValueError("task members must exist and contain its actual investigator")
            if any(self.items[key].status != HypothesisStatus.OPEN for key in members):
                raise ValueError("cannot overwrite a closed candidate with a shared verdict")
            tokens = int(verdict.get("investigation_tokens", 0))
            outcome = verdict.get("investigation_outcome")
            allowed = {
                "resolved",
                "insufficient-evidence",
                "policy-deferred",
                "provider-error",
                "output-error",
                "budget-exhausted",
                "tool-error",
            }
            if outcome not in allowed or ((str(verdict["status"]) != "unknown") != (outcome == "resolved")):
                raise ValueError("task verdict requires a consistent typed outcome")
            previous = self.investigation_tasks.get(task_id, {})
            for identity in members:
                self.apply_verdict(
                    identity,
                    **{
                        **verdict,
                        "investigation_tokens": tokens if identity == charged_to else 0,
                        "investigation_unreported_tokens": verdict.get("investigation_unreported_tokens", 0)
                        if identity == charged_to
                        else 0,
                    },
                    investigation_task_id=task_id,
                )
            self.investigation_tasks[task_id] = {
                **{
                    key: previous[key]
                    for key in ("budget_steps_limit", "token_limit", "budget_policy", "request_usage_synced")
                    if key in previous
                },
                **(
                    {"total_steps": int(previous["total_steps"]) + int(verdict.get("investigation_steps", 0))}
                    if "total_steps" in previous
                    else {}
                ),
                **(
                    {
                        "total_unreported_tokens": int(previous["total_unreported_tokens"])
                        + int(verdict.get("investigation_unreported_tokens", 0))
                    }
                    if "total_unreported_tokens" in previous
                    else {}
                ),
                "members": list(members),
                "charged_to": charged_to,
                "outcome": verdict.get("investigation_outcome", ""),
                "verdict": str(verdict["status"]),
                "tokens": tokens,
                "total_tokens": int(previous.get("total_tokens", 0)) + tokens,
                **(
                    {"unreported_tokens": verdict.get("investigation_unreported_tokens", 0)}
                    if self.protocol_version >= 3
                    else {}
                ),
                "attempts": int(previous.get("attempts", 0)) + 1,
            }

    def open(self) -> list[Hypothesis]:
        with self._lock:
            return [copy.deepcopy(item) for item in self.items.values() if item.status == HypothesisStatus.OPEN]

    def to_dict(self) -> dict[str, Any]:
        with self._lock:
            return {
                "run_id": self.run_id,
                "head_sha": self.head_sha,
                "workspace_digest": self.workspace_digest,
                "items": {key: value.to_dict() for key, value in sorted(self.items.items())},
                "no_issue_units": dict(sorted(self.no_issue_units.items())),
                "unresolved_units": dict(sorted(self.unresolved_units.items())),
                "protocol_version": self.protocol_version,
                **({"evidence_records": copy.deepcopy(self.evidence_records)} if self.protocol_version >= 5 else {}),
                "investigation_tasks": copy.deepcopy(self.investigation_tasks),
                **(
                    {
                        "source_audits": copy.deepcopy(self.source_audits),
                        "source_audit_batches": self.source_audit_batches,
                    }
                    if self.protocol_version >= 4
                    else {}
                ),
                **(
                    {
                        "discovery_blocks": copy.deepcopy(self.discovery_blocks),
                        "request_budget": copy.deepcopy(self.request_budget),
                        "publication_reviews": copy.deepcopy(self.publication_reviews),
                        "structural_checks": copy.deepcopy(self.structural_checks),
                    }
                    if self.protocol_version >= 3
                    else {}
                ),
            }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> HypothesisLedger:
        return cls(
            run_id=str(data.get("run_id", "")),
            head_sha=str(data.get("head_sha", "")),
            workspace_digest=str(data.get("workspace_digest", "")),
            items={key: Hypothesis.from_dict(value) for key, value in data.get("items", {}).items()},
            no_issue_units=dict(data.get("no_issue_units", {})),
            unresolved_units=dict(data.get("unresolved_units", {})),
            protocol_version=int(data.get("protocol_version", 1)),
            investigation_tasks=copy.deepcopy(data.get("investigation_tasks", {})),
            discovery_blocks=copy.deepcopy(data.get("discovery_blocks", {})),
            request_budget=copy.deepcopy(data.get("request_budget", {})),
            publication_reviews=copy.deepcopy(data.get("publication_reviews", {})),
            source_audits=copy.deepcopy(data.get("source_audits", {})),
            # Old records do not identify shared batches. Count each persisted
            # attempt conservatively rather than give a restored run new calls.
            source_audit_batches=int(
                data.get(
                    "source_audit_batches",
                    min(4, sum(max(0, int(row.get("attempts", 0))) for row in data.get("source_audits", {}).values())),
                )
            ),
            structural_checks=copy.deepcopy(data.get("structural_checks", {})),
            evidence_records=copy.deepcopy(data.get("evidence_records", {})),
        )
