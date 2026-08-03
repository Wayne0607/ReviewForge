"""Scheduler — priority-ordered, concurrency-capped task dispatch.

The Planner emits review tasks; the Scheduler decides the order they start
(higher-priority dimensions like security first) and how many run at once
(bounded concurrency), then dispatches each through a worker coroutine.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

# Reviewer dispatch priority (higher starts first). Security leads; style trails.
DEFAULT_PRIORITY: dict[str, int] = {
    "security_reviewer": 100,
    "dependency_reviewer": 80,
    "performance_reviewer": 70,
    "accessibility_reviewer": 50,
    "testing_reviewer": 40,
    "doc_reviewer": 30,
    "correctness_reviewer": 25,
    "style_reviewer": 20,
}


class Scheduler:
    """Priority queue + bounded-concurrency dispatcher for review tasks."""

    def __init__(self, concurrency: int = 4, priority: dict[str, int] | None = None) -> None:
        self._concurrency = max(1, concurrency)
        self._priority = priority or DEFAULT_PRIORITY

    def order(self, tasks: list[Any]) -> list[Any]:
        """Return tasks sorted by descending reviewer priority (stable for ties)."""
        return sorted(tasks, key=lambda t: self._priority.get(getattr(t, "reviewer", ""), 10), reverse=True)

    async def dispatch(self, tasks: list[Any], worker: Callable[[Any], Awaitable[None]]) -> None:
        """Run `worker(task)` for each task: priority-ordered start, concurrency-capped.

        A per-dispatch semaphore bounds in-flight workers; tasks are launched in
        priority order so higher-priority reviewers acquire slots first. A worker
        that raises does not abort the others (each is isolated).
        """
        if not tasks:
            return
        sem = asyncio.Semaphore(self._concurrency)

        async def _guarded(task: Any) -> None:
            async with sem:
                await worker(task)

        ordered = self.order(tasks)
        await asyncio.gather(*[_guarded(t) for t in ordered], return_exceptions=True)
