"""Helpers for extracting structured JSON from reasoning-model output."""

from __future__ import annotations

import json
import re
from typing import Any

_REASONING_BLOCK = re.compile(r"<think\b[^>]*>.*?</think\s*>", re.IGNORECASE | re.DOTALL)
_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.IGNORECASE | re.DOTALL)


def strip_reasoning_blocks(content: object) -> str:
    """Remove provider reasoning blocks while preserving the final answer."""

    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") in {"text", "output_text"}:
                parts.append(str(block.get("text") or block.get("content") or ""))
            elif getattr(block, "type", "") in {"text", "output_text"}:
                parts.append(str(getattr(block, "text", "") or getattr(block, "content", "")))
        text = "\n".join(parts)
    else:
        return ""
    return _REASONING_BLOCK.sub("", text).strip()


def extract_json_value(
    content: object,
    *,
    required_key: str | None = None,
    allow_list: bool = True,
) -> Any | None:
    """Return the first JSON value matching the requested output envelope."""

    cleaned = strip_reasoning_blocks(content)
    if not cleaned:
        return None
    sources = [*_JSON_FENCE.findall(cleaned), cleaned]
    decoder = json.JSONDecoder()
    for source in sources:
        source = source.strip()
        try:
            candidates = [json.loads(source)]
        except json.JSONDecodeError:
            candidates = []
            for index, char in enumerate(source):
                if char not in "[{":
                    continue
                try:
                    value, _end = decoder.raw_decode(source, index)
                except json.JSONDecodeError:
                    continue
                candidates.append(value)
        for value in candidates:
            if isinstance(value, dict) and (required_key is None or required_key in value):
                return value
            if allow_list and isinstance(value, list) and required_key in {None, "findings"}:
                return value
    return None
