from dataclasses import replace

import pytest

from reviewforge.review_delivery.contracts import Binding
from reviewforge.review_delivery.store import DeliveryStore


@pytest.fixture
def binding():
    return Binding("run", "org/repo", 42, "base", "head", {"base": "b", "head": "h"}, {"model": "m"})


@pytest.fixture
def store(tmp_path, binding):
    db = DeliveryStore(tmp_path / "state.db", binding)
    yield db
    db.close()


def prepare(store, suffix=""):
    lead = store.create_session("lead", "lead")
    verifier = store.create_session("verifier", f"verifier{suffix}")
    source = store.put_source("original", "head", b"source", {"path": "a.py", "start_line": 1, "revision": "head"})
    finding = store.create_finding({"title": f"defect{suffix}"}, [source["id"]], lead["id"])
    task = store.create_verification(finding["id"], verifier["id"])
    return finding, task, source


def test_accept_requires_independent_actual_delivery_and_survives_restart(tmp_path, binding):
    path = tmp_path / "review.db"
    store = DeliveryStore(path, binding)
    finding, task, source = prepare(store)
    store.record_delivery("lead", [source["id"]])
    with pytest.raises(ValueError, match="not delivered"):
        store.record_verdict(task["id"], "accept", [source["id"]], reason="refutation considered")
    store.record_delivery("verifier", [source["id"]])
    verdict = store.record_verdict(task["id"], "accept", [source["id"]], reason="refutation considered")
    store.append_turn("verifier", {"text": "done"}, "turn-1")
    store.close()
    reopened = DeliveryStore(path, binding)
    assert reopened.get_finding(finding["id"])["state"] == "accept"
    assert reopened.get_outbox(finding["id"])["verdict_id"] == verdict["id"]
    assert (
        reopened.record_verdict(task["id"], "accept", [source["id"]], reason="refutation considered")["id"]
        == verdict["id"]
    )
    assert reopened.list_turns("verifier")[0]["payload"] == {"text": "done"}
    reopened.close()
    with pytest.raises(ValueError, match="binding mismatch"):
        DeliveryStore(path, replace(binding, head_sha="other"))


def test_reads_notes_and_unrelated_findings_do_not_invalidate(store):
    first, task, source = prepare(store)
    store.record_delivery("verifier", [source["id"]])
    store.record_verdict(task["id"], "accept", [source["id"]], reason="refutation considered")
    second, other, _ = prepare(store, "2")
    work = store.create_research("What is the rule?", "lead")
    nav = store.put_source("navigation", "base", b"hits")
    store.add_note(work["id"], "lead", "look here", [nav["id"]])
    store.record_delivery("verifier2", [source["id"]])
    store.record_verdict(
        other["id"], "needs_evidence", [], missing_fact="policy", attempted=["repo"], recovery_condition="supply policy"
    )
    assert store.get_finding(first["id"])["state"] == "accept"
    assert store.get_finding(first["id"])["revision"] == 1
    assert store.get_outbox(first["id"])["status"] == "intended"
    assert store.get_finding(second["id"])["state"] == "needs_evidence"
    assert store.create_verification(second["id"], "verifier2")["id"] != other["id"]


def test_late_verdict_and_independent_sources_are_fenced(store):
    finding, task, source = prepare(store)
    base = store.put_source("original", "base", b"source", {"path": "a.py", "start_line": 1, "revision": "base"})
    assert base["id"] != source["id"]
    store.record_delivery("verifier", [source["id"]])
    with pytest.raises(ValueError, match="not delivered"):
        store.record_verdict(task["id"], "accept", [base["id"]], reason="refutation considered")
    store.challenge_finding(finding["id"], "relevant counterexample")
    with pytest.raises(ValueError, match="stale"):
        store.record_verdict(task["id"], "accept", [source["id"]], reason="refutation considered")
    with pytest.raises(ValueError, match="independent"):
        store.create_verification(finding["id"], "lead")


def test_navigation_and_corrupted_original_never_qualify_as_evidence(store):
    _, task, source = prepare(store)
    navigation = store.put_source("navigation", "head", b"match")
    store.record_delivery("verifier", [navigation["id"], source["id"]])
    with pytest.raises(ValueError, match="original"):
        store.record_verdict(task["id"], "accept", [navigation["id"]], reason="refutation considered")
    store.db.execute("UPDATE sources SET content=? WHERE id=?", (b"tampered", source["id"]))
    with pytest.raises(ValueError, match="hash mismatch"):
        store.record_verdict(task["id"], "accept", [source["id"]], reason="refutation considered")
    assert store.list_verdicts() == []
    assert store.list_outbox() == []


def test_uncertain_delivery_blocks_only_that_finding_and_reuses_receipt(store):
    first, task, source = prepare(store)
    second, other, _ = prepare(store, "2")
    for verifier in ("verifier", "verifier2"):
        store.record_delivery(verifier, [source["id"]])
    store.record_verdict(task["id"], "accept", [source["id"]], reason="refutation considered")
    store.record_verdict(other["id"], "accept", [source["id"]], reason="refutation considered")
    item = store.get_outbox(first["id"])
    store.freeze_outbox(first["id"], 1, "comment")
    store.transition_outbox(first["id"], 1, "intended", "uncertain")
    with pytest.raises(ValueError, match="reconcile"):
        store.challenge_finding(first["id"], "new evidence")
    store.challenge_finding(second["id"], "new evidence")
    with pytest.raises(ValueError, match="status changed"):
        store.transition_outbox(first["id"], 1, "intended", "uncertain")
    store.transition_outbox(first["id"], 1, "uncertain", "delivered", remote_id=123)
    store.challenge_finding(first["id"], "new evidence")
    with pytest.raises(ValueError, match="stale"):
        store.freeze_outbox(first["id"], 1, "comment")
    new_task = store.create_verification(first["id"], "verifier")
    store.record_verdict(new_task["id"], "accept", [source["id"]], reason="refutation considered")
    updated = store.get_outbox(first["id"])
    assert updated["marker"] == item["marker"]
    assert updated["remote_id"] == 123
    assert updated["finding_revision"] == 2


def test_rejected_and_gap_decisions_are_durable_and_payload_cannot_be_rewritten(store):
    finding, task, source = prepare(store)
    store.record_delivery("verifier", [source["id"]])
    with pytest.raises(ValueError, match="differs"):
        store.record_verdict(
            task["id"],
            "accept",
            [source["id"]],
            approved_payload={"title": "different defect"},
            reason="refutation considered",
        )
    rejected = store.record_verdict(task["id"], "reject", [source["id"]], reason="refuted")
    assert rejected["verdict"] == "reject"
    assert store.get_outbox(finding["id"]) is None
    assert store.create_finding({"title": "defect"}, [source["id"]], "lead")["id"] == finding["id"]
    assert store.create_finding({"title": "duplicate"}, [], "lead", duplicate_of=finding["id"])["id"] == finding["id"]


def test_provenance_validation_and_snapshot_external_hash(tmp_path, binding):
    import hashlib

    content = b'{"body": "fixed", "title": "issue"}'
    provenance = {
        "revision": "update-time",
        "sha256": hashlib.sha256(content).hexdigest(),
        "source_url": "https://example.test/issues/1",
        "captured_at": "capture-time",
        "historical_head_time": False,
    }
    binding = replace(binding, source_environment={"external": {"issue:1": provenance}})
    store = DeliveryStore(tmp_path / "external.db", binding)
    with pytest.raises(ValueError, match="revision"):
        store.put_source("original", "head", b"x", {"revision": "base"})
    with pytest.raises(ValueError, match="navigation"):
        store.put_source("original", "head", b"x", {"revision": "head", "navigation_only": True})
    with pytest.raises(ValueError, match="provenance"):
        store.put_source("original", "external", content, {"path": "issue:2", "provenance": provenance})
    with pytest.raises(ValueError, match="content"):
        store.put_source("original", "external", b"altered", {"path": "issue:1", "provenance": provenance})
    src = store.put_source("original", "external", content, {"path": "issue:1", "provenance": provenance})
    assert store.get_source(src["id"])["content"] == content
    store.close()


def test_reading_additional_evidence_is_not_semantic_change(store):
    finding, task, source = prepare(store)
    store.record_delivery("verifier", [source["id"]])
    store.record_verdict(task["id"], "accept", [source["id"]], reason="refutation considered")
    extra = store.put_source("original", "head", b"other", {"revision": "head"})
    result = store.update_finding(finding["id"], finding["claim"], [source["id"], extra["id"]])
    assert result["revision"] == 1
    assert result["source_ids"] == [source["id"]]
    assert result["state"] == "accept"
    with pytest.raises(ValueError, match="same_defect_reason"):
        store.update_finding(finding["id"], {"title": "new explanation"}, [source["id"]])
    changed = store.update_finding(
        finding["id"], {"title": "new explanation"}, [source["id"]], same_defect_reason="same original trigger"
    )
    assert changed["revision"] == 2
    assert changed["state"] == "open"
    assert store.db.execute(
        "SELECT claim FROM finding_history WHERE finding_id=? AND revision=1", (finding["id"],)
    ).fetchone()


def test_multiple_verifiers_cannot_replace_a_definitive_decision(store):
    finding, task, source = prepare(store)
    store.create_session("verifier", "other")
    competing = store.create_verification(finding["id"], "other")
    store.record_delivery("verifier", [source["id"]])
    store.record_delivery("other", [source["id"]])
    store.record_verdict(task["id"], "accept", [source["id"]], reason="refutation considered")
    with pytest.raises(ValueError, match="already decided"):
        store.record_verdict(competing["id"], "reject", [source["id"]], reason="refuted")
    assert store.create_verification(finding["id"], "other")["id"] == task["id"]


def test_only_note_holders_receive_task_change_notification(store):
    store.create_session("lead", "lead")
    store.create_session("research", "helper")
    task = store.create_research("policy?", "helper")
    store.record_note_reads("lead", {task["id"]: 0})
    store.add_note(task["id"], "helper", "new navigation")
    assert store.changed_note_tasks("lead") == [task["id"]]
    assert store.changed_note_tasks("helper") == []
    store.record_note_reads("lead", {task["id"]: store.list_notes(task["id"])[-1]["seq"]})
    assert store.changed_note_tasks("lead") == []


def test_acceptance_and_outbox_rollback_together_on_write_failure(store):
    finding, task, source = prepare(store)
    store.record_delivery("verifier", [source["id"]])
    store.db.execute("CREATE TRIGGER fail_outbox BEFORE INSERT ON outbox BEGIN SELECT RAISE(ABORT, 'disk error'); END")
    with pytest.raises(Exception, match="disk error"):
        store.record_verdict(task["id"], "accept", [source["id"]], reason="refutation considered")
    assert store.list_verdicts() == []
    assert store.list_outbox() == []
    assert store.get_finding(finding["id"])["state"] == "open"
    assert store.get_verification(task["id"])["status"] == "open"


def test_replayed_notes_and_challenges_are_idempotent_after_restart(tmp_path, binding):
    path = tmp_path / "replay.db"
    store = DeliveryStore(path, binding)
    finding, _, source = prepare(store)
    task = store.create_research("question", "lead")
    note = store.add_note(task["id"], "lead", "look", [source["id"]], note_id="note-call")
    changed = store.challenge_finding(finding["id"], "counterexample", [source["id"]], challenge_id="challenge-call")
    store.close()
    store = DeliveryStore(path, binding)
    assert store.add_note(task["id"], "lead", "look", [source["id"]], note_id="note-call") == note
    assert (
        store.challenge_finding(finding["id"], "counterexample", [source["id"]], challenge_id="challenge-call")
        == changed
    )
    assert len(store.list_notes()) == 1
    assert store.get_finding(finding["id"])["revision"] == 2
    assert store.get_finding_revision(finding["id"], 2)["reason"] == "counterexample"
    with pytest.raises(ValueError, match="note id already used"):
        store.add_note(task["id"], "lead", "different", [source["id"]], note_id="note-call")
    with pytest.raises(ValueError, match="challenge id already used"):
        store.challenge_finding(finding["id"], "different", [source["id"]], challenge_id="challenge-call")
    assert store.get_finding(finding["id"])["revision"] == 2
    store.close()


def test_rejection_retracts_only_an_existing_delivered_comment(store):
    finding, task, source = prepare(store)
    store.record_delivery("verifier", [source["id"]])
    store.record_verdict(task["id"], "accept", [source["id"]], reason="refutation considered")
    old = store.get_outbox(finding["id"])
    store.freeze_outbox(finding["id"], 1, "old published finding")
    store.transition_outbox(finding["id"], 1, "intended", "uncertain")
    store.transition_outbox(finding["id"], 1, "uncertain", "delivered", remote_id=42)
    store.challenge_finding(finding["id"], "counterexample", [source["id"]])
    new_task = store.create_verification(finding["id"], "verifier")
    rejected = store.record_verdict(new_task["id"], "reject", [source["id"]], reason="counterexample disproves claim")
    outbox = store.get_outbox(finding["id"])
    assert outbox["verdict_id"] == rejected["id"]
    assert outbox["finding_revision"] == 2
    assert outbox["status"] == "intended"
    assert outbox["body"] is None
    assert outbox["last_delivered_body"] == "old published finding"
    assert outbox["marker"] == old["marker"]
    assert outbox["remote_id"] == 42
    store.freeze_outbox(finding["id"], 2, "retracted: counterexample")
    store.transition_outbox(finding["id"], 2, "intended", "uncertain")
    store.transition_outbox(finding["id"], 2, "uncertain", "delivered", remote_id=42)
    assert store.get_outbox(finding["id"])["last_delivered_body"] == "retracted: counterexample"


def test_head_changed_before_write_preserves_previous_delivered_body(store):
    finding, task, source = prepare(store)
    store.record_delivery("verifier", [source["id"]])
    store.record_verdict(task["id"], "accept", [source["id"]], reason="refutation considered")
    store.freeze_outbox(finding["id"], 1, "published version")
    store.transition_outbox(finding["id"], 1, "intended", "delivered", remote_id=42)
    store.challenge_finding(finding["id"], "new evidence", [source["id"]])
    task = store.create_verification(finding["id"], "verifier")
    store.record_verdict(task["id"], "accept", [source["id"]], reason="still valid")
    store.freeze_outbox(finding["id"], 2, "not yet published")
    store.transition_outbox(finding["id"], 2, "intended", "head_changed")
    assert store.get_outbox(finding["id"])["last_delivered_body"] == "published version"


def test_missing_evidence_updates_only_previously_delivered_comment(store):
    finding, task, source = prepare(store)
    store.record_delivery("verifier", [source["id"]])
    store.record_verdict(task["id"], "accept", [source["id"]], reason="refutation considered")
    initial = store.get_outbox(finding["id"])
    store.freeze_outbox(finding["id"], 1, "confirmed original")
    store.transition_outbox(finding["id"], 1, "intended", "delivered", remote_id=42)
    store.challenge_finding(finding["id"], "new policy question", [source["id"]])
    task = store.create_verification(finding["id"], "verifier")
    gap = store.record_verdict(
        task["id"],
        "needs_evidence",
        [source["id"]],
        missing_fact="policy",
        attempted=["repo"],
        recovery_condition="provide policy",
    )
    item = store.get_outbox(finding["id"])
    assert item["verdict_id"] == gap["id"]
    assert item["finding_revision"] == 2
    assert item["approved_payload"] == finding["claim"]
    assert item["marker"] == initial["marker"]
    assert item["remote_id"] == 42
    assert item["last_delivered_body"] == "confirmed original"
    assert item["body"] is None
    store.freeze_outbox(finding["id"], 2, "verification pending: policy missing")
    store.transition_outbox(finding["id"], 2, "intended", "uncertain")
    later_task = store.create_verification(finding["id"], "verifier")
    with pytest.raises(ValueError, match="reconcile"):
        store.record_verdict(later_task["id"], "reject", [source["id"]], reason="counterexample")
    assert store.get_verification(later_task["id"])["status"] == "open"
    store.transition_outbox(finding["id"], 2, "uncertain", "delivered", remote_id=42)


def test_initial_missing_evidence_has_no_outbox(store):
    finding, task, source = prepare(store)
    store.record_verdict(
        task["id"],
        "needs_evidence",
        [],
        missing_fact="policy",
        attempted=["repo"],
        recovery_condition="provide policy",
    )
    assert store.get_outbox(finding["id"]) is None


def test_reopen_recovers_frozen_binding_without_creating_a_run(tmp_path, binding):
    missing = tmp_path / "missing.db"
    with pytest.raises(ValueError, match="existing delivery database"):
        DeliveryStore.reopen(missing)
    assert not missing.exists()
    path = tmp_path / "existing.db"
    original = DeliveryStore(path, binding)
    finding, _, _ = prepare(original)
    original.close()
    reopened = DeliveryStore.reopen(path)
    assert reopened.binding == binding
    assert reopened.get_finding(finding["id"])["claim"] == finding["claim"]
    reopened.close()
    with pytest.raises(ValueError, match="binding mismatch"):
        DeliveryStore(path, replace(binding, head_sha="different"))
    path.unlink()  # Windows detects a leaked SQLite handle here.


def test_reopen_refuses_invalid_binding_and_incomplete_database(tmp_path, binding):
    import sqlite3

    incomplete = tmp_path / "incomplete.db"
    db = sqlite3.connect(incomplete)
    db.execute("CREATE TABLE binding (id INTEGER, value TEXT)")
    db.execute("INSERT INTO binding VALUES (1, '{}')")
    db.commit()
    db.close()
    with pytest.raises(ValueError, match="schema is incomplete"):
        DeliveryStore.reopen(incomplete)
    path = tmp_path / "bad-binding.db"
    store = DeliveryStore(path, binding)
    store.db.execute("UPDATE binding SET value='{}' WHERE id=1")
    store.close()
    with pytest.raises(ValueError, match="binding is invalid"):
        DeliveryStore.reopen(path)
    path.unlink()


def test_retry_cooldown_survives_restart_and_revision(tmp_path, binding, monkeypatch):
    import reviewforge.review_delivery.store as store_module

    monkeypatch.setattr(store_module.time, "time", lambda: 1000.0)
    path = tmp_path / "cooldown.db"
    store = DeliveryStore(path, binding)
    finding, task, source = prepare(store)
    store.record_delivery("verifier", [source["id"]])
    store.record_verdict(task["id"], "accept", [source["id"]], reason="refutation considered")
    store.freeze_outbox(finding["id"], 1, "first")
    store.transition_outbox(finding["id"], 1, "intended", "uncertain")
    store.transition_outbox(finding["id"], 1, "uncertain", "intended", retry_not_before=1100)
    store.challenge_finding(finding["id"], "new evidence", [source["id"]])
    task = store.create_verification(finding["id"], "verifier")
    store.record_verdict(task["id"], "accept", [source["id"]], reason="still valid")
    store.close()
    store = DeliveryStore.reopen(path)
    assert store.get_outbox(finding["id"])["retry_not_before"] == 1100
    store.freeze_outbox(finding["id"], 2, "second")
    with pytest.raises(ValueError, match="cooldown active"):
        store.transition_outbox(finding["id"], 2, "intended", "uncertain")
    monkeypatch.setattr(store_module.time, "time", lambda: 1100.0)
    store.transition_outbox(finding["id"], 2, "intended", "uncertain")
    assert store.get_outbox(finding["id"])["retry_not_before"] is None
    store.close()
