"""Stable data contracts for the independent v4.2 review kernel.

Strings used as IDs are controller-assigned, not hashes supplied by a model.
Evidence and case revisions are bound to a Run by the store.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Run:
    id: str
    repo: str
    base: str
    head: str
    environment: str = ""
    model: str = ""
    policy: str = "v42"
    health: str = "running"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ReviewScope:
    id: str
    run_id: str
    title: str
    regions: list[dict[str, Any]] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    status: str = "pending"
    checked_regions: list[dict[str, Any]] = field(default_factory=list)
    case_ids: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    revision: int = 0
    working_state: dict[str, Any] = field(default_factory=dict)


@dataclass
class Question:
    id: str
    run_id: str
    statement: str
    scope_id: str = ""
    case_id: str = ""
    status: str = "open"
    answer: str = ""
    answer_status: str = "supported"
    interpretations: list[dict[str, Any]] = field(default_factory=list)
    change_reason: str = ""
    evidence_refs: list[str] = field(default_factory=list)
    owner: str = ""
    revision: int = 0


@dataclass
class NavigationItem:
    """Untrusted investigation navigation; never evidence or a case verdict."""

    id: str
    run_id: str
    scope_id: str
    kind: str
    text: str
    evidence_refs: list[str] = field(default_factory=list)
    status: str = "open"
    revision: int = 0
    reason: str = ""


@dataclass
class Case:
    id: str
    run_id: str
    scope_id: str = ""
    trigger: str = ""
    mechanism: str = ""
    # Each claim has its own stable id, statement, impact and eventual verdict.
    claims: list[dict[str, Any]] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)
    counter_evidence_refs: list[str] = field(default_factory=list)
    question_ids: list[str] = field(default_factory=list)
    status: str = "candidate"
    revision: int = 0


@dataclass(frozen=True)
class EvidenceRef:
    id: str
    run_id: str
    repo: str
    revision: str
    side: str
    kind: str
    content_hash: str
    path: str = ""
    start_line: int | None = None
    end_line: int | None = None
    environment: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class Task:
    id: str
    run_id: str
    kind: str
    object_id: str
    input_revision: int
    status: str = "pending"
    owner: str = ""
    instruction: str = ""
    lease_token: str = ""
    lease_expires: float = 0.0
    result: dict[str, Any] = field(default_factory=dict)
    revision: int = 0


class RevisionConflict(ValueError):  # noqa: N818
    """An expected revision no longer matches current state."""


class BindingError(ValueError):
    """A run, snapshot, or evidence binding is invalid."""


class LeaseError(ValueError):
    """A lease is expired or no longer owned by this worker."""
