"""Coverage Ledger — deterministic, serializable coverage model for ReviewForge v3.

Owns the coverage matrix over semantic units × review dimensions.  Every cell
tracks lifecycle from pending through terminal resolution.  The ledger is the
single source of truth for "has every required review been completed?" and is
designed for stable JSON round-trip so it can be persisted and resumed.

Policy
------
- Every semantic unit receives a ``correctness`` cell (mandatory).
- Explicit risk signals (security-sensitive, localization resource, cross-PR
  evidence, error-handling patterns, contract surfaces, testing scope) create
  mandatory dimension cells for the affected unit.
- Low-risk optional dimensions (performance, compatibility) are bounded by a
  configurable cap, but **no fixed global cap may silently drop mandatory
  high-risk coverage**.
- ``no_issue`` closure requires explicit evidence text.
- Transitions are validated; invalid transitions raise ``ValueError``.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


# ── Enums ───────────────────────────────────────────────────────────────────


class CoverageDimension(str, Enum):
    """Review dimensions tracked by the coverage ledger."""

    CORRECTNESS = "correctness"
    CONTRACT = "contract"
    ERROR_HANDLING = "error-handling"
    SECURITY = "security"
    TESTING = "testing"
    LOCALIZATION = "localization"
    PERFORMANCE = "performance"
    COMPATIBILITY = "compatibility"
    CROSS_PR = "cross-PR"


class CoverageStatus(str, Enum):
    """Lifecycle status of a coverage cell."""

    PENDING = "pending"
    ASSIGNED = "assigned"
    COVERED = "covered"
    NO_ISSUE = "no_issue"
    ABSTAINED = "abstained"
    FAILED = "failed"


# ── Valid state transitions ─────────────────────────────────────────────────

_VALID_TRANSITIONS: dict[CoverageStatus, set[CoverageStatus]] = {
    CoverageStatus.PENDING: {CoverageStatus.ASSIGNED, CoverageStatus.FAILED},
    CoverageStatus.ASSIGNED: {
        CoverageStatus.COVERED,
        CoverageStatus.NO_ISSUE,
        CoverageStatus.ABSTAINED,
        CoverageStatus.FAILED,
    },
    CoverageStatus.COVERED: set(),  # terminal
    CoverageStatus.NO_ISSUE: set(),  # terminal
    CoverageStatus.ABSTAINED: set(),  # terminal
    CoverageStatus.FAILED: {CoverageStatus.ASSIGNED},  # retry
}

# Terminal statuses — once a cell reaches one of these it is resolved.
TERMINAL_STATUSES: frozenset[CoverageStatus] = frozenset(
    {
        CoverageStatus.COVERED,
        CoverageStatus.NO_ISSUE,
        CoverageStatus.ABSTAINED,
        CoverageStatus.FAILED,
    }
)

# Dimensions that are mandatory for every semantic unit regardless of risk.
_ALWAYS_MANDATORY: frozenset[CoverageDimension] = frozenset(
    {CoverageDimension.CORRECTNESS}
)

# Risk signals → mandatory dimensions they imply.
_RISK_SIGNAL_MAP: dict[str, CoverageDimension] = {
    "security-sensitive-symbol": CoverageDimension.SECURITY,
    "security-sensitive": CoverageDimension.SECURITY,
    "localization-resource": CoverageDimension.LOCALIZATION,
    "localization": CoverageDimension.LOCALIZATION,
    "cross-PR": CoverageDimension.CROSS_PR,
    "cross-pr": CoverageDimension.CROSS_PR,
    "error-handling": CoverageDimension.ERROR_HANDLING,
    "error_handling": CoverageDimension.ERROR_HANDLING,
    "contract-surface": CoverageDimension.CONTRACT,
    "contract": CoverageDimension.CONTRACT,
    "testing-scope": CoverageDimension.TESTING,
    "testing": CoverageDimension.TESTING,
}

# Dimensions that may be bounded by a global cap for low-risk units.
_OPTIONAL_BOUNDED: frozenset[CoverageDimension] = frozenset(
    {CoverageDimension.PERFORMANCE, CoverageDimension.COMPATIBILITY}
)

# File patterns that imply localization resource.
_LOCALE_EXTENSIONS = frozenset(
    {".properties", ".po", ".pot", ".arb", ".strings", ".resx", ".ftl"}
)
_LOCALE_MARKERS = frozenset(
    {"/i18n/", "/l10n/", "/locale/", "/locales/", "/translations/"}
)


# ── CoverageCell ────────────────────────────────────────────────────────────


@dataclass
class CoverageCell:
    """Tracks the review lifecycle for one (unit, dimension) pair."""

    unit_id: str
    path: str
    line: int
    dimension: CoverageDimension
    risk: int = 0
    mandatory: bool = False
    status: CoverageStatus = CoverageStatus.PENDING
    attempts: int = 0
    assigned_task_ids: list[str] = field(default_factory=list)
    finding_ids: list[str] = field(default_factory=list)
    terminal_reason: str = ""
    evidence: str = ""

    # ── Lifecycle ───────────────────────────────────────────────────────

    def transition(self, new_status: CoverageStatus, **kwargs: Any) -> None:
        """Validate and apply a status transition.

        Raises ``ValueError`` on illegal transitions or missing evidence for
        ``no_issue`` closure.
        """
        allowed = _VALID_TRANSITIONS.get(self.status, set())
        if new_status not in allowed:
            raise ValueError(
                f"Invalid transition {self.status.value} → {new_status.value} "
                f"for cell ({self.unit_id}, {self.dimension.value})"
            )

        if new_status == CoverageStatus.NO_ISSUE:
            evidence = kwargs.get("evidence", self.evidence)
            if not evidence or not str(evidence).strip():
                raise ValueError(
                    f"no_issue closure requires explicit evidence for "
                    f"cell ({self.unit_id}, {self.dimension.value})"
                )
            self.evidence = str(evidence).strip()

        if new_status == CoverageStatus.ASSIGNED:
            task_id = kwargs.get("task_id", "")
            if task_id and task_id not in self.assigned_task_ids:
                self.assigned_task_ids.append(task_id)
            self.attempts += 1

        if new_status == CoverageStatus.FAILED:
            self.attempts += 1

        if "terminal_reason" in kwargs:
            self.terminal_reason = str(kwargs["terminal_reason"])

        if "evidence" in kwargs and new_status != CoverageStatus.NO_ISSUE:
            self.evidence = str(kwargs["evidence"])

        self.status = new_status

    def add_finding(self, finding_id: str) -> None:
        """Record a finding that covers this cell."""
        if finding_id and finding_id not in self.finding_ids:
            self.finding_ids.append(finding_id)

    def is_terminal(self) -> bool:
        """Return True when the cell has reached a final state."""
        return self.status in TERMINAL_STATUSES

    # ── Serialization ───────────────────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        return {
            "unit_id": self.unit_id,
            "path": self.path,
            "line": self.line,
            "dimension": self.dimension.value,
            "risk": self.risk,
            "mandatory": self.mandatory,
            "status": self.status.value,
            "attempts": self.attempts,
            "assigned_task_ids": list(self.assigned_task_ids),
            "finding_ids": list(self.finding_ids),
            "terminal_reason": self.terminal_reason,
            "evidence": self.evidence,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CoverageCell:
        return cls(
            unit_id=str(data["unit_id"]),
            path=str(data["path"]),
            line=int(data["line"]),
            dimension=CoverageDimension(data["dimension"]),
            risk=int(data.get("risk", 0)),
            mandatory=bool(data.get("mandatory", False)),
            status=CoverageStatus(data.get("status", "pending")),
            attempts=int(data.get("attempts", 0)),
            assigned_task_ids=list(data.get("assigned_task_ids", [])),
            finding_ids=list(data.get("finding_ids", [])),
            terminal_reason=str(data.get("terminal_reason", "")),
            evidence=str(data.get("evidence", "")),
        )


# ── CoverageLedger ──────────────────────────────────────────────────────────


class CoverageLedger:
    """Deterministic, serializable coverage matrix over units × dimensions.

    Build from a ``SemanticChangeSet`` (or any dict matching its documented
    shape — no hard runtime dependency on the semantic_diff module).  The
    ledger owns completion rules and cell lifecycle.
    """

    # Cap for optional-bounded dimensions when the unit is low-risk.  A value
    # of 0 disables bounded dimensions entirely; ``None`` means unlimited.
    _DEFAULT_OPTIONAL_CAP: int | None = 200

    def __init__(
        self,
        cells: list[CoverageCell] | None = None,
        optional_cap: int | None = _DEFAULT_OPTIONAL_CAP,
    ) -> None:
        self._cells: list[CoverageCell] = cells if cells is not None else []
        self._optional_cap = optional_cap

    # ── Construction from SemanticChangeSet ─────────────────────────────

    @classmethod
    def from_change_set(
        cls,
        change_set: dict[str, Any],
        optional_cap: int | None = _DEFAULT_OPTIONAL_CAP,
    ) -> CoverageLedger:
        """Build a ledger from a SemanticChangeSet-shaped dict.

        The input is duck-typed: only the documented keys are read.  This
        avoids a hard import dependency on ``semantic_diff`` while modules
        are developed in parallel.

        Required cell policy
        ~~~~~~~~~~~~~~~~~~~~
        1. Every semantic unit → ``correctness`` cell (mandatory).
        2. Risk signals map to mandatory dimension cells via ``_RISK_SIGNAL_MAP``.
        3. Localization file paths → mandatory ``localization`` cell.
        4. Cross-PR evidence in unit metadata → mandatory ``cross-PR`` cell.
        5. Low-risk optional dimensions (performance, compatibility) are
           bounded by *optional_cap*, but mandatory cells are never dropped.
        """
        units: list[dict[str, Any]] = list(change_set.get("units", []))
        cells: list[CoverageCell] = []
        optional_count = 0

        for unit in units:
            unit_id = str(unit.get("id", ""))
            path = str(unit.get("path", ""))
            risk = _coerce_int(unit.get("risk", 0))
            line = _coerce_int(unit.get("line", 0))
            risk_signals = _extract_risk_signals(unit)

            # 1. Always-mandatory dimensions
            for dim in _ALWAYS_MANDATORY:
                cells.append(
                    CoverageCell(
                        unit_id=unit_id,
                        path=path,
                        line=line,
                        dimension=dim,
                        risk=risk,
                        mandatory=True,
                    )
                )

            # 2. Risk-signal-driven mandatory dimensions
            seen_dims: set[CoverageDimension] = set(_ALWAYS_MANDATORY)
            for signal in risk_signals:
                dim = _RISK_SIGNAL_MAP.get(signal)
                if dim and dim not in seen_dims:
                    seen_dims.add(dim)
                    cells.append(
                        CoverageCell(
                            unit_id=unit_id,
                            path=path,
                            line=line,
                            dimension=dim,
                            risk=risk,
                            mandatory=True,
                        )
                    )

            # 3. Localization file-path heuristic
            if (
                CoverageDimension.LOCALIZATION not in seen_dims
                and _is_localization_path(path)
            ):
                seen_dims.add(CoverageDimension.LOCALIZATION)
                cells.append(
                    CoverageCell(
                        unit_id=unit_id,
                        path=path,
                        line=line,
                        dimension=CoverageDimension.LOCALIZATION,
                        risk=risk,
                        mandatory=True,
                    )
                )

            # 4. Cross-PR metadata heuristic
            if CoverageDimension.CROSS_PR not in seen_dims and _has_cross_pr_signal(unit):
                seen_dims.add(CoverageDimension.CROSS_PR)
                cells.append(
                    CoverageCell(
                        unit_id=unit_id,
                        path=path,
                        line=line,
                        dimension=CoverageDimension.CROSS_PR,
                        risk=risk,
                        mandatory=True,
                    )
                )

            # 5. Optional bounded dimensions
            for dim in _OPTIONAL_BOUNDED:
                if dim in seen_dims:
                    continue
                if optional_cap is not None and optional_count >= optional_cap:
                    break
                seen_dims.add(dim)
                cells.append(
                    CoverageCell(
                        unit_id=unit_id,
                        path=path,
                        line=line,
                        dimension=dim,
                        risk=risk,
                        mandatory=False,
                    )
                )
                optional_count += 1

        return cls(cells=cells, optional_cap=optional_cap)

    # ── Query ───────────────────────────────────────────────────────────

    @property
    def cells(self) -> list[CoverageCell]:
        """Return a shallow copy of all cells (read-only view)."""
        return list(self._cells)

    def get_cell(self, unit_id: str, dimension: CoverageDimension) -> CoverageCell | None:
        """Look up a single cell by (unit_id, dimension)."""
        for cell in self._cells:
            if cell.unit_id == unit_id and cell.dimension == dimension:
                return cell
        return None

    def pending_cells(self, dimension: CoverageDimension | None = None) -> list[CoverageCell]:
        """Return pending cells, highest-risk first (stable sort).

        When *dimension* is provided, filter to that dimension only.
        """
        candidates = [
            c
            for c in self._cells
            if c.status == CoverageStatus.PENDING
            and (dimension is None or c.dimension == dimension)
        ]
        # Sort: mandatory first, then descending risk, then path/line for
        # deterministic ordering.
        candidates.sort(
            key=lambda c: (not c.mandatory, -c.risk, c.path, c.line, c.dimension.value)
        )
        return candidates

    def non_terminal_cells(self, dimension: CoverageDimension | None = None) -> list[CoverageCell]:
        """Return cells that have not yet reached a terminal status."""
        return [
            c
            for c in self._cells
            if not c.is_terminal()
            and (dimension is None or c.dimension == dimension)
        ]

    def cells_for_unit(self, unit_id: str) -> list[CoverageCell]:
        """Return all cells for a given unit."""
        return [c for c in self._cells if c.unit_id == unit_id]

    def cells_by_dimension(self, dimension: CoverageDimension) -> list[CoverageCell]:
        """Return all cells for a given dimension."""
        return [c for c in self._cells if c.dimension == dimension]

    # ── Mutation ────────────────────────────────────────────────────────

    def assign(self, unit_id: str, dimension: CoverageDimension, task_id: str) -> CoverageCell:
        """Assign a pending cell to a task.

        Raises ``ValueError`` if the cell is not found or not in a valid
        state for assignment.
        """
        cell = self._require_cell(unit_id, dimension)
        cell.transition(CoverageStatus.ASSIGNED, task_id=task_id)
        return cell

    def record_finding(
        self,
        unit_id: str,
        dimension: CoverageDimension,
        finding_id: str,
    ) -> CoverageCell:
        """Attach a finding to an assigned cell and mark it covered.

        The cell must be in ``assigned`` status.  Raises ``ValueError``
        otherwise.
        """
        cell = self._require_cell(unit_id, dimension)
        if cell.status != CoverageStatus.ASSIGNED:
            raise ValueError(
                f"Cannot record finding on cell ({unit_id}, {dimension.value}) "
                f"in status {cell.status.value}; must be assigned"
            )
        cell.add_finding(finding_id)
        cell.transition(
            CoverageStatus.COVERED,
            terminal_reason=f"finding:{finding_id}",
        )
        return cell

    def close_no_issue(
        self,
        unit_id: str,
        dimension: CoverageDimension,
        evidence: str,
    ) -> CoverageCell:
        """Close a cell with no issue found.

        Requires explicit *evidence* text.  The cell must be ``assigned``.
        """
        cell = self._require_cell(unit_id, dimension)
        if cell.status != CoverageStatus.ASSIGNED:
            raise ValueError(
                f"Cannot close no_issue on cell ({unit_id}, {dimension.value}) "
                f"in status {cell.status.value}; must be assigned"
            )
        cell.transition(CoverageStatus.NO_ISSUE, evidence=evidence)
        return cell

    def abstain(
        self,
        unit_id: str,
        dimension: CoverageDimension,
        reason: str,
    ) -> CoverageCell:
        """Mark a cell as abstained (reviewer chose not to produce a finding).

        The cell must be ``assigned``.
        """
        cell = self._require_cell(unit_id, dimension)
        if cell.status != CoverageStatus.ASSIGNED:
            raise ValueError(
                f"Cannot abstain cell ({unit_id}, {dimension.value}) "
                f"in status {cell.status.value}; must be assigned"
            )
        cell.transition(CoverageStatus.ABSTAINED, terminal_reason=reason)
        return cell

    def fail(
        self,
        unit_id: str,
        dimension: CoverageDimension,
        reason: str,
    ) -> CoverageCell:
        """Mark the current attempt as failed.

        From ``pending`` or ``assigned`` state.  A failed cell may be retried
        by calling ``assign`` again.
        """
        cell = self._require_cell(unit_id, dimension)
        cell.transition(CoverageStatus.FAILED, terminal_reason=reason)
        return cell

    def retry(
        self,
        unit_id: str,
        dimension: CoverageDimension,
        task_id: str,
    ) -> CoverageCell:
        """Re-assign a failed cell to a new task (alias for assign on FAILED)."""
        cell = self._require_cell(unit_id, dimension)
        if cell.status != CoverageStatus.FAILED:
            raise ValueError(
                f"Cannot retry cell ({unit_id}, {dimension.value}) "
                f"in status {cell.status.value}; must be failed"
            )
        cell.transition(CoverageStatus.ASSIGNED, task_id=task_id)
        return cell

    # ── Completion rules ────────────────────────────────────────────────

    def is_complete(self) -> bool:
        """Return True when every cell has reached a terminal status."""
        return all(c.is_terminal() for c in self._cells)

    def mandatory_complete(self) -> bool:
        """Return True when every mandatory cell has reached a terminal status."""
        return all(c.is_terminal() for c in self._cells if c.mandatory)

    def completion_summary(self) -> dict[str, Any]:
        """Return a structured summary of ledger completion state."""
        total = len(self._cells)
        by_status: dict[str, int] = {}
        by_dimension: dict[str, dict[str, int]] = {}
        mandatory_total = 0
        mandatory_resolved = 0

        for cell in self._cells:
            # Status counts
            status_key = cell.status.value
            by_status[status_key] = by_status.get(status_key, 0) + 1

            # Dimension counts
            dim_key = cell.dimension.value
            if dim_key not in by_dimension:
                by_dimension[dim_key] = {}
            by_dimension[dim_key][status_key] = (
                by_dimension[dim_key].get(status_key, 0) + 1
            )

            # Mandatory tracking
            if cell.mandatory:
                mandatory_total += 1
                if cell.is_terminal():
                    mandatory_resolved += 1

        return {
            "total": total,
            "by_status": by_status,
            "by_dimension": by_dimension,
            "mandatory_total": mandatory_total,
            "mandatory_resolved": mandatory_resolved,
            "complete": self.is_complete(),
            "mandatory_complete": self.mandatory_complete(),
        }

    # ── Serialization ───────────────────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        return {
            "optional_cap": self._optional_cap,
            "cells": [c.to_dict() for c in self._cells],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CoverageLedger:
        cells = [CoverageCell.from_dict(c) for c in data.get("cells", [])]
        return cls(
            cells=cells,
            optional_cap=data.get("optional_cap", cls._DEFAULT_OPTIONAL_CAP),
        )

    # ── Internals ───────────────────────────────────────────────────────

    def _require_cell(self, unit_id: str, dimension: CoverageDimension) -> CoverageCell:
        cell = self.get_cell(unit_id, dimension)
        if cell is None:
            raise KeyError(
                f"No coverage cell for ({unit_id}, {dimension.value})"
            )
        return cell


# ── Module-level helpers ────────────────────────────────────────────────────


def _extract_risk_signals(unit: dict[str, Any]) -> list[str]:
    """Return normalised risk signal names from a unit dict.

    Supports both ``risk_signals`` as a list of strings and as a list of
    dicts with a ``type`` key (the documented SemanticChangeSet shape).
    """
    raw = unit.get("risk_signals", [])
    signals: list[str] = []
    for item in raw:
        if isinstance(item, str):
            signals.append(item)
        elif isinstance(item, dict):
            t = item.get("type", "")
            if t:
                signals.append(str(t))
    return signals


def _is_localization_path(path: str) -> bool:
    """Heuristic: does the file path look like a localization resource?"""
    lower = path.lower()
    if any(lower.endswith(ext) for ext in _LOCALE_EXTENSIONS):
        return True
    if any(marker in lower for marker in _LOCALE_MARKERS) and lower.endswith(
        (".json", ".yaml", ".yml")
    ):
        return True
    return False


def _has_cross_pr_signal(unit: dict[str, Any]) -> bool:
    """Heuristic: does the unit carry cross-PR evidence?"""
    # Explicit cross-PR risk signal
    for signal in _extract_risk_signals(unit):
        normalized = signal.lower().replace("_", "-")
        if normalized in ("cross-pr", "cross_pr"):
            return True
    # Metadata flag
    meta = unit.get("metadata", {})
    if isinstance(meta, dict) and meta.get("cross_pr"):
        return True
    # live_references with cross-PR hints
    refs = unit.get("live_references", [])
    if isinstance(refs, list) and len(refs) > 0:
        # If there are references to other files, it could be cross-PR
        # This is a weak signal — only use if explicitly flagged
        pass
    return False


def _coerce_int(value: Any) -> int:
    """Safely coerce to int, defaulting to 0."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
