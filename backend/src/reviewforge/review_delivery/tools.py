"""Native tool definitions for the finding delivery agents."""


def spec(name, description, properties=None, required=()):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties or {},
                "required": list(required),
                "additionalProperties": False,
            },
        },
    }


STR = {"type": "string"}
REFS = {"type": "array", "items": STR}
CLAIM = {
    "type": "object",
    "properties": {"title": STR, "body": STR},
    "required": ["title", "body"],
    "additionalProperties": False,
    "description": "Title and Markdown body describing the concrete trigger and impact.",
}
COMMON = [
    spec(
        "check_text",
        "Check an exact case-sensitive literal across every file matching a glob in pinned head/base, "
        "including files skipped by search. Returns an auditable check artifact with file hashes, matches "
        "and gaps. A complete zero-match result proves only this literal is absent in this scope; "
        "you must establish that this scope/predicate is sufficient for your semantic claim. "
        "Binary/unreadable files and empty scopes cannot prove absence.",
        {"glob": STR, "literal": STR, "side": {"type": "string", "enum": ["head", "base"]}},
        ["glob", "literal"],
    ),
    spec(
        "set_progress",
        "Update your current investigation focus and next steps; this is navigation, not evidence.",
        {"focus": STR, "next_steps": STR, "finding_ids": REFS},
        ["focus", "next_steps"],
    ),
    spec("read_progress", "Read prior focus and next-step snapshots from this session; they are navigation."),
    spec(
        "retain_source",
        "Keep a source already delivered to this session in its working set for cross-batch comparison.",
        {"source_id": STR},
        ["source_id"],
    ),
    spec(
        "release_source",
        "Remove a source from this session's active working set; it remains saved and readable.",
        {"source_id": STR},
        ["source_id"],
    ),
    spec("list_sources", "List saved source identities and provenance; read originals to inspect them."),
    spec(
        "read_saved_source",
        "Read an original saved source by ID. Notes and search results are navigation, not proof.",
        {"source_id": STR},
        ["source_id"],
    ),
    spec("read_notes", "Read shared research notes, which are untrusted navigation.", {"task_id": STR}),
    spec("changed_notes", "List research tasks with notes added since this session last read them."),
    spec(
        "add_note",
        "Save a navigation note for other agents.",
        {"task_id": STR, "text": STR, "source_ids": REFS},
        ["text"],
    ),
]
LEAD = [
    spec("list_findings", "List this run's findings and their current dispositions."),
    spec(
        "submit_finding",
        "Submit a distinct defect for independent refutation. Use update_finding for a revision of the same defect.",
        {"claim": CLAIM, "source_ids": REFS},
        ["claim", "source_ids"],
    ),
    spec(
        "update_finding",
        "Revise the same finding, preserving its identity.",
        {"finding_id": STR, "claim": CLAIM, "source_ids": REFS, "same_defect_reason": STR},
        ["finding_id", "claim", "source_ids", "same_defect_reason"],
    ),
    spec(
        "challenge_finding",
        "Reopen a specific finding when newly obtained counterevidence is relevant.",
        {"finding_id": STR, "reason": STR, "source_ids": REFS},
        ["finding_id", "reason", "source_ids"],
    ),
    spec(
        "retry_verification",
        "Retry a finding blocked on evidence after obtaining the missing information.",
        {"finding_id": STR},
        ["finding_id"],
    ),
    spec("delegate_research", "Ask a focused assistant a concrete research question.", {"question": STR}, ["question"]),
    spec("read_research", "Read a delegated research result.", {"task_id": STR}, ["task_id"]),
    spec(
        "finish_review",
        "Finish the review. For each remaining gap, explain the missing fact, checked sources, "
        "and request the specific authoritative input that would permit recovery. Use free text; "
        "absence alone is not a recovery condition.",
        {"summary": STR, "gaps": {"type": "array", "items": STR}},
        ["summary", "gaps"],
    ),
]
RESEARCH = [
    spec(
        "finish_research",
        "Return an answer to the delegated question with original source IDs and concrete gaps. "
        "For each gap, identify the missing fact, checked sources and specific input needed to resume.",
        {"answer": STR, "source_ids": REFS, "gaps": {"type": "array", "items": STR}},
        ["answer", "source_ids", "gaps"],
    )
]
VERIFY = [
    spec(
        "submit_verdict",
        "After attempting to refute the finding, judge it using originals received in this session. "
        "Declare every absence premise needed by accept/reject in absence_claims (otherwise []). "
        "Each requires an explicit bounded scope and complete original files or a complete zero-match "
        "check_text artifact, all cited in source_ids. Partial searches/excerpts cannot prove absence. "
        "For needs_evidence specify the missing fact, attempted sources and recovery condition.",
        {
            "verdict": {"type": "string", "enum": ["accept", "reject", "needs_evidence"]},
            "source_ids": REFS,
            "reason": STR,
            "absence_claims": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"fact": STR, "scope": STR, "source_ids": REFS},
                    "required": ["fact", "scope", "source_ids"],
                    "additionalProperties": False,
                },
            },
            "missing_fact": STR,
            "attempted": REFS,
            "recovery_condition": STR,
        },
        ["verdict", "source_ids", "reason", "absence_claims"],
    )
]
