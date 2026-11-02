"""Deterministic recovery inputs for model context-capacity failures."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from typing import Any

from reviewforge.review_v42.context import ContextBuilder
from reviewforge.review_v42.contracts import EvidenceRef


def is_context_length_exceeded(exc: BaseException) -> bool:
    """Only a provider's explicit structured error code is a capacity signal."""
    if getattr(exc, "code", None) == "context_length_exceeded":
        return True
    pending = [getattr(exc, "body", None)]
    response = getattr(exc, "response", None)
    if response is not None:
        try:
            pending.append(response.json())
        except (AttributeError, TypeError, ValueError):
            pass
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            if item.get("code") == "context_length_exceeded":
                return True
            pending.extend(value for value in item.values() if isinstance(value, (dict, list, tuple)))
        elif isinstance(item, (list, tuple)):
            pending.extend(item)
    return False


def evidence_directory(store: Any) -> list[dict[str, Any]]:
    """List every stored source, including observations not linked to a case.

    The source bodies remain in immutable storage. Entries include the original
    tool invocation to distinguish searches and support explicit full rereads.
    """
    return [asdict(ref) for ref in store.list(EvidenceRef)]


def reviewer_context(store: Any, scope_id: str) -> dict[str, Any]:
    """Reconstruct current decisions and a complete, metadata-only source index."""
    state = ContextBuilder(store).reviewer(scope_id)
    # Scope revision is controller-owned and advances on lease/status changes;
    # it is not an extra review fact or a reason to retry the same oversized input.
    state["scope"].pop("revision", None)
    for key in ("regions", "checked_regions"):
        for row in state["scope"].get(key, []):
            if "patch" in row:
                row["patch_available_via_read_diff"] = True
                del row["patch"]
    state["evidence"] = evidence_directory(store)
    state["context_recovery"] = {
        "patches": "Complete changed-region patches are available via read_diff(path).",
        "evidence": "All saved observations are indexed above. read_evidence(ref) returns the complete original; "
        "optional start_line/end_line return explicit pages with continuation metadata.",
        "coverage": "The index is navigation, not evidence that its contents were read or reviewed.",
    }
    return state


def action_receipt_signature(name: str, result: dict[str, Any]) -> str:
    """Ignore bookkeeping churn when deciding if the same tool receipt repeated.

    Original bytes and full metadata remain untouched in the evidence store.
    Only known transient fields in structured test receipts are ignored here.
    """
    transient = {"timestamp", "created_at", "started_at", "finished_at", "image_id", "container_id"}

    def clean(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: clean(item) for key, item in value.items() if key not in transient}
        if isinstance(value, list):
            return [clean(item) for item in value]
        return value

    comparable = {key: value for key, value in result.items() if key not in {"evidence_ref", "metadata"}}
    metadata = result.get("metadata") or {}
    comparable["metadata"] = {
        key: metadata[key] for key in ("status", "complete", "navigation_only") if key in metadata
    }
    if name == "run_check" and isinstance(comparable.get("content"), str):
        try:
            comparable["content"] = clean(json.loads(comparable["content"]))
        except (ValueError, TypeError):
            pass
    return json.dumps(comparable, ensure_ascii=False, sort_keys=True, default=str)


def context_fingerprint(messages: list[dict[str, Any]]) -> str:
    encoded = json.dumps(messages, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def encode_session(messages: list[dict[str, Any]], failed_contexts: set[str]) -> list[dict[str, Any]]:
    return [*messages, {"type": "v42_context_recovery", "failed_contexts": sorted(failed_contexts)}]


def decode_session(records: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], set[str]]:
    messages, failed = [], set()
    for record in records:
        if record.get("type") == "v42_context_recovery":
            failed.update(value for value in record.get("failed_contexts", []) if isinstance(value, str))
        elif record.get("type") != "v42_question_failures":
            messages.append(record)
    return messages, failed
