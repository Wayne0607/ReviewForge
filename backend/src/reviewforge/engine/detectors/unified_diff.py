"""Unified-diff helpers with GitHub RIGHT-side line mapping.

GitHub review comments use the line number in the post-image of a diff (``side=RIGHT``).
Patch-text positions and indexes in an ``added_lines`` list are not valid substitutes,
especially when a patch contains context, deletions, or multiple hunks.
"""

from __future__ import annotations

import re

_HUNK_HEADER = re.compile(
    r"^@@ -(?P<old_start>\d+)(?:,(?P<old_count>\d+))? "
    r"\+(?P<new_start>\d+)(?:,(?P<new_count>\d+))? @@(?:.*)$"
)


def _iter_right_lines(diff: str) -> list[tuple[int, str, bool]]:
    """Return mapped post-image lines as ``(line, content, is_added)``."""

    right_lines: list[tuple[int, str, bool]] = []
    old_line = 0
    new_line = 0
    old_remaining = 0
    new_remaining = 0
    in_hunk = False

    for raw_line in (diff or "").splitlines():
        header = _HUNK_HEADER.match(raw_line)
        if header:
            old_line = int(header.group("old_start"))
            new_line = int(header.group("new_start"))
            old_remaining = int(header.group("old_count") or 1)
            new_remaining = int(header.group("new_count") or 1)
            in_hunk = True
            continue

        # A malformed hunk marker must not inherit coordinates from the previous
        # hunk. A new file section likewise ends any truncated prior hunk.
        if raw_line.startswith("@@") or raw_line.startswith("diff --git "):
            in_hunk = False
            continue

        if not in_hunk or raw_line.startswith("\\ No newline at end of file"):
            continue

        if not raw_line:
            # Every line within a unified hunk has a one-character prefix. An empty
            # raw line therefore indicates malformed/truncated input.
            in_hunk = False
            continue

        prefix = raw_line[0]
        if prefix == "+":
            if new_remaining <= 0:
                in_hunk = False
                continue
            right_lines.append((new_line, raw_line[1:], True))
            new_line += 1
            new_remaining -= 1
        elif prefix == "-":
            if old_remaining <= 0:
                in_hunk = False
                continue
            old_line += 1
            old_remaining -= 1
        elif prefix == " ":
            if old_remaining <= 0 or new_remaining <= 0:
                in_hunk = False
                continue
            right_lines.append((new_line, raw_line[1:], False))
            old_line += 1
            new_line += 1
            old_remaining -= 1
            new_remaining -= 1
        else:
            in_hunk = False
            continue

        if old_remaining == 0 and new_remaining == 0:
            in_hunk = False

    return right_lines


def iter_right_lines(diff: str) -> list[tuple[int, str]]:
    """Return every commentable RIGHT-side line in valid unified-diff hunks.

    GitHub permits inline review comments on additions and visible context lines,
    but not deletion-only LEFT-side lines. Unanchored or malformed patch text is
    ignored instead of inventing coordinates.
    """

    return [(line_no, content) for line_no, content, _is_added in _iter_right_lines(diff)]


def iter_added_lines(diff: str) -> list[tuple[int, str]]:
    """Return ``(new_file_line, content)`` for mapped added lines.

    Only additions inside a syntactically valid unified-diff hunk are returned. If
    GitHub omits/truncates a patch before a hunk header, there is no trustworthy
    RIGHT-side anchor, so those ``+`` lines are intentionally ignored rather than
    numbered relative to the patch text.

    Already-parsed additions remain usable when the tail of a hunk is truncated:
    their line numbers are fully determined by the hunk's ``+<start>`` coordinate.
    """

    return [(line_no, content) for line_no, content, is_added in _iter_right_lines(diff) if is_added]
