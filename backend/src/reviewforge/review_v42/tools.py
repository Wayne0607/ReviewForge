"""Read-only tool adapter: store the complete raw result before returning it."""

from __future__ import annotations

import json
from typing import Any

from reviewforge.review_v42.contracts import EvidenceRef


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
            ref = self.store.get(EvidenceRef, args["ref"])
            raw = self.store.read_evidence(ref.id)
            try:
                content = raw.decode("utf-8")
            except UnicodeDecodeError:
                return {"evidence_ref": ref.id, "content": None, "status": "unavailable_non_utf8"}
            start, end = args.get("start_line"), args.get("end_line")
            if start is None and end is None:
                return {"evidence_ref": ref.id, "content": content, "complete": True}
            if type(start) is not int or type(end) is not int or start < 1 or end < start:
                raise ValueError("start_line and end_line must be a valid explicit page")
            first = ref.start_line or 1
            lines = content.splitlines(keepends=True)
            last = first + len(lines) - 1
            if start < first or start > last:
                raise ValueError("page begins outside the stored evidence")
            actual_end = min(end, last)
            return {
                "evidence_ref": ref.id,
                "content": "".join(lines[start - first : actual_end - first + 1]),
                "complete": start == first and actual_end == last,
                "start_line": start,
                "end_line": actual_end,
                "first_line": first,
                "last_line": last,
                "next_line": actual_end + 1 if actual_end < last else None,
                "full_content_available": True,
            }
        result = await self.gateway(name, args)
        if not isinstance(result, dict) or not all(key in result for key in ("content", "revision", "side", "kind")):
            raise ValueError("tool result missing bound evidence envelope")
        status = (result.get("metadata") or {}).get("status", "available")
        if status in {"unavailable", "error", "failed"}:
            raise RuntimeError(f"tool {name}: {status}")
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
        return {"evidence_ref": ref.id, "content": visible, "metadata": result.get("metadata") or {}}
