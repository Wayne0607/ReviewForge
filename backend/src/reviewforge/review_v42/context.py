"""Assemble current work only; audit history is deliberately never implicit."""

from __future__ import annotations

import json
from dataclasses import asdict

from .contracts import Case, EvidenceRef, FrontierEntry, NavigationItem, Question, ReviewScope, Task
from .store import ReviewStore


def evidence_summary(ref: EvidenceRef) -> dict:
    """A navigation index, never a substitute for reading an evidence body."""
    return {name: getattr(ref, name) for name in ("id", "path", "side", "revision", "start_line", "end_line", "kind")}


class ContextBuilder:
    def __init__(self, store: ReviewStore):
        self.store = store

    def reviewer(self, scope_id: str) -> dict:
        scope = self.store.get(ReviewScope, scope_id)
        questions = [q for q in self.store.list(Question) if q.scope_id == scope_id]
        cases = [c for c in self.store.list(Case) if c.scope_id == scope_id]
        scope_view = asdict(scope)
        if self.store.run.policy in {
            "v42-stateful-2",
            "v42-workspace-3",
            "v42-frontier-4",
            "v42-frontier-5",
            "v42-units-6",
        }:
            for key in ("regions", "checked_regions"):
                for row in scope_view.get(key, []):
                    if "patch" in row:
                        row["patch_available_via_read_diff"] = True
                        del row["patch"]
        else:
            scope_view.pop("working_state", None)
        refs = {r for q in questions for r in q.evidence_refs}
        refs.update(r for q in questions for item in q.interpretations for r in item.get("evidence_refs", []))
        refs.update(r for c in cases for r in c.evidence_refs + c.counter_evidence_refs)
        result = {
            "pr": {
                key: self.store.run.metadata[key]
                for key in ("title", "description", "output_language")
                if key in self.store.run.metadata
            },
            "scope": scope_view,
            "questions": [asdict(q) for q in questions],
            "cases": [asdict(c) for c in cases],
            "evidence": (
                [evidence_summary(ref) for ref in self.store.list(EvidenceRef)]
                if self.store.run.policy
                in {"v42-stateful-2", "v42-workspace-3", "v42-frontier-4", "v42-frontier-5", "v42-units-6"}
                else [asdict(self.store.get(EvidenceRef, r)) for r in sorted(refs)]
            ),
            "scope_index": [{"id": s.id, "title": s.title, "status": s.status} for s in self.store.list(ReviewScope)],
        }
        if self.store.run.policy in {"v42-workspace-3", "v42-frontier-4", "v42-frontier-5", "v42-units-6"}:
            result["navigation"] = [
                {key: getattr(item, key) for key in ("id", "kind", "text", "status", "evidence_refs", "revision")}
                for item in self.store.list(NavigationItem)
                if item.scope_id == scope_id and item.status == "open"
            ]
        if self.store.run.policy == "v42-units-6":
            plan = self.store.get_unit_plan()
            if plan is not None:
                task_ids = [t.id for t in self.store.list(Task) if t.kind == "review" and t.object_id == scope_id]
                local_refs = {
                    ref for task_id in task_ids for entry in self.store.list_frontier(task_id) for ref in entry.aliases
                }
                local_refs.update(r for question in questions for r in question.evidence_refs)
                local_refs.update(
                    r
                    for question in questions
                    for item in question.interpretations
                    for r in item.get("evidence_refs", [])
                )
                local_refs.update(r for case in cases for r in case.evidence_refs + case.counter_evidence_refs)
                result["evidence"] = [
                    evidence_summary(ref) for ref in self.store.list(EvidenceRef) if ref.id in local_refs
                ]
                result["unit_plan"] = {
                    "primary_scope_ids": plan.primary_scope_ids,
                    "integration_scope_id": plan.integration_scope_id,
                    "manifest_hash": plan.manifest_hash,
                }
                if scope_id == plan.integration_scope_id:
                    result["integration_obligations"] = self.store.list_integration_obligations()
                    result["primary_units"] = [
                        {
                            "id": unit.id,
                            "title": unit.title,
                            "status": unit.status,
                            "region_ids": [r["id"] for r in unit.regions],
                        }
                        for unit in self.store.list(ReviewScope)
                        if unit.id in plan.primary_scope_ids
                    ]
        return result

    def navigation_page(self, scope_id: str, *, cursor: str | None = None, limit: int | None = None) -> dict:
        page = self.store.list_navigation(scope_id, cursor=cursor, limit=limit)
        return {
            "items": [
                {key: getattr(item, key) for key in ("id", "kind", "text", "status", "evidence_refs", "revision")}
                for item in page["items"]
            ],
            "next_cursor": page["next_cursor"],
        }

    def evidence_page(self, *, cursor: str | None = None, limit: int | None = None) -> dict:
        page = self.store.list_evidence_page(cursor=cursor, limit=limit)
        return {"items": [evidence_summary(ref) for ref in page["items"]], "next_cursor": page["next_cursor"]}

    def _search_document(self, ref_id: str) -> tuple[EvidenceRef, dict] | None:
        """Recognize only the fixed repository-search envelope; unknown forms stay raw."""
        if self.store.run.policy not in {"v42-frontier-5", "v42-units-6"}:
            return None
        ref = self.store.get(EvidenceRef, ref_id)
        meta = ref.metadata
        if not isinstance(meta, dict):
            return None
        tool = meta.get("tool_name")
        args = meta.get("tool_args")
        run = self.store.run
        if (
            ref.kind != "search"
            or ref.run_id != run.id
            or ref.repo != run.repo
            or ref.side != "head"
            or ref.revision != run.head
            or ref.path
            or ref.start_line is not None
            or ref.end_line is not None
            or meta.get("navigation_only") is not True
            or meta.get("status") != "available"
            or type(meta.get("complete")) is not bool
            or tool not in {"search_code", "find_references"}
            or not isinstance(args, dict)
            or not isinstance(meta.get("workspace_digest"), str)
            or not meta["workspace_digest"]
            or meta["workspace_digest"] != run.metadata.get("workspace_digest")
        ):
            return None
        query_key = "query" if tool == "search_code" else "symbol"
        if (
            set(args) - {query_key, "glob", "offset"}
            or not isinstance(args.get(query_key), str)
            or not args[query_key]
            or not isinstance(args.get("glob", ""), str)
            or type(args.get("offset", 0)) is not int
            or args.get("offset", 0) < 0
        ):
            return None

        def no_duplicates(pairs: list[tuple[str, object]]) -> dict:
            obj = {}
            for key, value in pairs:
                if key in obj:
                    raise ValueError("duplicate JSON key")
                obj[key] = value
            return obj

        raw = self.store.read_evidence(ref_id)
        try:
            document = json.loads(raw.decode("utf-8"), object_pairs_hook=no_duplicates)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            return None
        if not isinstance(document, dict) or set(document) != {
            "query",
            "glob",
            "matches",
            "offset",
            "next_offset",
            "total_matches",
            "skipped_paths",
            "aliases_resolved",
        }:
            return None
        matches, skipped = document["matches"], document["skipped_paths"]
        offset, total, following = document["offset"], document["total_matches"], document["next_offset"]
        if (
            document["query"] != args[query_key]
            or document["glob"] != args.get("glob", "")
            or type(offset) is not int
            or offset != args.get("offset", 0)
            or type(total) is not int
            or total < 0
            or (following is not None and type(following) is not int)
            or following != (offset + 100 if offset + 100 < total else None)
            or not isinstance(matches, list)
            or len(matches) != min(100, max(0, total - offset))
            or not isinstance(skipped, list)
            or any(not isinstance(path, str) or not path for path in skipped)
            or document["aliases_resolved"] is not False
            or meta["complete"] != (not skipped and following is None)
        ):
            return None
        if any(
            not isinstance(match, dict)
            or set(match) != {"path", "line", "text"}
            or not isinstance(match["path"], str)
            or not match["path"]
            or type(match["line"]) is not int
            or match["line"] < 1
            or not isinstance(match["text"], str)
            for match in matches
        ):
            return None
        return ref, document

    def search_index(self, entry: FrontierEntry) -> dict | None:
        """Project a complete pinned search as untrusted navigation, or keep it raw."""
        if self.store.run.policy not in {"v42-frontier-5", "v42-units-6"} or entry.kind != "search":
            return None
        current = next((item for item in self.store.list_frontier(entry.task_id) if item.id == entry.id), None)
        if current is None or asdict(current) != asdict(entry) or not entry.aliases:
            return None
        sources = []
        for ref_id in entry.aliases:
            parsed = self._search_document(ref_id)
            if parsed is None:
                return None
            ref, document = parsed
            # A page of a search document must not be projected as the whole source.
            if ref.content_hash != entry.content_hash:
                return None
            sources.append(
                {
                    "ref": ref.id,
                    "tool_name": ref.metadata["tool_name"],
                    "tool_args": ref.metadata["tool_args"],
                    "run_id": ref.run_id,
                    "repo": ref.repo,
                    "head": self.store.run.head,
                    "side": ref.side,
                    "revision": ref.revision,
                    "content_hash": ref.content_hash,
                    "offset": document["offset"],
                    "next_offset": document["next_offset"],
                    "total_matches": document["total_matches"],
                    "matches": [{"path": match["path"], "line": match["line"]} for match in document["matches"]],
                    "skipped_count": len(document["skipped_paths"]),
                    "complete": ref.metadata["complete"],
                    "aliases_resolved": False,
                }
            )
        return {
            "kind": "search_index",
            "entry_id": entry.id,
            "sources": sources,
            "notice": "Navigation only, not evidence. Full original and skipped paths remain available by ref.",
        }

    def search_section(self, ref_id: str, section: str, *, offset: int, limit: int) -> dict:
        """Explicitly page original search matches or skipped paths without truncation."""
        if section not in {"matches", "skipped_paths"}:
            raise ValueError("unknown search section")
        if type(offset) is not int or offset < 0 or type(limit) is not int or limit < 1:
            raise ValueError("offset must be nonnegative and limit must be positive")
        parsed = self._search_document(ref_id)
        if parsed is None:
            raise ValueError("reference is not a recognized fixed search result")
        ref, document = parsed
        items = document[section]
        if offset > len(items):
            raise ValueError("offset is outside search section")
        end = min(offset + limit, len(items))
        return {
            "kind": "search_section",
            "ref": ref.id,
            "section": section,
            "offset": offset,
            "items": items[offset:end],
            "next_cursor": end if end < len(items) else None,
            "total_items": len(items),
            "search_offset": document["offset"],
            "search_next_offset": document["next_offset"],
            "search_total_matches": document["total_matches"],
            "complete": ref.metadata["complete"],
            "content_hash": ref.content_hash,
            "navigation_only": True,
            "tool_name": ref.metadata["tool_name"],
            "tool_args": ref.metadata["tool_args"],
        }

    def verifier(self, case_id: str) -> dict:
        case = self.store.get(Case, case_id)
        # Whitelist claim inputs rather than forwarding reviewer analysis or verdict fields.
        claims = [{key: claim[key] for key in ("id", "statement", "impact") if key in claim} for claim in case.claims]

        def sources(ids: list[str]) -> list[dict]:
            result = []
            for ref in ids:
                raw = self.store.read_evidence(ref)
                try:
                    content, status = raw.decode("utf-8"), "available"
                except UnicodeDecodeError:
                    content, status = None, "unavailable_non_utf8"
                result.append({"ref": asdict(self.store.get(EvidenceRef, ref)), "content": content, "status": status})
            return result

        return {
            "case_id": case.id,
            "revision": case.revision,
            "trigger": case.trigger,
            "mechanism": case.mechanism,
            "claims": claims,
            "evidence": sources(case.evidence_refs),
            "counter_evidence": sources(case.counter_evidence_refs),
        }
