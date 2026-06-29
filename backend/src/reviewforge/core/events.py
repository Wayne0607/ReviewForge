"""Event Bus — structured observability for the review pipeline.

Supports:
- JSONL file logging (append-only, one event per line)
- Subscriber callbacks (used by orchestrator, webhook, etc.)
- Event filtering by type
- Review traceability (every state change is an event)
- B4: contextvar-based run_id for concurrent task isolation
"""

from __future__ import annotations

import contextvars
import json
import time
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

# B4: 每个 asyncio task 独立的 run_id
_run_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("run_id", default="")


@dataclass
class ReviewEvent:
    """A single review event with metadata."""

    event_type: str
    data: dict[str, Any]
    timestamp: float = field(default_factory=time.time)
    run_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "_event": self.event_type,
            "_ts": self.timestamp,
            "_run_id": self.run_id,
            **self.data,
        }


class EventBus:
    """Event bus with JSONL logging and subscriber callbacks.

    Events are the audit trail of the review pipeline.
    Every state change (task claimed, finding created, comment posted) is an event.
    """

    def __init__(self, log_dir: Path | None = None) -> None:
        self._subscribers: list[Callable[[ReviewEvent], None]] = []
        self._log_dir = log_dir

    def set_run_id(self, run_id: str) -> None:
        """Set the current run ID for this asyncio context."""
        _run_id_var.set(run_id)

    def subscribe(self, callback: Callable[[ReviewEvent], None]) -> None:
        """Subscribe to all events."""
        self._subscribers.append(callback)

    def subscribe_type(self, event_type: str, callback: Callable[[ReviewEvent], None]) -> None:
        """Subscribe to events of a specific type."""
        def _filter(event: ReviewEvent) -> None:
            if event.event_type == event_type:
                callback(event)
        self._subscribers.append(_filter)

    def emit(self, event_type: str, data: dict[str, Any] | None = None) -> ReviewEvent:
        """Emit an event. Logs to JSONL and notifies subscribers."""
        current_run_id = _run_id_var.get("")
        event = ReviewEvent(
            event_type=event_type,
            data=data or {},
            run_id=current_run_id,
        )

        # Log to JSONL
        if self._log_dir:
            self._log_dir.mkdir(parents=True, exist_ok=True)
            log_path = self._log_dir / f"{current_run_id or 'default'}.jsonl"
            try:
                with open(log_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(event.to_dict(), ensure_ascii=False) + "\n")
            except Exception as e:
                logger.error(f"Failed to write event log: {e}")

        # Notify subscribers
        for cb in self._subscribers:
            try:
                cb(event)
            except Exception as e:
                logger.error(f"Event subscriber error: {e}")

        # Also log to Python logger
        logger.info(f"[{event_type}] {json.dumps(data or {}, ensure_ascii=False)}")

        return event

    def get_events(self, run_id: str | None = None) -> list[ReviewEvent]:
        """Read events from the JSONL log file."""
        if not self._log_dir:
            return []

        rid = run_id or _run_id_var.get("") or "default"
        log_path = self._log_dir / f"{rid}.jsonl"
        if not log_path.exists():
            return []

        events = []
        with open(log_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    events.append(ReviewEvent(
                        event_type=data.pop("_event", "unknown"),
                        timestamp=data.pop("_ts", 0),
                        run_id=data.pop("_run_id", ""),
                        data=data,
                    ))
                except json.JSONDecodeError:
                    continue
        return events
