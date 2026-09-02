"""Run-scoped hypothesis ledger for the hypothesis pipeline."""

from __future__ import annotations

import copy
import threading
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


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

    def __post_init__(self) -> None:
        if self.status not in {"success", "not_found", "error"}:
            raise ValueError(f"unsupported observation status: {self.status!r}")
        if len(self.excerpt) > 1_200:
            raise ValueError("observation excerpt exceeds 1200 characters")


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

    def __post_init__(self) -> None:
        if self.severity not in _SEVERITY_RANK:
            raise ValueError(f"unsupported hypothesis severity: {self.severity!r}")
        if self.evidence_strength not in {"none", "weak", "strong"}:
            raise ValueError(f"unsupported evidence strength: {self.evidence_strength!r}")
        if not self.identity or not self.sites:
            raise ValueError("hypothesis requires identity and at least one site")

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["mechanism"] = self.mechanism.value
        data["status"] = self.status.value
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Hypothesis:
        observations = []
        for raw in data.get("observations", []):
            item = dict(raw)
            if item.get("line_range") is not None:
                item["line_range"] = tuple(item["line_range"])
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
    _lock: threading.RLock = field(default_factory=threading.RLock, init=False, repr=False, compare=False)

    def upsert(self, hypothesis: Hypothesis) -> tuple[Hypothesis, bool]:
        if not hypothesis.identity or not hypothesis.sites:
            raise ValueError("hypothesis requires identity and at least one site")
        with self._lock:
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
        )
