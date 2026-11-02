"""Read-only tool adapter: store the complete raw result before returning it."""

from __future__ import annotations

import json
from typing import Any

from reviewforge.review_v42.contracts import BindingError, EvidenceRef
from reviewforge.review_v42.line_semantics import source_lines, source_slice


class ToolUnavailableError(RuntimeError):
    """A tool reported an unavailable result, with its safe diagnostic reason."""

    def __init__(self, name: str, status: str, reason: str = ""):
        self.name, self.status, self.reason = name, status, reason
        super().__init__(f"tool {name}: {status}" + (f": {reason}" if reason else ""))


class InvalidEvidenceRequestError(ValueError):
    """An evidence ID or page is invalid; this does not imply source unavailability."""


class EvidenceGateway:
    """Adapt an async callable(name, args) returning a bound evidence envelope.

    The envelope must contain content, revision, side and kind; path, ranges,
    environment and metadata are optional. Failures raise and are not evidence.
    No automatic truncation is applied to the returned content.
    """

    def __init__(self, store: Any, gateway: Any):
        self.store = store
        self.gateway = gateway
        self.tool_specs = getattr(gateway, "tool_specs", None)

    async def execute(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        if name == "read_evidence":
            try:
                ref = self.store.get(EvidenceRef, args["ref"])
            except KeyError as exc:
                if self.store.run.policy in {"v42-frontier-4", "v42-frontier-5", "v42-units-6"}:
                    raise InvalidEvidenceRequestError(
                        "unknown evidence reference; consult the evidence directory"
                    ) from exc
                raise
            start, end = args.get("start_line"), args.get("end_line")
            if (
                self.store.run.policy in {"v42-frontier-4", "v42-frontier-5", "v42-units-6"}
                and (start is not None or end is not None)
                and (type(start) is not int or type(end) is not int or start < 1 or end < start)
            ):
                raise InvalidEvidenceRequestError("start_line and end_line must be a valid explicit page")
            raw = self.store.read_evidence(ref.id)
            try:
                content = raw.decode("utf-8")
            except UnicodeDecodeError:
                return {"evidence_ref": ref.id, "content": None, "status": "unavailable_non_utf8"}
            if start is None and end is None:
                return {"evidence_ref": ref.id, "content": content, "complete": True}
            if type(start) is not int or type(end) is not int or start < 1 or end < start:
                if self.store.run.policy in {"v42-frontier-4", "v42-frontier-5", "v42-units-6"}:
                    raise InvalidEvidenceRequestError("start_line and end_line must be a valid explicit page")
                raise ValueError("start_line and end_line must be a valid explicit page")
            first = ref.start_line or 1
            lines = source_lines(self.store.run, content, keepends=True)
            last = first + len(lines) - 1
            if start < first or start > last:
                if self.store.run.policy in {"v42-frontier-4", "v42-frontier-5", "v42-units-6"}:
                    raise InvalidEvidenceRequestError("page begins outside the stored evidence")
                raise ValueError("page begins outside the stored evidence")
            actual_end = min(end, last)
            page_content = source_slice(self.store.run, content, start - first + 1, actual_end - first + 1)
            result = {
                "evidence_ref": ref.id,
                "content": page_content,
                "complete": start == first and actual_end == last,
                "start_line": start,
                "end_line": actual_end,
                "first_line": first,
                "last_line": last,
                "next_line": actual_end + 1 if actual_end < last else None,
                "full_content_available": True,
            }
            if (
                self.store.run.metadata.get("shared_investigation") == "shared-1"
                and ref.kind == "source"
                and ref.side in {"base", "head"}
            ):
                info = ref.metadata
                digest = self.store.run.metadata.get("workspace_digest")
                if (
                    ref.run_id != self.store.run.id
                    or ref.repo != self.store.run.repo
                    or ref.revision != getattr(self.store.run, ref.side)
                    or not ref.path
                    or not isinstance(digest, str)
                    or not digest
                    or info.get("workspace_digest") != digest
                    or info.get("status", "available") not in {"available", "success", "ok"}
                    or info.get("complete", True) is not True
                    or info.get("navigation_only") is True
                    or info.get("tool_name") not in {None, "read_source"}
                    or (ref.end_line is not None and ref.end_line != last)
                ):
                    raise BindingError("source page requires an available, complete fixed-snapshot source")
                page_ref = self.store.put_evidence(
                    page_content.encode("utf-8"),
                    revision=ref.revision,
                    side=ref.side,
                    kind=ref.kind,
                    path=ref.path,
                    start_line=start,
                    end_line=actual_end,
                    environment=ref.environment,
                    metadata={**info, "parent_evidence_ref": ref.id},
                )
                result["evidence_ref"] = page_ref.id
                result["original_evidence_ref"] = ref.id
                result["requested_end_line"] = end
                result["actual_end_line"] = actual_end
            return result
        result = await self.gateway(name, args)
        if not isinstance(result, dict) or not all(key in result for key in ("content", "revision", "side", "kind")):
            raise ValueError("tool result missing bound evidence envelope")
        metadata = result.get("metadata") or {}
        status = metadata.get("status", "available")
        if status in {"unavailable", "error", "failed"}:
            receipt = metadata.get("original_receipt") or {}
            reason = metadata.get("reason") or (receipt.get("reason") if isinstance(receipt, dict) else "")
            if not reason and result["kind"] == "failure":
                reason = result["content"]
            raise ToolUnavailableError(name, status, reason if isinstance(reason, str) else "")
        if status in {"not_found", "not_changed", "out_of_range"} and (
            result["kind"] != "query" or metadata.get("navigation_only") is not True
        ):
            raise ValueError("negative query result requires navigation-only query provenance")
        content = result["content"]
        if not isinstance(content, (str, bytes)):
            content = json.dumps(content, ensure_ascii=False, sort_keys=True)
        blob = content.encode("utf-8") if isinstance(content, str) else content
        ref = self.store.put_evidence(
            blob,
            revision=result["revision"],
            side=result["side"],
            kind=result["kind"],
            path=result.get("path", ""),
            start_line=result.get("start_line"),
            end_line=result.get("end_line"),
            environment=result.get("environment", ""),
            metadata={**(result.get("metadata") or {}), "tool_name": name, "tool_args": args},
        )
        try:
            visible = blob.decode("utf-8")
        except UnicodeDecodeError:
            return {
                "evidence_ref": ref.id,
                "content": None,
                "status": "unavailable_non_utf8",
                "metadata": result.get("metadata") or {},
            }
        return {**result, "evidence_ref": ref.id, "content": visible, "metadata": metadata}
