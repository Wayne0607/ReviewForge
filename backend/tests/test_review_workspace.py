from __future__ import annotations

import copy
import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from reviewforge.engine.hypothesis import Hypothesis, HypothesisLedger, Mechanism, Observation, Site
from reviewforge.engine.review_session import EvidenceStore, fingerprint
from reviewforge.engine.review_workspace import SharedReviewWorkspace


def ledger() -> HypothesisLedger:
    return HypothesisLedger("run-a", "head-a", "workspace-a", protocol_version=11)


def source(store: EvidenceStore, *, path: str = "service.py", text: str = "return client.owner") -> str:
    return store.source(SimpleNamespace(side="after", path=path, line=12, text=text, changed=True))


def publish(workspace: SharedReviewWorkspace, evidence_id: str, **changes):
    return workspace.publish_progress(
        **{
            "task_id": "discovery-a",
            "actor": "discovery",
            "unit_ids": ["unit-a"],
            "paths": ["service.py"],
            "summary": "Follow the owner passed to the client",
            "open_questions": ["Which owner does the caller use?"],
            "resolved_questions": [],
            "next_action": "Read the caller",
            "evidence_ids": [evidence_id],
            **changes,
        }
    )


def test_writer_handoff_is_visible_to_a_different_consumer_and_survives_checkpoint():
    state = ledger()
    evidence_id = source(EvidenceStore(state))
    publish(SharedReviewWorkspace(state), evidence_id)
    consumer = SharedReviewWorkspace(state)
    view = consumer.view(task_id="different-task", paths=["service.py"], role="investigation")
    assert view["notes"][0]["next_action"] == "Read the caller"
    assert view["notes"][0]["trust"] == "untrusted-navigation-only"
    assert view["evidence_catalog"][0]["evidence_id"] == evidence_id
    saved = json.loads(json.dumps(state.to_dict()))
    restored = HypothesisLedger.from_dict(saved)
    assert SharedReviewWorkspace(restored).view(paths=["service.py"], role="investigation") == view
    assert SharedReviewWorkspace(restored).read_evidence([evidence_id])[0]["excerpt"] == "return client.owner"


@pytest.mark.parametrize("field", ["run_id", "head_sha", "workspace_digest"])
def test_checkpoint_cannot_move_to_a_different_run_or_checkout(field):
    state = ledger()
    publish(SharedReviewWorkspace(state), source(EvidenceStore(state)))
    saved = state.to_dict()
    saved[field] = "other"
    with pytest.raises(ValueError, match="workspace"):
        HypothesisLedger.from_dict(saved)


@pytest.mark.parametrize("field", ["run_id", "head_sha", "workspace_digest"])
def test_raw_evidence_cannot_be_copied_to_a_different_binding_even_without_notes(field):
    state = ledger()
    source(EvidenceStore(state))
    other = ledger()
    setattr(other, field, "other")
    other.shared_workspace = {}
    other.evidence_records = copy.deepcopy(state.evidence_records)
    with pytest.raises(ValueError, match="evidence-binding"):
        EvidenceStore(other)
    with pytest.raises(ValueError, match="evidence-binding"):
        SharedReviewWorkspace(other)


def test_original_evidence_bytes_are_hashed_on_read_and_checkpoint_restore():
    state = ledger()
    evidence_id = source(EvidenceStore(state))
    workspace = SharedReviewWorkspace(state)
    state.evidence_records[evidence_id]["excerpt"] = "return injected_owner"
    with pytest.raises(ValueError, match="evidence-binding"):
        workspace.read_evidence([evidence_id])
    with pytest.raises(ValueError, match="evidence-binding"):
        HypothesisLedger.from_dict(state.to_dict())


def test_unknown_evidence_and_model_notes_cannot_become_source():
    state = ledger()
    workspace = SharedReviewWorkspace(state)
    with pytest.raises(ValueError, match="evidence-binding"):
        publish(workspace, "ev_does_not_exist")
    note = workspace.publish_progress(task_id="a", actor="discovery", summary="Invented source: allow_all()")
    assert not state.evidence_records
    with pytest.raises(ValueError, match="evidence-binding"):
        workspace.read_evidence([note["id"]])
    bad = EvidenceStore(state).put({"kind": "model-note", "excerpt": "allow_all()"})
    with pytest.raises(ValueError, match="evidence-binding"):
        workspace.read_evidence([bad])


def test_caller_mutation_never_changes_notes_evidence_views_or_checkpoint():
    state = ledger()
    evidence_id = source(EvidenceStore(state))
    workspace = SharedReviewWorkspace(state)
    questions = ["Who owns the client?"]
    note = publish(workspace, evidence_id, open_questions=questions)
    questions.append("mutated input")
    note["open_questions"].append("mutated output")
    view = workspace.view(unit_ids=["unit-a"])
    view["notes"][0]["paths"].clear()
    view["evidence_catalog"][0]["path"] = "wrong.py"
    evidence = workspace.read_evidence([evidence_id])
    evidence[0]["excerpt"] = "wrong source"
    saved = state.to_dict()
    saved["shared_workspace"]["notes"].clear()
    saved["evidence_records"][evidence_id]["excerpt"] = "wrong snapshot"
    fresh = workspace.view(unit_ids=["unit-a"])
    assert fresh["notes"][0]["open_questions"] == ["Who owns the client?"]
    assert fresh["notes"][0]["paths"] == ["service.py"]
    assert fresh["evidence_catalog"][0]["path"] == "service.py"
    assert workspace.read_evidence([evidence_id])[0]["excerpt"] == "return client.owner"


def test_identical_updates_are_idempotent_under_concurrent_workers():
    state = ledger()
    evidence_id = source(EvidenceStore(state))
    with ThreadPoolExecutor(max_workers=8) as pool:
        notes = list(pool.map(lambda _: publish(SharedReviewWorkspace(state), evidence_id), range(40)))
    assert all(note == notes[0] for note in notes)
    assert len(state.shared_workspace["notes"]) == 1
    assert len(state.evidence_records) == 1


def test_current_snapshot_replaces_stale_questions_but_preserves_audit_history_after_sorted_restore():
    state = ledger()
    workspace = SharedReviewWorkspace(state)
    identity = source(EvidenceStore(state))
    old = publish(workspace, identity)
    new = publish(
        workspace,
        identity,
        summary="Caller supplies the owner",
        open_questions=[],
        resolved_questions=["Which owner does the caller use?"],
        next_action="Compare contract",
    )
    assert old["id"] != new["id"]
    assert len(state.shared_workspace["notes"]) == 2
    assert workspace.view(paths=["service.py"])["notes"] == [new]
    restored = HypothesisLedger.from_dict(json.loads(json.dumps(state.to_dict(), sort_keys=True)))
    other = SharedReviewWorkspace(restored)
    assert other.view(paths=["service.py"])["notes"] == [new]
    assert publish(other, identity) == old
    assert other.view(paths=["service.py"])["notes"] == [old]
    assert len(restored.shared_workspace["notes"]) == 2


@pytest.mark.parametrize("tamper", ["missing", "unknown", "other_owner"])
def test_current_pointer_is_validated_against_its_task_and_actor(tamper):
    state = ledger()
    workspace = SharedReviewWorkspace(state)
    identity = source(EvidenceStore(state))
    first = publish(workspace, identity)
    second = publish(workspace, identity, task_id="different-task", actor="investigation")
    saved = state.to_dict()
    current = saved["shared_workspace"]["current"]
    owner = next(key for key, value in current.items() if value == first["id"])
    if tamper == "missing":
        del current[owner]
    elif tamper == "unknown":
        current[owner] = "note_unknown"
    else:
        current[owner] = second["id"]
    with pytest.raises(ValueError, match="workspace-current"):
        HypothesisLedger.from_dict(saved)


def test_concurrent_independent_handoffs_do_not_drop_notes_or_duplicate_evidence():
    state = ledger()

    def write(index):
        store = EvidenceStore(state)
        identity = source(store)
        return publish(SharedReviewWorkspace(state), identity, task_id=f"task-{index}")

    with ThreadPoolExecutor(max_workers=8) as pool:
        notes = list(pool.map(write, range(40)))
    assert len(state.shared_workspace["notes"]) == len(notes)
    assert len(state.evidence_records) == 1


@pytest.mark.parametrize("role", ["proof", "source_audit"])
def test_blind_views_expose_only_raw_catalog_without_upstream_judgment(role):
    state = ledger()
    evidence_id = source(EvidenceStore(state))
    workspace = SharedReviewWorkspace(state)
    publish(workspace, evidence_id, summary="UPSTREAM_CONFIRMED_SECRET", next_action="UPSTREAM_THINKING_SECRET")
    hypothesis = Hypothesis(
        "h_1",
        "unit::owner",
        "unit-a",
        Mechanism.WRONG_ARGUMENT,
        "CANDIDATE_CLAIM_SECRET",
        "TRIGGER_SECRET",
        "IMPACT_SECRET",
        "QUESTION_SECRET",
        "REFUTATION_SECRET",
        [Site("service.py", 12, "call()")],
        "error",
        "discovery",
    )
    state.upsert(hypothesis)
    state.discovery_blocks["discovery-a"] = {"unit_ids": ["unit-a"], "session": {"status": "failed"}}
    view = workspace.view(paths=["service.py"], role=role)
    assert set(view) == {"binding", "role", "evidence_catalog"}
    assert view["evidence_catalog"][0]["evidence_id"] == evidence_id
    assert "SECRET" not in json.dumps(view)
    normal = workspace.view(unit_ids=["unit-a"], role="investigation")
    assert normal["candidates"][0]["claim"] == "CANDIDATE_CLAIM_SECRET"
    assert normal["tasks"][0]["status"] == "failed"


def test_unknown_role_fails_closed():
    with pytest.raises(ValueError, match="role"):
        SharedReviewWorkspace(ledger()).view(role="proof_with_notes")


def test_unknown_catalog_fails_closed():
    with pytest.raises(ValueError, match="catalog"):
        SharedReviewWorkspace(ledger()).view(catalog="references_with_notes")


def test_directory_groups_hundreds_of_records_without_losing_ranges_ids_or_original_bytes():
    state = ledger()
    store = EvidenceStore(state)
    ids = []
    expected = []
    for line in [*range(1, 301), *range(401, 501)]:
        text = f"value_{line} = original({line})"
        expected.append(text)
        ids.append(store.source(SimpleNamespace(side="after", path="large.py", line=line, text=text, changed=True)))
    workspace = SharedReviewWorkspace(state)
    note = workspace.publish_progress(
        task_id="large-task",
        actor="discovery",
        paths=["large.py"],
        summary="Review the omitted gap next",
        evidence_ids=ids,
    )
    directory = workspace.view(paths=["large.py"], catalog="directory")
    assert directory["evidence_catalog"] == []
    assert directory["evidence_directory"] == [
        {
            "kind": "delivered-source",
            "path": "large.py",
            "side": "after",
            "tool": "delivered-source",
            "status": "success",
            "source_sha": "head-a",
            "record_count": 400,
            "line_ranges": [[1, 300], [401, 500]],
        }
    ]
    references = workspace.view(paths=["large.py"])
    directory_text = json.dumps(directory["evidence_directory"])
    reference_text = json.dumps(references["evidence_catalog"])
    assert directory_text.count('"path": "large.py"') == 1
    assert reference_text.count('"path": "large.py"') == len(ids)
    assert len(directory_text) < len(reference_text)
    assert all(identity not in directory_text for identity in ids)
    assert directory["notes"] == [note]
    returned_ids = [item["evidence_id"] for item in references["evidence_catalog"]]
    assert set(returned_ids) == set(ids)
    actual = {row["evidence_id"]: row["excerpt"] for row in workspace.read_evidence(returned_ids)}
    assert [actual[identity] for identity in ids] == expected
    restored = HypothesisLedger.from_dict(json.loads(json.dumps(state.to_dict(), sort_keys=True)))
    resumed = SharedReviewWorkspace(restored)
    assert resumed.view(paths=["large.py"], catalog="directory") == directory
    assert resumed.read_evidence(ids) == workspace.read_evidence(ids)


def test_directory_merges_actual_overlapping_and_adjacent_ranges_and_keeps_tool_status_side_groups():
    state = ledger()
    store = EvidenceStore(state)
    for index, span in enumerate(((2, 4), (4, 7), (8, 9), (12, 12))):
        store.observation(
            Observation(
                str(index),
                "read_file",
                f"q{index}",
                "file.py",
                (1, 100),
                "head-a",
                str(index),
                f"snippet{index}",
                "success",
                span,
            )
        )
    store.observation(
        Observation("failed", "read_file", "missing", "file.py", (1, 100), "head-a", "d", "", "not_found")
    )
    store.observation(
        Observation("grep", "grep", "match", "file.py", None, "head-a", "d", "file.py:20:match", "success")
    )
    store.source(SimpleNamespace(side="before", path="file.py", line=1, text="old", changed=True), base_sha="base")
    store.source(SimpleNamespace(side="after", path="file.py", line=1, text="new", changed=True))
    workspace = SharedReviewWorkspace(state)
    entries = workspace.view(paths=["file.py"], catalog="directory")["evidence_directory"]
    assert len(entries) == 5
    read = next(row for row in entries if row["tool"] == "read_file" and row["status"] == "success")
    assert read["record_count"] == 4 and read["line_ranges"] == [[2, 9], [12, 12]]
    failed = next(row for row in entries if row["status"] == "not_found")
    assert failed["record_count"] == 1 and failed["line_ranges"] == []
    grep = next(row for row in entries if row["tool"] == "grep")
    assert grep["line_ranges"] == []
    before = next(row for row in entries if row.get("side") == "before")
    assert before["source_sha"] == "base" and before["line_ranges"] == [[1, 1]]
    # Returned aggregation is still isolated from persistent evidence.
    read["line_ranges"][0][1] = 1000
    again = workspace.view(paths=["file.py"], catalog="directory")["evidence_directory"]
    assert next(row for row in again if row["tool"] == "read_file" and row["status"] == "success")["line_ranges"] == [
        [2, 9],
        [12, 12],
    ]


@pytest.mark.parametrize("role", ["proof", "source_audit"])
def test_blind_directory_contains_only_raw_metadata(role):
    state = ledger()
    workspace = SharedReviewWorkspace(state)
    publish(workspace, source(EvidenceStore(state)), summary="PRIVATE_UPSTREAM_JUDGMENT")
    view = workspace.view(paths=["service.py"], role=role, catalog="directory")
    assert set(view) == {"binding", "role", "evidence_catalog", "evidence_directory"}
    assert view["evidence_catalog"] == [] and view["evidence_directory"][0]["record_count"] == 1
    assert "PRIVATE_UPSTREAM_JUDGMENT" not in json.dumps(view)


def test_path_and_unit_queries_find_cross_group_evidence_but_do_not_dump_unrelated_work():
    state = ledger()
    store = EvidenceStore(state)
    evidence_id = source(store)
    unrelated = source(store, path="unrelated.py")
    workspace = SharedReviewWorkspace(state)
    publish(workspace, evidence_id)
    publish(workspace, unrelated, task_id="other", paths=["unrelated.py"], unit_ids=["unit-b"])
    for filters in ({"paths": ["service.py"]}, {"unit_ids": ["unit-a"]}, {"task_id": "discovery-a"}):
        view = workspace.view(**filters)
        assert [row["evidence_id"] for row in view["evidence_catalog"]] == [evidence_id]
        assert [row["task_id"] for row in view["notes"]] == ["discovery-a"]
    assert not workspace.view()["notes"] and not workspace.view()["evidence_catalog"]
    fresh_raw = source(store, path="caller.py")
    assert workspace.view(paths=["caller.py"])["evidence_catalog"][0]["evidence_id"] == fresh_raw


def test_tool_observation_metadata_and_original_bytes_are_available_without_note_copy():
    state = ledger()
    observation = Observation(
        "obs", "read_file", "query", "caller.py", (1, 3), "head-a", "digest", "owner = current_user", "success", (2, 2)
    )
    evidence_id = EvidenceStore(state).observation(observation)
    workspace = SharedReviewWorkspace(state)
    view = workspace.view(paths=["caller.py"])
    assert view["evidence_catalog"][0]["excerpt_line_range"] == [2, 2]
    assert "excerpt" not in view["evidence_catalog"][0]
    assert workspace.read_evidence([evidence_id])[0]["observation"]["excerpt"] == observation.excerpt
    assert not state.shared_workspace["notes"]


def test_shared_dependency_path_selects_the_original_groups_note_and_task():
    state = ledger()
    store = EvidenceStore(state)
    identity = source(store, path="messages_en.json")
    workspace = SharedReviewWorkspace(state)
    publish(workspace, identity, paths=["messages_it.json"])
    state.discovery_blocks["discovery-a"] = {"unit_ids": ["unit-a"], "session": {"status": "running"}}
    view = workspace.view(task_id="another-group", paths=["messages_en.json"])
    assert view["notes"][0]["paths"] == ["messages_it.json"]
    assert view["tasks"][0]["task_id"] == "discovery-a"
    assert view["evidence_catalog"][0] == {
        "evidence_id": identity,
        "kind": "delivered-source",
        "path": "messages_en.json",
        "side": "after",
        "source_sha": "head-a",
        "line": 12,
        "tool": "delivered-source",
        "status": "success",
        "line_range": [12, 12],
    }


def test_context_and_before_sources_preserve_original_side_and_version():
    state = ledger()
    store = EvidenceStore(state)
    before = store.source(
        SimpleNamespace(side="before", path="old.py", line=2, text="old()", changed=True), base_sha="base-a"
    )
    context = store.source(SimpleNamespace(side="context", path="caller.py", line=3, text="call()", changed=False))
    workspace = SharedReviewWorkspace(state)
    rows = workspace.read_evidence([before, context])
    assert (rows[0]["side"], rows[0]["source_sha"]) == ("before", "base-a")
    assert (rows[1]["side"], rows[1]["source_sha"]) == ("context", "head-a")


def test_rehashed_outer_record_cannot_hide_wrong_nested_head():
    state = ledger()
    raw = {"kind": "tool-observation", "observation": {"sha": "wrong-head", "excerpt": "call()"}}
    evidence_id = EvidenceStore(state).put(raw)
    with pytest.raises(ValueError, match="observation-binding"):
        SharedReviewWorkspace(state).read_evidence([evidence_id])


def test_no_count_or_byte_truncation_of_shared_navigation():
    state = ledger()
    workspace = SharedReviewWorkspace(state)
    text = "long navigation " * 10_000
    publish(workspace, source(EvidenceStore(state)), summary=text, open_questions=[f"Question {i}" for i in range(80)])
    view = workspace.view(paths=["service.py"])
    assert view["notes"][0]["summary"] == text
    assert len(view["notes"][0]["open_questions"]) == 80


def test_tampered_note_checkpoint_is_rejected_without_turning_it_into_evidence():
    state = ledger()
    note = publish(SharedReviewWorkspace(state), source(EvidenceStore(state)))
    saved = state.to_dict()
    saved["shared_workspace"]["notes"][note["id"]]["summary"] = "changed judgment"
    with pytest.raises(ValueError, match="workspace-note"):
        HypothesisLedger.from_dict(saved)


@pytest.mark.parametrize("protocol", range(1, 11))
def test_legacy_protocol_shapes_and_evidence_hashes_do_not_change(protocol):
    state = HypothesisLedger("old-run", "old-head", "old-workspace", protocol_version=protocol)
    identity = source(EvidenceStore(state))
    assert "run_id" not in state.evidence_records[identity]
    assert identity == "ev_" + fingerprint(state.evidence_records[identity])[:24]
    saved = state.to_dict()
    assert "shared_workspace" not in saved
    assert HypothesisLedger.from_dict(saved).to_dict() == saved
    with pytest.raises(ValueError, match="protocol 11"):
        SharedReviewWorkspace(state)


def test_protocol_11_does_not_silently_upgrade_old_evidence():
    old = HypothesisLedger("run-a", "head-a", "workspace-a", protocol_version=10)
    source(EvidenceStore(old))
    saved = old.to_dict()
    saved["protocol_version"] = 11
    with pytest.raises(ValueError, match="evidence-binding"):
        HypothesisLedger.from_dict(saved)
