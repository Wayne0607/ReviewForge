"""Loop Detection — two-stage rescue pattern.

Stage 1 (rescue): 3 consecutive same-signature tasks → drain duplicates + hint to Planner.
Stage 2 (stall):  rescue repeated → halt with loop_stalled.
"""

from __future__ import annotations

import hashlib
from collections import deque


class LoopDetector:
    def __init__(self, window_size: int = 3) -> None:
        self._window_size = window_size
        self._signatures: deque[str] = deque(maxlen=window_size)
        self._rescued: set[str] = set()
        self._stalled = False

    @property
    def is_stalled(self) -> bool:
        return self._stalled

    @staticmethod
    def make_signature(reviewer: str, files: list[str]) -> str:
        file_hash = hashlib.sha1(",".join(sorted(files)).encode()).hexdigest()[:8]
        return f"{reviewer}:{file_hash}"

    def check(self, signature: str) -> str | None:
        """Returns: 'rescue' | 'stall' | None."""
        self._signatures.append(signature)

        if len(self._signatures) < self._window_size:
            return None
        if len(set(self._signatures)) != 1:
            return None

        if signature not in self._rescued:
            self._rescued.add(signature)
            self._signatures.clear()  # reset window; stall needs 3 more consecutive
            return "rescue"

        self._stalled = True
        return "stall"
