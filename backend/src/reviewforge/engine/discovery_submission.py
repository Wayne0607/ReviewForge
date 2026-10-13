"""Flat native discovery records; source and coverage validation stay downstream.

Native calls are output records, never executable repository tools. Required
primitive fields avoid nullable nested objects and empty candidate fields on
clean/deferred records. One malformed call prevents complete coverage, while
individually complete candidates can still reach the original strict consumer.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from langchain_core.runnables.base import RunnableBindingBase
from langchain_core.tools import StructuredTool
from pydantic import ConfigDict, Field, ValidationError, create_model

from reviewforge.engine.discovery import DiscoveryBlock
from reviewforge.engine.discovery_aliases import target_site_choices
from reviewforge.engine.discovery_output import DiscoveryOutput
from reviewforge.engine.hypothesis import Mechanism
from reviewforge.engine.native_submission import inspect_tool_calls


def discovery_submission_tools(block: DiscoveryBlock, aliases: dict[str, str]) -> list[StructuredTool]:
    """Build provider shapes before budget planning; membership stays local.

    Unit IDs are a small literal vocabulary. Source/site aliases already appear
    beside delivered source: repeating their large enums costs the same budget
    again. A provider sees the rN pattern, while decoding must check the actual
    block alias registry and per-unit site set before the original source checks.
    """
    unit_type = Literal[tuple(unit.id for unit in block.units)]
    config = ConfigDict(extra="forbid", strict=True)

    def prose():
        return str, Field(min_length=1, max_length=600)

    def tool(name: str, fields: dict[str, Any]) -> StructuredTool:
        schema = create_model(name.title().replace("_", ""), __config__=config, **fields)

        def submit(**kwargs: Any) -> str:
            raise RuntimeError("discovery submission is an output format, not an executable tool")

        return StructuredTool.from_function(
            func=submit,
            name=name,
            description="Submit one exact-source-backed record for a current discovery target.",
            args_schema=schema,
        )

    defer = tool("defer_unit", {"unit_id": (unit_type, ...), "reason": prose()})
    if not aliases:
        # No empty enum, invented source or clean fallback when material is absent.
        return [defer]
    reference_type = Annotated[str, Field(pattern=r"^r[0-9]+$")]
    sites = tuple(
        dict.fromkeys(alias for unit in block.units for alias in target_site_choices(block, unit.id, aliases))
    )
    common = {
        "unit_id": (unit_type, ...),
        "before": prose(),
        "after": prose(),
        "observable": prose(),
        "source_refs": (list[reference_type], Field(min_length=1, max_length=12)),
    }
    if block.structural_changes:
        # Text-only targets have no structural facts to select. Do not expose
        # irrelevant path choices and then ask the model to leave them empty.
        # Mixed blocks still require an explicit [] for their text targets.
        common["change_fields"] = (list[Literal["previous_path", "path"]], Field(max_length=2))
    hypothesis = {
        **common,
        "mechanism": (Literal[tuple(value.value for value in Mechanism)], ...),
        "severity": (Literal["error", "warning", "info"], ...),
        "source_refs": (list[reference_type], Field(min_length=2, max_length=13)),
        "source_roles": (list[Literal["behavior", "trigger"]], Field(min_length=2, max_length=13)),
        "site_refs": (list[reference_type], Field(max_length=len(sites))),
        **{field: prose() for field in ("claim", "trigger", "impact", "open_question", "refutation", "discrepancy")},
    }
    return [tool("submit_hypothesis", hypothesis), tool("submit_no_issue", {**common, "checked": prose()}), defer]


def bind_discovery_submission(llm: Any, tools: list[StructuredTool]) -> Any:
    """Preserve prior request options and bind the exact client-validated shape.

    Delegating bind_tools through a RunnableBinding can lose its request kwargs.
    Providers still own wire conversion, and every wrapper remains on the call
    path. StructuredTool's inferred subset schema can omit extra=forbid; deliver
    the original required primitive schema instead, without provider strict flags.
    """
    original = llm
    while isinstance(llm, RunnableBindingBase):
        llm = llm.bound
    wire = [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.args_schema.model_json_schema(),
            },
        }
        for tool in tools
    ]
    bound = llm.bind_tools(wire, tool_choice="any")
    options = getattr(bound, "kwargs", None)
    if not isinstance(options, dict):
        raise TypeError("provider submission tools must return bindable request arguments")
    return original.bind(**options)


def _record_payload(
    name: str, args: dict[str, Any], block: DiscoveryBlock, aliases: dict[str, str]
) -> tuple[str, dict[str, Any]]:
    if any(isinstance(value, str) and not value.strip() for value in args.values()):
        raise ValueError("empty-discovery-prose")
    unit_id = args["unit_id"]
    if name == "defer_unit":
        return "deferred_units", {"unit_id": unit_id, "reason": args["reason"]}
    if any(reference not in aliases for reference in args["source_refs"]):
        raise ValueError("unknown-discovery-source-alias")
    if block.structural_changes:
        if "change_fields" not in args:
            raise ValueError("missing-discovery-change-selection")
        fields = args["change_fields"]
    else:
        if "change_fields" in args:
            raise ValueError("unexpected-discovery-change-selection")
        fields = []
    change = block.structural_changes.get(unit_id)
    if change is None and fields or change is not None and set(fields) != {"previous_path", "path"}:
        raise ValueError("invalid-discovery-change-selection")
    references = args["source_refs"]
    trigger = None
    if name == "submit_hypothesis":
        allowed_sites = set(target_site_choices(block, unit_id, aliases))
        if any(reference not in allowed_sites for reference in args["site_refs"]):
            raise ValueError("invalid-discovery-site-alias")
        roles = args["source_roles"]
        if len(roles) != len(references) or roles.count("trigger") != 1 or not 1 <= roles.count("behavior") <= 12:
            raise ValueError("invalid-discovery-source-roles")
        trigger = references[roles.index("trigger")]
        references = [reference for reference, role in zip(references, roles, strict=True) if role == "behavior"]
    behavior = {field: args[field] for field in ("before", "after", "observable")} | {
        "source_facts": [{"source_ref": reference} for reference in references]
    }
    if change is not None:
        # Selection is explicit. Values and identity come only from trusted state;
        # the existing exact change-fact/consumer validation is still mandatory.
        behavior["change_facts"] = [
            {"kind": "file-change", "change_id": change.id, "field": field, "value": getattr(change, field)}
            for field in fields
        ]
    item = {"unit_id": unit_id, "behavior": behavior}
    if name == "submit_no_issue":
        return "no_issue_units", item | {"checked": args["checked"]}
    return "hypotheses", item | {
        **{
            field: args[field]
            for field in (
                "mechanism",
                "severity",
                "claim",
                "trigger",
                "impact",
                "open_question",
                "refutation",
                "discrepancy",
            )
        },
        "trigger_basis": {"source_ref": trigger},
        "sites": [{"source_ref": reference} for reference in args["site_refs"]],
    }


def parse_discovery_submission(
    response: Any, tools: list[StructuredTool], block: DiscoveryBlock, aliases: dict[str, str]
) -> tuple[DiscoveryOutput | None, list[str]]:
    """Decode native calls without coercion, repair, or a second model request.

    None means there is no native response: the caller may strictly parse the
    original text in that same receipt, including its complete-record framing.
    V4 never retries a different formatting payload. Native errors must never
    take the text fallback, even when accompanying prose would parse as JSON.
    """
    extra = getattr(response, "additional_kwargs", {}) or {}
    native = (
        bool(getattr(response, "tool_calls", None))
        or bool(getattr(response, "invalid_tool_calls", None))
        or any(
            key in extra and extra[key] is not None and (not isinstance(extra[key], list) or bool(extra[key]))
            for key in ("reviewforge_raw_tool_calls", "tool_calls")
        )
    )
    if not native:
        return None, []
    calls, errors = inspect_tool_calls(response)
    metadata = getattr(response, "response_metadata", {}) or {}
    if metadata.get("finish_reason") in {"length", "max_tokens"} or metadata.get("stop_reason") == "max_tokens":
        errors.append("truncated-native-discovery")
    schemas = {tool.name: tool.args_schema for tool in tools}
    payload: dict[str, Any] = {name: [] for name in ("hypotheses", "no_issue_units", "deferred_units")}
    if not calls and not errors:
        errors.append("empty-native-discovery")
    content = getattr(response, "content", "")
    if content.strip() if isinstance(content, str) else content:
        errors.append("mixed-native-discovery-text")
    for call in calls:
        name = call["name"]
        if name not in schemas:
            errors.append("unknown-discovery-tool")
            continue
        try:
            args = schemas[name].model_validate(call["args"]).model_dump()
            collection, item = _record_payload(name, args, block, aliases)
        except (ValidationError, ValueError, TypeError):
            errors.append("invalid-native-discovery-record")
            continue
        payload[collection].append(item)
    return DiscoveryOutput(payload, not errors, "partial-native-tool-calls" if errors else "native-tool-calls"), errors
