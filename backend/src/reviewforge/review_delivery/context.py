"""Session-local source residency and evidence-state cycle diagnostics.

Residency is reconstructed from durable receipts, not a claim that a model has
received a source. Only a successful subsequent model request proves delivery.
"""

from __future__ import annotations

import json
from copy import deepcopy

READ_TOOLS = {
    # Explicit observation capabilities. run_check executes a diagnostic and
    # must not be classified by a tool-name substring or a gateway default.
    "read_source",
    "read_diff",
    "search_code",
    "find_references",
    "search_knowledge",
    "read_knowledge",
    "list_sources",
    "read_saved_source",
    "read_progress",
    "read_notes",
    "changed_notes",
    "list_findings",
    "read_research",
}


def resident_sources(events, get_source):
    """Retain this session's read originals until explicitly released or reread."""
    active = {}
    for event in events:
        if event.get("type") == "receipt" and event.get("ok"):
            for source_id in event.get("source_ids", []):
                if get_source(source_id)["kind"] == "original":
                    active[source_id] = None
        elif event.get("type") == "retain":
            active[event["source_id"]] = None
        elif event.get("type") == "release":
            active.pop(event["source_id"], None)
    return list(active)


def projected_receipt(event, resident, released=()):
    """Keep native tool receipts, with original bytes present once in the request."""
    message = deepcopy(event["message"])

    def project(value):
        if isinstance(value, list):
            return [project(item) for item in value]
        if not isinstance(value, dict):
            return value
        identity = value.get("source_id", value.get("id"))
        pointer = "content_in_working_set" if identity in resident else "source_content_released"
        if "content" in value and (identity in resident or identity in released):
            value = {**{key: item for key, item in value.items() if key != "content"}, pointer: identity}
        return {key: project(item) for key, item in value.items()}

    projected = project(event.get("result"))
    if projected != event.get("result"):
        message["data"]["content"] = json.dumps(projected, ensure_ascii=False, sort_keys=True, default=str)
    return message


def native_history(events, resident, delivered):
    """Replay this session's provider state and tool exchanges, never another's.

    Preserve provider-returned assistant fields. Already delivered historical
    bodies can be represented by durable handles. The latest newly requested
    result remains available even if it is navigation or explicitly released.
    """
    released = set()
    for event in events:
        if event.get("type") == "release":
            released.add(event["source_id"])
        elif event.get("type") == "retain":
            released.discard(event["source_id"])
        elif event.get("type") == "receipt" and event.get("ok"):
            released.difference_update(event.get("source_ids", []))
    responses = [e for e in events if e.get("type") == "response"]
    messages = []
    for event in responses:
        messages.extend(event.get("raw_message", event["message"]))
        receipts = sorted(
            [e for e in events if e.get("type") == "receipt" and e["request_id"] == event["request_id"]],
            key=lambda e: e["index"],
        )
        prunable = released.intersection(delivered) if event is not responses[-1] else ()
        messages.extend(projected_receipt(e, resident, prunable) for e in receipts)
    return messages


def read_history(events, get_source):
    """A compact navigation index of this session's completed read operations.

    Preserve query identity, source handles and completeness, not repeated raw
    bodies or another session's history. These receipts are navigation, not proof.
    """
    calls = {}
    index = {}
    for event in events:
        if event.get("type") == "response":
            calls[event["request_id"]] = [
                call for message in event["message"] for call in message["data"].get("tool_calls", [])
            ]
        elif event.get("type") == "receipt":
            batch = calls.get(event["request_id"], [])
            position = event["index"]
            if position >= len(batch) or batch[position]["name"] not in READ_TOOLS:
                continue
            call = batch[position]
            operation = {"tool": call["name"], "args": call.get("args", {})}
            sources = []
            for identity in event.get("source_ids", []):
                source = get_source(identity)
                metadata = source["metadata"].get("metadata", {})
                sources.append(
                    {
                        "id": identity,
                        "kind": source["kind"],
                        **{k: metadata[k] for k in ("status", "complete") if k in metadata},
                    }
                )
            index[json.dumps(operation, sort_keys=True)] = {
                **operation,
                "ok": event["ok"],
                "sources": sources,
                **(
                    {"error": event["result"]["error"]}
                    if isinstance(event.get("result"), dict) and event["result"].get("error")
                    else {}
                ),
            }
    return list(index.values())


def reading_cycle(events, basis, actions):
    """Warn on repeated read actions, including differently composed batches.

    A new material basis or an intervening non-reading exchange starts a fresh
    investigation sequence. There is no limit on distinct reads or requests.
    """
    prior = []
    for event in reversed(events):
        if event.get("type") != "exchange_done":
            continue
        cycle = event.get("reading_cycle")
        if not cycle or cycle["basis"] != basis:
            break
        prior.append(cycle)
    seen = {action for item in prior for action in item["actions"]}
    repeated = set(actions).issubset(seen)
    warned = any(item["status"] == "warning" for item in prior)
    return {
        "basis": basis,
        "actions": sorted(set(actions)),
        "status": "halt" if repeated and warned else "warning" if repeated else "reading",
    }
