"""Deterministic local report and publication-ready comments; no network delivery."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from typing import Any

from .contracts import Case, EvidenceRef, NavigationItem, Question, ReviewScope
from .verifier import POLICY, _citation, validate_result


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def _unit_manifest_errors(store: Any, scopes: list[ReviewScope]) -> list[str]:
    """Defensively compare unit coverage to the immutable run manifest."""
    regions = store.run.metadata.get("changed_regions")
    if not isinstance(regions, list) or not regions:
        return ["fixed changed-region manifest is absent or empty"]
    expected: dict[str, dict[str, Any]] = {}
    for region in regions:
        if not isinstance(region, dict) or not isinstance(region.get("id"), str) or not region["id"]:
            return ["fixed changed-region manifest has an invalid region"]
        if region["id"] in expected:
            return ["fixed changed-region manifest contains duplicate identities"]
        expected[region["id"]] = region
    errors = []
    owned: dict[str, str] = {}
    for scope in scopes:
        if scope.status == "superseded" or not scope.regions:
            continue
        if scope.run_id != store.run.id:
            errors.append(f"unit {scope.id} belongs to another run")
            continue
        assigned = {}
        for region in scope.regions:
            if not isinstance(region, dict) or not isinstance(region.get("id"), str):
                errors.append(f"unit {scope.id} has an invalid assigned region")
                continue
            identity = region["id"]
            if identity in assigned or identity in owned:
                errors.append(f"region {identity} has multiple primary owners")
            assigned[identity] = region
            owned[identity] = scope.id
            if identity not in expected or expected[identity] != region:
                errors.append(f"region {identity} differs from the fixed manifest")
            if any(region.get(key) for key in ("unsupported", "unavailable", "binary", "parse_failed")):
                errors.append(f"region {identity} has unavailable or unsupported source")
        checked = {}
        for region in scope.checked_regions:
            if not isinstance(region, dict) or not isinstance(region.get("id"), str):
                errors.append(f"unit {scope.id} has an invalid checked-region receipt")
                continue
            identity = region["id"]
            if identity in checked:
                errors.append(f"unit {scope.id} repeats checked region {identity}")
            checked[identity] = region
        if scope.status != "reviewed" or scope.unresolved or checked != assigned:
            errors.append(f"unit {scope.id} has incomplete or inconsistent region receipts")
    missing = sorted(set(expected) - set(owned))
    if missing:
        errors.append("manifest regions have no primary owner: " + ", ".join(missing))
    return errors


def _unit_integration_errors(store: Any, scopes: list[ReviewScope], db: Any = None) -> list[str]:
    """Check the independently persisted plan and every current integration receipt."""
    try:
        state = store.validate_units_completion(db=db)
        if not isinstance(state, dict):
            raise ValueError("integration validation returned an invalid view")
        plan = state.get("plan")
        review = state.get("integration_review")
        obligations = state.get("obligations")
        if not isinstance(plan, dict) or not isinstance(review, dict):
            raise ValueError("independent integration plan or review is missing")
        ids = plan.get("primary_scope_ids")
        if (
            plan.get("run_id") != store.run.id
            or plan.get("manifest_hash") != _digest(store.run.metadata.get("changed_regions"))
            or not plan.get("workspace_digest")
            or not plan.get("base_workspace_digest")
            or plan.get("workspace_digest") != store.run.metadata.get("workspace_digest")
            or plan.get("base_workspace_digest") != store.run.metadata.get("base_workspace_digest")
            or not isinstance(ids, list)
            or not ids
            or len(ids) != len(set(ids))
            or not all(isinstance(item, str) and item for item in ids)
        ):
            raise ValueError("unit plan is not bound to this fixed run and manifest")
        by_id = {scope.id: scope for scope in scopes if scope.status != "superseded"}
        primaries = {scope.id for scope in by_id.values() if scope.regions}
        integration_id = plan.get("integration_scope_id")
        integration = by_id.get(integration_id)
        if (
            set(ids) != primaries
            or integration_id in primaries
            or integration is None
            or integration.run_id != store.run.id
            or integration.regions
            or sorted(integration.dependencies) != sorted(ids)
            or review.get("run_id") != store.run.id
            or review.get("scope_id") != integration_id
            or not review.get("id")
        ):
            raise ValueError("integration review is not the dedicated plan scope")
        tasks = state.get("integration_tasks")
        if not isinstance(tasks, list) or not all(isinstance(task, dict) for task in tasks):
            raise ValueError("independent integration task is missing")
        integration_tasks = {
            task.get("id"): task
            for task in tasks
            if task.get("run_id") == store.run.id
            and task.get("kind") == "review"
            and task.get("object_id") == integration_id
            and isinstance(task.get("id"), str)
        }
        if not any(
            task.get("status") == "completed" and task.get("input_revision") == integration.revision
            for task in integration_tasks.values()
        ):
            raise ValueError("independent integration task has not completed current scope")
        if not isinstance(obligations, list) or not obligations:
            raise ValueError("independent integration has no obligations")
        unit_coverage: set[str] = set()
        cross_coverage = False
        seen: set[str] = set()
        for obligation in obligations:
            if not isinstance(obligation, dict):
                raise ValueError("invalid integration obligation")
            oid = obligation.get("id")
            involved = obligation.get("unit_ids")
            refs = obligation.get("evidence_refs")
            if (
                not isinstance(oid, str)
                or not oid
                or oid in seen
                or obligation.get("run_id") != store.run.id
                or obligation.get("integration_scope_id") != integration_id
                or obligation.get("task_id") not in integration_tasks
                or not obligation.get("turn_id")
                or not isinstance(involved, list)
                or not involved
                or len(set(involved)) != len(involved)
                or not set(involved) <= set(ids)
                or not isinstance(obligation.get("description"), str)
                or not obligation["description"].strip()
                or obligation.get("valid") is not True
                or obligation.get("status") != "checked"
                or obligation.get("disposition") != "checked"
                or not obligation.get("input_digest")
                or obligation.get("input_digest") != obligation.get("receipt_digest")
                or not isinstance(obligation.get("reason"), str)
                or not obligation["reason"].strip()
                or not isinstance(refs, list)
                or not refs
            ):
                raise ValueError("integration obligation is incomplete, stale or incorrectly bound")
            seen.add(oid)
            if obligation.get("kind") == "discovered":
                origin = obligation.get("origin_evidence_refs")
                if (
                    not isinstance(origin, list)
                    or not origin
                    or not set(origin) <= set(refs)
                    or not obligation.get("origin_task_id")
                    or not obligation.get("origin_turn_id")
                ):
                    raise ValueError("discovered relationship lost its original source or provenance")
            if obligation.get("kind") == "unit" and len(involved) == 1:
                unit_coverage.add(involved[0])
            if obligation.get("kind") == "cross_unit" and set(involved) == set(ids):
                cross_coverage = True
            source_paths: set[str] = set()
            for ref_id in refs:
                ref = store.get(EvidenceRef, ref_id)
                text = store.read_evidence(ref.id).decode("utf-8")
                nonempty = next(((i, line) for i, line in enumerate(text.splitlines()) if line.strip()), None)
                if nonempty is None:
                    raise ValueError("integration source is empty")
                index, quote = nonempty
                citation = {"ref": ref.id, "quote": quote}
                if ref.start_line is not None:
                    citation.update(start_line=ref.start_line + index, end_line=ref.start_line + index)
                _citation(store, citation)
                if ref.kind == "source" and ref.side in {"base", "head"}:
                    source_paths.add(ref.path)
            for unit_id in involved:
                if not source_paths.intersection(region.get("path") for region in by_id[unit_id].regions):
                    raise ValueError("integration receipt lacks source for an involved primary unit")
        if unit_coverage != set(ids) or not cross_coverage:
            raise ValueError("integration lacks a primary or cross-unit obligation")
        if state.get("valid") is not True:
            reasons = state.get("reasons")
            raise ValueError("; ".join(map(str, reasons)) if reasons else "integration is not complete")
    except (AttributeError, KeyError, TypeError, ValueError, OSError, UnicodeError) as exc:
        return [str(exc) or "current independent integration receipt unavailable"]
    return []


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

    def local_report(self, *, db: Any = None) -> dict[str, Any]:
        scopes = self.store.list(ReviewScope)
        cases = self.store.list(Case)
        questions = self.store.list(Question)
        comments, verified, unresolved, duplicates = [], [], [], []
        units = self.store.run.policy == "v42-units-6"
        pending_cases: set[str] | None = set()
        if units:
            try:
                pending = self.store.pending_unit_cases(db=db)
                if not isinstance(pending, list) or any(
                    not isinstance(item, dict) or not isinstance(item.get("case_id"), str) or not item["case_id"]
                    for item in pending
                ):
                    raise ValueError("invalid pending-case view")
                pending_cases = {item["case_id"] for item in pending}
            except (AttributeError, KeyError, TypeError, ValueError, OSError):
                pending_cases = None
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
                    if units and (pending_cases is None or case.id in pending_cases):
                        unresolved.append(
                            {"case_id": case.id, "claim_id": claim_id, "reason": "new evidence awaits unit review"}
                        )
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
        workspace = self.store.run.policy in {"v42-workspace-3", "v42-frontier-4", "v42-frontier-5", "v42-units-6"}
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
        if units:
            unresolved.extend(
                {"kind": "unit_coverage", "reason": reason} for reason in _unit_manifest_errors(self.store, scopes)
            )
            unresolved.extend(
                {"kind": "integration", "reason": reason} for reason in _unit_integration_errors(self.store, scopes, db)
            )
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
            report = self.publication.local_report(db=db)
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
            current = {comment["id"]: comment for comment in self.publication.local_report(db=db)["comments"]}
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
