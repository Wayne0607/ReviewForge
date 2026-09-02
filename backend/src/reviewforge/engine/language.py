"""Deterministic output-language selection for review prompts.

The legacy path keeps its historical ``zh-CN`` default in
``ReviewForgeConfig``.  New callers can pass ``output_language="auto"`` and
have the language selected from the PR body and newly added diff comments.
This module intentionally contains no model or network calls so the decision
is stable in tests and benchmark runs.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

OUTPUT_LANGUAGES = frozenset({"auto", "en", "zh-CN"})
LEGACY_OUTPUT_LANGUAGE = "zh-CN"

# Count CJK Unified Ideographs rather than punctuation, symbols, or ASCII
# identifiers.  The extension ranges cover the common supplementary and
# compatibility blocks without classifying Japanese/Korean scripts as Chinese
# output when the configured choices only distinguish English and zh-CN.
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\U00020000-\U0002fa1f]")
_COMMENT_START_RE = re.compile(r"(?:^|\s)(?P<marker>#|//|--|/\*|<!--)(?P<body>.*)$")


def normalize_output_language(value: Any, default: str = LEGACY_OUTPUT_LANGUAGE) -> str:
    """Normalize a configured language while preserving a valid fallback."""

    if isinstance(value, str):
        candidate = value.strip()
        if candidate.lower() == "zh-cn":
            return "zh-CN"
        if candidate in {"auto", "en"}:
            return candidate
    if default == "auto":
        return "auto"
    if default == "en":
        return "en"
    return LEGACY_OUTPUT_LANGUAGE


def _value(source: Any, key: str, default: Any = "") -> Any:
    if isinstance(source, Mapping):
        return source.get(key, default)
    return getattr(source, key, default)


def extract_diff_comments(diff: str | Mapping[str, Any] | None) -> str:
    """Return text from added diff comments in deterministic source order.

    Unified-diff metadata and unchanged/deleted lines are ignored.  The
    extractor recognizes the comment forms used by the repository's supported
    languages (``#``, ``//``, ``--``, block comments, and HTML comments).
    It is intentionally conservative: code/string text is not considered PR
    prose merely because it contains CJK characters.
    """

    if diff is None:
        return ""
    if isinstance(diff, Mapping):
        # StateStore currently keeps paths as strings, but sorting by their
        # string form also keeps hand-authored mappings with mixed key types
        # deterministic.
        chunks = [str(diff[key] or "") for key in sorted(diff, key=str)]
        return "\n".join(_extract_comments_from_text(chunk) for chunk in chunks)
    return _extract_comments_from_text(str(diff))


def _extract_comments_from_text(diff: str) -> str:
    comments: list[str] = []
    in_block = False
    for raw_line in diff.splitlines():
        # Only additions represent the changed code/comments.  ``+++`` is a
        # file header, not an added source line.
        if not raw_line.startswith("+") or raw_line.startswith("+++"):
            continue
        line = raw_line[1:]
        if in_block:
            end = line.find("*/")
            if end < 0:
                comments.append(line)
                continue
            comments.append(line[:end])
            line = line[end + 2 :]
            in_block = False

        match = _COMMENT_START_RE.search(line)
        if not match:
            continue
        marker = match.group("marker")
        body = match.group("body")
        if marker == "/*":
            end = body.find("*/")
            if end < 0:
                comments.append(body)
                in_block = True
            else:
                comments.append(body[:end])
        elif marker == "<!--":
            end = body.find("-->")
            comments.append(body[: end if end >= 0 else len(body)])
        else:
            comments.append(body)
    return "\n".join(comments)


def cjk_ratio(text: str | None) -> float:
    """Return CJK ideograph count divided by non-whitespace characters."""

    value = str(text or "")
    denominator = sum(not character.isspace() for character in value)
    if denominator == 0:
        return 0.0
    return len(_CJK_RE.findall(value)) / denominator


def _state_pr_body(state: Any) -> str:
    return str(_value(state, "pr_body", "") or "")


def _state_diff(state: Any) -> str | Mapping[str, Any]:
    file_diffs = _value(state, "file_diffs", None)
    if isinstance(file_diffs, Mapping):
        return file_diffs
    return str(_value(state, "diff_summary", "") or "")


def resolve_output_language(state: Any, config: Any) -> str:
    """Resolve ``auto`` from PR prose and added comments.

    ``state`` may be a ``StateStore`` or a mapping in deterministic unit
    tests.  ``config`` may be ``ReviewForgeConfig``, the future
    ``PipelineV4Config``, or a mapping with an ``output_language`` key.
    """

    configured = normalize_output_language(_value(config, "output_language", LEGACY_OUTPUT_LANGUAGE))
    if configured != "auto":
        return configured

    source = "\n".join((_state_pr_body(state), extract_diff_comments(_state_diff(state))))
    return "zh-CN" if cjk_ratio(source) > 0.30 else "en"


__all__ = [
    "LEGACY_OUTPUT_LANGUAGE",
    "OUTPUT_LANGUAGES",
    "cjk_ratio",
    "extract_diff_comments",
    "normalize_output_language",
    "resolve_output_language",
]
