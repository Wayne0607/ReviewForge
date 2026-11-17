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
        # This is an executed artifact, not a repository file location.
        "path": "",
        "content": json.dumps(payload, ensure_ascii=False, sort_keys=True),
        "metadata": {"status": "available", "complete": not gaps, "check_type": "closed-text-1"},
    }


def manifest_digest(files):
    return (
        "sha256:"
        + hashlib.sha256(
            json.dumps(files, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )


def validate_absence(claims, get_source, cited, delivered, get_manifest):
    """Check coverage against snapshot membership, never infer facts from prose."""
    if not isinstance(claims, list):
        raise ValueError("absence_claims must be a list (empty when no negative premise is used)")
    for claim in claims:
        if (
            not isinstance(claim, dict)
            or set(claim) != {"fact", "scope", "source_ids"}
            or not isinstance(claim["fact"], str)
            or not claim["fact"].strip()
            or not isinstance(claim["source_ids"], list)
            or not claim["source_ids"]
        ):
            raise ValueError("each absence premise requires fact, structured scope and source_ids")
        scope = claim["scope"]
        if (
            not isinstance(scope, dict)
            or set(scope) != {"side", "glob"}
            or scope["side"] not in {"base", "head"}
            or not isinstance(scope["glob"], str)
            or not scope["glob"]
        ):
            raise ValueError("absence scope requires explicit side and nonempty glob")
        manifest = get_manifest(scope["side"])
        required = {p for p in manifest if fnmatch.fnmatchcase(p, scope["glob"])}
        if not required:
            raise ValueError("absence scope matches no files; select the authoritative content scope")
        covered, groups = set(), {}
        for identity in claim["source_ids"]:
            if identity not in cited or identity not in delivered:
                raise ValueError("absence evidence must be cited and independently delivered")
            source = get_source(identity)
            if source["kind"] != "original":
                raise ValueError("navigation cannot prove absence")
            if source["snapshot_side"] != scope["side"]:
                continue  # An authentic comparison source cannot close the other snapshot.
            metadata = source["metadata"]
            details = metadata.get("metadata", {})
            if details.get("check_type") == "closed-text-1":
                proof = json.loads(source["content"])
                expected = {p: h for p, h in manifest.items() if fnmatch.fnmatchcase(p, proof["glob"])}
                actual = {f["path"]: f["sha256"] for f in proof["files"]}
                if (
                    proof.get("schema") == "closed-text-1"
                    and proof.get("complete") is True
                    and proof.get("snapshot_digest") == manifest_digest(manifest)
                    and proof.get("matches") == []
                    and proof.get("gaps") == []
                    and actual == expected
                ):
                    covered.update(actual)
                # Positive/incomplete checks are supporting observations, not absence proof.
                # Complete originals may still prove the semantic premise independently.
                continue
            path = metadata.get("path")
            if path not in required or metadata.get("kind") != "source":
                continue
            if details.get("empty") and source["content"] == b"" and manifest[path] == hashlib.sha256(b"").hexdigest():
                covered.add(path)
                continue
            total = details.get("file_lines")
            start, end = metadata.get("start_line"), metadata.get("end_line")
            if type(total) is not int or type(start) is not int or type(end) is not int or total < 1:
                continue
            parts = source["content"].split(b"\n")
            lines = [part + b"\n" for part in parts[:-1]] + ([parts[-1]] if parts[-1] else [])
            if end - start + 1 != len(lines) or start < 1 or end > total:
                raise ValueError("original line range does not match absence scope bytes")
            groups.setdefault(path, []).append((start, total, lines))
        for path, ranges in groups.items():
            total, lines = ranges[0][1], {}
            for start, count, body in ranges:
                if count != total:
                    raise ValueError("inconsistent original file size in absence proof")
                for number, line in enumerate(body, start):
                    if number in lines and lines[number] != line:
                        raise ValueError("conflicting original lines in absence proof")
                    lines[number] = line
            if len(lines) == total and set(lines) == set(range(1, total + 1)):
                raw = b"".join(lines[n] for n in range(1, total + 1))
                if hashlib.sha256(raw).hexdigest() != manifest[path]:
                    raise ValueError("complete original differs from pinned scope manifest")
                covered.add(path)
        missing = sorted(required - covered)
        if missing:
            raise ValueError(
                "absence scope is incomplete: "
                + json.dumps({"scope": scope, "missing_paths": missing})
                + "; read complete originals or a complete zero-match check_text result"
            )
