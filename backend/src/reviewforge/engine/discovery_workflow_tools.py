"""One native envelope, independently validated typed assessment records."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, create_model

from reviewforge.engine.atomic_candidates import atomic_proposal_schema
from reviewforge.engine.session_tools import discovery_tools, result_tool

KINDS = {"hypothesis": "submit_hypothesis", "no_issue": "submit_no_issue", "deferred": "defer_unit"}
_PROGRESS_EXEMPT = {"submit_assessments", "repair_assessments", "checkpoint_review_progress"}


class DiscoveryReviewProgress(BaseModel):
    """Visible investigation state, never provider reasoning or source proof."""

    model_config = ConfigDict(extra="forbid", strict=True)

    summary: str = Field(min_length=1)
    open_questions: list[str]
    resolved_questions: list[str]
    next_action: str = Field(min_length=1)
    evidence_ids: list[str]


class DiscoveryReviewProgressV13(DiscoveryReviewProgress):
    """One local progress fragment, preserved in an ordered navigation journal."""

    summary: str = Field(
        min_length=1,
        description="The local topic of this action. Prior topics remain in progress_journal; "
        "this fragment does not replace all prior progress or establish source truth.",
    )
    open_questions: list[str] = Field(
        description="Questions still open for this local topic, not a complete task inventory."
    )
    resolved_questions: list[str] = Field(
        description="Locally addressed questions as untrusted navigation. A correction must identify the earlier "
        "specific proposition/event; it does not erase unrelated journal entries."
    )
    next_action: str = Field(
        min_length=1,
        description="The next missing fact or independent ready submission, "
        "considering existing source and journal history.",
    )
    evidence_ids: list[str] = Field(
        description="Existing immutable ledger evidence IDs beginning ev_..., "
        "never obs_N audit IDs or fN source aliases. "
        "An empty array is allowed; notes themselves are not evidence."
    )


def requires_review_progress(name):
    return name not in _PROGRESS_EXEMPT


def with_review_progress(tools, *, protocol_version):
    if protocol_version < 12:
        return tools
    progress_schema = DiscoveryReviewProgressV13 if protocol_version >= 13 else DiscoveryReviewProgress
    description = (
        " Include a local review_progress fragment; previous topics remain ordered in progress_journal. "
        "Identify specific earlier propositions when correcting them. evidence_ids are existing ev_... IDs, "
        "not obs_N audit IDs, and may be empty. Notes are navigation, never source proof or private reasoning."
        if protocol_version >= 13
        else " Include review_progress describing already established facts, "
        "open/resolved questions and the next requested action. Evidence IDs may be empty. "
        "This is visible navigation, never source proof or private reasoning."
    )
    return [
        tool.model_copy(
            update={
                "args_schema": create_model(
                    tool.args_schema.__name__ + "WithReviewProgress",
                    __base__=tool.args_schema,
                    review_progress=(progress_schema, ...),
                ),
                "description": tool.description + description,
            }
        )
        if requires_review_progress(tool.name)
        else tool
        for tool in tools
    ]


def extract_review_progress(tool, args):
    validated = tool.args_schema.model_validate(args)
    progress = validated.review_progress.model_dump()
    # Preserve original source argument types: broker validation rejects bool
    # and fractional coordinates before Pydantic can coerce them to integers.
    source_args = {key: value for key, value in args.items() if key != "review_progress"}
    return source_args, progress


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
    if protocol_version >= 11:
        atomic_record = atomic_proposal_schema(hypothesis_record)
        records["atomic_hypotheses"] = atomic_record
        record = Annotated[
            hypothesis_record | clean_record | deferred_record | atomic_record, Field(discriminator="kind")
        ]
    tool = result_tool(
        "submit_assessments",
        "Submit independently validated pending records. Use atomic_hypotheses for independent consequences."
        if protocol_version >= 11
        else "Submit records for ALL pending units in one call. Each record is a hypothesis, no_issue or deferred.",
        {
            "records": (
                list[record],
                Field(min_length=1, **({"max_length": max(4, 2 * len(block.units))} if protocol_version < 10 else {})),
            )
        },
    )
    return tool, records
