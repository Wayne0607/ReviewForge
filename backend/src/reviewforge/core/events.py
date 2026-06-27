"""Event Bus — synchronous pub/sub for observability."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable


class EventBus:
    """Simple event bus with JSONL logging and subscriber callbacks."""

    def __init__(self, log_path: Path | None = None) -> None:
        self._subscribers: list[Callable[[str, dict[str, Any]], None]] = []
        self._log_path = log_path

    def subscribe(self, callback: Callable[[str, dict[str, Any]], None]) -> None:
        self._subscribers.append(callback)

    def emit(self, event_type: str, data: dict[str, Any] | None = None) -> None:
        payload = data or {}
        payload["_event"] = event_type
        payload["_ts"] = time.time()

        for cb in self._subscribers:
            try:
                cb(event_type, payload)
            except Exception:
                pass  # subscriber errors must not break the pipeline

        if self._log_path:
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(payload, ensure_ascii=False) + "\n")
