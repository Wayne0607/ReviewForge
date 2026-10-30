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
from dataclasses import asdict, replace
from typing import Literal

from pydantic import Field

from reviewforge.engine.discovery import SourceLine, _source_segments
from reviewforge.engine.discovery_session import extend_source_aliases, extend_sources, restore_observation
from reviewforge.engine.hypothesis import Observation
from reviewforge.engine.investigation_evidence import READ_ONLY_TOOLS
from reviewforge.engine.review_session import fingerprint
from reviewforge.engine.session_tools import result_tool

WORKSPACE_ACTIONS = {"checkpoint_review_progress", "read_shared_evidence", "query_review_workspace"}


def workspace_tools(protocol_version=11):
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
            (
                "Inspect a compact directory of related progress and evidence. "
                "Set catalog='references' explicitly to obtain every original evidence ID for a selected dependency."
                if protocol_version >= 12
                else "Find existing related investigation notes and evidence before acquiring a dependency again."
            ),
            {
                "paths": (list[str], ...),
                "unit_ids": (list[str], ...),
                **({"catalog": (Literal["directory", "references"], "directory")} if protocol_version >= 12 else {}),
            },
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


def publish_review_progress(work, progress):
    """Controller-owned scope includes only this group and actually read paths."""
    return work.workspace.publish_progress(
        task_id=work.session.record["task_id"],
        actor="discovery",
        unit_ids=[unit.id for unit in work.block.units],
        paths=sorted(
            {unit.path for unit in work.block.units}
            | {observation.path for observation in work.broker._observations if observation.path}
        ),
        **progress,
    )


def workspace_action(work, name, args, store):
    schema = next(tool.args_schema for tool in workspace_tools(work.protocol_version) if tool.name == name)
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
    if work.protocol_version >= 12:
        return _import_records(work, records, store)
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


def _observation_value(observation):
    value = asdict(observation)
    value.pop("id")
    return json.loads(json.dumps(value, ensure_ascii=False))


def _source_matches(source, expected):
    return (
        source.side == expected.side
        and source.path == expected.path
        and source.line == expected.line
        and source.text == expected.text
    )


def _has_source(work, expected):
    if any(_source_matches(source, expected) for source in work.block.facts):
        return True
    return all(
        any(_source_matches(source, replace(expected, text=part)) for source in work.block.source_refs.values())
        for _start, part in _source_segments(expected.text)
    )


def _read_sources(observations):
    return [
        SourceLine("context", observation.path, observation.excerpt_line_range[0] + offset, line)
        for observation in observations
        if observation.tool == "read_file" and observation.excerpt_line_range
        for offset, line in enumerate(observation.excerpt.splitlines())
    ]


def _prepare_import(record, head_sha):
    if record["kind"] == "tool-observation":
        _source, observation = _prepare_record(record, head_sha)
        return [], [observation]
    if record["kind"] != "delivered-source":
        raise ValueError("unsupported-shared-evidence-kind")
    if record["side"] not in {"before", "after", "context"} or (
        record["side"] != "before" and record["source_sha"] != head_sha
    ):
        raise ValueError("shared-source-is-not-pinned-head")
    from reviewforge.engine.source_navigation import _pieces

    text, first = record["excerpt"], record["line"]
    sources = [
        SourceLine(record["side"], record["path"], first + offset, line, changed=record["changed"])
        for offset, line in enumerate(text.splitlines())
    ]
    if record["side"] == "before":
        return sources, []
    observations = [
        Observation(
            id="",
            tool="read_file",
            query="read_file "
            + json.dumps(
                {"path": record["path"], "start": first, "end": first + max(1, len(text.splitlines())) - 1},
                sort_keys=True,
            ),
            path=record["path"],
            line_range=(first, first + max(1, len(text.splitlines())) - 1),
            sha=head_sha,
            result_digest=hashlib.sha256(text.encode()).hexdigest()[:16],
            excerpt=part,
            status="success",
            excerpt_line_range=span,
        )
        for part, span in _pieces(text, first)
    ]
    return sources, observations


def _existing_import(work, record, sources, observations, by_id, by_value):
    identity = record["evidence_id"]
    prior = work.record.get("workspace_imports", {}).get(identity)
    if prior is not None:
        if (
            not isinstance(prior, dict)
            or prior.get("consumer_task_id") != work.session.record["task_id"]
            or prior.get("evidence_fingerprint") != fingerprint(record)
            or not isinstance(prior.get("observation_ids"), list)
            or not isinstance(prior.get("source_refs"), list)
            or any(local_id not in by_id for local_id in prior["observation_ids"])
            or any(alias not in work.aliases for alias in prior["source_refs"])
        ):
            raise ValueError("invalid-shared-evidence-import-binding")
        local = [by_id[local_id] for local_id in prior["observation_ids"]]
        if not sources and not local:
            raise ValueError("missing-shared-observation-import")
        if local and [_observation_value(item) for item in local] != [
            _observation_value(item) for item in observations
        ]:
            raise ValueError("invalid-shared-evidence-import-content")
        if sources and not all(_has_source(work, expected) for expected in sources):
            raise ValueError("missing-shared-source-import")
        if prior["source_refs"] != _source_aliases(work, sources or _read_sources(observations)):
            raise ValueError("invalid-shared-source-alias-import")
        return prior
    if sources and all(_has_source(work, expected) for expected in sources):
        return {"observation_ids": [], "source_refs": _source_aliases(work, sources)}
    if not sources:
        observation = by_value.get(fingerprint(record["observation"]))
        if observation is not None and _observation_value(observation) == record["observation"]:
            return {
                "observation_ids": [observation.id],
                "source_refs": _source_aliases(work, _read_sources(observations)),
            }
    return None


def _source_aliases(work, sources):
    return sorted(
        alias
        for alias, reference in work.aliases.items()
        if reference in work.block.source_refs
        and any(
            actual.side == expected.side
            and actual.path == expected.path
            and actual.line == expected.line
            and actual.text in {part for _start, part in _source_segments(expected.text)}
            for expected in sources
            for actual in [work.block.source_refs[reference]]
        )
    )


def _import_records(work, records, store):
    prepared = [_prepare_import(record, store.ledger.head_sha) for record in records]
    by_id = {observation.id: observation for observation in work.broker._observations}
    by_value = {fingerprint(_observation_value(observation)): observation for observation in by_id.values()}
    # Validate restored consumer mappings and the complete batch before writes.
    existing = [
        _existing_import(work, record, *group, by_id, by_value) for record, group in zip(records, prepared, strict=True)
    ]
    imported = []
    for record, (sources, observations), prior in zip(records, prepared, existing, strict=True):
        identity, local_ids = record["evidence_id"], []
        if prior is None:
            if sources:
                for source in sources:
                    if not any(_source_matches(item, source) for item in work.block.facts):
                        work.block.facts.append(source)
                work.block.register_source_refs()
                extend_source_aliases(work.block, work.aliases)
            else:
                for observation in observations:
                    extend_sources(work.block, observation, work.aliases)
            for observation in observations:
                saved = replace(observation, id=_next_id(work))
                work.broker._observations.append(saved)
                remember_evidence(work, store.observation(saved), saved)
                local_ids.append(saved.id)
            aliases = _source_aliases(work, sources or _read_sources(observations))
        else:
            local_ids, aliases = prior["observation_ids"][:], prior["source_refs"][:]
        remember_evidence(work, identity)
        binding = {
            "consumer_task_id": work.session.record["task_id"],
            "evidence_fingerprint": fingerprint(record),
            "observation_ids": local_ids,
            "source_refs": aliases,
        }
        work.record.setdefault("workspace_imports", {})[identity] = binding
        reply = {
            "evidence_id": identity,
            "observation_ids": local_ids,
            "source_refs": aliases,
            "reused": prior is not None,
        }
        if local_ids:
            reply["observation_id"] = local_ids[0]
        else:
            reply["source_reference_only"] = True
        if prior is None:
            reply["record"] = copy.deepcopy(record)
        imported.append(reply)
    return {
        "evidence": imported,
        "instruction": "Reuse the returned local observation IDs and source aliases. "
        "No new source is established by rereading an already held evidence ID; notes remain navigation.",
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
