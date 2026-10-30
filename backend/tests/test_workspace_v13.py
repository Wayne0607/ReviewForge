"""Ordered untrusted progress survives later tool-local snapshots.

The regression is the stopped Cal.com AddGuestsDialog task: one response first
recorded confirmed locale keys, then another tool's empty resolved list replaced
the task snapshot. Protocol 13 keeps both visible without declaring either true.
"""

from __future__ import annotations

import copy
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from reviewforge.engine.discovery import SourceLine
from reviewforge.engine.hypothesis import HypothesisLedger
from reviewforge.engine.review_session import EvidenceStore
from reviewforge.engine.review_workspace import SharedReviewWorkspace

TASK = "generator:db_f986ef87206fca64"
PATH = "apps/web/components/dialog/AddGuestsDialog.tsx"
RESOLVED = (
    "i18n keys additional_guests/new_guests_added/guests_added/emails_must_be_unique_valid exist in en/common.json"
)


def book(protocol=13, run="run"):
    return HypothesisLedger(run, "head", "workspace", protocol_version=protocol)


def publish(workspace, *, summary, resolved_questions=(), **changes):
    return workspace.publish_progress(
        **{
            "task_id": TASK,
            "actor": "discovery",
            "unit_ids": ["dialog", "email"],
            "paths": [PATH],
            "summary": summary,
            "open_questions": ["Is AttendeeAddGuestsEmail registered for renderEmail?"],
            "resolved_questions": resolved_questions,
            "next_action": "Inspect template registration",
            "evidence_ids": [],
            **changes,
        }
    )


def history(view, task=TASK, actor="discovery"):
    return next(group for group in view["progress_journal"] if group["task_id"] == task and group["actor"] == actor)


def test_real_double_tool_progress_keeps_resolved_locale_keys_after_empty_local_snapshot():
    state = book()
    workspace = SharedReviewWorkspace(state)
    first = publish(workspace, summary="i18n keys confirmed present in en/common.json.", resolved_questions=[RESOLVED])
    second = publish(workspace, summary="Need to confirm the string-keyed renderEmail template registration.")
    view = workspace.view(task_id=TASK, catalog="directory")
    assert view["notes"] == [second]
    assert view["notes"][0]["resolved_questions"] == []
    events = history(view)["events"]
    assert [event["note_id"] for event in events] == [first["id"], second["id"]]
    assert [event["sequence"] for event in events] == [1, 2]
    assert events[0]["resolved_questions"] == [RESOLVED] and events[1]["resolved_questions"] == []
    assert history(view)["trust"] == "untrusted-navigation-history"
    assert all(event["trust"] == "untrusted-navigation-only" for event in events)
    assert "evidence_catalog" not in view and not state.evidence_records


def test_cross_turn_sorted_checkpoint_restore_preserves_order_and_later_explicit_correction():
    state = book()
    workspace = SharedReviewWorkspace(state)
    first = publish(workspace, summary="i18n keys confirmed present in en/common.json.", resolved_questions=[RESOLVED])
    second = publish(workspace, summary="Checking the server-side guest contract.")
    state = HypothesisLedger.from_dict(json.loads(json.dumps(state.to_dict(), sort_keys=True)))
    workspace = SharedReviewWorkspace(state)
    correction = publish(
        workspace,
        summary="Correction to the earlier key-existence note: existence does not establish placeholder parity.",
        open_questions=["Does guests_added_event_type_subject accept the arguments supplied by this caller?"],
        resolved_questions=["The keys are present, but the earlier claim of complete locale validation was too broad."],
    )
    view = workspace.view(task_id=TASK)
    events = history(view)["events"]
    assert [event["note_id"] for event in events] == [first["id"], second["id"], correction["id"]]
    assert events[0]["resolved_questions"] == [RESOLVED]
    assert "Correction" in events[2]["summary"]
    assert view["notes"] == [correction]
    assert "resolved_questions" not in history(view)  # No authoritative union of old and new opinions.
    assert set(history(view)) == {"task_id", "actor", "trust", "events"}


def test_exact_consecutive_duplicates_are_idempotent_but_a_later_reassertion_remains_an_event():
    state = book()
    workspace = SharedReviewWorkspace(state)
    first = publish(workspace, summary="A", resolved_questions=[RESOLVED])
    assert publish(workspace, summary="A", resolved_questions=[RESOLVED]) == first
    publish(workspace, summary="B")
    assert publish(workspace, summary="A", resolved_questions=[RESOLVED]) == first
    events = history(workspace.view(task_id=TASK))["events"]
    assert [event["summary"] for event in events] == ["A", "B", "A"]
    assert len(state.shared_workspace["notes"]) == 2


def test_concurrent_identical_publishes_do_not_create_duplicate_journal_events():
    state = book()
    with ThreadPoolExecutor(max_workers=8) as pool:
        notes = list(
            pool.map(lambda _: publish(SharedReviewWorkspace(state), summary="Same local progress"), range(32))
        )
    assert all(note == notes[0] for note in notes)
    assert len(history(SharedReviewWorkspace(state).view(task_id=TASK))["events"]) == 1


def test_path_or_unit_related_history_retains_later_corrections_without_leaking_unrelated_owners():
    state = book()
    workspace = SharedReviewWorkspace(state)
    evidence_id = EvidenceStore(state).source(SourceLine("context", "dependency.py", 3, "actual_source()"))
    publish(workspace, summary="Related earlier dependency progress", evidence_ids=[evidence_id])
    publish(
        workspace, summary="Later correction, with a different local path", paths=["other.py"], unit_ids=["other-unit"]
    )
    publish(
        workspace, summary="PRIVATE_UNRELATED", task_id="unrelated", paths=["private.py"], unit_ids=["private-unit"]
    )
    for filters in ({"paths": ["dependency.py"]}, {"unit_ids": ["dialog"]}, {"task_id": TASK}):
        view = workspace.view(**filters, catalog="directory")
        assert len(view["progress_journal"]) == 1
        assert [event["sequence"] for event in history(view)["events"]] == [1, 2]
        assert "Later correction" in view["notes"][0]["summary"]
        assert "PRIVATE_UNRELATED" not in json.dumps(view)
        assert history(view)["events"][0]["evidence_ids"] == [evidence_id]
    empty = workspace.view(paths=["not-reviewed.py"], catalog="directory")
    assert empty["progress_journal"] == [] and empty["notes"] == []


def test_each_actor_keeps_its_own_ordered_journal_without_a_fabricated_cross_actor_order():
    workspace = SharedReviewWorkspace(book())
    publish(workspace, summary="Discovery first")
    publish(workspace, summary="Investigation first", actor="investigation")
    publish(workspace, summary="Discovery second")
    view = workspace.view(task_id=TASK)
    assert [event["summary"] for event in history(view)["events"]] == ["Discovery first", "Discovery second"]
    assert [event["sequence"] for event in history(view, actor="investigation")["events"]] == [1]


@pytest.mark.parametrize("role", ["proof", "source_audit"])
def test_blind_empty_views_never_expose_notes_journal_or_prior_judgments(role):
    workspace = SharedReviewWorkspace(book())
    publish(workspace, summary="PRIVATE_UPSTREAM_CLAIM", resolved_questions=["PRIVATE_PRIOR_JUDGMENT"])
    publish(workspace, summary="PRIVATE_CORRECTION")
    view = workspace.view(task_id=TASK, role=role, catalog="directory")
    assert set(view) == {"binding", "role", "evidence_directory"}
    assert view["evidence_directory"] == [] and "PRIVATE_" not in json.dumps(view)


def test_journal_does_not_expand_source_records_or_replay_provider_reasoning_and_returns_deep_copies():
    state = book()
    workspace = SharedReviewWorkspace(state)
    store = EvidenceStore(state)
    ids = [store.source(SourceLine("context", "dependency.py", line, f"source_{line}()")) for line in range(1, 101)]
    publish(workspace, summary="Source was checked", evidence_ids=ids)
    publish(workspace, summary="Another local check")
    state.discovery_blocks[TASK] = {"reasoning": "PRIVATE_PROVIDER_REASONING", "unit_ids": ["dialog"], "units": {}}
    view = workspace.view(task_id=TASK, catalog="directory")
    assert len(view["evidence_directory"]) == 1 and view["evidence_directory"][0]["record_count"] == 100
    assert "PRIVATE_PROVIDER_REASONING" not in json.dumps(view)
    assert "source_1()" not in json.dumps(view)
    assert set(history(view)["events"][0]) == {
        "sequence",
        "note_id",
        "summary",
        "open_questions",
        "resolved_questions",
        "next_action",
        "evidence_ids",
        "unit_ids",
        "paths",
        "trust",
    }
    history(view)["events"][0]["resolved_questions"].append("mutated output")
    assert "mutated output" not in json.dumps(workspace.view(task_id=TASK))
    assert len(workspace.read_evidence(ids)) == 100


@pytest.mark.parametrize(
    "damage",
    [
        "missing_journal",
        "unknown_owner",
        "empty_events",
        "order",
        "sequence",
        "boolean_sequence",
        "unknown_note",
        "other_owner_note",
        "digest",
        "previous_digest",
        "current",
        "orphan_note",
        "other_run_chain",
    ],
)
def test_illegal_journal_restore_fails_closed_on_owner_binding_order_or_current_snapshot(damage):
    state = book()
    workspace = SharedReviewWorkspace(state)
    first = publish(workspace, summary="First")
    publish(workspace, summary="Second")
    publish(workspace, summary="Third")
    unrelated = publish(workspace, summary="Unrelated", task_id="other")
    saved = state.to_dict()
    shared = saved["shared_workspace"]
    owner = next(key for key, note_id in shared["current"].items() if note_id != unrelated["id"])
    events = shared["journal"][owner]
    if damage == "missing_journal":
        del shared["journal"]
    elif damage == "unknown_owner":
        shared["journal"]["owner_unknown"] = events
    elif damage == "empty_events":
        shared["journal"][owner] = []
    elif damage == "order":
        events[0], events[1] = events[1], events[0]
    elif damage == "sequence":
        events[1]["sequence"] = 4
    elif damage == "boolean_sequence":
        events[0]["sequence"] = True
    elif damage == "unknown_note":
        events[0]["note_id"] = "note_unknown"
    elif damage == "other_owner_note":
        events[0]["note_id"] = unrelated["id"]
    elif damage == "digest":
        events[0]["digest"] = "changed"
    elif damage == "previous_digest":
        events[1]["previous_digest"] = "changed"
    elif damage == "current":
        shared["current"][owner] = first["id"]
    elif damage == "orphan_note":
        shared["journal"][owner] = events[2:]
    else:
        other = SharedReviewWorkspace(book(run="another-run"))
        publish(other, summary="First")
        publish(other, summary="Second")
        publish(other, summary="Third")
        shared["journal"][owner] = copy.deepcopy(next(iter(other.ledger.shared_workspace["journal"].values())))
    with pytest.raises(ValueError, match="workspace"):
        HypothesisLedger.from_dict(saved)


@pytest.mark.parametrize("protocol", [11, 12])
def test_existing_protocols_keep_current_only_shape_and_are_not_upgraded(protocol):
    state = book(protocol)
    workspace = SharedReviewWorkspace(state)
    publish(workspace, summary="Earlier local note", resolved_questions=[RESOLVED])
    current = publish(workspace, summary="Latest local note")
    view = workspace.view(task_id=TASK)
    assert view["notes"] == [current] and "progress_journal" not in view
    assert "journal" not in state.shared_workspace
    assert HypothesisLedger.from_dict(state.to_dict()).to_dict() == state.to_dict()
    changed = state.to_dict()
    changed["protocol_version"] = 13
    with pytest.raises(ValueError, match="workspace"):
        HypothesisLedger.from_dict(changed)
