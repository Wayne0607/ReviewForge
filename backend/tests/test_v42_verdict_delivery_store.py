"""The opt-in verifier freezes claim-scoped originals and outcomes in one transaction."""

import json
from dataclasses import asdict

import pytest

from reviewforge.review_v42.contracts import (
    BindingError,
    Case,
    LeaseError,
    Question,
    ReviewScope,
    RevisionConflict,
    Run,
    Task,
)
from reviewforge.review_v42.store import ReviewStore
from reviewforge.review_v42.verifier import POLICY, verification_gap_id


def _board(tmp_path, *, enabled=True, counter=False, reviewed=False):
    region = {"id": "a", "path": "a.py", "side": "head", "start_line": 1, "end_line": 2, "changed_lines": [1]}
    metadata = {
        "workspace_digest": "snapshot",
        "base_workspace_digest": "base-snapshot",
        "changed_regions": [region],
        "shared_investigation": "shared-1",
        "candidate_draft_policy": "candidate-draft-1",
        "source_line_policy": "git-lf-1",
        "frontier_projection_policy": "source-line-union-1",
        "decision_cycle_policy": "decision-cycle-1",
    }
    if enabled:
        metadata["verification_delivery_policy"] = "claim-delivery-1"
    store = ReviewStore(
        tmp_path / "board.db", Run("r", "repo", "base", "head", policy="v42-units-6", metadata=metadata)
    )
    store.install_unit_plan(
        [ReviewScope("a", "r", "a", regions=[region])], ReviewScope("i", "r", "i", dependencies=["a"])
    )
    if reviewed:
        # Seed an already-reviewed scope; the delivery must leave the whole responsibility record unchanged.
        scope = asdict(store.get(ReviewScope, "a"))
        scope["status"] = "reviewed"
        with store._connect() as db:
            db.execute("UPDATE objects SET payload=? WHERE kind='ReviewScope' AND id='a'", (json.dumps(scope),))
    initial = _source(store)
    base = _source(store, side="base")
    case = store.add(
        Case(
            "case",
            "r",
            scope_id="a",
            trigger="x",
            mechanism="bad",
            claims=[{"id": k, "statement": f"claim {k}", "impact": "loss"} for k in ("first", "second")],
            evidence_refs=[initial.id],
            counter_evidence_refs=[base.id] if counter else [],
        )
    )
    store.add(Task("verify", "r", "verify", "case", 0))
    lease = store.acquire_task("verify", "worker", 10, now=1)
    return store, case, initial, base, lease


def _source(store, *, side="head", path="a.py", kind="source", raw=b"bad\nresult\n"):
    return store.put_evidence(
        raw,
        revision=getattr(store.run, side),
        side=side,
        kind=kind,
        path=path,
        start_line=1,
        end_line=2,
        metadata={
            "workspace_digest": "snapshot",
            "tool_name": "read_source",
            "tool_args": {"path": path, "side": side},
        },
    )


def _delivery(claim_id, ref, observed, *, reason="premise disproved", verdict="reject", gaps=None):
    return {
        "claim_id": claim_id,
        "observed_refs": [r.id for r in observed],
        "result": {
            "verdict": verdict,
            "reason": reason,
            "case_trigger": "x",
            "claim_id": claim_id,
            "claim_statement": f"claim {claim_id}",
            "gaps": gaps or [],
            "facts": {
                "counterexample": {
                    "statement": "trigger absent",
                    "citations": [
                        {"ref": ref.id, "quote": "bad", "start_line": 1, "end_line": 1},
                    ],
                }
            }
            if ref is not None
            else {},
        },
    }


def _save(store, case, lease, deliveries, *, now=2):
    return store.save_verification_delivery(
        case.id, case.revision, POLICY, deliveries, task_id=lease.id, lease_token=lease.lease_token, now=now
    )


def _events(store, action):
    with store._connect() as db:
        return [json.loads(row[0]) for row in db.execute("SELECT payload FROM events WHERE action=?", (action,))]


def test_atomic_claim_delivery_preserves_base_head_and_case_scope(tmp_path):
    store, case, head, base, lease = _board(tmp_path, reviewed=True)
    scope = store.get(ReviewScope, "a")
    task = store.get(Task, lease.id)
    navigation = _source(store, kind="diff")
    other = _source(store, path="other.py")
    # HEAD and base are distinct provenance even when the raw body is identical.
    assert head.content_hash == base.content_hash and head.id != base.id
    incoming = [_delivery("second", other, [other]), _delivery("first", base, [base, head, navigation])]
    first, second = _save(store, case, lease, incoming)
    assert [d["claim_id"] for d in (first, second)] == ["first", "second"]
    assert set(first["evidence_refs"]) == {head.id, base.id}
    assert set(first["delivery"]["observed_evidence_refs"]) == {head.id, base.id}
    assert set(second["evidence_refs"]) == {head.id, other.id}
    assert second["delivery"]["observed_evidence_refs"] == [other.id]
    assert first["delivery"]["case_evidence_refs"] == second["delivery"]["case_evidence_refs"] == [head.id]
    assert store.get(Case, case.id) == case
    assert store.get(ReviewScope, scope.id) == scope
    assert store.get(Task, lease.id) == task
    assert store.pending_unit_cases() == []
    assert _events(store, "verification_navigation") == [
        {"claim_id": "first", "evidence_refs": [navigation.id], "input_revision": 0, "task_id": lease.id},
    ]
    assert _save(store, case, lease, incoming) == [first, second]
    assert _events(store, "verdict") == [first, second]
    store.release_task(lease.id, lease.lease_token, now=3)
    replacement = store.acquire_task(lease.id, "restart", 10, now=3)
    assert _save(store, case, replacement, [], now=4) == [first, second]
    with pytest.raises(RevisionConflict, match="already frozen"):
        _save(store, case, replacement, [_delivery("first", base, [base, head], reason="new decision")], now=4)


@pytest.mark.parametrize("failure", ["sibling", "unseen_case", "navigation", "invalid_quote", "bad_audit", "omitted"])
def test_failed_second_claim_rolls_back_first_verdict_and_navigation(tmp_path, failure):
    store, case, head, base, lease = _board(tmp_path)
    navigation = _source(store, kind="diff", raw=b"bad\nnavigation\n")
    first = _delivery("first", base, [base, navigation])
    observed = _source(store, path="other.py")
    bad_audit = _source(store, kind="search", raw=b"bad\nsearch\n")
    second = {
        "sibling": _delivery("second", base, [observed]),
        "unseen_case": _delivery("second", head, [observed]),
        "navigation": _delivery("second", navigation, [navigation]),
        "invalid_quote": _delivery("second", observed, [observed]),
        "bad_audit": _delivery("second", observed, [observed, bad_audit]),
        "omitted": None,
    }[failure]
    if failure == "invalid_quote":
        second["result"]["facts"]["counterexample"]["citations"][0]["quote"] = "not in original source"
    if failure == "bad_audit":
        (store.blob_dir / bad_audit.content_hash).write_bytes(b"tampered")
    with pytest.raises(ValueError):
        _save(store, case, lease, [first] if second is None else [first, second])
    assert store.get_verdict(case.id, "first", POLICY) is None
    assert store.get_verdict(case.id, "second", POLICY) is None
    assert _events(store, "verdict") == _events(store, "verification_navigation") == []


def test_mixed_batch_requires_exact_durable_question_and_keeps_real_invalidation(tmp_path):
    store, case, head, base, lease = _board(tmp_path, reviewed=True)
    gap = "What is the missing policy?"
    qid = verification_gap_id(case.id, "second", gap)
    incoming = [
        _delivery("first", base, [base]),
        _delivery("second", None, [head], verdict="needs_evidence", gaps=[gap]),
    ]
    with pytest.raises(BindingError, match="durable Case gap"):
        _save(store, case, lease, incoming)
    assert store.get_verdict(case.id, "first", POLICY) is None
    question = Question(qid, "r", gap, scope_id="a", case_id=case.id, owner="verifier")
    with pytest.raises(BindingError, match="cannot change Case evidence"):
        store.record_verification_observations(
            lease.id, lease.lease_token, case.revision, [question], [head.id, base.id], now=2
        )
    assert store.get(Case, case.id) == case and store.list(Question) == []
    revised = store.record_verification_observations(
        lease.id,
        lease.lease_token,
        case.revision,
        [question],
        case.evidence_refs,
        now=2,
    )
    assert revised.revision == 1 and revised.evidence_refs == case.evidence_refs
    assert store.get(ReviewScope, "a").status == "pending"
    with pytest.raises(RevisionConflict):
        _save(store, case, lease, incoming)
    with pytest.raises(LeaseError):
        _save(store, revised, lease, incoming)
    store.add(Task("verify-new", "r", "verify", case.id, revised.revision))
    replacement = store.acquire_task("verify-new", "worker", 10, now=3)
    assert [d["verdict"] for d in _save(store, revised, replacement, incoming, now=4)] == ["reject", "needs_evidence"]
    assert store.get(Case, case.id) == revised
    question = store.get(Question, qid)
    store.update(Question, qid, question.revision, status="resolved", answer="Known")
    assert store.get_verdict(case.id, "first", POLICY) is None
    assert store.get_verdict(case.id, "second", POLICY) is None


def test_case_counterevidence_is_required_even_when_not_cited(tmp_path):
    store, case, head, base, lease = _board(tmp_path, counter=True)
    with pytest.raises(BindingError, match="counter-evidence"):
        _save(store, case, lease, [_delivery(claim["id"], head, [head]) for claim in case.claims])
    assert _events(store, "verdict") == []
    frozen = _save(store, case, lease, [_delivery(claim["id"], head, [head, base]) for claim in case.claims])
    assert set(frozen[0]["delivery"]["case_evidence_refs"]) == {head.id, base.id}


@pytest.mark.parametrize("changed", ["original_blob", "new_blob", "case_input"])
def test_reads_recheck_full_case_input_and_additional_original(tmp_path, changed):
    store, case, head, _, lease = _board(tmp_path)
    extra = _source(store, side="base", raw=b"bad\nother\n")
    first, second = _save(store, case, lease, [_delivery("first", extra, [extra]), _delivery("second", head, [head])])
    assert store.get_verdict(case.id, "first", POLICY) == first
    if changed == "case_input":
        payload = asdict(case)
        payload["claims"][0]["impact"] = "altered at same revision"
        with store._connect() as db:
            db.execute("UPDATE objects SET payload=? WHERE kind='Case' AND id=?", (json.dumps(payload), case.id))
    else:
        (store.blob_dir / (head if changed == "original_blob" else extra).content_hash).write_bytes(b"tampered")
    with pytest.raises(BindingError):
        store.get_verdict(case.id, "first", POLICY)
    if changed == "new_blob":
        assert store.get_verdict(case.id, "second", POLICY) == second


def test_lease_expiring_inside_transaction_rolls_back_everything(tmp_path, monkeypatch):
    store, case, head, base, lease = _board(tmp_path)
    incoming = [_delivery("first", base, [base]), _delivery("second", head, [head])]
    with pytest.raises(LeaseError, match="current verification lease"):
        _save(store, case, lease, incoming, now=lease.lease_expires)
    moments = iter((2, lease.lease_expires))
    monkeypatch.setattr(
        "reviewforge.review_v42.store.time", type("Clock", (), {"time": staticmethod(lambda: next(moments))})
    )
    with pytest.raises(LeaseError, match="during delivery"):
        _save(store, case, lease, incoming, now=None)
    assert _events(store, "verdict") == []
    assert store.get_verdict(case.id, "first", POLICY) is None


def test_old_and_new_verdict_contracts_do_not_cross(tmp_path):
    store, case, head, _, lease = _board(tmp_path / "new")
    with pytest.raises(BindingError, match="claim delivery transaction"):
        store.save_verdict(
            case.id,
            "first",
            case.revision,
            POLICY,
            "reject",
            [head.id],
            {},
            task_id=lease.id,
            lease_token=lease.lease_token,
            now=2,
        )
    old, old_case, old_head, _, old_lease = _board(tmp_path / "old", enabled=False)
    with pytest.raises(BindingError, match="verification delivery profile"):
        _save(old, old_case, old_lease, [])
    previous = old.save_verdict(
        old_case.id,
        "first",
        old_case.revision,
        POLICY,
        "reject",
        [old_head.id],
        {},
        task_id=old_lease.id,
        lease_token=old_lease.lease_token,
        now=2,
    )
    assert old.get_verdict(old_case.id, "first", POLICY) == previous
    with store._connect() as db:
        db.execute(
            "INSERT INTO verdicts VALUES (?,?,?,?,?,?)", ("legacy", case.id, "first", 0, POLICY, json.dumps(previous))
        )
    with pytest.raises(BindingError, match="does not bind a claim delivery"):
        store.get_verdict(case.id, "first", POLICY)
