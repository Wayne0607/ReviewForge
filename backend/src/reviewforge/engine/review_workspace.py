"""Run-bound navigation for reviewers; model notes are never source evidence.

The ledger remains the single owner of candidates and immutable raw evidence.
This workspace stores only model-authored handoffs and evidence references. A
consumer must create its own source observations before citing shared bytes.
"""

from __future__ import annotations

import copy
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from reviewforge.engine.review_session import fingerprint

if TYPE_CHECKING:
    from reviewforge.engine.hypothesis import HypothesisLedger

_PROTOCOL = "shared-review-workspace-v1"
_TRUST = "untrusted-navigation-only"
_ROLES = {"discovery", "investigation", "proof", "source_audit"}
_NOTE_FIELDS = {
    "task_id",
    "actor",
    "unit_ids",
    "paths",
    "summary",
    "open_questions",
    "resolved_questions",
    "next_action",
    "evidence_ids",
    "trust",
}


def _owner(note: dict[str, Any]) -> str:
    return "owner_" + fingerprint({"task_id": note["task_id"], "actor": note["actor"]})[:24]


def _journal_event(binding, owner, sequence, note_id, previous_digest):
    event = {"sequence": sequence, "note_id": note_id, "previous_digest": previous_digest}
    return {**event, "digest": fingerprint({"binding": binding, "owner": owner, **event})}


def _merge_ranges(ranges: list[list[int]]) -> list[list[int]]:
    """Describe exactly the union of known source spans, retaining gaps."""
    merged: list[list[int]] = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged


def _strings(values: Iterable[str], *, name: str) -> list[str]:
    if isinstance(values, (str, bytes)):
        raise ValueError(f"workspace {name} must be a sequence of strings")
    result = []
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"workspace {name} requires nonempty strings")
        if value not in result:
            result.append(value)
    return result


class SharedReviewWorkspace:
    """Persistent, relevant worksets with a fail-closed blind-review view."""

    def __init__(self, ledger: HypothesisLedger):
        if ledger.protocol_version < 11:
            raise ValueError("shared workspace requires protocol 11")
        self.ledger = ledger
        self._binding = {
            "run_id": ledger.run_id,
            "head_sha": ledger.head_sha,
            "workspace_digest": ledger.workspace_digest,
        }
        with ledger._lock:
            if not ledger.shared_workspace:
                ledger.shared_workspace = {
                    "protocol": _PROTOCOL,
                    "binding": dict(self._binding),
                    "notes": {},
                    "current": {},
                    **({"journal": {}} if ledger.protocol_version >= 13 else {}),
                }
            self._validate_state()
            self._validate_evidence(ledger.evidence_records, include_records=False)

    def _validate_evidence(
        self,
        evidence_ids: Iterable[str],
        *,
        include_records: bool = True,
    ) -> list[dict[str, Any]]:
        rows = []
        for identity in _strings(evidence_ids, name="evidence_ids"):
            record = self.ledger.evidence_records.get(identity)
            if (
                not isinstance(record, dict)
                or any(record.get(key) != value for key, value in self._binding.items())
                or identity != "ev_" + fingerprint(record)[:24]
                or record.get("kind") not in {"delivered-source", "tool-observation"}
            ):
                raise ValueError(f"invalid-shared-evidence-binding: {identity}")
            # A valid outer hash cannot make a mismatched nested observation a
            # fixed-head source. The original store enforces this on insertion.
            if record["kind"] == "tool-observation" and (
                not isinstance(record.get("observation"), dict)
                or record["observation"].get("sha") != self._binding["head_sha"]
            ):
                raise ValueError(f"invalid-shared-observation-binding: {identity}")
            if record["kind"] == "delivered-source" and (
                record.get("side") not in {"before", "after", "context"}
                or (record["side"] in {"after", "context"} and record.get("source_sha") != self._binding["head_sha"])
            ):
                raise ValueError(f"invalid-shared-source-binding: {identity}")
            if include_records:
                rows.append({**copy.deepcopy(record), "evidence_id": identity})
        return rows

    def _validate_state(self) -> None:
        if self._binding != {
            "run_id": self.ledger.run_id,
            "head_sha": self.ledger.head_sha,
            "workspace_digest": self.ledger.workspace_digest,
        }:
            raise ValueError("shared-workspace-binding-mismatch")
        state = self.ledger.shared_workspace
        if (
            not isinstance(state, dict)
            or set(state)
            != {"protocol", "binding", "notes", "current"}
            | ({"journal"} if self.ledger.protocol_version >= 13 else set())
            or state.get("protocol") != _PROTOCOL
            or state.get("binding") != self._binding
            or not isinstance(state.get("notes"), dict)
            or not isinstance(state.get("current"), dict)
        ):
            raise ValueError("invalid-restored-shared-workspace")
        for identity, note in state["notes"].items():
            if not isinstance(note, dict) or set(note) != _NOTE_FIELDS | {"id"}:
                raise ValueError("invalid-restored-workspace-note")
            value = {key: item for key, item in note.items() if key != "id"}
            if note["id"] != identity or identity != "note_" + fingerprint(value)[:24] or note["trust"] != _TRUST:
                raise ValueError("invalid-restored-workspace-note")
            for key in ("task_id", "actor", "summary"):
                if not isinstance(note[key], str) or not note[key].strip():
                    raise ValueError("invalid-restored-workspace-note")
            if not isinstance(note["next_action"], str):
                raise ValueError("invalid-restored-workspace-note")
            for key in ("unit_ids", "paths", "open_questions", "resolved_questions", "evidence_ids"):
                if not isinstance(note[key], list) or _strings(note[key], name=key) != note[key]:
                    raise ValueError("invalid-restored-workspace-note")
            self._validate_evidence(note["evidence_ids"], include_records=False)
        owners = {_owner(note) for note in state["notes"].values()}
        if set(state["current"]) != owners:
            raise ValueError("invalid-restored-workspace-current")
        for owner, identity in state["current"].items():
            if not isinstance(identity, str) or identity not in state["notes"]:
                raise ValueError("invalid-restored-workspace-current")
            if _owner(state["notes"][identity]) != owner:
                raise ValueError("invalid-restored-workspace-current")
        if self.ledger.protocol_version >= 13:
            journal = state["journal"]
            if not isinstance(journal, dict) or set(journal) != owners:
                raise ValueError("invalid-restored-workspace-journal")
            recorded = set()
            for owner, events in journal.items():
                if not isinstance(events, list) or not events:
                    raise ValueError("invalid-restored-workspace-journal")
                previous_digest, previous_note = "", ""
                for sequence, event in enumerate(events, 1):
                    if (
                        not isinstance(event, dict)
                        or set(event) != {"sequence", "note_id", "previous_digest", "digest"}
                        or type(event.get("sequence")) is not int
                        or event["sequence"] != sequence
                        or not isinstance(event.get("note_id"), str)
                        or event["note_id"] not in state["notes"]
                        or event["note_id"] == previous_note
                        or _owner(state["notes"][event["note_id"]]) != owner
                        or event != _journal_event(self._binding, owner, sequence, event["note_id"], previous_digest)
                    ):
                        raise ValueError("invalid-restored-workspace-journal-order-or-binding")
                    recorded.add(event["note_id"])
                    previous_digest, previous_note = event["digest"], event["note_id"]
                if state["current"][owner] != previous_note:
                    raise ValueError("invalid-restored-workspace-journal-current")
            if recorded != set(state["notes"]):
                raise ValueError("invalid-restored-workspace-journal-orphan-note")

    def publish_progress(
        self,
        *,
        task_id: str,
        actor: str,
        unit_ids: Iterable[str] = (),
        paths: Iterable[str] = (),
        summary: str,
        open_questions: Iterable[str] = (),
        resolved_questions: Iterable[str] = (),
        next_action: str = "",
        evidence_ids: Iterable[str] = (),
    ) -> dict[str, Any]:
        for name, value in (("task_id", task_id), ("actor", actor), ("summary", summary)):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"workspace {name} requires a nonempty string")
        if not isinstance(next_action, str):
            raise ValueError("workspace next_action must be a string")
        value = {
            "task_id": task_id,
            "actor": actor,
            "summary": summary,
            "next_action": next_action,
            "trust": _TRUST,
            "unit_ids": sorted(_strings(unit_ids, name="unit_ids")),
            "paths": sorted(_strings(paths, name="paths")),
            "open_questions": _strings(open_questions, name="open_questions"),
            "resolved_questions": _strings(resolved_questions, name="resolved_questions"),
            "evidence_ids": sorted(_strings(evidence_ids, name="evidence_ids")),
        }
        with self.ledger._lock:
            self._validate_state()
            self._validate_evidence(value["evidence_ids"], include_records=False)
            identity = "note_" + fingerprint(value)[:24]
            note = {"id": identity, **value}
            saved = self.ledger.shared_workspace["notes"].setdefault(identity, note)
            if saved != note:
                raise ValueError("workspace-note-identity-collision")
            owner = _owner(note)
            if self.ledger.protocol_version >= 13:
                events = self.ledger.shared_workspace["journal"].setdefault(owner, [])
                if not events or events[-1]["note_id"] != identity:
                    events.append(
                        _journal_event(
                            self._binding,
                            owner,
                            len(events) + 1,
                            identity,
                            events[-1]["digest"] if events else "",
                        )
                    )
            self.ledger.shared_workspace["current"][owner] = identity
            return copy.deepcopy(saved)

    def read_evidence(self, evidence_ids: Iterable[str]) -> list[dict[str, Any]]:
        with self.ledger._lock:
            self._validate_state()
            return self._validate_evidence(evidence_ids)

    def view(
        self,
        *,
        task_id: str = "",
        unit_ids: Iterable[str] = (),
        paths: Iterable[str] = (),
        role: str = "discovery",
        catalog: str = "references",
    ) -> dict[str, Any]:
        if role not in _ROLES:
            raise ValueError("unsupported-workspace-role")
        if catalog not in {"references", "directory"}:
            raise ValueError("unsupported-workspace-catalog")
        if not isinstance(task_id, str):
            raise ValueError("workspace task_id must be a string")
        units, selected_paths = set(_strings(unit_ids, name="unit_ids")), set(_strings(paths, name="paths"))

        def related(identity: str, row_units: Iterable[str], row_paths: Iterable[str]) -> bool:
            return bool(
                (task_id and identity == task_id)
                or units.intersection(row_units)
                or selected_paths.intersection(row_paths)
            )

        with self.ledger._lock:
            self._validate_state()
            notes = []
            state = self.ledger.shared_workspace
            journals = []
            journal_notes = []
            for owner in sorted(state["current"]):
                note = state["notes"][state["current"][owner]]
                history = (
                    [state["notes"][event["note_id"]] for event in state["journal"][owner]]
                    if self.ledger.protocol_version >= 13
                    else [note]
                )
                selected = False
                for progress in history:
                    evidence_paths = []
                    for identity in progress["evidence_ids"]:
                        record = self.ledger.evidence_records[identity]
                        raw = record.get("observation", {}) if record["kind"] == "tool-observation" else record
                        if raw.get("path"):
                            evidence_paths.append(raw["path"])
                    if related(progress["task_id"], progress["unit_ids"], [*progress["paths"], *evidence_paths]):
                        selected = True
                        break
                if selected:
                    notes.append(note)
                    if self.ledger.protocol_version >= 13:
                        # Include this related owner's ordered context, even if
                        # a later correction omits the original dependency path.
                        # No union of historical opinions becomes a current fact.
                        journal_notes.extend(history)
                        journals.append(
                            {
                                "task_id": note["task_id"],
                                "actor": note["actor"],
                                "trust": "untrusted-navigation-history",
                                "events": [
                                    {
                                        "sequence": event["sequence"],
                                        "note_id": event["note_id"],
                                        **{
                                            key: state["notes"][event["note_id"]][key]
                                            for key in (
                                                "summary",
                                                "open_questions",
                                                "resolved_questions",
                                                "next_action",
                                                "evidence_ids",
                                                "unit_ids",
                                                "paths",
                                                "trust",
                                            )
                                        },
                                    }
                                    for event in state["journal"][owner]
                                ],
                            }
                        )
            evidence_ids = {identity for note in [*notes, *journal_notes] for identity in note["evidence_ids"]}
            for identity, record in self.ledger.evidence_records.items():
                if not isinstance(record, dict):
                    raise ValueError("invalid-shared-evidence-record")
                raw = record.get("observation", {}) if record.get("kind") == "tool-observation" else record
                if isinstance(raw, dict) and raw.get("path") in selected_paths:
                    evidence_ids.add(identity)
            references = []
            directory = {}
            self._validate_evidence(evidence_ids, include_records=False)
            for identity in sorted(evidence_ids):
                record = self.ledger.evidence_records[identity]
                raw = record.get("observation", {}) if record["kind"] == "tool-observation" else record
                metadata = {
                    key: copy.deepcopy(raw[key])
                    for key in (
                        "path",
                        "side",
                        "source_sha",
                        "line",
                        "line_range",
                        "excerpt_line_range",
                        "status",
                        "tool",
                    )
                    if key in raw
                }
                if record["kind"] == "delivered-source":
                    metadata.update(tool="delivered-source", status="success")
                    if isinstance(raw.get("line"), int) and isinstance(raw.get("excerpt"), str):
                        start = raw["line"]
                        metadata["line_range"] = [start, start + max(1, len(raw["excerpt"].splitlines())) - 1]
                if catalog == "references":
                    references.append({"evidence_id": identity, "kind": record["kind"], **metadata})
                    continue
                group = {"kind": record["kind"]} | {
                    key: metadata[key] for key in ("path", "side", "tool", "status", "source_sha") if key in metadata
                }
                if record["kind"] == "tool-observation":
                    group["source_sha"] = raw["sha"]
                entry = directory.setdefault(fingerprint(group), {**group, "record_count": 0, "line_ranges": []})
                entry["record_count"] += 1
                # Requested tool windows can contain unsaved bytes. Only an
                # actual excerpt range describes delivered source coverage.
                span = (
                    metadata.get("line_range")
                    if record["kind"] == "delivered-source"
                    else raw.get("excerpt_line_range")
                )
                if span is not None:
                    if (
                        not isinstance(span, (list, tuple))
                        or len(span) != 2
                        or any(type(value) is not int for value in span)
                        or span[0] < 1
                        or span[1] < span[0]
                    ):
                        raise ValueError("invalid-workspace-evidence-range")
                    entry["line_ranges"].append(list(span))
            result = {"binding": dict(self._binding), "role": role, "evidence_catalog": references}
            if catalog == "directory":
                entries = []
                for key in sorted(directory):
                    entry = directory[key]
                    entry["line_ranges"] = _merge_ranges(entry["line_ranges"])
                    entries.append(entry)
                result["evidence_directory"] = entries
                if self.ledger.protocol_version >= 12:
                    # An empty references array incorrectly suggests that no
                    # evidence exists. This view deliberately exposes only the
                    # directory; references remain available on explicit query.
                    result.pop("evidence_catalog")
            if role in {"proof", "source_audit"}:
                return copy.deepcopy(result)
            candidates = [
                {
                    "id": item.id,
                    "unit_id": item.unit_id,
                    "claim": item.claim,
                    "status": item.status.value,
                    "task_id": item.investigation_task_id,
                    "paths": [site.path for site in item.sites],
                }
                for item in self.ledger.items.values()
                if related(item.investigation_task_id, [item.unit_id], [site.path for site in item.sites])
            ]
            candidate_tasks = {row["task_id"] for row in candidates if row["task_id"]}
            note_tasks = {note["task_id"] for note in notes}
            tasks = []
            for identity, record in self.ledger.discovery_blocks.items():
                if identity in note_tasks or related(identity, record.get("unit_ids", ()), ()):
                    tasks.append(
                        {
                            "task_id": identity,
                            "phase": "discovery",
                            "unit_ids": record.get("unit_ids", []),
                            "status": record.get("session", {}).get("status", "pending"),
                        }
                    )
            for identity, record in self.ledger.investigation_tasks.items():
                if identity == task_id or identity in candidate_tasks or identity in note_tasks:
                    tasks.append(
                        {
                            "task_id": identity,
                            "phase": "investigation",
                            **{key: record[key] for key in ("outcome", "verdict") if key in record},
                        }
                    )
            result.update(notes=notes, tasks=tasks, candidates=candidates)
            if self.ledger.protocol_version >= 13:
                result["progress_journal"] = journals
            return copy.deepcopy(result)
