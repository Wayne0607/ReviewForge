"""One native envelope, independently validated typed assessment records."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, create_model

from reviewforge.engine.session_tools import discovery_tools, result_tool

KINDS = {"hypothesis": "submit_hypothesis", "no_issue": "submit_no_issue", "deferred": "defer_unit"}


def assessment_tools(block, *, protocol_version=6):
    originals = {tool.name: tool.args_schema for tool in discovery_tools(block, protocol_version=protocol_version)}
    records = {
        kind: create_model(
            kind.title() + "Assessment",
            __base__=originals[name],
            kind=(Literal[kind], ...),
            **(
                {
                    "discrepancy": (
                        str | None,
                        Field(default=None, min_length=1, **({"max_length": 600} if protocol_version < 9 else {})),
                    )
                }
                if protocol_version >= 7 and kind == "hypothesis"
                else {}
            ),
        )
        for kind, name in KINDS.items()
    }
    hypothesis_record, clean_record, deferred_record = records.values()
    record = Annotated[hypothesis_record | clean_record | deferred_record, Field(discriminator="kind")]
    tool = result_tool(
        "submit_assessments",
        "Submit records for ALL pending units in one call. Each record is a hypothesis, no_issue or deferred.",
        {"records": (list[record], Field(min_length=1, max_length=max(4, 2 * len(block.units))))},
    )
    return tool, records
