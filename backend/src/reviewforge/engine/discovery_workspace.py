"""Protocol 11 discovery handoffs and source-based progress transitions.

Shared notes preserve investigative context, but only validated raw records can
enter the consumer's source registry. Repetition uses acquired facts and stable
controller outcomes, never model prose, observation identities or read windows.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import replace

from pydantic import Field

from reviewforge.engine.discovery import SourceLine
from reviewforge.engine.discovery_session import extend_source_aliases, extend_sources, restore_observation
from reviewforge.engine.hypothesis import Observation
from reviewforge.engine.investigation_evidence import READ_ONLY_TOOLS
from reviewforge.engine.review_session import fingerprint
from reviewforge.engine.session_tools import result_tool

WORKSPACE_ACTIONS = {"checkpoint_review_progress", "read_shared_evidence", "query_review_workspace"}


def workspace_tools():
    return [
        result_tool(
            "checkpoint_review_progress",
            "Save investigation notes and a handoff. Notes are navigation, never evidence or a unit verdict.",
            {
                "summary": (str, ...),
                "open_questions": (list[str], ...),
                "resolved_questions": (list[str], ...),
                "next_action": (str, ...),
                "evidence_ids": (list[str], ...),
            },
        ),
        result_tool(
            "read_shared_evidence",
            "Read immutable evidence from this run and establish local observations and source references.",
            {"evidence_ids": (list[str], Field(min_length=1))},
        ),
        result_tool(
            "query_review_workspace",
            "Find existing related investigation notes and evidence before acquiring a dependency again.",
            {"paths": (list[str], ...), "unit_ids": (list[str], ...)},
        ),
    ]


def workspace_view(work):
    view = work.workspace.view(
        task_id=work.session.record["task_id"],
        unit_ids=[unit.id for unit in work.block.units],
        paths=sorted(
            {unit.path for unit in work.block.units} | {obs.path for obs in work.broker._observations if obs.path}
        ),
        role="discovery",
        catalog="directory",
    )
    # Source aliases already deliver this task's actual source. Automatic
    # inputs need a navigable directory, not another per-line evidence index.
    # Query a dependency explicitly for its complete immutable evidence IDs.
    view["local_observations"] = copy.deepcopy(work.record.get("workspace_observation_ids", {}))
    return view


def remember_evidence(work, identity, observation=None):
    ids = work.record.setdefault("workspace_evidence_ids", [])
    if identity not in ids:
        ids.append(identity)
    if observation is not None:
        work.record.setdefault("workspace_observation_ids", {})[observation.id] = identity


def workspace_action(work, name, args, store):
    schema = next(tool.args_schema for tool in workspace_tools() if tool.name == name)
    args = schema.model_validate(args).model_dump()
    if name == "query_review_workspace":
        return work.workspace.view(task_id=work.session.record["task_id"], role="discovery", **args)
    if name == "checkpoint_review_progress":
        note = work.workspace.publish_progress(
            task_id=work.session.record["task_id"],
            actor="discovery",
            unit_ids=[unit.id for unit in work.block.units],
            paths=[unit.path for unit in work.block.units],
            **args,
        )
        return {"note": note, "trust": "untrusted-navigation-only"}
    # Validate the whole request before mutating consumer-local evidence.
    records = work.workspace.read_evidence(args["evidence_ids"])
    prepared = [_prepare_record(record, store.ledger.head_sha) for record in records]
    imported = []
    for record, (source, observation) in zip(records, prepared, strict=True):
        identity = record["evidence_id"]
        if source is not None:
            work.block.facts.append(source)
            work.block.register_source_refs()
            extend_source_aliases(work.block, work.aliases)
            if source.side == "before":
                # Observation.sha is the pinned head throughout downstream
                # proof consumers; it cannot represent naked base bytes.
                # Preserve the explicitly sided discovery reference without
                # manufacturing head evidence for investigation/ProofGate.
                remember_evidence(work, identity)
                imported.append(
                    {
                        "evidence_id": identity,
                        "source_reference_only": True,
                        "record": copy.deepcopy(record),
                        "instruction": "Base source retains its before alias only. "
                        "Use read_diff for a local diff observation.",
                    }
                )
                continue
        else:
            extend_sources(work.block, observation, work.aliases)
        observation = replace(observation, id=_next_id(work))
        work.broker._observations.append(observation)
        remember_evidence(work, store.observation(observation), observation)
        imported.append({"evidence_id": identity, "observation_id": observation.id, "record": copy.deepcopy(record)})
    return {
        "evidence": imported,
        "instruction": "Source aliases are supplied in the next request; notes are not evidence.",
    }


def _prepare_record(record, head_sha):
    if record["kind"] == "tool-observation":
        observation = restore_observation(record["observation"] | {"id": ""})
        if observation.tool not in READ_ONLY_TOOLS or observation.status != "success" or observation.sha != head_sha:
            raise ValueError("shared-evidence-is-not-successful-pinned-source")
        return None, observation
    if record["kind"] != "delivered-source":
        raise ValueError("unsupported-shared-evidence-kind")
    source = SourceLine(record["side"], record["path"], record["line"], record["excerpt"], changed=record["changed"])
    if source.side == "before":
        return source, None
    if source.side not in {"after", "context"} or record["source_sha"] != head_sha:
        raise ValueError("shared-source-is-not-pinned-head")
    return source, Observation(
        id="",
        tool="read_file",
        query="read_file "
        + json.dumps({"path": source.path, "start": source.line, "end": source.line}, sort_keys=True),
        path=source.path,
        line_range=(source.line, source.line),
        sha=head_sha,
        result_digest=fingerprint(record),
        excerpt=source.text,
        status="success",
        excerpt_line_range=(source.line, source.line),
    )


def _next_id(work):
    identity = f"obs_{work.broker._obs_counter}"
    work.broker._obs_counter += 1
    return identity


def progress_state(work):
    """Unique fixed-version source locations and native navigation facts."""
    source = sorted(
        {
            ("before" if item.side == "before" else "head", item.path, item.line, item.text)
            for item in work.block.source_refs.values()
        }
    )
    navigation = sorted(_all_navigation_facts(work))
    accepted = {}
    for unit_id, records in work.flow.get("accepted_records", {}).items():
        for record in records:
            entry = {key: record[key] for key in ("kind", "trigger_ref", "mechanism") if key in record} | {
                "unit_id": unit_id
            }
            for key in ("source_refs", "site_refs"):
                if key in record:
                    entry[key] = sorted(set(record[key]))
            accepted[fingerprint(entry)] = entry
    return {
        "source": source,
        "navigation": navigation,
        "pending": sorted(work.pending),
        "accepted": [accepted[key] for key in sorted(accepted)],
        "atomic_accepted": {
            identity: sorted(entry["accepted"])
            for identity, entry in work.flow.get("atomic_proposals", {}).items()
            if entry["accepted"]
        },
        "finished": {key: row.get("status") for key, row in work.record["units"].items() if row.get("finished")},
    }


def _all_navigation_facts(work):
    facts, diffs = set(), {}
    for observation in work.broker._observations:
        if observation.status != "success" or observation.tool in {"read_file", "read_shared_source"}:
            continue
        if observation.tool == "read_diff":
            diffs.setdefault((observation.path, observation.query, observation.result_digest), []).append(observation)
        else:
            facts.update(_navigation_facts(observation))
    evidence_order = {identity: index for index, identity in enumerate(work.workspace.ledger.evidence_records)}
    local_ids = work.record.get("workspace_observation_ids", {})
    for (_path, _query, digest), observations in diffs.items():
        # Ledger insertion order preserves the producer's native source-window
        # fragments even if a consumer requests their IDs in another order.
        observations.sort(key=lambda obs: evidence_order.get(local_ids.get(obs.id), len(evidence_order)))
        unique = list({obs.excerpt: obs for obs in observations}.values())
        text = "".join(obs.excerpt for obs in unique)
        if hashlib.sha256(text.encode()).hexdigest().startswith(digest):
            # Only a complete hash-matching window supplies coordinates across
            # observation boundaries. Incomplete fragments stay navigation.
            facts.update(_navigation_facts(unique[0], text=text))
        else:
            for observation in unique:
                facts.update(_navigation_facts(observation))
    return facts


def _navigation_facts(observation, *, text=None):
    text = observation.excerpt if text is None else text
    if observation.tool != "read_diff":
        return {(observation.tool, observation.path, line) for line in text.splitlines() if line.strip()}
    # Hunk/window sizes are presentation, not newly inspected source. Retain
    # actual side + coordinates + source, including newly reached context.
    facts = set()
    before = after = None
    for line in text.splitlines():
        header = re.match(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
        if header:
            before, after = int(header[1]), int(header[2])
        elif before is not None and line.startswith(("-", "+", " ")):
            side = "before" if line.startswith("-") else "head"
            number = before if side == "before" else after
            facts.add(("read_diff", observation.path, json.dumps([side, number, line[1:]], ensure_ascii=False)))
            before += not line.startswith("+")
            after += not line.startswith("-")
        elif line.strip() and not line.startswith(("--- ", "+++ ", "\\ No newline")):
            facts.add(("read_diff", observation.path, line))
    return facts


def initialize_progress(work):
    work.flow.setdefault("semantic_progress", {"state": fingerprint(progress_state(work)), "transitions": []})


def repeated_progress(work, response):
    """A first idle/think/handoff transition is allowed; repeating it is not.

    Fresh source/navigation or accepted work starts a new transition epoch.
    Different stable failure fields or genuinely different failed search
    directions remain repairable. Merely rephrased notes cannot reset it.
    """
    state = fingerprint(progress_state(work))
    history = work.flow.setdefault("semantic_progress", {"state": state, "transitions": []})
    if history["state"] != state:
        history.update(state=state, transitions=[])
        # A productive transition must not count as the first failed retry.
        # The next thought/handoff/read with no new facts still has room to
        # act on that evidence before an actual repeated idle state stops it.
        return False
    failures = []
    for entry in work.flow["feedback"]:
        if "error" in entry:
            failures.append(
                {
                    key: entry[key]
                    for key in ("action", "unit_id", "error", "fields", "missing_source_sides", "failure_kind")
                    if key in entry
                }
            )
        elif entry.get("result") in ("unresolved", "capacity-deferred"):
            failures.append({"unit_id": entry.get("unit_id"), "result": entry["result"]})
    failed_queries = []
    for obs in work.broker._observations:
        if obs.status == "success":
            continue
        try:
            query = json.loads(obs.query.removeprefix(obs.tool + " "))
        except (ValueError, TypeError):
            query = {"path": obs.path}
        if isinstance(query, dict):
            query = {key: value for key, value in query.items() if key not in {"start", "end", "limit", "offset"}}
        failed_queries.append({"tool": obs.tool, "path": obs.path, "status": obs.status, "direction": query})
    transition = {
        "actions": sorted({call.get("name", "") for call in (getattr(response, "tool_calls", []) or [])}),
        "workspace_directions": sorted(
            {
                fingerprint(
                    {
                        key: sorted(args.get(key, []))
                        for key in ("paths", "unit_ids")
                        if isinstance(args.get(key), list) and all(isinstance(value, str) for value in args[key])
                    }
                )
                for call in (getattr(response, "tool_calls", []) or [])
                if call.get("name") == "query_review_workspace" and isinstance(args := call.get("args"), dict)
            }
        ),
        "failures": sorted(failures, key=fingerprint),
        "failed_directions": sorted({fingerprint(entry) for entry in failed_queries}),
    }
    signature = fingerprint(transition)
    if signature in history["transitions"]:
        work.flow["feedback"].append(
            {
                "error": "no-progress-cycle",
                "reason": "Repeated source/navigation facts, accepted work and stable controller outcome.",
                "pending_units": sorted(work.pending),
                "instruction": "This work remains incomplete. Resume with a missing fact or a valid assessment repair; "
                "rephrased notes and repeated windows cannot establish clean coverage.",
            }
        )
        work.session.event(
            "no-progress-cycle", reason="repeated-source-and-controller-state", state=state, transition=transition
        )
        return True
    history["transitions"].append(signature)
    return False
