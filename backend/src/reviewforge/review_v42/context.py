"""Assemble current work only; audit history is deliberately never implicit."""

from __future__ import annotations

from dataclasses import asdict

from .contracts import Case, EvidenceRef, Question, ReviewScope
from .store import ReviewStore


class ContextBuilder:
    def __init__(self, store: ReviewStore):
        self.store = store

    def reviewer(self, scope_id: str) -> dict:
        scope = self.store.get(ReviewScope, scope_id)
        questions = [q for q in self.store.list(Question) if q.scope_id == scope_id]
        cases = [c for c in self.store.list(Case) if c.scope_id == scope_id]
        refs = {r for q in questions for r in q.evidence_refs}
        refs.update(r for q in questions for item in q.interpretations for r in item.get("evidence_refs", []))
        refs.update(r for c in cases for r in c.evidence_refs + c.counter_evidence_refs)
        return {
            "pr": {
                key: self.store.run.metadata[key]
                for key in ("title", "description", "output_language")
                if key in self.store.run.metadata
            },
            "scope": asdict(scope),
            "questions": [asdict(q) for q in questions],
            "cases": [asdict(c) for c in cases],
            "evidence": [asdict(self.store.get(EvidenceRef, r)) for r in sorted(refs)],
            "scope_index": [{"id": s.id, "title": s.title, "status": s.status} for s in self.store.list(ReviewScope)],
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
