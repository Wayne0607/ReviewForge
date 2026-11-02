"""Changed regions derived solely from fixed base and head snapshot bodies."""

from __future__ import annotations

import difflib
import hashlib
import re
from types import SimpleNamespace
from typing import Any

from .line_semantics import source_lines
from .snapshot import read_snapshot_blob, snapshot_identity


def _structural(path: str, change: str, reason: str, *, binary: bool = False) -> dict[str, Any]:
    side = "base" if change == "deleted" else "head"
    identity = hashlib.sha256(f"{path}\0{change}\0{reason}".encode()).hexdigest()[:16]
    return {
        "id": "r_" + identity,
        "path": path,
        "side": side,
        "start_line": 0,
        "end_line": 0,
        "base_start_line": 0,
        "base_end_line": 0,
        "changed_lines": [],
        "patch": "",
        "change": change,
        "unsupported": True,
        "binary": binary,
        "reason": reason,
    }


async def pinned_regions(base_workspace: Any, head_workspace: Any, *, run: Any = None) -> dict[str, Any]:
    """Compare every fixed path; source hashes and any requested bytes are verified.

    Renames are represented as deletion plus addition. Non-text files and
    byte-only changes retain explicit structural responsibility.
    """
    from .adapter import changed_regions

    line_run = run if run is not None else SimpleNamespace(metadata={})
    base_identity = await snapshot_identity(base_workspace)
    head_identity = await snapshot_identity(head_workspace)
    base_manifest = base_workspace._review_v42_body_manifest
    head_manifest = head_workspace._review_v42_body_manifest
    regions: list[dict[str, Any]] = []
    for path in sorted(base_manifest.keys() | head_manifest.keys()):
        if base_manifest.get(path) == head_manifest.get(path):
            continue
        before = await read_snapshot_blob(base_workspace, path) if path in base_manifest else None
        after = await read_snapshot_blob(head_workspace, path) if path in head_manifest else None
        change = "added" if before is None else "deleted" if after is None else "modified"
        if (before is not None and b"\x00" in before) or (after is not None and b"\x00" in after):
            regions.append(_structural(path, change, "binary file change requires explicit inspection", binary=True))
            continue
        try:
            old_text = before.decode("utf-8", "strict") if before is not None else ""
            new_text = after.decode("utf-8", "strict") if after is not None else ""
        except UnicodeDecodeError:
            regions.append(_structural(path, change, "non-UTF-8 file change requires explicit inspection"))
            continue
        git_lines = line_run.metadata.get("source_line_policy") == "git-lf-1"
        lines = list(
            difflib.unified_diff(
                source_lines(line_run, old_text, keepends=git_lines),
                source_lines(line_run, new_text, keepends=git_lines),
                fromfile="a/" + path,
                tofile="b/" + path,
                lineterm="\n" if git_lines else "",
            )
        )
        if not lines:
            regions.append(
                _structural(path, change, "empty-file or line-ending-only change requires explicit inspection")
            )
            continue
        if git_lines:
            patch = "".join(
                line if line.endswith("\n") else line + "\n\\ No newline at end of file\n" for line in lines
            )
        else:
            patch = "\n".join(lines) + "\n"
        parsed = changed_regions(SimpleNamespace(files_changed=[path], file_diffs={path: patch}), run=line_run)
        for region in parsed:
            region["change"] = change
            header = re.match(r"^@@ -\d+(?:,\d+)? \+\d+(?:,(\d+))? @@", region["patch"])
            empty_head_range = header is not None and header.group(1) == "0"
            if change == "deleted" or empty_head_range:
                # No head location exists for an empty new-side range. Read
                # the original lines from the pinned base side.
                region["side"] = "base"
                region["start_line"] = region["base_start_line"]
                region["end_line"] = region["base_end_line"]
                region["changed_lines"] = []
        regions.extend(parsed)
    # Detect changed bodies, added paths or removed paths while building diffs.
    if (
        await snapshot_identity(base_workspace) != base_identity
        or await snapshot_identity(head_workspace) != head_identity
    ):
        raise ValueError("snapshot changed during diff generation")
    return {"regions": regions, "base_identity": base_identity, "head_identity": head_identity}
