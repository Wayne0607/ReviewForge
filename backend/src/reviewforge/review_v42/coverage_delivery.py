"""Audited incremental region receipts for the optional delivery profile."""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any

from .contracts import BindingError, ReviewScope, RevisionConflict, Task, is_navigation_reference
from .line_semantics import source_lines

if TYPE_CHECKING:
    from .store import ReviewStore

COVERAGE_DELIVERY_POLICY = "region-delivery-1"


def missing_ranges(first: int, last: int, spans: list[tuple[int, int]]) -> list[dict[str, int]]:
    cursor, missing = first, []
    for start, end in sorted(spans):
        if end < cursor or start > last:
            continue
        if start > cursor:
            missing.append({"start_line": cursor, "end_line": start - 1})
        cursor = max(cursor, end + 1)
    if cursor <= last:
        missing.append({"start_line": cursor, "end_line": last})
    return missing


def region_requirement(region: dict[str, Any], side: str | None = None) -> dict[str, Any]:
    base = region.get("change") == "deleted" or region.get("side") == "base"
    side = side or ("base" if base else "head")
    base = side == "base"
    return {
        "region_id": region["id"],
        "path": region["path"],
        "side": side,
        "start_line": region.get("base_start_line" if base else "start_line"),
        "end_line": region.get("base_end_line" if base else "end_line"),
    }


class CoverageDelivery:
    def __init__(self, store: ReviewStore):
        self.store = store

    @staticmethod
    def committed_result(db, task_id, turn_id, call, response_hash):
        row = db.execute(
            "SELECT payload FROM decision_steps WHERE task_id=? AND turn_id=? AND call_id=?",
            (task_id, turn_id, call["id"]),
        ).fetchone()
        if row is None:
            return None
        step = json.loads(row[0])
        if "coverage_result" not in step:
            return None
        if (
            call["name"] != "complete_scope"
            or step["args"] != call["args"]
            or step["response_hash"] != response_hash
            or step["status"] != "settled"
            or step.get("disposition") != "coverage_recorded"
        ):
            raise BindingError("saved region receipt differs from the audited submission")
        return step["coverage_result"]

    def recovery_receipts(self, task_id: str) -> list[dict[str, Any]]:
        with self.store._connect() as db:
            rows = db.execute(
                "SELECT payload FROM decision_steps s WHERE task_id=? "
                "AND json_type(payload,'$.coverage_result')='object' "
                "AND NOT EXISTS (SELECT 1 FROM turn_audit a WHERE a.task_id=s.task_id "
                "AND a.turn_id=s.turn_id AND a.phase='exchange') ORDER BY rowid",
                (task_id,),
            ).fetchall()
        return [
            {"kind": "recovered_region_receipts", "original_turn_id": step["turn_id"], **step["coverage_result"]}
            for (payload,) in rows
            for step in [json.loads(payload)]
        ]

    def _issues(self, db, region, observations, refs, delivered):
        requirement = region_requirement(region)
        eligible = [
            ref.id
            for ref in refs.values()
            if ref.kind == "source"
            and ref.path == requirement["path"]
            and ref.side == requirement["side"]
            and not is_navigation_reference(ref)
            and ref.metadata.get("complete", True) is not False
            and ref.metadata.get("status", "available") in {"available", "success", "ok"}
        ]
        details = {**requirement, "available_source_refs": [ref for ref in eligible if ref in delivered]}
        issues = []
        if not observations:
            return [{**details, "field": "payload.observations", "reason": "missing_review_judgment"}]
        cited = set()
        for index, observation in observations:
            for offset, ref_id in enumerate(observation["evidence_refs"]):
                field = f"payload.observations[{index}].evidence_refs[{offset}]"
                if ref_id not in refs:
                    exists = db.execute("SELECT 1 FROM objects WHERE kind='EvidenceRef' AND id=?", (ref_id,)).fetchone()
                    if exists:
                        self.store._unit_refs(db, [ref_id])
                    reason = "not_read_by_task" if exists else "unknown_reference"
                elif is_navigation_reference(refs[ref_id]):
                    reason = "navigation_only"
                elif ref_id not in delivered:
                    reason = "source_not_delivered"
                else:
                    cited.add(ref_id)
                    continue
                issues.append({**details, "field": field, "reason": reason, "evidence_ref": ref_id})
        if issues:
            return issues
        first, last = requirement["start_line"], requirement["end_line"]
        if type(first) is not int or type(last) is not int or first < 1 or last < first:
            return [{**details, "field": "payload.checked_region_ids", "reason": "unsupported_source_range"}]
        spans = [span for ref in eligible if ref in cited for span in delivered.get(ref, [])]
        if not missing_ranges(first, last, spans):
            return []
        if requirement["side"] == "head" and not region.get("changed_lines"):
            base = region_requirement(region, "base")
            base_spans = [
                span
                for ref in cited
                if refs[ref].kind == "source" and refs[ref].path == requirement["path"] and refs[ref].side == "base"
                for span in delivered.get(ref, [])
            ]
            if (
                type(base["start_line"]) is int
                and type(base["end_line"]) is int
                and 0 < base["start_line"] <= base["end_line"]
                and not missing_ranges(base["start_line"], base["end_line"], base_spans)
            ):
                return []
        shown = [span for ref in eligible for span in delivered.get(ref, [])]
        historical = [
            (
                refs[ref].start_line or 1,
                (refs[ref].start_line or 1)
                + len(source_lines(self.store.run, self.store.read_evidence(ref).decode("utf-8")))
                - 1,
            )
            for ref in eligible
        ]
        absent = missing_ranges(first, last, historical)
        if not missing_ranges(first, last, shown):
            reason = "missing_region_reference"
        elif not absent:
            reason = "source_not_delivered"
        else:
            reason = "missing_source_range"
        return [
            {
                **details,
                "field": "payload.observations",
                "reason": reason,
                "missing_ranges": absent if reason == "missing_source_range" else missing_ranges(first, last, spans),
                "read_source": [{"path": requirement["path"], "side": requirement["side"], **part} for part in absent],
                "stored_source_refs": eligible,
                "read_evidence": [{"ref": ref} for ref in eligible if ref not in delivered],
            }
        ]

    def record(self, task_id: str, token: str, turn_id: str, call_id: str, *, now=None) -> dict[str, Any]:
        from .store import _json

        if self.store.run.metadata.get("coverage_delivery_policy") != COVERAGE_DELIVERY_POLICY:
            raise BindingError("region delivery is not enabled")
        with self.store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            task = Task(
                **json.loads(
                    db.execute("SELECT payload FROM objects WHERE kind='Task' AND id=?", (task_id,)).fetchone()[0]
                )
            )
            self.store._guard_review_task(db, task_id, token, task.object_id, now)
            row = db.execute(
                "SELECT id,payload FROM decision_steps WHERE task_id=? AND turn_id=? AND call_id=?",
                (task_id, turn_id, call_id),
            ).fetchone()
            if row is None:
                raise BindingError("region receipts require a staged scope submission")
            step = json.loads(row[1])
            if "coverage_result" in step:
                return step["coverage_result"]
            args = step["args"]
            if step["action"] != "complete_review" or args.get("scope_id") != task.object_id:
                raise BindingError("region receipts require an audited scope submission")
            scope = self.store.get(ReviewScope, task.object_id)
            if scope.revision != step.get("scope_revision"):
                raise RevisionConflict("region submission scope revision changed")
            if scope.id not in self.store._unit_plan_db(db).primary_scope_ids:
                raise BindingError("only a primary scope can submit region receipts")
            payload = args["payload"]
            regions = {region["id"]: region for region in scope.regions}
            requested = payload.get("checked_region_ids", [])
            if not requested:
                raise ValueError("region submission requires checked_region_ids")
            if len(set(requested)) != len(requested):
                raise ValueError("duplicate region IDs")
            for index, observation in enumerate(payload.get("observations", [])):
                unknown = set(observation["region_ids"]) - regions.keys()
                if unknown:
                    raise ValueError(f"observation {index} names regions outside the scope")
            task_refs = self.store._task_frontier_refs(db, task_id)
            refs = {ref.id: ref for ref in self.store._unit_refs(db, sorted(task_refs))}
            delivered = self.store._require_delivered(db, task_id, token, turn_id, task_refs, required_ref_ids=set())
            accepted, issues = [], []
            for rid in requested:
                if rid not in regions:
                    issues.append({"region_id": rid, "field": "payload.checked_region_ids", "reason": "wrong_scope"})
                    continue
                observations = [
                    (index, observation)
                    for index, observation in enumerate(payload.get("observations", []))
                    if rid in observation["region_ids"]
                ]
                gaps = self._issues(db, regions[rid], observations, refs, delivered)
                if gaps:
                    issues.extend(gaps)
                    continue
                current = json.loads(
                    db.execute("SELECT payload FROM objects WHERE kind='ReviewScope' AND id=?", (scope.id,)).fetchone()[
                        0
                    ]
                )
                identity = "region_" + hashlib.sha256(_json([step["id"], rid]).encode()).hexdigest()
                self.store.commit_unit_decision(
                    task_id,
                    token,
                    current["revision"],
                    identity,
                    checked_region_ids=[rid],
                    observations=[{**observation, "region_ids": [rid]} for _, observation in observations],
                    questions=[],
                    cases=[],
                    unresolved=payload.get("unresolved_refs", []),
                    turn_id=turn_id,
                    finish=False,
                    now=now,
                    _db=db,
                )
                accepted.append(rid)
            current = json.loads(
                db.execute("SELECT payload FROM objects WHERE kind='ReviewScope' AND id=?", (scope.id,)).fetchone()[0]
            )
            result = {
                "status": "running",
                "scope_revision": current["revision"],
                "settled_region_ids": accepted,
                "remaining_region_ids": [
                    rid for rid in regions if rid not in {r["id"] for r in current["checked_regions"]}
                ],
                "errors": issues,
                "message": "Submit only remaining region receipts. When none remain, call complete_scope with "
                "finish true and empty checked_region_ids and observations to check final blockers.",
            }
            if issues:
                result["error"] = "Region receipts have correctable gaps. Valid sibling receipts are saved."
            self.store._settle_scope_decision(
                db,
                task_id,
                turn_id,
                call_id,
                scope_id=scope.id,
                scope_revision=step["scope_revision"],
                result_status="coverage_recorded",
                expected=payload,
            )
            step = json.loads(db.execute("SELECT payload FROM decision_steps WHERE id=?", (row[0],)).fetchone()[0])
            step["coverage_result"] = result
            db.execute("UPDATE decision_steps SET payload=? WHERE id=?", (_json(step), row[0]))
            self.store._event(db, "RegionSubmission", step["id"], "record", result)
            db.commit()
            return result
