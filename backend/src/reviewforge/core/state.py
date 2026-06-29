"""State Store — schema-validated shared state with deep-copy isolation.

All agent-visible state lives here. Agents get deep copies on read,
so concurrent agents cannot corrupt each other's view.
Every write is validated against a Pydantic schema.
"""

from __future__ import annotations

import copy
import uuid
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field


class FindingSchema(BaseModel):
    """Validation schema for findings."""

    id: str = Field(default_factory=lambda: f"finding_{uuid.uuid4().hex[:8]}")
    file: str = Field(..., min_length=1)
    line: int = Field(..., ge=0)
    severity: str = Field(default="info", pattern="^(info|warning|error)$")
    category: str = Field(default="", max_length=50)
    message: str = Field(..., min_length=1, max_length=1000)
    suggestion: str = Field(default="", max_length=2000)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    reviewer: str = Field(default="")
    status: str = Field(default="candidate", pattern="^(candidate|confirmed|false_positive|reported)$")
    verified_by: str = Field(default="")
    verify_reason: str = Field(default="", max_length=500)


class TaskSchema(BaseModel):
    """Validation schema for review tasks."""

    id: str = Field(default_factory=lambda: f"task_{uuid.uuid4().hex[:8]}")
    reviewer: str = Field(..., min_length=1)
    files: list[str] = Field(default_factory=list)
    rationale: str = Field(default="", max_length=500)
    status: str = Field(default="pending", pattern="^(pending|claimed|completed|failed)$")
    error: str = Field(default="", max_length=500)


class NoteSchema(BaseModel):
    """Validation schema for agent-to-planner notes."""

    id: str = Field(default_factory=lambda: f"note_{uuid.uuid4().hex[:8]}")
    from_agent: str = Field(..., min_length=1)
    type: str = Field(..., min_length=1, max_length=50)
    content: str = Field(..., min_length=1, max_length=2000)
    metadata: dict[str, Any] = Field(default_factory=dict)


@dataclass
class Finding:
    """A single review finding (validated on creation)."""

    id: str = field(default_factory=lambda: f"finding_{uuid.uuid4().hex[:8]}")
    file: str = ""
    line: int = 0
    severity: str = "info"
    category: str = ""
    message: str = ""
    suggestion: str = ""
    confidence: float = 0.5
    reviewer: str = ""
    status: str = "candidate"
    verified_by: str = ""
    verify_reason: str = ""

    def __post_init__(self) -> None:
        FindingSchema(
            id=self.id, file=self.file, line=self.line,
            severity=self.severity, category=self.category,
            message=self.message, suggestion=self.suggestion,
            confidence=self.confidence, reviewer=self.reviewer,
            status=self.status, verified_by=self.verified_by,
            verify_reason=self.verify_reason,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "file": self.file, "line": self.line,
            "severity": self.severity, "category": self.category,
            "message": self.message, "suggestion": self.suggestion,
            "confidence": self.confidence, "reviewer": self.reviewer,
            "status": self.status, "verified_by": self.verified_by,
            "verify_reason": self.verify_reason,
        }


@dataclass
class ReviewTask:
    """A task in the scheduler (validated on creation)."""

    id: str = field(default_factory=lambda: f"task_{uuid.uuid4().hex[:8]}")
    reviewer: str = ""
    files: list[str] = field(default_factory=list)
    rationale: str = ""
    status: str = "pending"
    error: str = ""

    def __post_init__(self) -> None:
        TaskSchema(
            id=self.id, reviewer=self.reviewer, files=self.files,
            rationale=self.rationale, status=self.status, error=self.error,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "reviewer": self.reviewer, "files": self.files,
            "rationale": self.rationale, "status": self.status, "error": self.error,
        }


@dataclass
class Note:
    """Agent-to-Planner feedback channel (validated on creation)."""

    id: str = field(default_factory=lambda: f"note_{uuid.uuid4().hex[:8]}")
    from_agent: str = ""
    type: str = ""
    content: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        NoteSchema(
            id=self.id, from_agent=self.from_agent,
            type=self.type, content=self.content, metadata=self.metadata,
        )


@dataclass
class StateStore:
    """In-memory state store with deep-copy read isolation and schema validation.

    This is the Lattice: single source of truth for all review state.
    Every write is validated against a Pydantic schema.
    """

    # PR context (set once at review start)
    pr_number: int = 0
    repo: str = ""
    head_sha: str = ""
    base_sha: str = ""
    files_changed: list[str] = field(default_factory=list)
    diff_summary: str = ""

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
        f = self.findings[finding_id]
        if "id" in kwargs and kwargs["id"] != f.id:
            raise ValueError("不允许修改 finding.id")
        unknown = [k for k in kwargs if not hasattr(f, k)]
        if unknown:
            raise ValueError(f"未知 finding 字段: {unknown}")
        candidate = {**f.to_dict(), **kwargs}
        FindingSchema(**candidate)
        for k, v in kwargs.items():
            setattr(f, k, v)

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
        t = self.tasks[task_id]
        if "id" in kwargs and kwargs["id"] != t.id:
            raise ValueError("不允许修改 task.id")
        unknown = [k for k in kwargs if not hasattr(t, k)]
        if unknown:
            raise ValueError(f"未知 task 字段: {unknown}")
        candidate = {**t.to_dict(), **kwargs}
        TaskSchema(**candidate)
        for k, v in kwargs.items():
            setattr(t, k, v)

    def add_note(self, note: Note) -> None:
        self.notes.append(note)

    def consume_notes(self) -> list[Note]:
        notes = copy.deepcopy(self.notes)
        self.notes.clear()
        return notes

    def snapshot(self) -> dict[str, Any]:
        return {
            "pr_number": self.pr_number,
            "repo": self.repo,
            "head_sha": self.head_sha,
            "files_changed": self.files_changed,
            "findings": {k: v.to_dict() for k, v in self.findings.items()},
            "tasks": {k: v.to_dict() for k, v in self.tasks.items()},
            "notes_count": len(self.notes),
        }
