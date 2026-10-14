"""Source acquisition and independently committed discovery work items."""

from __future__ import annotations

import copy
import json
from dataclasses import asdict, replace

from langchain_core.messages import HumanMessage, SystemMessage

from reviewforge.core.change_manifest import FileChange, is_verified_rename
from reviewforge.engine.discovery import SourceLine, plan_discovery
from reviewforge.engine.discovery_aliases import expand_response_refs, source_aliases, target_site_choices
from reviewforge.engine.discovery_submission import bind_discovery_submission
from reviewforge.engine.hypothesis import Observation
from reviewforge.engine.investigator import Investigator
from reviewforge.engine.prompts_v4 import load_prompt
from reviewforge.engine.review_session import EvidenceStore, ReviewSession, actions, controller_feedback
from reviewforge.engine.session_tools import discovery_tools


def restore_observation(raw):
    value = copy.deepcopy(raw)
    for key in ("line_range", "excerpt_line_range"):
        if value.get(key) is not None:
            value[key] = tuple(value[key])
    return Observation(**value)


def extend_sources(block, observation, aliases):
    """Only an actual saved read contributes citable head source, not search hits."""
    if observation.tool != "read_file" or observation.status != "success" or not observation.excerpt_line_range:
        return
    first = observation.excerpt_line_range[0]
    for offset, text in enumerate(observation.excerpt.splitlines()):
        block.facts.append(SourceLine("context", observation.path, first + offset, text))
    block.register_source_refs()
    existing = set(aliases.values())
    paths = {}
    for alias, reference in aliases.items():
        paths[block.source_refs[reference].path] = int(alias[1:].split(".", 1)[0])
    for reference, source in sorted(block.source_refs.items()):
        if reference in existing:
            continue
        if source.path not in paths:
            paths[source.path] = max(paths.values(), default=-1) + 1
        side = {"before": "B", "after": "R", "context": "C"}[source.side]
        prefix = f"f{paths[source.path]}.{side}{source.line}"
        alias, segment = prefix, 1
        while alias in aliases:
            alias = f"{prefix}.s{segment}"
            segment += 1
        aliases[alias] = reference


def source_packet(block, aliases, pending):
    """An explicit registry also delivers newly acquired source after each read."""
    from reviewforge.engine.discovery_aliases import target_source_choices

    reverse = {reference: alias for alias, reference in aliases.items()}
    return {
        "pending_units": [
            {
                "unit_id": unit.id,
                "path": unit.path,
                "symbol": unit.symbol,
                "before_state": block.unit_before_states.get(unit.id),
                "required_source_sides": target_source_choices(block, unit.id, aliases),
                "allowed_site_refs": target_site_choices(block, unit.id, aliases),
                **(
                    {"change": block.structural_changes[unit.id].to_dict()}
                    if unit.id in block.structural_changes
                    else {}
                ),
            }
            for unit in block.units
            if unit.id in pending
        ],
        "source": [
            [reverse[reference], source.side, source.path, source.line, source.text]
            for reference, source in block.source_refs.items()
        ],
        "overview_navigation_only": block.overview[:1500],
        "focus_navigation_only": block.focus,
        "unchecked_navigation_only": block.unchecked,
        "source_rows_may_be_partial": True,
    }


def result_payload(name, args, block, aliases):
    unit = args["unit_id"]
    if name == "defer_unit":
        return "deferred_units", {"unit_id": unit, "reason": args["reason"]}
    if any(reference not in aliases for reference in args["source_refs"]):
        raise ValueError("unknown-source-reference")
    item = {"unit_id": unit, "behavior": {"source_facts": [{"source_ref": ref} for ref in args["source_refs"]]}}
    change = block.structural_changes.get(unit)
    fields = args.get("change_fields", [])
    if change is None and fields or change is not None and set(fields) != {"previous_path", "path"}:
        raise ValueError("invalid-structural-selection")
    if change is not None:
        item["behavior"]["change_facts"] = [
            {"kind": "file-change", "change_id": change.id, "field": field, "value": getattr(change, field)}
            for field in fields
        ]
    if name == "submit_no_issue":
        return "no_issue_units", item | {"checked": args["checked"]}
    sites = args["site_refs"]
    if any(ref not in target_site_choices(block, unit, aliases) for ref in sites) or args["trigger_ref"] not in aliases:
        raise ValueError("invalid-target-or-trigger-reference")
    return "hypotheses", item | {
        **{
            key: args[key]
            for key in (
                "claim",
                "trigger",
                "impact",
                "open_question",
                "refutation",
                "discrepancy",
                "mechanism",
                "severity",
            )
        },
        "sites": [{"source_ref": ref} for ref in sites],
        "trigger_basis": {"source_ref": args["trigger_ref"]},
    }


async def run_discovery(generator, state, pack, changeset, ledger, *, overview_changeset=None):
    from reviewforge.engine.hypothesis_generator import (
        HypothesisGenerationResult,
        _discovery_available,
        _right_lines_by_path,
    )
    from reviewforge.engine.request_budget import plan_request

    result = HypothesisGenerationResult()
    if generator._executor is None:
        raise ValueError("protocol-5 discovery requires the pinned read-only executor")
    limits = replace(generator._discovery_limits, max_units=min(4, generator._discovery_limits.max_units))
    blocks = plan_discovery(
        changeset,
        pack,
        state.file_diffs or {},
        limits=limits,
        overview_changeset=overview_changeset or changeset,
        file_changes={
            path: change
            for path, change in (state.file_changes or {}).items()
            if isinstance(change, FileChange)
            and change.path == path
            and change.repo == state.repo
            and change.head_repo == (state.head_repo or state.repo)
            and is_verified_rename(change, ledger.head_sha)
        },
    )
    store = EvidenceStore(ledger)
    right = _right_lines_by_path(state)
    request_limit = min(
        [generator._session_request_limit, max(generator._discovery_max_blocks, 2 * len(blocks))]
        + [row["request_limit"] for row in ledger.discovery_blocks.values() if "request_limit" in row]
    )
    used_turns = sum(record.get("session", {}).get("turns", 0) for record in ledger.discovery_blocks.values())
    for block in blocks:
        key = f"{generator._source}:{block.id}"
        record = ledger.discovery_blocks.setdefault(
            key,
            {
                "source": generator._source,
                "unit_ids": [unit.id for unit in block.units],
                "units": {},
                "submission_mode": "native-actions",
                "status": "pending",
                "head_sha": ledger.head_sha,
                "workspace_digest": ledger.workspace_digest,
                "observations": [],
                "request_limit": request_limit,
            },
        )
        if record.get("head_sha") != ledger.head_sha or record.get("workspace_digest") != ledger.workspace_digest:
            raise ValueError("discovery-session-source-mismatch")
        pending = {unit.id for unit in block.units if record["units"].get(unit.id, {}).get("status") != "assessed"}
        if not pending:
            continue
        if record.get("source_registry"):
            block.source_refs.update({ref: SourceLine(**source) for ref, source in record["source_registry"].items()})
            block.facts.extend(block.source_refs.values())
        aliases = record.setdefault("source_ref_aliases", source_aliases(block.source_refs, scoped=True))
        for source in block.source_refs.values():
            store.source(source, base_sha=state.base_sha)
        broker = Investigator(None, generator._executor, protocol_version=5, evidence_cache=generator._evidence_cache)
        broker._state = state
        broker._observations = [restore_observation(raw) for raw in record["observations"]]
        broker._obs_counter = len(broker._observations)
        session = ReviewSession(record, task_id=key, phase="discovery", max_turns=3)
        feedback = []
        while pending and used_turns < request_limit and session.dispatch():
            used_turns += 1
            delivered_aliases = dict(aliases)
            active = replace(block, units=[unit for unit in block.units if unit.id in pending])
            outputs = discovery_tools(active)
            reads = broker._build_tools()
            tools = [*outputs, *reads]
            bound = bind_discovery_submission(generator._llm, tools)
            system = load_prompt("session_discovery_v5", output_language=generator._output_language)
            rules = "\n".join(
                dict.fromkeys(
                    generator._responsibility_rules.get(name, "")
                    for unit in active.units
                    for name in generator._unit_responsibilities.get(unit.id, [])
                )
            )
            chat = [
                SystemMessage(content=system + ("\nApplicable review rules:\n" + rules if rules else "")),
                HumanMessage(content=json.dumps(source_packet(block, aliases, pending), ensure_ascii=False)),
            ]
            if feedback:
                chat.append(controller_feedback(feedback, tools))
            if sum(len(message.content) for message in chat) > generator._max_input_chars:
                session.finish("capacity-deferred: session-input-limit")
                break
            plan = plan_request(bound, chat, max_output_tokens=4096, fallback_available=_discovery_available(ledger))
            record["request_plan"] = {"input_tokens": plan.input_tokens, "max_output_tokens": plan.max_output_tokens}
            record["request_attempted"] = True
            if generator._on_update:
                await generator._on_update(ledger)
            if plan.max_output_tokens < 512:
                session.finish("budget-exhausted")
                break
            try:
                response = await bound.ainvoke(chat, max_tokens=plan.max_output_tokens)
            except Exception as exc:
                session.finish(
                    "budget-exhausted" if getattr(exc, "outcome", "") == "budget-exhausted" else "provider-error"
                )
                session.event("dispatch-failed", error_type=type(exc).__name__)
                break
            result.blocks += 1
            calls, errors = actions(response, tools)
            session.event("receipt", actions=[call["name"] for call in calls], errors=errors)
            feedback = [{"error": error["reason"]} for error in errors]
            schemas = {tool.name: tool.args_schema for tool in outputs}
            groups = {}
            for call in calls:
                name, args = call["name"], call["args"]
                if name not in schemas:
                    before = len(broker._observations)
                    session.record["status"] = "needs-evidence"
                    try:
                        args = broker._prepared_tool_args(name, args)
                    except (ValueError, TypeError):
                        feedback.append({"action": name, "error": "invalid-source-tool-arguments"})
                        continue
                    reply = await broker._run_tool(name, args)
                    feedback.append({"action": name, "result": reply})
                    for observation in broker._observations[before:]:
                        store.observation(observation)
                        extend_sources(block, observation, aliases)
                    record["observations"] = [asdict(obs) for obs in broker._observations]
                    record["source_registry"] = {ref: asdict(source) for ref, source in block.source_refs.items()}
                    if generator._on_update:
                        await generator._on_update(ledger)
                    continue
                unit_id = args.get("unit_id")
                try:
                    args = schemas[name].model_validate(args).model_dump()
                    collection, item = result_payload(name, args, block, delivered_aliases)
                except (ValueError, TypeError):
                    feedback.append({"unit_id": unit_id, "error": "invalid-result-schema-or-reference"})
                    continue
                groups.setdefault(unit_id, []).append((collection, item))
            for unit_id, entries in groups.items():
                unit = next(unit for unit in block.units if unit.id == unit_id)
                payload = {name: [] for name in ("hypotheses", "no_issue_units", "deferred_units")}
                for collection, item in entries:
                    payload[collection].append(item)
                # Raw framing errors cannot prove clean coverage. Other valid
                # units in an unambiguous receipt are independent transactions.
                complete = not any(error["unit_id"] in {"", unit_id} for error in errors)
                local = {"units": {}, "output_framing": {"complete": complete}}
                allowance = min(
                    generator._discovery_max_hypotheses_per_block, max(0, generator._max_hypotheses - len(ledger.items))
                )
                await generator._consume_discovery(
                    expand_response_refs(payload, delivered_aliases),
                    replace(block, units=[unit]),
                    ledger,
                    result,
                    right,
                    allowance,
                    local,
                )
                row = local["units"][unit_id]
                record["units"][unit_id] = row
                if row["status"] in {"assessed", "capacity-deferred"} or any(
                    kind == "deferred_units" for kind, _ in entries
                ):
                    pending.discard(unit_id)
                feedback.append({"unit_id": unit_id, "result": row["status"], "reason": row.get("reason", "")})
                if row["status"] == "assessed" and unit_id in result.unresolved_units:
                    result.unresolved_units.remove(unit_id)
                if generator._on_update:
                    await generator._on_update(ledger)
            record["observations"] = [asdict(obs) for obs in broker._observations]
            record["source_registry"] = {ref: asdict(source) for ref, source in block.source_refs.items()}
            for item in ledger.items.values():
                if item.unit_id in {unit.id for unit in block.units} and item.attempts == 0 and not item.observations:
                    item.observations = copy.deepcopy(broker._observations)
            if generator._on_update:
                await generator._on_update(ledger)
        for unit_id in pending:
            reason = session.record["status"]
            if reason in {"pending", "running", "needs-evidence"}:
                reason = "session-action-limit" if used_turns < request_limit else "discovery-request-limit"
            generator._discovery_unresolved(ledger, result, unit_id, reason)
            record["units"][unit_id] = {"status": "unresolved", "reason": reason}
        record["status"] = (
            "completed" if all(row.get("status") == "assessed" for row in record["units"].values()) else "partial"
        )
        session.finish(record["status"])
        if generator._on_update:
            await generator._on_update(ledger)
    return result
