"""Small native result actions with program-bound work/case identity."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from langchain_core.tools import StructuredTool
from pydantic import ConfigDict, Field, create_model

from reviewforge.engine.hypothesis import Mechanism
from reviewforge.engine.review_session import fingerprint


def result_tool(name: str, description: str, fields: dict[str, Any]) -> StructuredTool:
    schema = create_model(name.title().replace("_", ""), __config__=ConfigDict(extra="forbid", strict=True), **fields)

    def submit(**kwargs):
        raise RuntimeError("Result actions are decoded by the controller, never executed")

    return StructuredTool.from_function(func=submit, name=name, description=description, args_schema=schema)


def prose():
    return str, Field(min_length=1, max_length=600)


def discovery_tools(block):
    unit = Literal[tuple(item.id for item in block.units)]
    ref = Annotated[str, Field(pattern=r"^f[0-9]+\.[BRC][0-9]+(?:\.s[0-9]+)?$")]
    common = {"unit_id": (unit, ...), "source_refs": (list[ref], Field(min_length=1, max_length=12))}
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
                    name: prose()
                    for name in ("claim", "trigger", "impact", "open_question", "refutation", "discrepancy")
                },
                "site_refs": (list[ref], Field(max_length=1)),
                "trigger_ref": (ref, ...),
            },
        ),
        result_tool(
            "submit_no_issue",
            "Finish one unit with an actual source-backed checked boundary.",
            {
                **common,
                "checked": prose(),
            },
        ),
        result_tool(
            "defer_unit",
            "Finish an unavailable boundary explicitly; it remains uncovered.",
            {
                "unit_id": (unit, ...),
                "reason": prose(),
            },
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


def decision_tools(hypothesis, head_sha: str):
    case = (Literal[case_identity(hypothesis, head_sha)], ...)
    refs = (list[str], Field(min_length=1, max_length=8))
    common = {"case_id": case, "reason": prose()}
    return [
        result_tool(
            "submit_confirmed",
            "Submit a conflicting expected/actual proof for the ORIGINAL bound case.",
            {
                **common,
                "expected": prose(),
                "actual": prose(),
                "expected_evidence": refs,
                "actual_evidence": refs,
            },
        ),
        result_tool(
            "submit_refuted",
            "Submit positive counterevidence disproving one original necessary premise.",
            {
                **common,
                "premise": (Literal["trigger", "mechanism", "impact", "regression"], ...),
                "statement": prose(),
                "evidence": refs,
            },
        ),
        result_tool(
            "submit_unknown",
            "Stop when a particular missing fact cannot be acquired in this session.",
            {
                **common,
                "missing_fact": prose(),
            },
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
