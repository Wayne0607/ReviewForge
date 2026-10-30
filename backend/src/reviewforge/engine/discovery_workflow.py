"""Protocol 6: fair work dispatch, bounded acquisition and recoverable results.

The previous three-turn block assumed one terminal response covered every unit
and never needed protocol repair. Reads, successful partial submissions and bad
receipts now consume separate, frozen allowances. Currency remains authoritative.
"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import asdict, dataclass, replace

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import ValidationError

from reviewforge.core.change_manifest import FileChange, is_verified_rename
from reviewforge.engine.discovery import SourceLine, plan_discovery
from reviewforge.engine.discovery_aliases import expand_response_refs, source_aliases
from reviewforge.engine.discovery_repair import apply_repairs, pending_repairs, remember, repair_request
from reviewforge.engine.discovery_session import extend_sources, restore_observation, result_payload, source_packet
from reviewforge.engine.discovery_submission import bind_discovery_submission
from reviewforge.engine.discovery_workflow_tools import KINDS, assessment_tools
from reviewforge.engine.investigator import Investigator
from reviewforge.engine.prompts_v4 import load_prompt
from reviewforge.engine.request_budget import plan_request
from reviewforge.engine.review_session import EvidenceStore, ReviewSession, actions

POLICY = "fair-assessments-v1"
REPAIR_POLICY = "fair-assessments-v2"
READ_REQUESTS = 2
REPAIR_REQUESTS = 2


def compact_packet(block, aliases, pending, observations):
    packet = source_packet(block, aliases, pending)
    files = {}
    rows = []
    for alias, _side, path, _line, text in packet.pop("source"):
        file_id = alias.split(".", 1)[0]
        if file_id in files and files[file_id] != path:
            raise ValueError("conflicting-source-file-alias")
        files[file_id] = path
        rows.append([alias, text])
    packet.update(files=files, rows=rows)
    packet["read_history_navigation_only"] = [
        {
            "tool": obs.tool,
            "path": obs.path,
            "requested_range": obs.line_range,
            "saved_range": obs.excerpt_line_range,
            "status": obs.status,
            "query_sha256": hashlib.sha256(obs.query.encode()).hexdigest(),
        }
        for obs in observations
    ]
    return packet


@dataclass
class Work:
    block: object
    record: dict
    broker: Investigator
    aliases: dict
    session: ReviewSession
    order: int
    protocol_version: int = 6
    stopped: str = ""

    @property
    def flow(self):
        return self.record["workflow"]

    @property
    def pending(self):
        return {unit.id for unit in self.block.units if not self.record["units"].get(unit.id, {}).get("finished")}


def request(generator, work, *, read=False, global_remaining=0):
    pending = work.pending
    active = replace(work.block, units=[unit for unit in work.block.units if unit.id in pending])
    result_tool, schemas = assessment_tools(active, protocol_version=work.protocol_version)
    tools = [result_tool, *(work.broker._build_tools() if read else [])]
    scope = {
        "policy": work.flow["policy"],
        "phase": "acquire" if read else "repair" if work.flow["invalid_receipts"] else "submit",
        "turn": work.session.record["turns"] + 1,
        "requests_remaining": max(
            0, min(work.session.record["max_turns"] - work.session.record["turns"], global_remaining) - 1
        ),
        "read_requests_remaining": max(0, READ_REQUESTS - work.flow["read_requests"] - int(read)),
        "last_turn": min(work.session.record["max_turns"] - work.session.record["turns"], global_remaining) <= 1,
    }
    if work.protocol_version >= 7 and not read and pending_repairs(work):
        scope.update(phase="field-repair", repair_record_count=len(pending_repairs(work)))
        repair_tool, chat = repair_request(work, schemas, scope)
        return bind_discovery_submission(generator._llm, [repair_tool]), chat, [repair_tool], schemas, scope
    packet = compact_packet(work.block, work.aliases, pending, work.broker._observations) | {"session": scope}
    rules = "\n".join(
        dict.fromkeys(
            generator._responsibility_rules.get(name, "")
            for unit in active.units
            for name in generator._unit_responsibilities.get(unit.id, [])
        )
    )
    prompt = load_prompt(
        "discovery_workflow_v9" if work.protocol_version >= 9 else "discovery_workflow_v6",
        output_language=generator._output_language,
    )
    if work.protocol_version >= 7:
        prompt += "\nThe duplicate discrepancy description is optional. Do not repeat claim/trigger/impact to fill it."
    chat = [
        SystemMessage(content=prompt + ("\nApplicable review rules:\n" + rules if rules else "")),
        HumanMessage(content=json.dumps(packet, ensure_ascii=False)),
    ]
    if work.flow["feedback"]:
        chat.append(
            HumanMessage(
                content=json.dumps(
                    {"controller_receipt": work.flow["feedback"], "allowed_actions": [tool.name for tool in tools]},
                    ensure_ascii=False,
                )
            )
        )
    return bind_discovery_submission(generator._llm, tools), chat, tools, schemas, scope


async def checkpoint(generator, ledger):
    if generator._on_update:
        await generator._on_update(ledger)


def save_observations(work):
    work.record["observations"] = [asdict(obs) for obs in work.broker._observations]
    work.record["source_registry"] = {ref: asdict(source) for ref, source in work.block.source_refs.items()}


def rejected(name, args, reason, fields=None):
    receipt = {"action": name, "unit_id": str(args.get("unit_id") or "")[:128], "error": reason}
    if fields:
        receipt["fields"] = fields
    # Preserve a malformed submission as quoted repair material, never a fact.
    if len(json.dumps(args, ensure_ascii=False)) <= 8000:
        receipt["rejected_record_untrusted"] = copy.deepcopy(args)
    return receipt


async def consume(generator, work, response, tools, schemas, delivered, ledger, result, right, store):
    calls, errors = actions(response, tools)
    feedback = [{"error": error["reason"], "unit_id": error["unit_id"]} for error in errors]
    groups = {}
    reads = 0
    had_result = False
    invalid_units = set()
    touched = set()
    carried = work.flow.setdefault("accepted_records", {}) if work.protocol_version >= 7 else {}
    for call in calls:
        name, args = call["name"], call["args"]
        if name not in {"submit_assessments", "repair_assessments"}:
            before = len(work.broker._observations)
            try:
                prepared = work.broker._prepared_tool_args(name, args)
            except (ValueError, TypeError):
                feedback.append(rejected(name, args, "invalid-source-tool-arguments"))
                continue
            reply = await work.broker._run_tool(name, prepared)
            feedback.append({"action": name, "result": reply})
            for obs in work.broker._observations[before:]:
                store.observation(obs)
                extend_sources(work.block, obs, work.aliases)
                reads += 1
            save_observations(work)
            await checkpoint(generator, ledger)
            continue
        had_result = True
        work.flow["mode"] = "result"
        if name == "repair_assessments":
            records, repair_errors = apply_repairs(work, args)
            feedback.extend(repair_errors)
            errors.extend({"reason": entry["error"], "unit_id": entry.get("unit_id", "")} for entry in repair_errors)
            args = {"records": records}
        else:
            records = args.get("records")
        if (
            set(args) != {"records"}
            or not isinstance(records, list)
            or not 1 <= len(records) <= max(4, 2 * len(work.pending))
        ):
            feedback.append(rejected(name, args, "invalid-assessment-envelope"))
            continue
        for raw in records:
            if not isinstance(raw, dict):
                feedback.append({"error": "assessment-record-must-be-object"})
                continue
            unit_id = raw.get("unit_id")
            if isinstance(unit_id, str):
                touched.add(unit_id)
            schema = schemas.get(raw.get("kind")) if isinstance(raw.get("kind"), str) else None
            if schema is None:
                feedback.append(rejected(name, raw, "unknown-assessment-kind"))
                if isinstance(unit_id, str):
                    invalid_units.add(unit_id)
                continue
            try:
                args = schema.model_validate(raw).model_dump(exclude_none=True)
            except ValidationError as exc:
                fields = [
                    {"field": list(error["loc"]), "type": error["type"]} for error in exc.errors(include_input=False)
                ]
                feedback.append(rejected(name, raw, "invalid-assessment-schema", fields))
                if isinstance(unit_id, str):
                    invalid_units.add(unit_id)
                if work.protocol_version >= 7:
                    remember(work, raw, fields, schemas)
                continue
            try:
                collection, item = result_payload(KINDS[args["kind"]], args, work.block, delivered)
            except (ValueError, TypeError):
                feedback.append(rejected(name, raw, "invalid-assessment-reference"))
                invalid_units.add(args["unit_id"])
                continue
            groups.setdefault(args["unit_id"], []).append((collection, item))
            if work.protocol_version >= 7:
                prior = carried.setdefault(args["unit_id"], [])
                if args not in prior:
                    prior.append(copy.deepcopy(args))
    if work.protocol_version >= 7:
        invalid_units.update(value["raw"]["unit_id"] for value in pending_repairs(work).values())
        for unit_id in touched & work.pending:
            if unit_id in carried:
                groups[unit_id] = [
                    result_payload(KINDS[args["kind"]], args, work.block, delivered) for args in carried[unit_id]
                ]
    counted_error = work.protocol_version >= 7 and (bool(errors) or any("error" in entry for entry in feedback))
    if counted_error:
        # Freeze a rejected receipt before an independent sibling checkpoint
        # can be interrupted. Recovery must not grant a fresh repair allowance.
        work.flow["invalid_receipts"] += 1
    committed = 0
    for unit_id, entries in groups.items():
        unit = next(unit for unit in work.block.units if unit.id == unit_id)
        payload = {name: [] for name in ("hypotheses", "no_issue_units", "deferred_units")}
        for collection, item in entries:
            payload[collection].append(item)
        local = {
            "units": {},
            "output_framing": {
                "complete": (work.protocol_version < 7 or unit_id not in invalid_units)
                and not any(e["unit_id"] in {"", unit_id} for e in errors)
            },
        }
        allowance = min(
            generator._discovery_max_hypotheses_per_block, max(0, generator._max_hypotheses - len(ledger.items))
        )
        await generator._consume_discovery(
            expand_response_refs(payload, delivered),
            replace(work.block, units=[unit]),
            ledger,
            result,
            right,
            allowance,
            local,
        )
        if work.protocol_version >= 7:
            validated = local.get("validated_assessments", [])
            keep = []
            for raw in carried.get(unit_id, []):
                collection, item = result_payload(KINDS[raw["kind"]], raw, work.block, delivered)
                expanded = expand_response_refs({collection: [item]}, delivered)[collection][0]
                normalized = generator._resolve_discovery_sources(expanded, work.block)
                if (raw["kind"], normalized) in validated:
                    keep.append(raw)
            carried[unit_id] = keep
        row = local["units"][unit_id]
        finished = (work.protocol_version < 7 or unit_id not in invalid_units) and (
            row["status"] in {"assessed", "capacity-deferred"} or any(kind == "deferred_units" for kind, _ in entries)
        )
        if finished:
            carried.pop(unit_id, None)
        work.record["units"][unit_id] = row | {"finished": finished}
        committed += int(finished)
        feedback.append({"unit_id": unit_id, "result": row["status"], "reason": row.get("reason", "")})
        if row["status"] == "assessed" and unit_id in result.unresolved_units:
            result.unresolved_units.remove(unit_id)
        await checkpoint(generator, ledger)
    save_observations(work)
    for item in ledger.items.values():
        if item.unit_id in {unit.id for unit in work.block.units} and item.attempts == 0 and not item.observations:
            item.observations = copy.deepcopy(work.broker._observations)
    if errors or any("error" in entry for entry in feedback) or not (reads or committed):
        if not counted_error:
            work.flow["invalid_receipts"] += 1
        if not reads:
            work.flow["mode"] = "result"
    if had_result and work.pending:
        feedback.append(
            {"pending_units": sorted(work.pending), "instruction": "Submit only the unfinished records, together."}
        )
    # The output cap bounds source replies; still bound accumulated feedback.
    work.flow["feedback"] = (
        feedback
        if len(json.dumps(feedback, ensure_ascii=False)) <= 16000
        else [
            {key: value for key, value in entry.items() if key not in {"result", "rejected_record_untrusted"}}
            for entry in feedback
        ]
    )
    work.session.event(
        "receipt", actions=[call["name"] for call in calls], feedback=work.flow["feedback"], committed=committed
    )
    await checkpoint(generator, ledger)


async def run_discovery(generator, state, pack, changeset, ledger, *, overview_changeset=None):
    from reviewforge.engine.hypothesis_generator import (
        HypothesisGenerationResult,
        _discovery_available,
        _discovery_output_floor,
        _right_lines_by_path,
    )

    if generator._executor is None:
        raise ValueError("protocol-6 discovery requires the pinned read-only executor")
    policy = REPAIR_POLICY if ledger.protocol_version >= 7 else POLICY
    blocks = plan_discovery(
        changeset,
        pack,
        state.file_diffs or {},
        limits=replace(generator._discovery_limits, max_units=min(4, generator._discovery_limits.max_units)),
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
    result = HypothesisGenerationResult()
    store = EvidenceStore(ledger)
    right = _right_lines_by_path(state)
    # New v9 runs need capacity for both source acquisition and incremental
    # results. A fixed 32 requests cannot cover large PRs even when every
    # request succeeds. The calculated ceiling remains finite and is frozen on
    # the first checkpoint; historical and resumed ceilings are never raised.
    base_limit = generator._session_request_limit
    if ledger.protocol_version >= 9:
        base_limit = max(base_limit, min(128, 2 * len(blocks) + sum(len(block.units) for block in blocks)))
    request_limit = min(
        [base_limit] + [r["request_limit"] for r in ledger.discovery_blocks.values() if "request_limit" in r]
    )
    acknowledged = {
        unit_id: copy.deepcopy(row)
        for record in ledger.discovery_blocks.values()
        if record.get("source") == generator._source
        and record.get("head_sha") == ledger.head_sha
        and record.get("workspace_digest") == ledger.workspace_digest
        for unit_id, row in record.get("units", {}).items()
        if row.get("finished")
    }
    worklist = []
    for order, block in enumerate(blocks):
        key = f"{generator._source}:{block.id}"
        record = ledger.discovery_blocks.setdefault(
            key,
            {
                "source": generator._source,
                "unit_ids": [u.id for u in block.units],
                "units": {},
                "submission_mode": policy,
                "status": "pending",
                "head_sha": ledger.head_sha,
                "workspace_digest": ledger.workspace_digest,
                "observations": [],
                "request_limit": request_limit,
                "workflow": {
                    "policy": policy,
                    "mode": "acquire",
                    "read_requests": 0,
                    "result_requests": 0,
                    "invalid_receipts": 0,
                    "feedback": [],
                },
            },
        )
        if (
            record.get("head_sha") != ledger.head_sha
            or record.get("workspace_digest") != ledger.workspace_digest
            or record.get("workflow", {}).get("policy") != policy
        ):
            raise ValueError("discovery-workflow-binding-mismatch")
        for unit in block.units:
            if unit.id in acknowledged:
                record["units"][unit.id] = copy.deepcopy(acknowledged[unit.id])
        if record.get("source_registry"):
            block.source_refs.update({ref: SourceLine(**source) for ref, source in record["source_registry"].items()})
            block.facts.extend(block.source_refs.values())
        aliases = record.setdefault("source_ref_aliases", source_aliases(block.source_refs, scoped=True))
        broker = Investigator(
            None,
            generator._executor,
            protocol_version=ledger.protocol_version,
            evidence_cache=generator._evidence_cache,
        )
        broker._state = state
        broker._observations = [restore_observation(raw) for raw in record["observations"]]
        broker._obs_counter = len(broker._observations)
        session = ReviewSession(
            record, task_id=key, phase="discovery", max_turns=READ_REQUESTS + len(block.units) + REPAIR_REQUESTS
        )
        flow = record["workflow"]
        if (
            any(
                type(flow.get(name)) is not int or flow[name] < 0
                for name in ("read_requests", "result_requests", "invalid_receipts")
            )
            or flow["read_requests"] + flow["result_requests"] != session.record["turns"]
            or flow["read_requests"] > READ_REQUESTS
        ):
            raise ValueError("invalid-discovery-workflow-counters")
        # Neither head-file reads nor another model answer can manufacture an
        # absent before/after diff. Pure renames must arrive through the
        # verified structural path, not an impossible textual assessment.
        input_failure = (
            f"input-unavailable:{block.failure}"
            if ledger.protocol_version >= 9 and block.failure in {"missing-diff", "incomplete-diff"}
            else ""
        )
        if input_failure:
            record["input_failure"] = input_failure
        worklist.append(
            Work(block, record, broker, aliases, session, order, ledger.protocol_version, stopped=input_failure)
        )
    await checkpoint(generator, ledger)
    used = sum(record.get("session", {}).get("turns", 0) for record in ledger.discovery_blocks.values())
    while used < request_limit:
        active = [
            work
            for work in worklist
            if work.pending
            and not work.stopped
            and work.session.record["turns"] < work.session.record["max_turns"]
            and work.flow["result_requests"] < len(work.block.units) + REPAIR_REQUESTS
            and work.flow["invalid_receipts"] <= REPAIR_REQUESTS
        ]
        if not active:
            break
        # Least-dispatched groups first: optional continuation cannot starve an
        # unvisited group. Persistent counters make resumed ordering identical.
        work = min(active, key=lambda item: (item.session.record["turns"], item.order))
        remaining = request_limit - used
        available = _discovery_available(ledger)
        costs = {}
        closings = {}
        for other in active:
            closing = request(generator, other, global_remaining=remaining)
            allowance = min(
                generator._discovery_max_hypotheses_per_block, max(0, generator._max_hypotheses - len(ledger.items))
            )
            floor = _discovery_output_floor(len(other.pending), allowance, protocol_version=6)
            if closing[4]["phase"] == "field-repair":
                floor = min(4096, 512 + 256 * closing[4]["repair_record_count"])
            plan = plan_request(closing[0], closing[1], max_output_tokens=4096, fallback_available=available)
            costs[other.order] = plan.input_tokens + floor
            closings[other.order] = (closing, plan, floor)
        closing, closing_plan, floor = closings[work.order]
        other_reserve = sum(cost for key, cost in costs.items() if key != work.order)
        read = (
            work.flow["mode"] == "acquire"
            and work.flow["read_requests"] < READ_REQUESTS
            and remaining > len(active)
            and work.session.record["turns"] + 1 < work.session.record["max_turns"]
        )
        current = request(generator, work, read=True, global_remaining=remaining) if read else closing
        reserve = other_reserve + costs[work.order] + min(costs.values()) if read else 0
        plan = plan_request(
            current[0], current[1], max_output_tokens=4096, fallback_available=available, reserve_tokens=reserve
        )
        if read and plan.max_output_tokens < 512:
            read = False
            current, plan = closing, closing_plan
        if not read:
            work.flow["mode"] = "result"
            # Protect one useful result for each other pending group when the
            # forecast fits; impossible aggregate forecasts must not deadlock.
            reserve = (
                0
                if plan.available_tokens is None
                else min(other_reserve, max(0, plan.available_tokens - plan.input_tokens - floor))
            )
            plan = plan_request(
                current[0], current[1], max_output_tokens=4096, fallback_available=available, reserve_tokens=reserve
            )
        bound, chat, tools, schemas, scope = current
        if sum(len(message.content) for message in chat) > generator._max_input_chars:
            work.stopped = "session-input-limit"
            continue
        if plan.max_output_tokens < (512 if read else floor):
            work.stopped = "budget-exhausted"
            continue
        if not work.session.dispatch():
            continue
        used += 1
        work.flow["read_requests" if read else "result_requests"] += 1
        work.record["request_attempted"] = True
        work.record["request_plan"] = {
            "input_tokens": plan.input_tokens,
            "max_output_tokens": plan.max_output_tokens,
            "protected_result_tokens": reserve,
            **scope,
        }
        work.session.event("request-scope", **scope, protected_result_tokens=reserve)
        delivered = dict(work.aliases)
        await checkpoint(generator, ledger)
        try:
            response = await bound.ainvoke(chat, max_tokens=plan.max_output_tokens)
        except Exception as exc:
            work.stopped = "budget-exhausted" if getattr(exc, "outcome", "") == "budget-exhausted" else "provider-error"
            work.session.event("dispatch-failed", error_type=type(exc).__name__)
            await checkpoint(generator, ledger)
            continue
        result.blocks += 1
        for source in work.block.source_refs.values():
            store.source(source, base_sha=state.base_sha)
        await consume(generator, work, response, tools, schemas, delivered, ledger, result, right, store)
    for work in worklist:
        for unit_id in work.pending:
            reason = work.stopped or (
                "discovery-request-limit"
                if used >= request_limit
                else "protocol-repair-limit"
                if work.flow["invalid_receipts"] > REPAIR_REQUESTS
                else "result-request-limit"
            )
            generator._discovery_unresolved(ledger, result, unit_id, reason)
            work.record["units"][unit_id] = {"status": "unresolved", "reason": reason, "finished": False}
        work.record["status"] = (
            "completed" if all(row.get("status") == "assessed" for row in work.record["units"].values()) else "partial"
        )
        work.session.finish(work.record["status"])
    await checkpoint(generator, ledger)
    return result
