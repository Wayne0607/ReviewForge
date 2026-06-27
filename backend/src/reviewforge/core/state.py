"""State Store — Lattice-inspired shared state with deep-copy isolation.

All agent-visible state lives here. Agents get deep copies on read,
so concurrent agents cannot corrupt each other's view.
"""

from __future__ import annotations

import copy
import uuid
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Finding:
    """A single review finding."""

    id: str = field(default_factory=lambda: f"finding_{uuid.uuid4().hex[:8]}")
    file: str = ""
    line: int = 0
    severity: str = "info"  # info / warning / error
    category: str = ""
    message: str = ""
    suggestion: str = ""
    confidence: float = 0.0
    reviewer: str = ""
    status: str = "candidate"  # candidate / confirmed / false_positive
    verified_by: str = ""
    verify_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "file": self.file,
            "line": self.line,
            "severity": self.severity,
            "category": self.category,
            "message": self.message,
            "suggestion": self.suggestion,
            "confidence": self.confidence,
            "reviewer": self.reviewer,
            "status": self.status,
            "verified_by": self.verified_by,
            "verify_reason": self.verify_reason,
        }


@dataclass
class ReviewTask:
    """A task in the scheduler."""

    id: str = field(default_factory=lambda: f"task_{uuid.uuid4().hex[:8]}")
    reviewer: str = ""
    files: list[str] = field(default_factory=list)
    rationale: str = ""
    status: str = "pending"  # pending / claimed / completed / failed
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "reviewer": self.reviewer,
            "files": self.files,
            "rationale": self.rationale,
            "status": self.status,
            "error": self.error,
        }


@dataclass
class Note:
    """Agent-to-Planner feedback channel. Consumed then deleted."""

    id: str = field(default_factory=lambda: f"note_{uuid.uuid4().hex[:8]}")
    from_agent: str = ""
    type: str = ""  # needs_more_context / false_positive_suspected / ...
    content: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class StateStore:
    """In-memory state store with deep-copy read isolation.

    This is the Lattice: single source of truth for all review state.
    """

    # PR context (set once at review start)
    pr_number: int = 0
    repo: str = ""
    head_sha: str = ""
    base_sha: str = ""
    files_changed: list[str] = field(default_factory=list)
    diff_summary: str = ""  # compact diff for Planner

    # Runtime state
    findings: dict[str, Finding] = field(default_factory=dict)
    tasks: dict[str, ReviewTask] = field(default_factory=dict)
    notes: list[Note] = field(default_factory=list)

    def add_finding(self, finding: Finding) -> str:
        self.findings[finding.id] = finding
        return finding.id

    def get_finding(self, finding_id: str) -> Finding:
        return copy.deepcopy(self.findings[finding_id])

    def list_findings(self, status: str | None = None) -> list[Finding]:
        if status:
            return copy.deepcopy([f for f in self.findings.values() if f.status == status])
        return copy.deepcopy(list(self.findings.values()))

    def update_finding(self, finding_id: str, **kwargs: Any) -> None:
        for k, v in kwargs.items():
            if hasattr(self.findings[finding_id], k):
                setattr(self.findings[finding_id], k, v)

    def add_task(self, task: ReviewTask) -> str:
        self.tasks[task.id] = task
        return task.id

    def get_task(self, task_id: str) -> ReviewTask:
        return copy.deepcopy(self.tasks[task_id])

    def list_tasks(self, status: str | None = None) -> list[ReviewTask]:
        if status:
            return copy.deepcopy([t for t in self.tasks.values() if t.status == status])
        return copy.deepcopy(list(self.tasks.values()))

    def update_task(self, task_id: str, **kwargs: Any) -> None:
        for k, v in kwargs.items():
            if hasattr(self.tasks[task_id], k):
                setattr(self.tasks[task_id], k, v)

    def add_note(self, note: Note) -> None:
        self.notes.append(note)

    def consume_notes(self) -> list[Note]:
        """Read and clear all notes (message-queue semantics)."""
        notes = copy.deepcopy(self.notes)
        self.notes.clear()
        return notes

    def snapshot(self) -> dict[str, Any]:
        """Create a serializable snapshot of the full state."""
        return {
            "pr_number": self.pr_number,
            "repo": self.repo,
            "head_sha": self.head_sha,
            "files_changed": self.files_changed,
            "findings": {k: v.to_dict() for k, v in self.findings.items()},
            "tasks": {k: v.to_dict() for k, v in self.tasks.items()},
            "notes_count": len(self.notes),
        }
