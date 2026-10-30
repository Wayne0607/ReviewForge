"""Validate controller-delivered original knowledge, never a model's summary."""

from __future__ import annotations

import copy
import hashlib
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlsplit

_FIELDS = {"status", "head_sha", "workspace_digest", "kind", "path", "revision", "content_sha256", "content"}


def validate_knowledge_receipt(raw: Any, *, head_sha: str, workspace_digest: str) -> dict[str, Any]:
    """Require the complete original receipt with its fixed source identity."""
    if not isinstance(raw, dict) or raw.get("kind") not in {"documentation", "issue"}:
        raise ValueError("invalid-knowledge-kind")
    expected = _FIELDS | ({"url"} if raw["kind"] == "issue" else set())
    if set(raw) != expected or raw.get("status") != "success":
        raise ValueError("invalid-knowledge-receipt")
    if raw.get("head_sha") != head_sha or raw.get("workspace_digest") != workspace_digest:
        raise ValueError("knowledge-receipt-binding-mismatch")
    for field in ("head_sha", "workspace_digest", "path", "revision", "content"):
        if not isinstance(raw.get(field), str) or not raw[field].strip():
            raise ValueError(f"invalid-knowledge-{field}")
    if raw.get("content_sha256") != hashlib.sha256(raw["content"].encode("utf-8")).hexdigest():
        raise ValueError("knowledge-content-hash-mismatch")
    if raw["kind"] == "documentation":
        path = raw["path"]
        if (
            PurePosixPath(path).is_absolute()
            or "\\" in path
            or ":" in path
            or any(part in {"", ".", ".."} for part in path.split("/"))
            or raw["revision"] != head_sha
        ):
            raise ValueError("invalid-knowledge-documentation-source")
    else:
        url = raw.get("url")
        if not isinstance(url, str):
            raise ValueError("invalid-knowledge-issue-source")
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.fragment
            or not parsed.path
            or raw["path"] != "issue:" + url
        ):
            raise ValueError("invalid-knowledge-issue-source")
    return copy.deepcopy(raw)
