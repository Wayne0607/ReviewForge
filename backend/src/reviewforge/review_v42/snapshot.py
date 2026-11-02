"""Byte-preserving and content-bound reads from a fixed v4.2 workspace.

The historical workspace digest is a path/size/mtime manifest, not a content
hash. This module freezes a separate digest of every file body and verifies it
again when serving a source; no legacy workspace semantics are changed.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
from pathlib import Path
from typing import Any

from reviewforge.tools.workspace import PRHeadWorkspace, WorkspaceUnavailable, _normalise_repo_path


async def _bytes(workspace: Any, path: str) -> bytes:
    if getattr(workspace, "_closed", False):
        raise WorkspaceUnavailable("workspace is closed")
    archive = getattr(workspace, "_archive_view", None)
    if archive is not None:
        if path not in archive.files:
            raise WorkspaceUnavailable("source not present in snapshot")
        return await asyncio.to_thread(archive.read_bytes, path)
    # Production materialized workspaces expose _local_path; reject symbolic
    # links even if they happen to resolve beneath the root.
    local_path = getattr(workspace, "_local_path", None)
    if callable(local_path):
        candidate = local_path(path)
        root = Path(workspace.info.root)
        if candidate is None:
            raise WorkspaceUnavailable("invalid source path")
        node = root
        for part in path.split("/"):
            node = node / part
            if node.is_symlink():
                raise WorkspaceUnavailable("symbolic-link source is not a regular snapshot file")
        try:
            return await asyncio.to_thread(candidate.read_bytes)
        except OSError as exc:
            raise WorkspaceUnavailable("snapshot source unavailable") from exc
    # Explicit byte-reading adapters support controlled tests. Never fall back
    # to text read_async, which normalizes newlines and replaces invalid bytes.
    reader = getattr(workspace, "read_bytes", None)
    if callable(reader):
        result = reader(path)
        result = await result if inspect.isawaitable(result) else result
        if isinstance(result, bytes):
            return result
    raise WorkspaceUnavailable("workspace cannot provide original bytes")


async def snapshot_identity(workspace: Any) -> str:
    """Freeze and revalidate an entire complete source snapshot by body hash."""
    if workspace.source != "tarball" or workspace.truncated or getattr(workspace, "_closed", False):
        raise WorkspaceUnavailable("complete fixed tarball workspace is required")
    if isinstance(workspace, PRHeadWorkspace) and workspace._archive_view is not None:
        paths = list(workspace._archive_view.files)
    elif isinstance(workspace, PRHeadWorkspace):
        root = Path(workspace.info.root)
        entries = list(root.rglob("*"))
        if any(entry.is_symlink() for entry in entries):
            raise WorkspaceUnavailable("snapshot contains an unexpected symbolic link")
        paths = [entry.relative_to(root).as_posix() for entry in entries if entry.is_file()]
    else:
        paths = list(workspace.list_paths())
    expected_count = getattr(getattr(workspace, "info", None), "file_count", None)
    if expected_count is not None and expected_count != len(paths):
        raise WorkspaceUnavailable("snapshot manifest no longer has the original file count")
    if any(not isinstance(path, str) or _normalise_repo_path(path) != path for path in paths):
        raise WorkspaceUnavailable("invalid snapshot path")
    if len(paths) != len(set(paths)):
        raise WorkspaceUnavailable("duplicate snapshot path")
    manifest = {path: hashlib.sha256(await _bytes(workspace, path)).hexdigest() for path in sorted(paths)}
    previous = getattr(workspace, "_review_v42_body_manifest", None)
    if previous is not None and previous != manifest:
        raise WorkspaceUnavailable("fixed workspace contents changed")
    # Both path and original body hash are included; no file-length shortcut.
    digest = hashlib.sha256(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    workspace._review_v42_body_manifest = manifest
    return "sha256:" + digest


async def read_snapshot_blob(workspace: Any, path: str) -> bytes:
    """Read and verify complete original bytes, including binary artifacts."""
    if not isinstance(path, str) or _normalise_repo_path(path) != path:
        raise ValueError("invalid snapshot path")
    manifest = getattr(workspace, "_review_v42_body_manifest", None)
    if manifest is None:
        raise WorkspaceUnavailable("snapshot identity must be established before reading")
    if path not in manifest:
        raise WorkspaceUnavailable("source not present in snapshot")
    raw = await _bytes(workspace, path)
    if hashlib.sha256(raw).hexdigest() != manifest[path]:
        raise WorkspaceUnavailable("fixed source body changed")
    return raw


async def read_snapshot_bytes(workspace: Any, path: str) -> bytes:
    """Read complete original UTF-8 bytes from the frozen source manifest."""
    raw = await read_snapshot_blob(workspace, path)
    raw.decode("utf-8", errors="strict")
    if b"\x00" in raw:
        raise WorkspaceUnavailable("binary source unavailable")
    return raw
