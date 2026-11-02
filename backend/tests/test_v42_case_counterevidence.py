"""Explicit cross-unit leads must invalidate only the affected case atomically."""

import json
import threading
from dataclasses import replace

import pytest

from reviewforge.review_v42.context import ContextBuilder
from reviewforge.review_v42.contracts import BindingError, Case, LeaseError, ReviewScope, RevisionConflict, Run, Task
from reviewforge.review_v42.publication import PublicationOutbox
from reviewforge.review_v42.store import ReviewStore


@pytest.fixture
def board(tmp_path):
    regions = [
        {"id": name, "path": name + ".py", "side": "head", "start_line": 1, "end_line": 2, "changed_lines": [1]}
        for name in ("a", "b")
    ]
    run = Run(
        "run",
        "repo",
        "base",
        "head",
        policy="v42-units-6",
        metadata={
            "shared_investigation": "shared-1",
            "workspace_digest": "ws",
            "base_workspace_digest": "base-ws",
            "changed_regions": regions,
        },
    )
    store = ReviewStore(tmp_path / "review.sqlite", run)
    store.install_unit_plan(
        [ReviewScope("a", "run", "a", regions=[regions[0]]), ReviewScope("b", "run", "b", regions=[regions[1]])],
        ReviewScope("integration", "run", "integration", dependencies=["a", "b"]),
    )
    source = store.put_evidence(
        b"first line\nsecond line\n",
        revision="head",
        side="head",
        kind="source",
        path="b.py",
        start_line=1,
        end_line=2,
        metadata={"workspace_digest": "ws"},
    )
    for name in ("a", "b"):
        store.add(Task("task-" + name, "run", "review", name, 0))
    a = store.acquire_task("task-a", "a", 100, now=0)
    b = store.acquire_task("task-b", "b", 100, now=0)
    deliver(store, b, "target-turn", source)
    case = Case(
        "case",
        "run",
        "b",
        trigger="concrete trigger",
        mechanism="concrete mechanism",
        claims=[{"id": "claim", "statement": "a behavior", "impact": "an impact"}],
        evidence_refs=[source.id],
    )
    store.commit_unit_decision(
        b.id,
        b.lease_token,
        0,
        "create-case",
        checked_region_ids=[],
        observations=[],
        questions=[],
        cases=[case],
        unresolved=[],
        turn_id="target-turn",
        now=2,
    )
    fact = {
        "statement": "supported statement",
        "citations": [{"ref": source.id, "quote": "first line", "start_line": 1, "end_line": 1}],
    }
    result = {
        "verdict": "accept",
        "reason": "independent decision",
        "gaps": [],
        "facts": {name: fact for name in ("trigger", "actual", "expected", "change", "impact")},
        "case_trigger": case.trigger,
        "claim_id": "claim",
        "claim_statement": "a behavior",
    }
    store.save_verdict(case.id, "claim", 0, "v42-independent-1", "accept", [source.id], result)
    counter = store.put_evidence(
        b"counter one\ncounter two\n",
        revision="head",
        side="head",
        kind="source",
        path="a.py",
        start_line=1,
        end_line=2,
        metadata={"workspace_digest": "ws"},
    )
    deliver(store, a, "source-turn", counter)
    return store, a, b, case, source, counter


def deliver(store, task, turn_id, ref, *, page=None, response=True):
    store.pin_frontier_for_task(task.id, task.lease_token, ref.id, now=1)
    first, last = page or (ref.start_line, ref.end_line)
    lines = store.read_evidence(ref.id).decode().splitlines(keepends=True)
    content = "".join(lines[first - ref.start_line : last - ref.start_line + 1])
    payload = {"evidence_ref": ref.id, "content": content, "start_line": first, "end_line": last}
    store.append_turn_audit(
        task.id,
        task.lease_token,
        turn_id,
        "request",
        [{"type": "tool", "data": {"content": json.dumps(payload)}}],
        now=1,
    )
    if response:
        store.append_turn_audit(
            task.id, task.lease_token, turn_id, "response", [{"type": "ai", "data": {"content": "ok"}}], now=1
        )
        store.append_turn_audit(
            task.id, task.lease_token, turn_id, "exchange", [{"type": "ai", "data": {"content": "ok"}}], now=1
        )


def register(board, **kwargs):
    store, task, _, case, _, counter = board
    return store.register_case_counterevidence(
        task.id,
        task.lease_token,
        case.id,
        [counter.id],
        turn_id="source-turn",
        reason="possible contradiction",
        now=2,
        **kwargs,
    )


def test_register_revokes_only_target_and_recovers_idempotently(board):
    store, task, _, case, source, counter = board
    other = store.add(
        Case(
            "other",
            "run",
            "b",
            trigger="other trigger",
            mechanism="other mechanism",
            claims=[{"id": "other-claim", "statement": "other", "impact": "other"}],
            evidence_refs=[source.id],
        )
    )
    store.save_verdict(other.id, "other-claim", 0, "v42-independent-1", "reject", [source.id], {"x": "frozen"})
    outbox = PublicationOutbox(store)
    old = next(c for c in outbox.freeze() if c["case_id"] == case.id)
    old_result = store.get_verdict(case.id, "claim", "v42-independent-1")["result"]
    updated = register(board, expected_revision=0)
    assert updated.revision == 1 and updated.counter_evidence_refs == [counter.id]
    assert store.get_verdict(case.id, "claim", "v42-independent-1") is None
    assert store.get_verdict(other.id, "other-claim", "v42-independent-1")["verdict"] == "reject"
    assert store.get(ReviewScope, "b").status == "pending"
    assert any(x["case_id"] == case.id for x in store.pending_unit_cases())
    assert any(x["ref_id"] == counter.id and x["scope_id"] == "b" for x in store.pending_unit_sources())
    with pytest.raises(ValueError, match="permission"):
        outbox.prepare(old["id"])
    packet = ContextBuilder(store).verifier(case.id)
    assert [item["ref"]["id"] for item in packet["counter_evidence"]] == [counter.id]
    assert "possible contradiction" not in json.dumps(packet)
    store.save_verdict(
        case.id, "claim", updated.revision, "v42-independent-1", "accept", [source.id, counter.id], old_result
    )
    assert outbox.freeze() == []
    assert register(board, expected_revision=0) == updated
    reopened = ReviewStore(store.path, store.run)
    assert reopened.get(Case, case.id) == updated
    with reopened._connect() as db:
        assert db.execute("SELECT count(*) FROM case_counterevidence_links").fetchone()[0] == 1
    assert store.get(Task, task.id).status == "leased"


def test_integration_case_gets_a_dedicated_followup_obligation(board):
    store, task, _, _, source, counter = board
    case = store.add(
        Case(
            "cross",
            "run",
            "integration",
            trigger="trigger",
            mechanism="mechanism",
            claims=[{"id": "claim", "statement": "claim", "impact": "impact"}],
            evidence_refs=[source.id],
        )
    )
    store.save_verdict(case.id, "claim", 0, "v42-independent-1", "reject", [source.id], {"result": "frozen"})
    updated = store.register_case_counterevidence(
        task.id, task.lease_token, case.id, [counter.id], turn_id="source-turn", reason="possible conflict", now=2
    )
    assert updated.revision == 1
    assert store.get_verdict(case.id, "claim", "v42-independent-1") is None
    pending = next(x for x in store.pending_unit_cases() if x["case_id"] == case.id)
    followup = next(x for x in store.list_integration_obligations() if x["id"] == pending["obligation_id"])
    assert followup["kind"] == "case_followup" and not followup["valid"]
    assert followup["origin_evidence_refs"] == [counter.id]


def test_all_pages_in_the_same_audited_request_can_establish_delivery(board):
    store, task, _, case, _, counter = board
    messages = [
        {
            "type": "tool",
            "data": {
                "content": json.dumps(
                    {
                        "evidence_ref": counter.id,
                        "content": text,
                        "start_line": line,
                        "end_line": line,
                    }
                )
            },
        }
        for line, text in ((1, "counter one\n"), (2, "counter two\n"))
    ]
    store.append_turn_audit(task.id, task.lease_token, "pages", "request", messages, now=1)
    store.append_turn_audit(
        task.id, task.lease_token, "pages", "response", [{"type": "ai", "data": {"content": "ok"}}], now=1
    )
    updated = store.register_case_counterevidence(
        task.id, task.lease_token, case.id, [counter.id], turn_id="pages", reason="possible conflict", now=2
    )
    assert updated.counter_evidence_refs == [counter.id]


def test_same_original_source_alias_is_idempotent_but_support_to_counter_is_new(board):
    store, task, _, case, source, counter = board
    deliver(store, task, "positive-as-counter", source)
    first = store.register_case_counterevidence(
        task.id,
        task.lease_token,
        case.id,
        [source.id],
        turn_id="positive-as-counter",
        reason="possible counterexample",
        now=2,
    )
    assert first.revision == 1 and source.id in first.counter_evidence_refs
    updated = register(board, expected_revision=1)
    alias = store.put_evidence(
        store.read_evidence(counter.id),
        revision="head",
        side="head",
        kind="source",
        path="a.py",
        start_line=1,
        end_line=2,
        metadata={"workspace_digest": "ws", "tool_args": {"page": "other"}},
    )
    deliver(store, task, "alias", alias)
    same = store.register_case_counterevidence(
        task.id,
        task.lease_token,
        case.id,
        [alias.id],
        turn_id="alias",
        reason="same source",
        expected_revision=0,
        now=2,
    )
    assert same == updated


@pytest.mark.parametrize(
    "fault",
    [
        "missing_page",
        "no_response",
        "wrong_lease",
        "stale_case",
        "navigation",
        "wrong_workspace",
        "cross_run",
        "different_revision",
    ],
)
def test_untrusted_or_unreceived_sources_cannot_change_a_case(board, fault):
    store, task, _, case, _, counter = board
    ref, turn_id, token, expected = counter, "source-turn", task.lease_token, None
    if fault == "missing_page":
        turn_id = "partial"
        deliver(store, task, turn_id, ref, page=(1, 1))
    elif fault == "no_response":
        turn_id = "failed"
        deliver(store, task, turn_id, ref, response=False)
    elif fault == "wrong_lease":
        token = "wrong"
    elif fault == "stale_case":
        expected = 99
    elif fault == "navigation":
        ref = store.put_evidence(
            b"not proof\n",
            revision="head",
            side="head",
            kind="search",
            path="a.py",
            start_line=1,
            end_line=1,
            metadata={"workspace_digest": "ws", "navigation_only": True},
        )
        turn_id = "navigation"
        deliver(store, task, turn_id, ref)
    elif fault == "wrong_workspace":
        ref = store.put_evidence(
            b"wrong workspace\n",
            revision="head",
            side="head",
            kind="source",
            path="a.py",
            start_line=1,
            end_line=1,
            metadata={"workspace_digest": "other"},
        )
    elif fault in {"cross_run", "different_revision"}:
        # Corruption or a forged envelope must not be accepted even if its ID exists.
        with store._connect() as db:
            changed = replace(
                counter, **({"run_id": "elsewhere"} if fault == "cross_run" else {"revision": "other-head"})
            )
            db.execute(
                "UPDATE objects SET payload=? WHERE kind='EvidenceRef' AND id=?",
                (json.dumps(changed.__dict__), counter.id),
            )
    with pytest.raises((BindingError, LeaseError, RevisionConflict)):
        store.register_case_counterevidence(
            task.id, token, case.id, [ref.id], turn_id=turn_id, reason="lead", expected_revision=expected, now=2
        )
    assert store.get(Case, case.id).revision == 0
    assert not store.pending_unit_cases()


def test_non_opt_in_is_rejected(board):
    store, task, _, case, _, counter = board
    store.run = replace(store.run, metadata={**store.run.metadata, "shared_investigation": ""})
    with pytest.raises(BindingError, match="opt-in"):
        store.register_case_counterevidence(
            task.id, task.lease_token, case.id, [counter.id], turn_id="source-turn", reason="lead", now=2
        )


def test_prepare_cannot_race_past_counterevidence_transaction(board, monkeypatch):
    store, _, _, case, _, _ = board
    outbox = PublicationOutbox(store)
    old = next(c for c in outbox.freeze() if c["case_id"] == case.id)
    entered, release = threading.Event(), threading.Event()
    original = store._require_delivered

    def held(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "_require_delivered", held)
    outcomes = {}

    def register_worker():
        try:
            outcomes["registered"] = register(board)
        except Exception as exc:
            outcomes["register_error"] = exc

    def prepare_worker():
        try:
            outcomes["prepared"] = outbox.prepare(old["id"])
        except Exception as exc:
            outcomes["prepare_error"] = exc

    reporter = threading.Thread(target=register_worker)
    publisher = threading.Thread(target=prepare_worker)
    reporter.start()
    assert entered.wait(5)
    publisher.start()
    release.set()
    reporter.join(10)
    publisher.join(10)
    assert not reporter.is_alive() and not publisher.is_alive()
    assert "registered" in outcomes and isinstance(outcomes.get("prepare_error"), ValueError), outcomes


def test_concurrent_duplicate_leads_increment_case_only_once(board):
    store, _, _, case, _, _ = board
    barrier = threading.Barrier(3)
    outcomes = []

    def worker():
        barrier.wait()
        try:
            outcomes.append(register(board, expected_revision=0))
        except Exception as exc:
            outcomes.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(10)
    assert all(not thread.is_alive() for thread in threads)
    assert len(outcomes) == 2 and all(isinstance(item, Case) and item.revision == 1 for item in outcomes)
    assert store.get(Case, case.id).revision == 1
    with store._connect() as db:
        assert db.execute("SELECT count(*) FROM case_counterevidence_links").fetchone()[0] == 1
