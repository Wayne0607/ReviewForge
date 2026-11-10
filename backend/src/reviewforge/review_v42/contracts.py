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
class FrontierEntry:
    """A task's retained original source; this is not itself an evidence reference."""

    id: str
    run_id: str
    task_id: str
    scope_id: str
    repo: str
    source_revision: str
    side: str
    path: str
    start_line: int | None
    end_line: int | None
    kind: str
    environment: str
    content_hash: str
    aliases: list[str] = field(default_factory=list)
    status: str = "active"
    item_revision: int = 0
    release_reason: str = ""
    is_page: bool = False


@dataclass(frozen=True)
class UnitPlan:
    run_id: str
    manifest_hash: str
    workspace_digest: str
    base_workspace_digest: str
    primary_scope_ids: list[str]
    integration_scope_id: str
    revision: int = 0


@dataclass(frozen=True)
class IntegrationReview:
    id: str
    run_id: str
    scope_id: str
    revision: int = 0


@dataclass
class IntegrationObligation:
    id: str
    run_id: str
    integration_scope_id: str
    kind: str
    unit_ids: list[str]
    description: str
    evidence_refs: list[str] = field(default_factory=list)
    disposition: str = "open"
    reason: str = ""
    receipt_digest: str = ""
    revision: int = 0
    task_id: str = ""
    turn_id: str = ""
    origin_evidence_refs: list[str] = field(default_factory=list)
    origin_task_id: str = ""
    origin_turn_id: str = ""


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


CLAIM_CYCLE_POLICY = "claim-delivery-2"
VERIFICATION_DELIVERY_POLICIES = frozenset({"claim-delivery-1", CLAIM_CYCLE_POLICY})


class VerificationIncomplete(Exception):  # noqa: N818
    """Some claims have no validated verdict. Their gaps remain on the board."""


class RevisionConflict(ValueError):  # noqa: N818
    """An expected revision no longer matches current state."""


class DraftRetractionConflict(RevisionConflict):  # noqa: N818
    """Current draft state from the transaction that refused the withdrawal."""

    def __init__(self, draft_id: str, draft_revision: int, draft_status: str):
        super().__init__("candidate draft revision or status changed")
        self.draft_id = draft_id
        self.draft_revision = draft_revision
        self.draft_status = draft_status


class BindingError(ValueError):
    """A run, snapshot, or evidence binding is invalid."""


class LeaseError(ValueError):
    """A lease is expired or no longer owned by this worker."""
