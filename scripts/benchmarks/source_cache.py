"""Strict, opt-in read-only cache for fixed benchmark source archives."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

_SCHEMA = "reviewforge-source-cache-v1"
_SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
_ARCHIVE_SHA = re.compile(r"[0-9a-f]{64}\Z")


class SourceCacheError(ValueError):
    """A configured cache cannot prove the requested source identity."""


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise SourceCacheError("duplicate cache manifest field")
        result[key] = value
    return result


def cache_key(repo: str, sha: str) -> str:
    if not isinstance(repo, str) or not _REPO.fullmatch(repo) or any(p in {".", ".."} for p in repo.split("/")):
        raise SourceCacheError("cache requires an exact owner/repository identity")
    if not isinstance(sha, str) or not _SHA.fullmatch(sha):
        raise SourceCacheError("cache requires a complete lowercase commit SHA")
    return hashlib.sha256(f"{repo}:{sha}".encode()).hexdigest()


def read_cached_tarball(directory: str | Path, repo: str, sha: str) -> bytes:
    """Return verified bytes or fail; never fall back to mutable or online input."""
    key = cache_key(repo, sha)
    directory = Path(directory)
    try:
        manifest = json.loads(
            (directory / f"{key}.json").read_text(encoding="utf-8"),
            object_pairs_hook=_unique_object,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SourceCacheError("source cache manifest unavailable or invalid") from exc
    if (
        not isinstance(manifest, dict)
        or set(manifest) != {"schema", "repo", "sha", "archive_sha256"}
        or manifest["schema"] != _SCHEMA
        or manifest["repo"] != repo
        or manifest["sha"] != sha
        or not isinstance(manifest["archive_sha256"], str)
        or not _ARCHIVE_SHA.fullmatch(manifest["archive_sha256"])
    ):
        raise SourceCacheError("source cache manifest identity mismatch")
    try:
        payload = (directory / f"{key}.tar.gz").read_bytes()
    except OSError as exc:
        raise SourceCacheError("source cache archive unavailable") from exc
    if hashlib.sha256(payload).hexdigest() != manifest["archive_sha256"]:
        raise SourceCacheError("source cache archive hash mismatch")
    return payload
