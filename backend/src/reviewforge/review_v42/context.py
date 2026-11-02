"""Assemble current work only; audit history is deliberately never implicit."""

from __future__ import annotations

from dataclasses import asdict

from .contracts import Case, EvidenceRef, NavigationItem, Question, ReviewScope
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
        if self.store.run.policy in {"v42-stateful-2", "v42-workspace-3"}:
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
                if self.store.run.policy in {"v42-stateful-2", "v42-workspace-3"}
                else [asdict(self.store.get(EvidenceRef, r)) for r in sorted(refs)]
            ),
            "scope_index": [{"id": s.id, "title": s.title, "status": s.status} for s in self.store.list(ReviewScope)],
        }
        if self.store.run.policy == "v42-workspace-3":
            result["navigation"] = [
                {key: getattr(item, key) for key in ("id", "kind", "text", "status", "evidence_refs", "revision")}
                for item in self.store.list(NavigationItem)
                if item.scope_id == scope_id and item.status == "open"
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
