"""Validate native result arguments before schema or source interpretation.

OpenAI-style SDKs expose parsed tool arguments as well as the original argument
string. A permissive JSON parse can erase duplicate keys; typing the resulting
dict cannot recover them. Retain the same uniqueness boundary as text outputs.
"""

from __future__ import annotations

import json
from typing import Any


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("duplicate-native-argument-key")
        result[name] = value
    return result


def _invalid_constant(value: str) -> Any:
    raise ValueError("non-json-native-argument-constant")


def raw_tool_calls(response: Any) -> list[dict[str, Any]] | None:
    extra = getattr(response, "additional_kwargs", {}) or {}
    key = next(
        (name for name in ("reviewforge_raw_tool_calls", "tool_calls") if name in extra and extra[name] is not None),
        None,
    )
    if key is None:
        return None
    raw = extra[key]
    if not isinstance(raw, list):
        raise ValueError("invalid-raw-tool-calls")
    return raw


def strict_tool_call(call: Any, raw: Any = None) -> dict[str, Any]:
    """Validate a single untouched call and its explicitly associated raw body."""
    if not isinstance(call, dict) or not isinstance(call.get("name"), str) or not isinstance(call.get("args"), dict):
        raise ValueError("invalid-native-tool-call")
    if raw is not None:
        function = raw.get("function") if isinstance(raw, dict) else None
        if (
            not isinstance(function, dict)
            or raw.get("type", "function") != "function"
            or function.get("name") != call["name"]
            or raw.get("id") != call.get("id")
        ):
            raise ValueError("mismatched-native-tool-call")
        arguments = function.get("arguments")
        if isinstance(arguments, str):
            arguments = json.loads(arguments, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
        if not isinstance(arguments, dict) or arguments != call["args"]:
            raise ValueError("changed-native-tool-arguments")
    return call


def inspect_tool_calls(response: Any) -> tuple[list[dict[str, Any]], list[str]]:
    """Keep individually complete calls, recording any incomplete outer stream.

    The caller may retain complete candidate leads, but any error prevents
    coverage/clean receipts. Raw calls associate only by unique provider ID;
    absent IDs use order only when both complete lists have the same length.
    """
    errors = ["invalid-native-tool-call"] if getattr(response, "invalid_tool_calls", None) else []
    calls = getattr(response, "tool_calls", None)
    if calls is None:
        calls = []
    if not isinstance(calls, list):
        return [], [*errors, "invalid-native-tool-call"]
    try:
        raw_calls = raw_tool_calls(response)
    except ValueError:
        return [], [*errors, "invalid-raw-tool-calls"]
    if raw_calls is not None and len(raw_calls) != len(calls):
        errors.append("mismatched-native-tool-calls")
    valid = []
    consumed_raw: set[int] = set()
    for index, call in enumerate(calls):
        try:
            raw = None
            if raw_calls is not None:
                identity = call.get("id") if isinstance(call, dict) else None
                matches = [
                    (raw_index, row)
                    for raw_index, row in enumerate(raw_calls)
                    if isinstance(row, dict) and identity and row.get("id") == identity
                ]
                if identity and len(matches) == 1:
                    raw_index, raw = matches[0]
                elif not identity and len(raw_calls) == len(calls):
                    raw_index, raw = index, raw_calls[index]
                else:
                    raise ValueError("mismatched-native-tool-call")
                if raw is None or raw_index in consumed_raw:
                    raise ValueError("mismatched-native-tool-call")
                consumed_raw.add(raw_index)
            valid.append(strict_tool_call(call, raw))
        except json.JSONDecodeError:
            errors.append("malformed-native-json")
        except (ValueError, TypeError):
            errors.append("invalid-native-tool-call")
    if raw_calls is not None and len(consumed_raw) != len(raw_calls):
        errors.append("unmatched-raw-tool-calls")
    return valid, errors


def strict_tool_calls(response: Any) -> list[dict[str, Any]]:
    """Require a complete unambiguous native response before judging a proof."""
    calls, errors = inspect_tool_calls(response)
    if errors:
        raise ValueError("invalid-native-submission")
    return calls
