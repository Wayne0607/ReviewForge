"""Source line boundaries, opt-in to Git's LF-only physical line semantics."""

from __future__ import annotations

from typing import Any


def source_line_policy(run: Any) -> str | None:
    """Return the selected source policy; only an absent key means legacy."""
    if "source_line_policy" not in run.metadata:
        return None
    policy = run.metadata["source_line_policy"]
    if not isinstance(policy, str) or policy != "git-lf-1":
        raise ValueError("unknown or invalid source line policy")
    return policy


def source_lines(run: Any, text: str, *, keepends: bool = False) -> list[str]:
    """Split decoded source while retaining every non-LF character under git-lf-1."""
    if source_line_policy(run) is None:
        return text.splitlines(keepends=keepends)
    if not text:
        return []
    parts = text.split("\n")
    if parts[-1] == "":
        parts.pop()
        trailing = True
    else:
        trailing = False
    if not keepends:
        return parts
    return [part + ("\n" if index < len(parts) - 1 or trailing else "") for index, part in enumerate(parts)]


def source_slice(run: Any, text: str, start_line: int, end_line: int) -> str:
    """Return an exact, inclusive 1-based line range without normalizing bytes."""
    lines = source_lines(run, text, keepends=True)
    if type(start_line) is not int or type(end_line) is not int or start_line < 1 or end_line < start_line:
        raise ValueError("invalid source line range")
    if end_line > len(lines):
        raise ValueError("source line range exceeds content")
    return "".join(lines[start_line - 1 : end_line])


def source_quote_lines(run: Any, text: str, quote: str, *, first_line: int = 1) -> list[tuple[int, int]]:
    """Locate every exact quote occurrence and return its inclusive physical lines."""
    policy = source_line_policy(run)
    if not isinstance(quote, str) or not quote:
        raise ValueError("quote must not be empty")
    if type(first_line) is not int or first_line < 1:
        raise ValueError("first line must be positive")
    result = []
    offset = text.find(quote)
    while offset >= 0:
        if policy == "git-lf-1":
            first = first_line + text[:offset].count("\n")
            last = first_line + text[: offset + len(quote) - 1].count("\n")
        else:
            first = first_line + text[:offset].count("\n")
            last = first + quote.count("\n")
        result.append((first, last))
        offset = text.find(quote, offset + 1)
    return result
