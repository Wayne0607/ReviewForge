"""Pinned structural file changes, kept separate from textual diff evidence.

An absent patch is an input gap. Only authoritative comparison-base/head Git
entries can establish unchanged bytes for a rename; PR line counts cannot.
The immutable record is also the ledger anchor. Trust still requires binding
it to the StateStore record produced by the upstream reader, not model JSON.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, fields
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from reviewforge.core.state import StateStore


def valid_sha(value: str) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", value))


def valid_repo(value: str) -> bool:
    return (
        isinstance(value, str)
        and bool(re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value))
        and all(part not in {".", ".."} for part in value.split("/"))
    )


def valid_path(value: str) -> bool:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        return False
    path = PurePosixPath(value)
    return not path.is_absolute() and str(path) == value and all(part not in {"", ".", ".."} for part in path.parts)


@dataclass(frozen=True)
class FileChange:
    id: str = ""
    repo: str = ""
    head_repo: str = ""
    path: str = ""
    previous_path: str = ""
    status: str = ""
    base_sha: str = ""
    head_sha: str = ""
    comparison_base_sha: str = ""
    base_blob_sha: str = ""
    head_blob_sha: str = ""
    base_mode: str = ""
    head_mode: str = ""
    base_type: str = ""
    head_type: str = ""
    patch_state: str = "absent-unverified"
    source: str = "github-pr-files"
    proof_error: str = ""

    def to_dict(self) -> dict[str, str]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FileChange:
        if not isinstance(data, dict):
            raise ValueError("file change must be an object")
        allowed = {field.name for field in fields(cls)}
        values = {key: value for key, value in data.items() if key in allowed}
        if any(not isinstance(value, str) for value in values.values()):
            raise ValueError("file change fields must be strings")
        return cls(**values)


def file_change_id(change: FileChange) -> str:
    identity = [
        change.repo,
        change.head_repo,
        change.base_sha,
        change.head_sha,
        change.previous_path,
        change.path,
        change.status,
    ]
    encoded = json.dumps(identity, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return "fc_" + hashlib.sha256(encoded).hexdigest()[:24]


def file_changes_from_pr_files(
    *, repo: str, head_repo: str, base_sha: str, head_sha: str, files: list[dict[str, Any]]
) -> dict[str, FileChange]:
    """Preserve PR-file metadata without claiming any blob proof or doing I/O."""
    from dataclasses import replace

    result: dict[str, FileChange] = {}
    for item in files:
        if not isinstance(item, dict) or not valid_path(item.get("filename")):
            continue
        patch = item.get("patch")
        change = FileChange(
            repo=repo,
            head_repo=head_repo or repo,
            path=item["filename"],
            previous_path=str(item.get("previous_filename") or ""),
            status=str(item.get("status") or ""),
            base_sha=base_sha,
            head_sha=head_sha,
            patch_state="present" if isinstance(patch, str) and patch.strip() else "absent-unverified",
        )
        result[change.path] = replace(change, id=file_change_id(change))
    return result


def populate_file_changes(state: StateStore, files: list[dict[str, Any]]) -> None:
    state.file_changes = file_changes_from_pr_files(
        repo=state.repo,
        head_repo=state.head_repo,
        base_sha=state.base_sha,
        head_sha=state.head_sha,
        files=files,
    )


def render_change_fact(change: FileChange) -> str:
    """Stable pure facts for a trusted read_change Observation, never a verdict."""
    # Source citations most often use the old/new paths. Keep them first so a
    # bounded Observation does not bury both behind repeated hash fields.
    values = change.to_dict()
    ordered = {key: values.pop(key) for key in ("path", "previous_path", "id", "status", "patch_state")}
    ordered.update(values)
    return json.dumps(ordered, ensure_ascii=False, separators=(",", ":"))


def is_verified_rename(change: FileChange, head_sha: str) -> bool:
    """Validate proof structure; callers must additionally bind to trusted state."""
    return (
        isinstance(change, FileChange)
        and all(isinstance(getattr(change, field.name), str) for field in fields(FileChange))
        and change.id == file_change_id(change)
        and valid_repo(change.repo)
        and valid_repo(change.head_repo)
        and valid_path(change.path)
        and valid_path(change.previous_path)
        and change.path != change.previous_path
        and change.status == "renamed"
        and change.patch_state == "verified-no-text-change"
        and change.source == "github-pr-files+git-tree"
        and not change.proof_error
        and all(
            valid_sha(value)
            for value in (change.base_sha, change.head_sha, change.comparison_base_sha, change.base_blob_sha)
        )
        and change.head_sha == head_sha
        and change.base_blob_sha == change.head_blob_sha
        and change.base_type == change.head_type == "blob"
        and change.base_mode == change.head_mode
        and change.base_mode in {"100644", "100755"}
    )
