"""Investigation handoffs are navigation; only verified raw bytes are citable."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace

from langchain_core.tools import StructuredTool

from reviewforge.engine.hypothesis import Observation
from reviewforge.engine.investigation_evidence import READ_ONLY_TOOLS

WORKSPACE_ACTIONS = frozenset({"checkpoint_investigation", "query_review_workspace", "read_shared_evidence"})


def error_reply(code):
    return json.dumps({"status": "error", "error": {"code": code}}, ensure_ascii=False)


def action_feedback(reply):
    """Decode controller-owned receipts without interpreting source or note prose."""
    try:
        value = json.loads(reply)
    except (ValueError, TypeError):
        value = None
    if isinstance(value, dict):
        if set(value) == {"status", "result"} and value["status"] == "success" and isinstance(value["result"], dict):
            return {"result": value["result"]}
        if set(value) == {"status", "error"} and value["status"] == "error" and isinstance(value["error"], dict):
            return {"error": value["error"]}
    return {"error": {"code": "invalid-shared-workspace-receipt"}}


def tools(worker):
    async def checkpoint_investigation(
        summary: str,
        open_questions: list[str],
        resolved_questions: list[str],
        next_action: str,
        evidence_ids: list[str],
    ) -> str:
        """Save untrusted progress, unresolved questions and a concrete next step for other tasks."""
        return await worker._run_tool(
            "checkpoint_investigation",
            {
                "summary": summary,
                "open_questions": open_questions,
                "resolved_questions": resolved_questions,
                "next_action": next_action,
                "evidence_ids": evidence_ids,
            },
        )

    async def read_shared_evidence(evidence_ids: list[str]) -> str:
        """Read verified raw evidence and create your own locally citable observations."""
        return await worker._run_tool("read_shared_evidence", {"evidence_ids": evidence_ids})

    async def query_review_workspace(paths: list[str], unit_ids: list[str]) -> str:
        """Find shared notes and raw evidence for dependency paths or units in this fixed run."""
        return await worker._run_tool("query_review_workspace", {"paths": paths, "unit_ids": unit_ids})

    return [
        StructuredTool.from_function(coroutine=checkpoint_investigation),
        StructuredTool.from_function(coroutine=read_shared_evidence),
        StructuredTool.from_function(coroutine=query_review_workspace),
    ]


def _identity(hypothesis):
    return hypothesis.investigation_task_id or hypothesis.identity


def publish_progress(workspace, store, hypothesis, observations, progress, *, result=None):
    """Persist source first; a model note never enters the evidence store."""
    evidence_ids = [store.observation(item) for item in observations if item.status == "success"]
    checkpoint = next((item for item in reversed(progress) if item.get("phase") == "shared-checkpoint"), {})
    evidence_ids = list(dict.fromkeys([*checkpoint.get("evidence_ids", []), *evidence_ids]))
    fields = {
        "summary": checkpoint.get("summary") or "Investigation in progress: " + hypothesis.open_question,
        "open_questions": checkpoint.get("open_questions", [hypothesis.open_question]),
        "resolved_questions": checkpoint.get("resolved_questions", []),
        "next_action": checkpoint.get("next_action", "Read evidence for the remaining proof premise."),
    }
    if result is not None:
        outcome = result.answer or result.reason or "Investigation result: " + result.verdict
        fields["summary"] = (checkpoint["summary"] + "\n" + outcome) if checkpoint.get("summary") else outcome
        fields["next_action"] = (
            "Independent source and proof verification is still required."
            if result.verdict == "confirmed"
            else checkpoint.get("next_action") or "Continue the unresolved evidence question."
            if result.verdict == "unknown"
            else ""
        )
        if result.verdict != "unknown":
            fields["resolved_questions"] = list(
                dict.fromkeys([*fields["resolved_questions"], hypothesis.open_question])
            )
            fields["open_questions"] = [q for q in fields["open_questions"] if q != hypothesis.open_question]
    return workspace.publish_progress(
        task_id=_identity(hypothesis),
        actor="investigation",
        unit_ids=[hypothesis.unit_id],
        paths=[site.path for site in hypothesis.sites],
        evidence_ids=evidence_ids,
        **fields,
    )


def _raw_observations(record, head_sha):
    if record["kind"] == "tool-observation":
        raw = dict(record["observation"])
        if raw.get("tool") not in READ_ONLY_TOOLS or raw.get("status") != "success" or raw.get("sha") != head_sha:
            raise ValueError("shared evidence is not successful pinned source")
        for key in ("line_range", "excerpt_line_range"):
            if raw.get(key) is not None:
                raw[key] = tuple(raw[key])
        return [Observation(id="", **raw)]
    if record["kind"] == "delivered-source":
        if record.get("side") not in {"after", "context"} or record.get("source_sha") != head_sha:
            raise ValueError("before-source cannot be rebound as pinned head source; use read_diff")
        text, line = record["excerpt"], record["line"]
        from reviewforge.engine.source_navigation import _pieces

        return [
            Observation(
                id="",
                tool="read_file",
                query="read_file " + json.dumps({"path": record["path"], "start": line, "end": line}, sort_keys=True),
                path=record["path"],
                line_range=(line, line),
                sha=head_sha,
                result_digest=hashlib.sha256(text.encode()).hexdigest()[:16],
                excerpt=part,
                status="success",
                excerpt_line_range=span,
            )
            for part, span in _pieces(text, line)
        ]
    raise ValueError("unsupported shared evidence kind")


async def run_action(worker, name, args):
    workspace, hypothesis = worker._shared_workspace, worker._current_hypothesis
    if workspace is None or hypothesis is None or worker._evidence_store is None:
        return error_reply("shared-investigation-workspace-unavailable")
    try:
        if name == "query_review_workspace":
            return json.dumps(
                {
                    "status": "success",
                    "result": workspace.view(
                        task_id=worker._task_id or _identity(hypothesis),
                        role="investigation",
                        **args,
                    ),
                },
                ensure_ascii=False,
            )
        if name == "checkpoint_investigation":
            note = workspace.publish_progress(
                task_id=worker._task_id or _identity(hypothesis),
                actor="investigation",
                unit_ids=[hypothesis.unit_id],
                paths=[site.path for site in hypothesis.sites],
                **args,
            )
            worker._progress.append({"phase": "shared-checkpoint", **args})
            result = {"note": note, "trust": "untrusted-navigation-only"}
        else:
            records = workspace.read_evidence(args["evidence_ids"])
            # Reject the complete batch before creating any consumer-local references.
            observations = [_raw_observations(record, worker._state.head_sha) for record in records]
            replies = []
            fragments = [
                (record, index, observation)
                for record, group in zip(records, observations, strict=True)
                for index, observation in enumerate(group)
            ]
            for record, fragment, observation in fragments:
                prior = next(
                    (
                        entry
                        for entry in worker._progress
                        if entry.get("phase") == "shared-evidence-import"
                        and entry.get("evidence_id") == record["evidence_id"]
                        and entry.get("source_fragment", 0) == fragment
                    ),
                    None,
                )
                if prior is not None:
                    saved = next(item for item in worker._observations if item.id == prior["observation_id"])
                else:
                    saved = replace(observation, id=f"obs_{worker._obs_counter}")
                    worker._obs_counter += 1
                    worker._evidence_store.observation(saved)
                    worker._observations.append(saved)
                    worker._progress.append(
                        {
                            "phase": "shared-evidence-import",
                            "evidence_id": record["evidence_id"],
                            "observation_id": saved.id,
                            "source_fragment": fragment,
                        }
                    )
                    if saved.tool == "read_diff":
                        worker._progress.append(
                            {
                                "phase": "diff-navigation",
                                "observation_id": saved.id,
                                "hunk_header": "shared saved diff fragment",
                                "complete_hunk": False,
                                "before_after_complete": False,
                            }
                        )
                replies.append(
                    {
                        "evidence_id": record["evidence_id"],
                        "observation_id": saved.id,
                        "saved_evidence": worker._observation_reply(saved),
                    }
                )
            result = {"evidence": replies}
    except (ValueError, KeyError, TypeError):
        return error_reply("invalid-shared-workspace-action-or-evidence-binding")
    if worker._on_progress is not None:
        await worker._on_progress(worker._observations, worker._progress)
    return json.dumps({"status": "success", "result": result}, ensure_ascii=False)
