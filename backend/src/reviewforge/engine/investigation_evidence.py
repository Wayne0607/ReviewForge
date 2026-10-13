"""Bounded, run-scoped reuse of successful read-only tool results.

This cache shares source acquisition, never a claim, assessment or verdict. An
investigator still saves its own bounded Observation before citing the result.
Failed/negative reads may finish an in-flight request but are never retained.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

READ_ONLY_TOOLS = frozenset(
    {"read_file", "read_diff", "read_change", "grep", "find_definition", "find_callers", "find_references"}
)
MAX_CACHE_ENTRIES = 512
MAX_CACHE_BYTES = 4 * 1024 * 1024
MAX_RESULT_CHARS = 6_000
_NEGATIVE = frozenset(
    {"no results", "not found", "no matches", "no definition found", "no callers found", "no literal references found"}
)


def result_status(text: str) -> str:
    stripped = text.strip()
    return "not_found" if not stripped or stripped.lower() in _NEGATIVE else "success"


def diff_evidence_window(patch: str, right_line: int, *, max_chars: int = 1_200) -> tuple[str, dict[str, Any]]:
    """Save a contiguous target span; never fabricate a shortened hunk header.

    A large hunk's original coordinates are navigation metadata, separate from
    citable source. A clipped deletion block cannot establish unchanged behavior
    or a complete before/after comparison. Small hunks retain their full shape.
    """
    lines = patch.splitlines(keepends=True)
    headers = [
        (index, match)
        for index, line in enumerate(lines)
        if (match := re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", line))
    ]
    for number, (index, match) in enumerate(headers):
        first, count = int(match[1]), int(match[2] or 1)
        if not first <= right_line < first + max(1, count):
            continue
        last = headers[number + 1][0] if number + 1 < len(headers) else len(lines)
        hunk = lines[index:last]
        if len("".join(hunk)) <= max_chars:
            return "".join(hunk), {}
        cursor, focus = first, 1
        for offset, line in enumerate(hunk[1:], 1):
            if line.startswith((" ", "+")):
                if cursor == right_line:
                    focus = offset
                    break
                cursor += 1
        begin = focus
        # Preserve immediate deleted/context lines, without letting a huge old
        # line displace the new operation from the saved 1200-character span.
        while begin > 1 and len("".join(hunk[begin - 1 : focus])) <= min(400, max_chars // 3):
            begin -= 1
        excerpt = "".join(hunk[begin:])[:max_chars]
        return excerpt, {
            "hunk_header": hunk[0].rstrip("\r\n")[:200],
            "complete_hunk": False,
            "before_after_complete": False,
            "omitted_lines_before": begin,
            "omitted_lines_after": max(0, len(hunk) - begin - len(excerpt.splitlines())),
        }
    return patch[:max_chars], {}


@dataclass(frozen=True)
class EvidenceScope:
    run_id: str
    head_sha: str
    workspace_digest: str


@dataclass(frozen=True)
class SharedToolResult:
    text: str
    shared_result_id: str = ""
    reused: bool = False


class SharedEvidenceCache:
    def __init__(
        self,
        scope: EvidenceScope,
        *,
        max_entries: int = MAX_CACHE_ENTRIES,
        max_bytes: int = MAX_CACHE_BYTES,
    ) -> None:
        self.scope = scope
        self._max_entries = min(MAX_CACHE_ENTRIES, max(0, max_entries))
        self._max_bytes = min(MAX_CACHE_BYTES, max(0, max_bytes))
        self._results: dict[str, SharedToolResult] = {}
        self._inflight: dict[str, asyncio.Task[SharedToolResult]] = {}
        self._bytes = 0

    @property
    def entry_count(self) -> int:
        return len(self._results)

    @property
    def byte_count(self) -> int:
        return self._bytes

    async def close(self) -> None:
        """Release this run's cache and any reads left after cancellation."""
        pending = list(self._inflight.values())
        for task in pending:
            if not task.done():
                task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        self._inflight.clear()
        self._results.clear()
        self._bytes = 0

    async def execute(
        self,
        name: str,
        args: dict[str, Any],
        executor: Callable[[str, dict[str, Any]], Awaitable[str]],
        *,
        scope: EvidenceScope,
    ) -> SharedToolResult:
        if scope != self.scope:
            raise ValueError("shared evidence scope does not match this run/head/workspace")
        if name not in READ_ONLY_TOOLS:
            raise ValueError("shared evidence accepts only read-only investigation tools")
        query = json.dumps([name, args], ensure_ascii=False, sort_keys=True, default=str)
        existing = self._results.get(query)
        if existing is not None:
            return SharedToolResult(existing.text, existing.shared_result_id, reused=True)
        pending = self._inflight.get(query)
        reused = pending is not None
        if pending is None:
            pending = asyncio.create_task(self._load(query, name, args, executor))
            self._inflight[query] = pending
        # One claim's cancellation must not cancel another claim's source read.
        result = await asyncio.shield(pending)
        return SharedToolResult(result.text, result.shared_result_id, reused=reused)

    async def _load(
        self,
        query: str,
        name: str,
        args: dict[str, Any],
        executor: Callable[[str, dict[str, Any]], Awaitable[str]],
    ) -> SharedToolResult:
        try:
            text = str(await executor(name, args) or "")[:MAX_RESULT_CHARS]
            if result_status(text) != "success":
                return SharedToolResult(text)
            identity = hashlib.sha256(
                json.dumps(
                    [self.scope.run_id, self.scope.head_sha, self.scope.workspace_digest, query, text],
                    ensure_ascii=False,
                ).encode("utf-8")
            ).hexdigest()[:24]
            result = SharedToolResult(text, f"sr_{identity}")
            size = len(text.encode("utf-8")) + len(query.encode("utf-8")) + len(result.shared_result_id)
            if len(self._results) < self._max_entries and self._bytes + size <= self._max_bytes:
                self._results[query] = result
                self._bytes += size
            return result
        finally:
            self._inflight.pop(query, None)


__all__ = ["EvidenceScope", "SharedEvidenceCache", "SharedToolResult", "result_status"]
