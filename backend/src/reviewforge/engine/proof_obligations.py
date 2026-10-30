"""Source-bound followup plans; missing obligations are navigation, never proof."""

from __future__ import annotations

import hashlib
import json
import re

from reviewforge.engine.review_session import fingerprint
from reviewforge.engine.source_navigation import packet as navigation_packet


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate-context-receipt-field")
        value[key] = item
    return value


def _context_facts(observations, progress):
    """Normalize complete verified receipts, never transport fragments or clocks."""
    from reviewforge.engine.knowledge_receipt import validate_knowledge_receipt

    by_id = {observation.id: observation for observation in observations}
    if len(by_id) != len(observations):
        return set()
    facts = set()
    for entry in progress:
        if not isinstance(entry, dict) or entry.get("phase") != "review-context-receipt":
            continue
        try:
            ids, tool = entry["observation_ids"], entry["tool"]
            if (
                tool not in {"read_review_knowledge", "run_repository_test"}
                or not isinstance(ids, list)
                or not ids
                or len(set(ids)) != len(ids)
                or not all(isinstance(identity, str) for identity in ids)
            ):
                continue
            group = [by_id[identity] for identity in ids]
            if any(
                obs.status != "success"
                or obs.tool != tool
                or obs.query != entry["query"]
                or obs.sha != entry["head_sha"]
                for obs in group
            ):
                continue
            text = "".join(obs.excerpt for obs in group)
            digest = hashlib.sha256(text.encode()).hexdigest()
            if entry.get("receipt_sha256") != digest or any(obs.result_digest != digest[:16] for obs in group):
                continue
            if not any(
                isinstance(row, dict)
                and row.get("phase") == "source-window-v9"
                and row.get("query") == entry["query"]
                and row.get("observation_ids") == ids
                and row.get("saved_chars") == len(text)
                and row.get("truncated") is False
                for row in progress
            ):
                continue
            receipt = json.loads(text, object_pairs_hook=_unique_object)
            head, workspace = entry["head_sha"], entry["workspace_digest"]
            if (
                not isinstance(head, str)
                or not head
                or not isinstance(workspace, str)
                or not workspace
                or not isinstance(receipt, dict)
                or receipt.get("status") != "success"
                or receipt.get("head_sha") != head
                or receipt.get("workspace_digest") != workspace
            ):
                continue
            if tool == "read_review_knowledge":
                receipt = validate_knowledge_receipt(receipt, head_sha=head, workspace_digest=workspace)
                stable = {
                    key: receipt[key]
                    for key in (
                        "head_sha",
                        "workspace_digest",
                        "kind",
                        "path",
                        "revision",
                        "content_sha256",
                    )
                }
                facts.add((head, receipt["path"], receipt["kind"], 0, fingerprint(stable)))
            else:
                if (
                    receipt.get("runtime") not in {"python-unittest", "node-test"}
                    or not isinstance(receipt.get("source_sha256"), str)
                    or not re.fullmatch(r"[0-9a-f]{64}", receipt["source_sha256"])
                    or not isinstance(receipt.get("image_id"), str)
                    or not re.fullmatch(r"sha256:[0-9a-f]{64}", receipt["image_id"])
                    or type(receipt.get("exit_code")) is not int
                    or not isinstance(receipt.get("argv"), list)
                    or not receipt["argv"]
                    or not all(isinstance(arg, str) and arg for arg in receipt["argv"])
                    or any(not isinstance(receipt.get(key), str) for key in ("stdout", "stderr", "path"))
                ):
                    continue
                stable = {
                    key: receipt[key]
                    for key in (
                        "head_sha",
                        "workspace_digest",
                        "source_sha256",
                        "runtime",
                        "image_id",
                        "argv",
                        "exit_code",
                    )
                }
                facts.add((head, workspace, "execution", 0, fingerprint(stable)))
        except (KeyError, TypeError, ValueError):
            continue
    return facts


def source_facts(observations, progress=()):
    facts = _context_facts(observations, progress)
    for observation in observations:
        if observation.status != "success" or not observation.excerpt.strip():
            continue
        if observation.tool in {"query_review_knowledge", "read_review_knowledge", "run_repository_test"}:
            continue
        if observation.tool == "read_file" and observation.excerpt_line_range:
            first = observation.excerpt_line_range[0]
            facts.update(
                (observation.sha, observation.path, "source", first + index, text)
                for index, text in enumerate(observation.excerpt.splitlines())
                if text.strip()
            )
        elif observation.tool in {"grep", "find_references", "find_callers"}:
            for text in observation.excerpt.splitlines():
                hit = re.match(r"^- (.+?):(\d+): ?(.*)$", text)
                facts.add(
                    (observation.sha, hit[1], "source", int(hit[2]), hit[3])
                    if hit
                    else (observation.sha, observation.path, observation.tool, 0, text)
                )
        else:
            facts.update(
                (observation.sha, observation.path, observation.tool, 0, text)
                for text in observation.excerpt.splitlines()
                if text.strip()
            )
    return facts


def source_state(observations, progress=()):
    return fingerprint(sorted(source_facts(observations, progress)))


def query_direction(name, args):
    return {
        "tool": name,
        "args": {
            key: value for key, value in args.items() if key not in {"start", "end", "max_hits", "limit", "offset"}
        },
    }


def failed_directions(observations):
    rows = []
    for observation in observations:
        if observation.status == "success":
            continue
        name, _, arguments = observation.query.partition(" ")
        try:
            args = json.loads(arguments)
        except (ValueError, TypeError):
            args = {"path": observation.path}
        if isinstance(args, dict):
            row = query_direction(name, args) | {"status": observation.status}
            if row not in rows:
                rows.append(row)
    return rows


def build_obligations(item, ledger):
    """Only the independent audit's actual missing inputs define this work."""
    from reviewforge.engine.investigator import _evidence_segments
    from reviewforge.engine.publication_contracts import contract_catalog

    facts = ledger.source_audits[item.id]["facts"]
    gaps = list(facts.get("gaps", []))
    missing = []
    if not contract_catalog(item, ledger):
        missing.append(
            (
                "behavior-contract",
                "Locate the consumer, schema, test or pinned requirement that establishes "
                "the supported input and required behavior.",
            )
        )
    if not facts.get("execution"):
        missing.append(
            (
                "execution-fact",
                "Read the concrete operation, its arguments and guards to establish "
                "the actual execution and observable consequence.",
            )
        )
    missing.extend(("source-gap", gap) for gap in gaps)
    observations = [obs for obs in item.observations if obs.status == "success"]
    paths = sorted({obs.path for obs in observations if obs.path})
    symbols = sorted(
        {
            match.group(1) or match.group(2)
            for obs in observations
            for match in re.finditer(r"\b(?:def|function|class)\s+(\w+)|\b(\w+)\s*\(", obs.excerpt)
            if (match.group(1) or match.group(2)) not in {"if", "for", "while", "switch", "catch"}
        }
    )
    refs = [f"{item.id}/{reference}" for obs in observations for reference in _evidence_segments(obs)]
    navigation = navigation_packet(observations, limit=None)
    actions = list(navigation["adjacent_windows"])
    actions.extend(
        {
            "tool": "read_file",
            "args": {
                "path": location["path"],
                "start": max(1, location["line"] - 3),
                "end": location["line"] + 8,
            },
        }
        for location in navigation["locations"]
        if not location["already_read"]
    )
    actions.extend({"tool": "find_references", "args": {"symbol": symbol}} for symbol in symbols)
    # Dedupe by actual tool arguments; no model-authored missing premise becomes source.
    actions = list({fingerprint(action): action for action in actions}.values())
    return [
        {
            "id": "ob_" + fingerprint([kind, question])[:20],
            "kind": kind,
            "question": question,
            "paths": paths,
            "symbols": symbols,
            "existing_refs": refs,
            "next_actions": actions,
            "trust": "navigation-only-not-evidence",
        }
        for kind, question in missing
    ]
