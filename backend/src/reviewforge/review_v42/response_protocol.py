"""Per-request JSON constraints for frozen compatible provider bindings."""

from __future__ import annotations

from typing import Any

JSON_CONSTRAINT = "openai-json-object-1"
JSON_REQUEST_OPTIONS = {"tool_choice": "none", "response_format": {"type": "json_object"}}


def response_options(metadata: dict[str, Any], role: str) -> dict[str, Any]:
    if metadata.get(f"{role}_response_constraint") == JSON_CONSTRAINT:
        return {"tool_choice": "none", "response_format": {"type": "json_object"}}
    return {}
