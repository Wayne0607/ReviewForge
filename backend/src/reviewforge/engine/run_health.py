"""Operational health aggregation for a review run.

The review summary is a public, backward-compatible result contract.  Run
health is deliberately kept separate from that contract so every exit path can
make the same retry/DB decision without teaching callers about individual
pipeline stages.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class StageResult:
    """Bounded operational outcome for one pipeline stage."""

    name: str
    failures: int = 0
    retryable: bool = False
    errors: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.failures < 0:
            raise ValueError("stage failures cannot be negative")

    @property
    def operationally_incomplete(self) -> bool:
        return self.failures > 0 or self.retryable or bool(self.errors)

    def failure_message(self) -> str:
        if self.errors:
            return "; ".join(self.errors)
        return f"{self.name} incomplete ({self.failures} failure(s))"


@dataclass(frozen=True)
class RunHealth:
    """Single source of truth for partial/completed run finalization."""

    tasks: StageResult
    planner: StageResult
    publication: StageResult
    delivery: StageResult

    @classmethod
    def build(
        cls,
        *,
        tasks_failed: int = 0,
        planner_errors: tuple[str, ...] = (),
        publication_failures: int = 0,
        publication_errors: tuple[str, ...] = (),
        publication_retryable: bool = False,
        delivery_failures: int = 0,
        delivery_errors: tuple[str, ...] = (),
        delivery_retryable: bool = False,
    ) -> RunHealth:
        return cls(
            tasks=StageResult(
                name="tasks",
                failures=tasks_failed,
                # Failed reviewer tasks are safe to retry: completed tasks and
                # reported findings are rehydrated and skipped on the next run.
                retryable=tasks_failed > 0,
            ),
            planner=StageResult(
                name="planner",
                failures=len(planner_errors),
                retryable=bool(planner_errors),
                errors=planner_errors,
            ),
            publication=StageResult(
                name="publication",
                failures=publication_failures,
                retryable=publication_retryable,
                errors=publication_errors,
            ),
            delivery=StageResult(
                name="delivery",
                failures=delivery_failures,
                retryable=delivery_retryable,
                errors=delivery_errors,
            ),
        )

    @property
    def operationally_incomplete(self) -> bool:
        return any(stage.operationally_incomplete for stage in self.stages)

    @property
    def retryable(self) -> bool:
        return any(stage.retryable for stage in self.stages)

    @property
    def stages(self) -> tuple[StageResult, ...]:
        return (self.tasks, self.planner, self.publication, self.delivery)

    @property
    def errors(self) -> list[str]:
        return [stage.failure_message() for stage in self.stages if stage.operationally_incomplete]

    def apply_to_summary(self, summary: dict[str, Any]) -> dict[str, Any]:
        """Preserve the legacy success shape and annotate incomplete runs."""
        if self.operationally_incomplete:
            summary.update({"status": "partial", "retryable": self.retryable})
        return summary

    def failures_payload(self) -> dict[str, int | bool]:
        """Stable failure counters for the append-only evaluation event."""
        return {
            "tasks_failed": self.tasks.failures,
            "planner": self.planner.failures,
            "publication": self.publication.failures,
            "delivery": self.delivery.failures,
            "operationally_incomplete": self.operationally_incomplete,
        }
