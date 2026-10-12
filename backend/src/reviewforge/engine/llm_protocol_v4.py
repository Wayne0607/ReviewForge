"""Per-request v4 output options; never mutate a shared model or legacy route."""

from __future__ import annotations

from typing import Any


def request_options(
    model: str, agent: str, *, closing: bool = False, json_mode: str = "auto", closure_thinking: str = "auto"
) -> dict[str, Any]:
    if json_mode not in {"auto", "off", "json_object"} or closure_thinking not in {"auto", "default", "disabled"}:
        raise ValueError("unsupported v4 output mode")
    deepseek = model.lower().startswith("deepseek-")
    final_output = agent != "investigator" or closing
    options: dict[str, Any] = {}
    if final_output and (json_mode == "json_object" or (json_mode == "auto" and deepseek)):
        options["response_format"] = {"type": "json_object"}
    if deepseek and final_output and (agent != "investigator" or closure_thinking in {"auto", "disabled"}):
        options.update(extra_body={"thinking": {"type": "disabled"}}, reasoning_effort=None)
    return options
