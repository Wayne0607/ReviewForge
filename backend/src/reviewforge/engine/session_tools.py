"""Small native result actions with program-bound work/case identity."""

from __future__ import annotations

import json
from typing import Annotated, Any, Literal

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, ConfigDict, Field, create_model, model_validator

from reviewforge.engine.hypothesis import Mechanism
from reviewforge.engine.review_session import fingerprint

MAX_RESULT_BYTES = 32_768


class ResultRecord(BaseModel):
    """Bound the transport record, rather than the length of its reasoning.

    Protocols 1–8 retain their original per-field schemas. In protocol 9 the
    finite provider output and this whole-record guard bound memory; cosmetic
    length never substitutes for case identity or evidence validation.
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    @model_validator(mode="before")
    @classmethod
    def bounded_record(cls, value):
        if len(json.dumps(value, ensure_ascii=False).encode("utf-8")) > MAX_RESULT_BYTES:
            raise ValueError("result-transport-record-too-large")
        return value


def result_tool(name: str, description: str, fields: dict[str, Any], *, bounded_record=False) -> StructuredTool:
    options = {"__base__": ResultRecord} if bounded_record else {"__config__": ConfigDict(extra="forbid", strict=True)}
    schema = create_model(name.title().replace("_", ""), **options, **fields)

    def submit(**kwargs):
        raise RuntimeError("Result actions are decoded by the controller, never executed")

    return StructuredTool.from_function(func=submit, name=name, description=description, args_schema=schema)


def prose(protocol_version=5):
    return str, Field(min_length=1, **({"max_length": 600} if protocol_version < 9 else {}))


def discovery_tools(block, *, protocol_version=5):
    unit = Literal[tuple(item.id for item in block.units)]
    ref = Annotated[str, Field(pattern=r"^f[0-9]+\.[BRC][0-9]+(?:\.s[0-9]+)?$")]
    common = {
        "unit_id": (unit, ...),
        "source_refs": (list[ref], Field(min_length=1, **({"max_length": 12} if protocol_version < 10 else {}))),
    }
    if block.structural_changes:
        common["change_fields"] = (list[Literal["previous_path", "path"]], Field(max_length=2))
    return [
        result_tool(
            "submit_hypothesis",
            "Submit one source-backed OPEN lead, not a confirmed defect.",
            {
                **common,
                "mechanism": (Literal[tuple(item.value for item in Mechanism)], ...),
                "severity": (Literal["error", "warning", "info"], ...),
                **{
                    name: prose(protocol_version)
                    for name in ("claim", "trigger", "impact", "open_question", "refutation", "discrepancy")
                },
                "site_refs": (list[ref], Field(max_length=1)),
                "trigger_ref": (ref, ...),
            },
            bounded_record=protocol_version == 9,
        ),
        result_tool(
            "submit_no_issue",
            "Finish one unit with an actual source-backed checked boundary.",
            {
                **common,
                "checked": prose(protocol_version),
            },
            bounded_record=protocol_version == 9,
        ),
        result_tool(
            "defer_unit",
            "Finish an unavailable boundary explicitly; it remains uncovered.",
            {
                "unit_id": (unit, ...),
                "reason": prose(protocol_version),
            },
            bounded_record=protocol_version == 9,
        ),
    ]


def case_identity(hypothesis, head_sha: str) -> str:
    return (
        "case_"
        + fingerprint(
            {
                "head_sha": head_sha,
                "id": hypothesis.id,
                **{name: getattr(hypothesis, name) for name in ("claim", "trigger", "impact")},
            }
        )[:24]
    )


def decision_tools(hypothesis, head_sha: str, *, protocol_version=5):
    case = (Literal[case_identity(hypothesis, head_sha)], ...)
    refs = (list[str], Field(min_length=1, **({"max_length": 8} if protocol_version < 10 else {})))
    common = {"case_id": case, "reason": prose(protocol_version)}
    return [
        result_tool(
            "submit_confirmed",
            "Submit a conflicting expected/actual proof for the ORIGINAL bound case.",
            {
                **common,
                "expected": prose(protocol_version),
                "actual": prose(protocol_version),
                "expected_evidence": refs,
                "actual_evidence": refs,
            },
            bounded_record=protocol_version == 9,
        ),
        result_tool(
            "submit_refuted",
            "Submit positive counterevidence disproving one original necessary premise.",
            {
                **common,
                "premise": (Literal["trigger", "mechanism", "impact", "regression"], ...),
                "statement": prose(protocol_version),
                "evidence": refs,
            },
            bounded_record=protocol_version == 9,
        ),
        result_tool(
            "submit_unknown",
            "Stop when a particular missing fact cannot be acquired in this session.",
            {
                **common,
                "missing_fact": prose(protocol_version),
            },
            bounded_record=protocol_version == 9,
        ),
    ]


def decision_payload(name: str, args: dict[str, Any], hypothesis) -> dict[str, Any]:
    """Only validated, case-bound actions reach this mapping; prose cannot vote."""
    payload = {
        "assessed_trigger": hypothesis.trigger,
        "reason": args["reason"],
        "severity": hypothesis.severity,
        "assessment": None,
        "counterevidence": None,
    }
    if name == "submit_confirmed":
        payload["assessment"] = {
            "expected": args["expected"],
            "actual": args["actual"],
            "comparison": "conflict",
            "expected_evidence": args["expected_evidence"],
            "actual_evidence": args["actual_evidence"],
        }
    elif name == "submit_refuted":
        payload["counterevidence"] = {key: args[key] for key in ("premise", "statement", "evidence")}
    elif name == "submit_unknown":
        payload["reason"] = args["missing_fact"]
    else:
        raise ValueError("unknown-case-result")
    return payload
