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


def projected_receipt(event, resident):
    """Keep native tool receipts, with original bytes present once in the request."""
    message = deepcopy(event["message"])
    result = event.get("result")
    if not isinstance(result, dict) or "content" not in result:
        return message
    identity = result.get("source_id", result.get("id"))
    if identity not in resident:
        return message
    projected = {key: value for key, value in result.items() if key != "content"}
    projected["content_in_working_set"] = identity
    message["data"]["content"] = json.dumps(projected, ensure_ascii=False, sort_keys=True, default=str)
    return message


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
