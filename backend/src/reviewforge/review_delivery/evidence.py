"""Closed scopes for negative premises, separate from navigation and judgment.

The model decides whether a scope and predicate prove its semantic claim. The
controller can prove only that complete originals cover that declared scope, or
that an exact literal check examined every member of a pinned file selection.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json

from reviewforge.review_v42.snapshot import read_snapshot_bytes, snapshot_identity


async def check_text(gateway, args):
    """Run an auditable literal check without search exclusions or result caps."""
    side, pattern, literal = args.get("side", "head"), args["glob"], args["literal"]
    if not pattern or not literal:
        raise ValueError("glob and literal must be nonempty")
    workspace = gateway.workspace if side == "head" else gateway.base_workspace
    if workspace is None:
        raise ValueError("complete pinned workspace is unavailable")
    digest = await snapshot_identity(workspace)
    paths = sorted(path for path in workspace._review_v42_body_manifest if fnmatch.fnmatchcase(path, pattern))
    files, matches, gaps = [], [], []
    for path in paths:
        try:
            raw = await read_snapshot_bytes(workspace, path)
            files.append({"path": path, "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)})
            content = raw.decode("utf-8")
            offset = content.find(literal)
            while offset >= 0:
                matches.append({"path": path, "line": content.count("\n", 0, offset) + 1})
                offset = content.find(literal, offset + 1)
        except Exception as exc:
            gaps.append({"path": path, "reason": str(exc), "error_type": type(exc).__name__})
    if not paths:
        gaps.append({"reason": "scope matches no files; this is not a content-absence proof"})
    if await snapshot_identity(workspace) != digest:
        raise ValueError("fixed scope changed while checking")
    revision = gateway.state.head_sha if side == "head" else gateway.base_sha
    payload = {
        "schema": "closed-text-1",
        "side": side,
        "revision": revision,
        "snapshot_digest": digest,
        "glob": pattern,
        "literal": literal,
        "case_sensitive": True,
        "files": files,
        "matches": matches,
        "gaps": gaps,
        "complete": not gaps,
    }
    return {
        "kind": "test",
        "side": side,
        "revision": revision,
        "path": "check_text:" + pattern,
        "content": json.dumps(payload, ensure_ascii=False, sort_keys=True),
        "metadata": {"status": "available", "complete": not gaps, "check_type": "closed-text-1"},
    }


def validate_absence(claims, get_source, cited, delivered):
    """Validate declared coverage, never infer absence from claim prose."""
    if not isinstance(claims, list):
        raise ValueError("absence_claims must be a list (empty when no negative premise is used)")
    for claim in claims:
        if (
            not isinstance(claim, dict)
            or set(claim) != {"fact", "scope", "source_ids"}
            or not isinstance(claim["fact"], str)
            or not claim["fact"].strip()
            or not isinstance(claim["scope"], str)
            or not claim["scope"].strip()
            or not isinstance(claim["source_ids"], list)
            or not claim["source_ids"]
        ):
            raise ValueError("each absence premise requires fact, explicit scope and source_ids")
        groups = {}
        for identity in claim["source_ids"]:
            if identity not in cited or identity not in delivered:
                raise ValueError("absence evidence must be cited and independently delivered")
            source = get_source(identity)
            if source["kind"] != "original":
                raise ValueError("navigation cannot prove absence")
            metadata = source["metadata"]
            details = metadata.get("metadata", {})
            if details.get("check_type") == "closed-text-1":
                proof = json.loads(source["content"])
                if (
                    proof.get("schema") != "closed-text-1"
                    or proof.get("complete") is not True
                    or not proof.get("files")
                    or proof.get("matches") != []
                    or proof.get("gaps") != []
                ):
                    raise ValueError("literal absence check is incomplete or found a counterexample")
                continue
            if metadata.get("kind") != "source":
                raise ValueError("absence requires complete source files or a closed check_text result")
            total = details.get("file_lines")
            if details.get("empty") and source["content"] == b"":
                continue
            start, end = metadata.get("start_line"), metadata.get("end_line")
            if type(total) is not int or type(start) is not int or type(end) is not int or total < 1:
                raise ValueError(
                    "absence scope needs file_lines and complete original ranges; read_source without a range"
                )
            key = source["snapshot_side"], metadata.get("path"), metadata.get("revision")
            groups.setdefault(key, []).append((start, end, total))
        for ranges in groups.values():
            cursor, total = 1, ranges[0][2]
            for start, end, count in sorted(ranges):
                if count != total or start > cursor or end < start or end > total:
                    raise ValueError("absence scope has missing or inconsistent original ranges")
                cursor = max(cursor, end + 1)
            if cursor != total + 1:
                raise ValueError("partial originals cannot prove file-wide absence; read remaining lines or check_text")
