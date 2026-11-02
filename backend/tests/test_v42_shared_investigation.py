"""Shared navigation is task-fenced and never substitutes for original evidence."""

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from reviewforge.review_v42.contracts import BindingError, LeaseError, ReviewScope, RevisionConflict, Run, Task
from reviewforge.review_v42.shared_investigation import SharedInvestigations
from reviewforge.review_v42.store import ReviewStore


@pytest.fixture
def board(tmp_path):
    regions = [
        {
            "id": name,
            "path": f"{name}.py",
            "side": "head",
            "start_line": 1,
            "end_line": 2,
            "base_start_line": 1,
            "base_end_line": 2,
            "changed_lines": [1],
            "patch": "",
        }
        for name in ("a", "b", "c")
    ]
    run = Run(
        "run",
        "org/repo",
        "base",
        "head",
        policy="v42-units-6",
        metadata={
            "workspace_digest": "snapshot",
            "base_workspace_digest": "base-snapshot",
            "changed_regions": regions,
            "shared_investigation": "shared-1",
        },
    )
    store = ReviewStore(tmp_path / "board.sqlite", run)
    scopes = [ReviewScope(r["id"], run.id, r["id"], regions=[r]) for r in regions]
    store.install_unit_plan(scopes, ReviewScope("integration", run.id, "integration", dependencies=["a", "b", "c"]))
    for name in ("a", "b", "c"):
        store.add(Task("task-" + name, run.id, "review", name, 0))
    tokens = {
        name: store.acquire_task("task-" + name, "worker-" + name, 100, now=0).lease_token for name in ("a", "b", "c")
    }
    return store, tokens, SharedInvestigations(store)


def source(store, path="a.py"):
    return store.put_evidence(
        b"line one\nline two\n",
        revision="head",
        side="head",
        kind="source",
        path=path,
        start_line=1,
        end_line=2,
        metadata={"workspace_digest": "snapshot"},
    )


def audit(store, task_id, token, turn_id, ref=None, *, page=False, now=1):
    messages = []
    if ref:
        content = "line one\n" if page else store.read_evidence(ref.id).decode()
        receipt = {"evidence_ref": ref.id, "content": content, "start_line": 1, "end_line": 1 if page else 2}
        messages.append({"type": "tool", "data": {"content": json.dumps(receipt)}})
    store.append_turn_audit(task_id, token, turn_id, "request", messages, now=now)
    response = [{"type": "ai", "data": {"content": "ok"}}]
    store.append_turn_audit(task_id, token, turn_id, "response", response, now=now)
    store.append_turn_audit(task_id, token, turn_id, "exchange", response, now=now)


def create(store, token, api, *, call="call-1", turn="turn-a"):
    audit(store, "task-a", token, turn)
    return api.create("task-a", token, "why?", ["a", "b"], ["a", "b"], turn_id=turn, tool_call_id=call, now=1)


def claim(api, task, token, item, *, op, now=2):
    return api.claim(task, token, item["id"], item["revision"], "take responsibility", operation_id=op, now=now)


def resume_b(store, tokens):
    old_id = tokens.get("b_id", "task-b")
    store.settle_task_stale(old_id, tokens["b"], {"reason": "shared investigation"}, now=1)
    revision = store.get(ReviewScope, "b").revision
    task_id = f"task-b-{revision}"
    store.add(Task(task_id, store.run.id, "review", "b", revision))
    tokens["b"] = store.acquire_task(task_id, "worker-b", 100, now=1).lease_token
    tokens["b_id"] = task_id
    return task_id


def update(api, task, token, item, *, op, turn, refs=(), status="open", now=3):
    return api.update(
        task,
        token,
        item["id"],
        item["revision"],
        question="why?",
        observations=[{"statement": "possibly related", "evidence_refs": list(refs)}] if refs else [],
        open_questions=[] if status == "resolved" else ["verify"],
        status=status,
        reason="investigation update",
        turn_id=turn,
        operation_id=op,
        now=now,
    )


def test_opt_in_and_run_binding_do_not_migrate_old_checkpoints(tmp_path, board):
    store, _, _ = board
    with pytest.raises(BindingError):
        ReviewStore(store.path, replace(store.run, metadata={**store.run.metadata, "shared_investigation": "other"}))
    run = Run("old", "repo", "base", "head", policy="v42-units-6")
    old = ReviewStore(tmp_path / "old.sqlite", run)
    with pytest.raises(BindingError):
        SharedInvestigations(old)
    with old._connect() as db:
        assert db.execute("SELECT name FROM sqlite_master WHERE name='shared_investigations'").fetchone() is None


def test_controller_identity_replay_and_independent_calls(board):
    store, tokens, api = board
    item = create(store, tokens["a"], api)
    assert create(store, tokens["a"], api) == item
    other = create(store, tokens["a"], api, call="call-2")
    assert other["id"] != item["id"]
    with pytest.raises(RevisionConflict):
        api.create(
            "task-a", tokens["a"], "different", ["a", "b"], ["a", "b"], turn_id="turn-a", tool_call_id="call-1", now=1
        )
    with pytest.raises(BindingError):
        api.create("task-a", tokens["a"], "?", ["a"], ["b"], turn_id="turn-a", tool_call_id="new", now=1)
    with pytest.raises(LeaseError):
        api.create("task-a", tokens["a"], "?", ["a"], ["a"], turn_id="missing", tool_call_id="new", now=1)


def test_claim_concurrency_expiry_and_recovery(board):
    store, tokens, api = board
    item = create(store, tokens["a"], api)
    bid = resume_b(store, tokens)
    ids = {"a": "task-a", "b": bid}

    def attempt(name):
        try:
            return claim(api, ids[name], tokens[name], item, op="claim-" + name)
        except (RevisionConflict, LeaseError):
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, ("a", "b")))
    winner = next(x for x in results if x)
    assert sum(x is not None for x in results) == 1
    name = "b" if winner["owner_task_id"] == "task-a" else "a"
    loser_id = ids[name]
    store.renew_task(loser_id, tokens[name], 200, now=3)
    recovered = SharedInvestigations(ReviewStore(store.path, store.run))
    new = claim(recovered, loser_id, tokens[name], winner, op="reclaim", now=102)
    assert new["owner_task_id"] == loser_id
    with pytest.raises(LeaseError):
        old_name = "a" if name == "b" else "b"
        update(api, winner["owner_task_id"], tokens[old_name], winner, op="old", turn="turn-a", now=102)
    assert any(e["action"] == "claim" for e in store.history("SharedInvestigation", item["id"]))


def test_delivery_page_fencing_and_explicit_handoff(board):
    store, tokens, api = board
    ref = source(store)
    item = claim(api, "task-a", tokens["a"], create(store, tokens["a"], api), op="claim")
    bid = resume_b(store, tokens)
    with pytest.raises(BindingError):
        update(api, "task-a", tokens["a"], item, op="undelivered", turn="turn-a", refs=[ref.id])
    audit(store, "task-a", tokens["a"], "page", ref, page=True)
    changed = update(api, "task-a", tokens["a"], item, op="observed", turn="page", refs=[ref.id])
    assert changed["observations"][0]["delivered_ranges"][ref.id] == [[1, 1]]
    assert changed["observations"][0]["delivered_task_id"] == "task-a"
    assert changed["observations"][0]["delivered_turn_id"] == "page"
    assert update(api, "task-a", tokens["a"], item, op="observed", turn="page", refs=[ref.id]) == changed
    released = api.release(
        "task-a", tokens["a"], item["id"], changed["revision"], "handoff", operation_id="release", now=3
    )
    claimed = claim(api, bid, tokens["b"], released, op="claim-b", now=4)
    audit(store, bid, tokens["b"], "b-empty")
    with pytest.raises(BindingError):
        update(api, bid, tokens["b"], claimed, op="borrow", turn="b-empty", refs=[ref.id], now=5)
    audit(store, bid, tokens["b"], "b-read", ref, now=5)
    done = update(api, bid, tokens["b"], claimed, op="resolved", turn="b-read", refs=[ref.id], status="resolved", now=6)
    assert done["status"] == "resolved" and done["owner_task_id"] == ""
    assert store.read_evidence(ref.id) == b"line one\nline two\n"
    with pytest.raises((LeaseError, RevisionConflict)):
        update(api, "task-a", tokens["a"], changed, op="stale", turn="page", refs=[ref.id], now=6)


def test_paged_directory_is_untrusted_scoped_and_stable(board):
    store, tokens, api = board
    items = [create(store, tokens["a"], api, call=f"call-{i}") for i in range(3)]
    bid = resume_b(store, tokens)
    page = api.list(bid, tokens["b"], limit=1, now=2)
    assert page["trust"] == "untrusted_navigation" and page["total"] == 3
    create(store, tokens["a"], api, call="later")
    bid = resume_b(store, tokens)
    second = api.list(bid, tokens["b"], limit=1, cursor=page["next_cursor"], now=2)
    third = api.list(bid, tokens["b"], limit=1, cursor=second["next_cursor"], now=2)
    assert {page["items"][0]["id"], second["items"][0]["id"], third["items"][0]["id"]} == {i["id"] for i in items}
    assert third["next_cursor"] is None
    with pytest.raises(BindingError):
        api.list("task-c", tokens["c"], limit=1, cursor=page["next_cursor"], now=2)
    with pytest.raises(BindingError):
        api.get("task-c", tokens["c"], items[0]["id"], now=2)


def test_invalid_ref_expired_task_and_operation_conflicts(board):
    store, tokens, api = board
    item = claim(api, "task-a", tokens["a"], create(store, tokens["a"], api), op="claim")
    bid = resume_b(store, tokens)
    with pytest.raises(BindingError):
        update(api, "task-a", tokens["a"], item, op="missing-ref", turn="turn-a", refs=["another-run-ref"])
    with pytest.raises(LeaseError):
        api.claim(bid, tokens["b"], item["id"], item["revision"], "compete", operation_id="b", now=3)
    with pytest.raises(RevisionConflict):
        api.release("task-a", tokens["a"], item["id"], item["revision"] - 1, "stale", operation_id="stale", now=3)
    with pytest.raises(LeaseError):
        api.get("task-a", tokens["a"], item["id"], now=100)
    changed = update(api, "task-a", tokens["a"], item, op="update", turn="turn-a")
    with pytest.raises(RevisionConflict):
        api.update(
            "task-a",
            tokens["a"],
            item["id"],
            item["revision"],
            question="different",
            observations=[],
            open_questions=["verify"],
            status="open",
            reason="investigation update",
            turn_id="turn-a",
            operation_id="update",
            now=3,
        )
    assert api.get("task-a", tokens["a"], item["id"], now=3)["revision"] == changed["revision"]


def test_failed_response_and_unauthorized_cross_task_turn_cannot_supply_evidence(board):
    store, tokens, api = board
    ref = source(store)
    item = claim(api, "task-a", tokens["a"], create(store, tokens["a"], api), op="claim")
    bid = resume_b(store, tokens)
    audit(store, bid, tokens["b"], "other-turn", ref)
    with pytest.raises(LeaseError):
        update(api, "task-a", tokens["a"], item, op="wrong-turn", turn="other-turn", refs=[ref.id])
    store.append_turn_audit("task-a", tokens["a"], "failed", "request", [], now=1)
    with pytest.raises(BindingError):
        update(api, "task-a", tokens["a"], item, op="failed-turn", turn="failed", refs=[ref.id])


def test_notes_do_not_touch_case_revision_or_verdict(board):
    from reviewforge.review_v42.contracts import Case

    store, tokens, api = board
    case = Case("case", store.run.id, scope_id="a", trigger="trigger", mechanism="mechanism")
    store.add(case)
    item = claim(api, "task-a", tokens["a"], create(store, tokens["a"], api), op="claim")
    updated = update(api, "task-a", tokens["a"], item, op="navigate", turn="turn-a")
    assert updated["trust"] == "untrusted_navigation"
    assert store.get(Case, case.id) == case


def test_case_directory_has_no_verdict_and_cursor_is_scoped(board):
    from reviewforge.review_v42.contracts import Case

    store, tokens, api = board
    ref = source(store)
    for n in range(3):
        store.add(
            Case(
                f"case-{n}",
                store.run.id,
                scope_id="b",
                trigger="possible trigger",
                claims=[{"id": "claim", "statement": "possible issue"}],
                evidence_refs=[ref.id],
            )
        )
    page = api.list_cases("task-a", tokens["a"], limit=1, now=1)
    assert page["total"] == 3 and page["items"][0]["scope_id"] == "b"
    assert page["items"][0]["evidence_refs"] == [ref.id]
    assert "verdict" not in json.dumps(page).lower()
    assert page["items"][0]["trust"] == "untrusted_navigation"
    store.add(Case("late", store.run.id, scope_id="a", trigger="later"))
    page2 = api.list_cases("task-a", tokens["a"], limit=1, cursor=page["next_cursor"], now=1)
    page3 = api.list_cases("task-a", tokens["a"], limit=1, cursor=page2["next_cursor"], now=1)
    assert page3["next_cursor"] is None and page3["total"] == 3
    with pytest.raises(BindingError):
        api.list_cases("task-b", tokens["b"], limit=1, cursor=page["next_cursor"], now=1)
    with pytest.raises(BindingError):
        api.list("task-a", tokens["a"], limit=1, cursor=page["next_cursor"], now=1)


def test_task_heartbeat_extends_investigation_ownership(board):
    store, tokens, api = board
    item = claim(api, "task-a", tokens["a"], create(store, tokens["a"], api), op="claim")
    bid = resume_b(store, tokens)
    store.renew_task("task-a", tokens["a"], 200, now=90)
    store.renew_task(bid, tokens["b"], 200, now=90)
    assert api.get("task-a", tokens["a"], item["id"], now=150)["lease_expires"] == 290
    with pytest.raises(LeaseError):
        claim(api, bid, tokens["b"], item, op="too-early", now=150)
    audit(store, "task-a", tokens["a"], "later-turn", now=150)
    changed = update(api, "task-a", tokens["a"], item, op="after-heartbeat", turn="later-turn", now=150)
    assert changed["owner_active"] is True


def test_preview_is_lightweight_and_excludes_closed(board):
    store, tokens, api = board
    item = claim(api, "task-a", tokens["a"], create(store, tokens["a"], api), op="claim")
    preview = api.preview("task-a", tokens["a"], now=2)
    assert preview["total"] == 1 and not preview["has_more"]
    assert preview["items"][0]["id"] == item["id"] and "observations" not in preview["items"][0]
    assert preview["trust"] == "untrusted_navigation"
    ref = source(store)
    audit(store, "task-a", tokens["a"], "source-turn", ref)
    update(api, "task-a", tokens["a"], item, op="close", turn="source-turn", refs=[ref.id], status="resolved")
    assert api.preview("task-a", tokens["a"], now=3)["total"] == 0


def test_shared_creation_fences_affected_scopes_and_blocks_completion(board):
    store, tokens, api = board
    before_c = store.get(ReviewScope, "c").revision
    item = create(store, tokens["a"], api)
    with pytest.raises(RevisionConflict):
        api.preview("task-b", tokens["b"], now=2)
    assert store.get(ReviewScope, "c").revision == before_c
    with store._connect() as db:
        assert store._pending_shared_investigations(db, "a") == [item["id"]]
        assert store._pending_shared_investigations(db, "b") == [item["id"]]
        assert store._pending_shared_investigations(db, "c") == []
        assert store._pending_shared_investigations(db, "integration") == [item["id"]]
    claimed = claim(api, "task-a", tokens["a"], item, op="claim")
    ref = source(store)
    audit(store, "task-a", tokens["a"], "source-turn", ref)
    update(api, "task-a", tokens["a"], claimed, op="close", turn="source-turn", refs=[ref.id], status="resolved")
    with store._connect() as db:
        assert store._pending_shared_investigations(db, "a") == []
        assert ("a", "a", ref.id) in {
            (pending["scope_id"], pending["region_id"], pending["ref_id"])
            for pending in store.pending_unit_sources(db=db)
        }
    assert store.get(ReviewScope, "a").checked_regions == []


def test_create_rolls_back_when_transaction_fence_fails(board, monkeypatch):
    store, tokens, api = board
    audit(store, "task-a", tokens["a"], "turn-a")

    def fail(*args, **kwargs):
        raise BindingError("cannot fence")

    monkeypatch.setattr(store, "_invalidate_scopes_for_shared", fail)
    with pytest.raises(BindingError):
        api.create(
            "task-a", tokens["a"], "why?", ["a", "b"], ["a", "b"], turn_id="turn-a", tool_call_id="failed", now=1
        )
    assert api.preview("task-a", tokens["a"], now=1)["total"] == 0


def test_preview_soft_page_keeps_all_items_and_complete_gate_checks_them(board):
    store, tokens, api = board
    expected = {create(store, tokens["a"], api, call=f"call-{i}")["id"] for i in range(23)}
    with store._connect() as db:
        before = db.execute("SELECT COUNT(*) FROM shared_investigation_snapshots").fetchone()[0]
    preview = api.preview("task-a", tokens["a"], now=2)
    assert preview["total"] == 23 and len(preview["items"]) == 20 and preview["has_more"]
    unseen = expected - {item["id"] for item in preview["items"]}
    assert len(unseen) == 3
    with store._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM shared_investigation_snapshots").fetchone()[0] == before
        assert set(store._pending_shared_investigations(db, "a")) == expected
    cursor = None
    found = set()
    while True:
        page = api.list("task-a", tokens["a"], limit=7, cursor=cursor, now=2)
        found.update(item["id"] for item in page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert found == expected


def test_resolved_requires_delivered_available_source_not_search_or_diff(board):
    store, tokens, api = board
    item = claim(api, "task-a", tokens["a"], create(store, tokens["a"], api), op="claim")
    with pytest.raises(BindingError):
        update(api, "task-a", tokens["a"], item, op="empty-close", turn="turn-a", status="resolved")
    for kind, metadata in [
        ("search", {}),
        ("diff", {}),
        ("source", {"navigation_only": True}),
        ("source", {"complete": False}),
        ("source", {"status": "unavailable"}),
        ("source", {"tool_name": "query_shared"}),
    ]:
        ref = store.put_evidence(
            b"line one\nline two\n",
            revision="head",
            side="head",
            kind=kind,
            path="a.py",
            start_line=1,
            end_line=2,
            metadata={"workspace_digest": "snapshot", **metadata},
        )
        turn = "turn-" + ref.id
        audit(store, "task-a", tokens["a"], turn, ref)
        with pytest.raises(BindingError):
            update(api, "task-a", tokens["a"], item, op="close-" + ref.id, turn=turn, refs=[ref.id], status="resolved")


def test_resolved_can_include_delivered_original_test_and_documentation(board):
    store, tokens, api = board
    item = claim(api, "task-a", tokens["a"], create(store, tokens["a"], api), op="claim")
    refs = [source(store)]
    for kind in ("test", "knowledge"):
        refs.append(
            store.put_evidence(
                b"line one\nline two\n",
                revision="head",
                side="head",
                kind=kind,
                path=f"{kind}.txt",
                start_line=1,
                end_line=2,
                metadata={"workspace_digest": "snapshot"},
            )
        )
    messages = [
        {
            "type": "tool",
            "data": {
                "content": json.dumps(
                    {
                        "evidence_ref": ref.id,
                        "content": store.read_evidence(ref.id).decode(),
                        "start_line": 1,
                        "end_line": 2,
                    }
                )
            },
        }
        for ref in refs
    ]
    store.append_turn_audit("task-a", tokens["a"], "mixed", "request", messages, now=1)
    store.append_turn_audit(
        "task-a", tokens["a"], "mixed", "response", [{"type": "ai", "data": {"content": "ok"}}], now=1
    )
    store.append_turn_audit(
        "task-a", tokens["a"], "mixed", "exchange", [{"type": "ai", "data": {"content": "ok"}}], now=1
    )
    closed = update(
        api, "task-a", tokens["a"], item, op="mixed", turn="mixed", refs=[r.id for r in refs], status="resolved"
    )
    assert set(closed["observations"][0]["evidence_refs"]) == {r.id for r in refs}
    assert {pending["ref_id"] for pending in store.pending_unit_sources()} == {refs[0].id}


def test_retraction_creates_no_fictitious_source_and_rolls_back_on_error(board, monkeypatch):
    store, tokens, api = board
    item = claim(api, "task-a", tokens["a"], create(store, tokens["a"], api), op="claim")
    original = store._apply_shared_resolution

    def fail(*args, **kwargs):
        raise BindingError("could not update unit responsibility")

    monkeypatch.setattr(store, "_apply_shared_resolution", fail)
    with pytest.raises(BindingError):
        update(api, "task-a", tokens["a"], item, op="withdraw", turn="turn-a", status="retracted")
    assert api.get("task-a", tokens["a"], item["id"], now=3)["status"] == "open"
    monkeypatch.setattr(store, "_apply_shared_resolution", original)
    closed = update(api, "task-a", tokens["a"], item, op="withdraw", turn="turn-a", status="retracted")
    assert closed["status"] == "retracted" and closed["reason"]
    assert store.pending_unit_sources() == []
    assert store.get(ReviewScope, "a").checked_regions == []
