"""Frame complete discovery records without asking a model to recreate them.

Only a missing outer object closer can be repaired as a complete response.
For a damaged array tail, complete preceding records may enter investigation,
but the caller must keep the entire block's coverage unresolved. No strings,
values, references or partial records are completed or guessed here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from reviewforge.core.json_output import strip_reasoning_blocks


class _DuplicateKeyError(ValueError):
    pass


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKeyError(key)
        result[key] = value
    return result


@dataclass(frozen=True)
class DiscoveryOutput:
    payload: dict[str, Any]
    complete: bool
    recovery: str = "none"


def frame_discovery_output(content: object) -> DiscoveryOutput | None:
    source = strip_reasoning_blocks(content)
    if source.startswith("```json\n") and source.endswith("\n```"):
        source = source[8:-4].strip()
    if source.startswith("["):
        return _record_array(source)
    if not source.startswith("{"):
        return None
    decoder = json.JSONDecoder(object_pairs_hook=_object)
    allowed = {"hypotheses", "no_issue_units", "deferred_units"}
    result = {}
    position = 1

    def skip():
        nonlocal position
        while position < len(source) and source[position].isspace():
            position += 1

    def prefix():
        if "hypotheses" in result and any(result.values()):
            return DiscoveryOutput(result, False, "complete-record-prefix")
        return None

    try:
        while True:
            skip()
            key, position = decoder.raw_decode(source, position)
            if not isinstance(key, str) or key not in allowed or key in result:
                return None
            skip()
            if source[position : position + 1] != ":":
                return None
            position += 1
            skip()
            if source[position : position + 1] != "[":
                return None
            position += 1
            values = result[key] = []
            skip()
            if source[position : position + 1] != "]":
                while True:
                    skip()
                    try:
                        value, position = decoder.raw_decode(source, position)
                    except json.JSONDecodeError:
                        return prefix()
                    values.append(value)
                    skip()
                    if source[position : position + 1] == "]":
                        break
                    if source[position : position + 1] != ",":
                        return prefix()
                    position += 1
            position += 1
            skip()
            if position == len(source):
                return DiscoveryOutput(result, True, "missing-root-closer") if "hypotheses" in result else None
            if source[position] == "}":
                return (
                    DiscoveryOutput(result, True)
                    if not source[position + 1 :].strip() and "hypotheses" in result
                    else None
                )
            if source[position] != ",":
                return None
            position += 1
    except _DuplicateKeyError:
        return None
    except json.JSONDecodeError:
        return prefix()


def _record_array(source: str) -> DiscoveryOutput | None:
    """Normalize an unambiguous complete record list; never infer its contents.

    Discovery's schema validation still owns required fields, citations and unit
    coverage. Empty or damaged lists cannot establish a completed block.
    """
    try:
        records = json.loads(source, object_pairs_hook=_object)
    except (json.JSONDecodeError, _DuplicateKeyError):
        return None
    if not isinstance(records, list) or not records:
        return None
    markers = {"claim": "hypotheses", "checked": "no_issue_units", "reason": "deferred_units"}
    payload = {name: [] for name in markers.values()}
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get("unit_id"), str) or not record["unit_id"]:
            return None
        kinds = [name for marker, name in markers.items() if marker in record]
        if len(kinds) != 1:
            return None
        payload[kinds[0]].append(record)
    return DiscoveryOutput(payload, True, "record-array-envelope")
