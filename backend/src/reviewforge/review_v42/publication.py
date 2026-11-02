"""Deterministic local report and publication-ready comments; no network delivery."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from typing import Any

from .contracts import Case, EvidenceRef, NavigationItem, Question, ReviewScope
from .verifier import POLICY, validate_result


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


class Publication:
    def __init__(self, store: Any, *, policy: str = POLICY):
        self.store, self.policy = store, policy

    def _anchor(self, result: dict[str, Any]) -> dict[str, Any] | None:
        regions = self.store.run.metadata.get("changed_regions", [])
        for citation in result["facts"]["actual"]["citations"]:
            ref = self.store.get(EvidenceRef, citation["ref"])
            if ref.side != "head" or ref.revision != self.store.run.head or not ref.path:
                continue
            start, end = citation.get("start_line"), citation.get("end_line")
            if not isinstance(start, int) or not isinstance(end, int):
                continue
            content = self.store.read_evidence(ref.id).decode("utf-8")
            selected = "\n".join(content.splitlines()[start - ref.start_line : end - ref.start_line + 1])
            quote = citation["quote"]
            positions = []
            offset = selected.find(quote)
            while offset >= 0:
                first = start + selected[:offset].count("\n")
                positions.append((first, first + quote.count("\n")))
                offset = selected.find(quote, offset + 1)
            for region in regions:
                if (
                    not isinstance(region, dict)
                    or region.get("unsupported")
                    or region.get("side") != "head"
                    or region.get("path") != ref.path
                ):
                    continue
                for line in region.get("changed_lines", []):
                    if isinstance(line, int) and any(first <= line <= last for first, last in positions):
                        return {"path": ref.path, "line": line, "side": "RIGHT"}
        return None

    def local_report(self) -> dict[str, Any]:
        scopes = self.store.list(ReviewScope)
        cases = self.store.list(Case)
        questions = self.store.list(Question)
        comments, verified, unresolved, duplicates = [], [], [], []
        seen = {}
        for case in cases:
            if not case.claims:
                unresolved.append({"case_id": case.id, "reason": "candidate has no concrete claim"})
                continue
            for claim in case.claims:
                claim_id = claim.get("id", "")
                try:
                    decision = self.store.get_verdict(case.id, claim_id, self.policy)
                    if not decision:
                        unresolved.append({"case_id": case.id, "claim_id": claim_id, "reason": "not verified"})
                        continue
                    if (
                        decision["run_id"],
                        decision["repo"],
                        decision["base"],
                        decision["head"],
                        decision["case_revision"],
                        decision["policy"],
                    ) != (
                        self.store.run.id,
                        self.store.run.repo,
                        self.store.run.base,
                        self.store.run.head,
                        case.revision,
                        self.policy,
                    ):
                        raise ValueError("verdict binding mismatch")
                    if (
                        decision["result"].get("case_trigger") != case.trigger
                        or decision["result"].get("claim_id") != claim_id
                        or decision["result"].get("claim_statement") != claim.get("statement")
                    ):
                        raise ValueError("original trigger or claim identity mismatch")
                    result = validate_result(self.store, decision["result"], set(decision["evidence_refs"]))
                    if result["verdict"] != decision["verdict"]:
                        raise ValueError("verdict/result mismatch")
                    item = {
                        "case_id": case.id,
                        "claim_id": claim_id,
                        "case_revision": case.revision,
                        "verdict": result["verdict"],
                        "result": result,
                    }
                    verified.append(item)
                    if result["verdict"] in {"needs_evidence", "unresolved"}:
                        unresolved.append(
                            {
                                "case_id": case.id,
                                "claim_id": claim_id,
                                "reason": result["reason"],
                                "gaps": result["gaps"],
                            }
                        )
                        continue
                    if result["verdict"] != "accept":
                        continue
                    facts = result["facts"]
                    chinese = self.store.run.metadata.get("output_language") == "zh-CN"
                    labels = (
                        ("触发条件", "实际行为", "预期行为", "改动关联", "影响")
                        if chinese
                        else ("Trigger", "Actual behavior", "Expected behavior", "Change", "Impact")
                    )
                    body = "\n\n".join(
                        (
                            f"**{labels[0]}:** " + case.trigger + " — " + facts["trigger"]["statement"],
                            f"**{labels[1]}:** " + facts["actual"]["statement"],
                            f"**{labels[2]}:** " + facts["expected"]["statement"],
                            f"**{labels[3]}:** " + facts["change"]["statement"],
                            f"**{labels[4]}:** " + facts["impact"]["statement"],
                        )
                    )
                    if any(
                        self.store.get(EvidenceRef, cite["ref"]).kind == "issue"
                        for cite in facts["expected"]["citations"]
                    ):
                        body += (
                            "\n\n**证据时点：** 外部 Issue 快照不能证明该需求在被审查的版本中已经存在。"
                            if chinese
                            else "\n\n**Evidence timing:** The external issue snapshot does not establish "
                            "that the requirement existed at the reviewed head revision."
                        )
                    anchor = self._anchor(result)
                    # Only exact semantic identity is deduplicated, not similar wording or path.
                    identity = _digest([case.trigger, case.mechanism, facts, anchor])
                    if identity in seen:
                        duplicates.append({"case_id": case.id, "claim_id": claim_id, "duplicate_of": seen[identity]})
                        continue
                    seen[identity] = {"case_id": case.id, "claim_id": claim_id}
                    if anchor is None:
                        unresolved.append(
                            {
                                "case_id": case.id,
                                "claim_id": claim_id,
                                "reason": "no verified changed head line for inline comment",
                            }
                        )
                        continue
                    authorization = _digest([decision, anchor, body])
                    comments.append(
                        {
                            "id": authorization,
                            "case_id": case.id,
                            "claim_id": claim_id,
                            "case_revision": case.revision,
                            "source_digest": decision["source_digest"],
                            "anchor": anchor,
                            "body": body,
                        }
                    )
                except (KeyError, ValueError, TypeError, OSError, UnicodeError) as exc:
                    unresolved.append(
                        {"case_id": case.id, "claim_id": claim_id, "reason": "invalid verification: " + str(exc)}
                    )
        active_scopes = [s for s in scopes if s.status != "superseded"]
        workspace = self.store.run.policy in {"v42-workspace-3", "v42-frontier-4", "v42-frontier-5"}
        unreviewed = [s.id for s in active_scopes if s.status != "reviewed" or (workspace and bool(s.unresolved))]
        open_questions = [q.id for q in questions if q.status in {"open", "disputed", "unresolved"}]
        open_navigation = []
        if workspace:
            active_scope_ids = {scope.id for scope in active_scopes}
            open_navigation = [
                {"id": item.id, "scope_id": item.scope_id, "text": item.text, "revision": item.revision}
                for item in self.store.list(NavigationItem)
                if item.scope_id in active_scope_ids and item.kind == "question" and item.status == "open"
            ]
            unresolved.extend({"kind": "navigation_question", **item} for item in open_navigation)
        status = "complete" if active_scopes and not unreviewed and not unresolved and not open_questions else "partial"
        report = {
            "run": asdict(self.store.run),
            "status": status,
            "scopes": [asdict(s) for s in scopes],
            "candidates": sum(len(case.claims) for case in cases),
            "verified": verified,
            "comments": comments,
            "unresolved": unresolved,
            "open_questions": open_questions,
            "duplicates": duplicates,
        }
        if workspace:
            report["open_navigation_questions"] = open_navigation
        return report


def local_report(store: Any) -> dict[str, Any]:
    return Publication(store).local_report()


class PublicationOutbox:
    """Durable local intents. A dispatcher must call prepare immediately before I/O.

    ``sending`` and ``unknown`` deliberately cannot be prepared again. A caller
    must reconcile ambiguous external delivery before it can request another try.
    This class never performs external I/O.
    """

    def __init__(self, store: Any, *, policy: str = POLICY):
        self.store = store
        self.publication = Publication(store, policy=policy)
        with self.store._connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS publication_outbox ("
                "id TEXT PRIMARY KEY, payload TEXT NOT NULL, status TEXT NOT NULL, receipt TEXT)"
            )
            db.execute("CREATE TABLE IF NOT EXISTS publication_batches (id TEXT PRIMARY KEY, payload TEXT NOT NULL)")

    def freeze(self) -> list[dict[str, Any]]:
        return self.freeze_batch()["report"]["comments"]

    def freeze_batch(self) -> dict[str, Any]:
        """Freeze the full report and its publishable intents in one transaction."""
        with self.store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            report = self.publication.local_report()
            batch_id = _digest(report)
            payload = json.dumps(report, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
            db.execute("INSERT OR IGNORE INTO publication_batches (id,payload) VALUES (?,?)", (batch_id, payload))
            comments = report["comments"]
            for comment in comments:
                payload = json.dumps(comment, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
                row = db.execute("SELECT payload FROM publication_outbox WHERE id=?", (comment["id"],)).fetchone()
                if row and row[0] != payload:
                    raise ValueError("publication identity collision")
                db.execute(
                    "INSERT OR IGNORE INTO publication_outbox (id,payload,status) VALUES (?,?,'pending')",
                    (comment["id"], payload),
                )
            db.commit()
        return {"id": batch_id, "report": report}

    def get_batch(self, identity: str) -> dict[str, Any]:
        with self.store._connect() as db:
            row = db.execute("SELECT payload FROM publication_batches WHERE id=?", (identity,)).fetchone()
        if not row:
            raise KeyError(identity)
        return {"id": identity, "report": json.loads(row[0])}

    def get(self, identity: str) -> dict[str, Any]:
        with self.store._connect() as db:
            row = db.execute("SELECT payload,status,receipt FROM publication_outbox WHERE id=?", (identity,)).fetchone()
        if not row:
            raise KeyError(identity)
        return {"comment": json.loads(row[0]), "status": row[1], "receipt": json.loads(row[2]) if row[2] else None}

    def prepare(self, identity: str) -> dict[str, Any]:
        """Atomically revalidate permission and claim one intent for a send."""
        with self.store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload,status FROM publication_outbox WHERE id=?", (identity,)).fetchone()
            if not row:
                raise KeyError(identity)
            if row[1] != "pending":
                raise ValueError("only a pending intent can be prepared; reconcile ambiguous delivery first")
            frozen = json.loads(row[0])
            current = {comment["id"]: comment for comment in self.publication.local_report()["comments"]}
            if current.get(identity) != frozen:
                raise ValueError("publication permission or final body is no longer current")
            db.execute("UPDATE publication_outbox SET status='sending' WHERE id=?", (identity,))
            db.commit()
        return frozen

    def acknowledge(self, identity: str, receipt: dict[str, Any]) -> None:
        """Record confirmed delivery; repeating an identical receipt is safe."""
        if not isinstance(receipt, dict) or not receipt:
            raise ValueError("a delivery receipt is required")
        encoded = json.dumps(receipt, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        with self.store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status,receipt FROM publication_outbox WHERE id=?", (identity,)).fetchone()
            if not row:
                raise KeyError(identity)
            if row == ("delivered", encoded):
                return
            if row[0] != "sending":
                raise ValueError("delivery is not in flight")
            db.execute("UPDATE publication_outbox SET status='delivered',receipt=? WHERE id=?", (encoded, identity))
            db.commit()

    def mark_unknown(self, identity: str) -> None:
        with self.store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status FROM publication_outbox WHERE id=?", (identity,)).fetchone()
            if not row:
                raise KeyError(identity)
            if row[0] == "unknown":
                return
            if row[0] != "sending":
                raise ValueError("only an in-flight send can become unknown")
            db.execute("UPDATE publication_outbox SET status='unknown' WHERE id=?", (identity,))
            db.commit()

    def reconcile(self, identity: str, *, delivered: bool, evidence: dict[str, Any]) -> None:
        """Resolve an unknown send from an external delivery lookup."""
        if type(delivered) is not bool or not isinstance(evidence, dict) or not evidence:
            raise ValueError("reconciliation needs a finding and supporting receipt")
        encoded = json.dumps(evidence, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        with self.store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status FROM publication_outbox WHERE id=?", (identity,)).fetchone()
            if not row:
                raise KeyError(identity)
            if row[0] != "unknown":
                raise ValueError("only unknown delivery can be reconciled")
            db.execute(
                "UPDATE publication_outbox SET status=?,receipt=? WHERE id=?",
                ("delivered" if delivered else "pending", encoded, identity),
            )
            db.commit()
