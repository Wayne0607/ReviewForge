from concurrent.futures import ThreadPoolExecutor

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


@pytest.fixture
def store(tmp_path):
    return ReviewStore(tmp_path / "run.sqlite", Run("r", "repo", "base-sha", "head-sha"))


def test_updates_do_not_erase_other_questions_and_conflict(store):
    store.add(Question("q1", "r", "first"))
    store.add(Question("q2", "r", "second"))
    store.update(Question, "q1", 0, answer="yes", status="answered")
    store.update(Question, "q2", 0, answer="no", status="answered")
    assert store.get(Question, "q1").answer == "yes"
    with pytest.raises(RevisionConflict):
        store.update(Question, "q1", 0, answer="stale")
    assert store.get(Question, "q1").answer == "yes"


def test_content_and_binding(store, tmp_path):
    payload = b"line one\n" + b"x" * 300_000
    ref = store.put_evidence(payload, revision="head-sha", side="head", kind="source", path="a.py")
    assert store.read_evidence(ref.id) == payload
    assert ref == store.put_evidence(payload, revision="head-sha", side="head", kind="source", path="a.py")
    with pytest.raises(BindingError):
        store.put_evidence(payload, revision="base-sha", side="head", kind="source")
    with pytest.raises(BindingError):
        ReviewStore(store.path, Run("r", "repo", "base-sha", "other-head"))
    other = ReviewStore(tmp_path / "other.sqlite", Run("r2", "repo", "base-sha", "other-head"))
    ref2 = other.put_evidence(payload, revision="other-head", side="head", kind="source", path="a.py")
    assert ref.id != ref2.id
    assert len(list(other.blob_dir.iterdir())) == 1
    with pytest.raises(BindingError):
        other.add(Case("x", "r2", evidence_refs=[ref.id]))
    (store.blob_dir / ref.content_hash).write_bytes(b"changed")
    with pytest.raises(BindingError):
        store.read_evidence(ref.id)


def test_lease_recovery_and_renewal(store):
    store.add(ReviewScope("s", "r", "scope"))
    store.add(Task("t", "r", "review", "s", 0))
    first = store.acquire_task("t", "a", 10, now=0)
    with pytest.raises(LeaseError):
        store.acquire_task("t", "b", 10, now=5)
    store.renew_task("t", first.lease_token, 10, now=5)
    with pytest.raises(LeaseError):
        store.acquire_task("t", "b", 10, now=11)
    restored = ReviewStore(store.path, store.run)
    second = restored.acquire_task("t", "b", 10, now=16)
    with pytest.raises(LeaseError):
        restored.complete_task("t", first.lease_token, {"ok": True}, now=17)
    restored.release_task("t", second.lease_token, now=17)
    third = restored.acquire_task("t", "c", 10, now=17)
    done = restored.complete_task("t", third.lease_token, {"ok": True}, now=18)
    assert restored.complete_task("t", third.lease_token, {"ok": True}, now=19) == done
    restored.add(Task("duplicate", "r", "review", "s", 0))
    with pytest.raises(LeaseError):
        restored.acquire_task("duplicate", "c", 10, now=19)


def test_concurrent_cas(store):
    store.add(Question("q", "r", "first"))

    def update(value):
        try:
            return store.update(Question, "q", 0, answer=value).answer
        except RevisionConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(update, ["a", "b"])).count("conflict") == 1


def test_concurrent_lease_and_stale_result(store):
    store.add(ReviewScope("s", "r", "scope"))
    store.add(Task("t", "r", "review", "s", 0))

    def acquire(owner):
        try:
            return store.acquire_task("t", owner, 10, now=0)
        except LeaseError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        tasks = list(pool.map(acquire, ["a", "b"]))
    assert sum(x is not None for x in tasks) == 1
    store.update(ReviewScope, "s", 0, status="reviewed")
    with pytest.raises(RevisionConflict):
        store.complete_task("t", next(x.lease_token for x in tasks if x), {}, now=1)


def test_frozen_per_claim_and_dependency_invalidation(store):
    ref = store.put_evidence(b"source", revision="head-sha", side="head", kind="source")
    store.add(Question("q", "r", "what?", case_id="c"))
    store.add(
        Case(
            "c",
            "r",
            trigger="x",
            claims=[{"id": "a", "statement": "one"}, {"id": "b", "statement": "two"}],
            evidence_refs=[ref.id],
            question_ids=["q"],
        )
    )
    decision = store.save_verdict("c", "a", 0, "p", "accept", [ref.id], {"reason": "source"})
    store.save_verdict("c", "b", 0, "p", "reject", [ref.id], {"reason": "counterexample"})
    assert store.get_verdict("c", "a", "p") == decision
    with pytest.raises(RevisionConflict):
        store.save_verdict("c", "a", 0, "p", "reject", [ref.id], {})
    store.update(Question, "q", 0, status="retracted", answer_status="retracted", change_reason="source disproved")
    assert store.get(Case, "c").revision == 1
    assert store.get_verdict("c", "a", "p") is None
    with pytest.raises(RevisionConflict):
        store.save_verdict("c", "a", 0, "p", "accept", [ref.id], {})


def test_verdict_requires_current_lease_even_after_takeover(store):
    ref = store.put_evidence(b"source", revision="head-sha", side="head", kind="source")
    store.add(Case("c", "r", claims=[{"id": "a", "statement": "s"}], evidence_refs=[ref.id]))
    store.add(Task("verify", "r", "verify", "c", 0))
    first = store.acquire_task("verify", "a", 10, now=0)
    with pytest.raises(LeaseError):
        store.save_verdict("c", "a", 0, "p", "accept", [ref.id], {}, now=1)
    second = store.acquire_task("verify", "b", 10, now=11)
    with pytest.raises(LeaseError):
        store.save_verdict(
            "c", "a", 0, "p", "accept", [ref.id], {}, task_id="verify", lease_token=first.lease_token, now=12
        )
    verdict = store.save_verdict(
        "c", "a", 0, "p", "accept", [ref.id], {}, task_id="verify", lease_token=second.lease_token, now=12
    )
    assert verdict["case_revision"] == 0
    (store.blob_dir / ref.content_hash).write_bytes(b"tampered")
    with pytest.raises(BindingError):
        store.get_verdict("c", "a", "p")


def test_new_question_invalidates_existing_case_and_retraction_needs_reason(store):
    store.add(Case("c", "r", claims=[{"id": "a", "statement": "s"}]))
    store.add(Question("q", "r", "question", case_id="c"))
    assert store.get(Case, "c").revision == 1
    with pytest.raises(ValueError):
        store.update(Question, "q", 0, status="retracted")
    assert store.get(Question, "q").revision == 0


def test_contract_rejects_hidden_or_ambiguous_claims_and_conflicts_are_audited(store):
    with pytest.raises(ValueError):
        store.add(Case("bad", "r", claims=[{"id": "a", "evidence_refs": ["ev_forged"]}]))
    with pytest.raises(ValueError):
        store.add(Case("bad", "r", claims=[{"id": "a"}, {"id": "a"}]))
    with pytest.raises(BindingError):
        store.add(Question("bad", "r", "q", interpretations=[{"id": "explanation", "evidence_refs": ["ev_forged"]}]))
    store.add(Question("q", "r", "q"))
    store.update(Question, "q", 0, answer="one")
    with pytest.raises(RevisionConflict):
        store.update(Question, "q", 0, answer="other")
    conflict = store.history("Question", "q")[-1]
    assert conflict["action"] == "conflict"
    assert conflict["payload"]["attempted_changes"] == {"answer": "other"}


def test_run_binding_includes_model_policy_and_metadata_and_lease_times_are_finite(store):
    for replacement in (
        Run("r", "repo", "base-sha", "head-sha", model="other"),
        Run("r", "repo", "base-sha", "head-sha", policy="other"),
        Run("r", "repo", "base-sha", "head-sha", metadata={"option": 1}),
    ):
        with pytest.raises(BindingError):
            ReviewStore(store.path, replacement)
    store.add(ReviewScope("s", "r", "scope"))
    store.add(Task("t", "r", "review", "s", 0))
    for invalid in (float("nan"), float("inf"), -1):
        with pytest.raises(ValueError):
            store.acquire_task("t", "worker", invalid, now=0)
    with pytest.raises(ValueError):
        store.acquire_task("t", "worker", 1, now=float("nan"))


def test_review_checkpoint_and_lease_takeover_are_atomic(store):
    store.add(ReviewScope("s", "r", "scope"))
    store.add(Task("review", "r", "review", "s", 0))
    first = store.acquire_task("review", "a", 10, now=0)
    changed = store.update_scope_for_task("review", first.lease_token, 0, now=1, status="running")
    assert changed.revision == 1
    assert store.get(Task, "review").input_revision == 1
    messages = [{"role": "assistant", "content": "reading"}]
    store.save_task_session("review", first.lease_token, messages, now=2)
    restored = ReviewStore(store.path, store.run)
    assert restored.load_task_session("review") == messages
    second = restored.acquire_task("review", "b", 10, now=11)
    with pytest.raises(LeaseError):
        restored.update_scope_for_task("review", first.lease_token, 1, now=12, status="reviewed")
    restored.update_scope_for_task("review", second.lease_token, 1, now=12, status="reviewed")
    assert restored.load_task_session("review") == []
    restored.save_task_session("review", second.lease_token, messages, now=13)
    assert restored.load_task_session("review") == messages
    assert restored.complete_task("review", second.lease_token, {"ok": True}, now=14).status == "completed"


def test_case_trigger_fixed_and_status_alone_cannot_reopen_verdict(store):
    store.add(Case("c", "r", trigger="x", claims=[{"id": "a"}]))
    with pytest.raises(ValueError):
        store.update(Case, "c", 0, trigger="different")
    store.save_verdict("c", "a", 0, "p", "unresolved", [], {})
    with pytest.raises(ValueError):
        store.update(Case, "c", 0, status="candidate-again")


def test_superseded_task_can_settle_without_publishing_or_reacquiring(store):
    store.add(Case("c", "r", claims=[{"id": "a"}]))
    store.add(Task("t", "r", "verify", "c", 0))
    task = store.acquire_task("t", "worker", 20, now=0)
    with pytest.raises(RevisionConflict):
        store.settle_task_stale("t", task.lease_token, {}, now=1)
    store.update(Case, "c", 0, mechanism="new")
    settled = store.settle_task_stale("t", task.lease_token, {"reason": "new evidence"}, now=2)
    assert settled.status == "superseded" and settled.result["current_revision"] == 1
    assert store.get_verdict("c", "a", "p") is None
    with pytest.raises(LeaseError):
        store.acquire_task("t", "worker", 20, now=3)


def test_semantic_writes_are_guarded_by_scope_lease_in_the_same_transaction(store):
    store.add(ReviewScope("s", "r", "scope"))
    store.add(ReviewScope("other", "r", "other"))
    store.add(Task("review", "r", "review", "s", 0))
    first = store.acquire_task("review", "a", 10, now=0)
    store.add_for_task("review", first.lease_token, Question("q", "r", "question", scope_id="s"), now=1)
    with pytest.raises(LeaseError):
        store.add_for_task("review", first.lease_token, Case("bad", "r", scope_id="other"), now=2)
    second = store.acquire_task("review", "b", 10, now=11)
    with pytest.raises(LeaseError):
        store.update_for_task("review", first.lease_token, Question, "q", 0, now=12, answer="stale")
    updated = store.update_for_task("review", second.lease_token, Question, "q", 0, now=12, answer="current")
    assert updated.answer == "current"
    store.update(ReviewScope, "s", 0, status="changed elsewhere")
    with pytest.raises(RevisionConflict):
        store.add_for_task("review", second.lease_token, Case("stale", "r", scope_id="s"), now=13)


def test_verifier_observations_require_current_lease_and_update_atomically(store):
    store.add(Case("c", "r", scope_id="s", claims=[{"id": "a"}]))
    store.add(Task("v", "r", "verify", "c", 0))
    first = store.acquire_task("v", "a", 10, now=0)
    second = store.acquire_task("v", "b", 10, now=11)
    question = Question("q", "r", "gap", scope_id="s", case_id="c")
    with pytest.raises(LeaseError):
        store.record_verification_observations("v", first.lease_token, 0, [question], [], now=12)
    with pytest.raises(KeyError):
        store.get(Question, "q")
    revised = store.record_verification_observations("v", second.lease_token, 0, [question], [], now=12)
    assert revised.revision == 1 and revised.question_ids == ["q"]
    assert store.get(Question, "q") == question


def test_delegated_question_task_is_lease_bound_and_resumable(store):
    store.add(ReviewScope("s", "r", "scope"))
    store.add(Question("q", "r", "question", scope_id="s"))
    store.add(Task("review", "r", "review", "s", 0))
    review = store.acquire_task("review", "owner", 100, now=0)
    question_task = Task("question", "r", "question", "q", 0)
    store.add_for_task("review", review.lease_token, question_task, now=1)
    lease = store.acquire_task("question", "investigator", 10, now=1)
    changed = store.update_for_task(
        "question", lease.lease_token, Question, "q", 0, now=2, answer="answer", status="answered"
    )
    assert changed.revision == 1
    assert store.get(Task, "question").input_revision == 1
    store.save_task_session("question", lease.lease_token, [{"role": "assistant", "content": "done"}], now=2)
    assert store.load_task_session("question")[0]["content"] == "done"
    assert store.complete_task("question", lease.lease_token, {"ok": True}, now=3).status == "completed"
    with pytest.raises(RevisionConflict):
        store.add_for_task("review", review.lease_token, Task("other", "r", "question", "q", 1), now=3)


def test_scope_partition_requires_exact_untouched_coverage(store):
    a, b = {"id": "a", "path": "a.py"}, {"id": "b", "path": "b.py"}
    store.add(ReviewScope("parent", "r", "parent", regions=[a, b]))
    first = ReviewScope("one", "r", "one", regions=[a])
    second = ReviewScope("two", "r", "two", regions=[b])
    with pytest.raises(ValueError):
        store.partition_scope("parent", 0, [first, ReviewScope("bad", "r", "bad", regions=[a])])
    assert store.get(ReviewScope, "parent").status == "pending"
    store.partition_scope("parent", 0, [first, second])
    assert store.get(ReviewScope, "parent").status == "superseded"
    assert store.get(ReviewScope, "one").regions == [a]
    assert store.get(ReviewScope, "two").regions == [b]


def test_external_evidence_has_distinct_source_revision_and_capture_binding(store):
    metadata = {
        "source_url": "https://github.com/org/repo/issues/1",
        "captured_at": "2026-11-02T00:00:00Z",
        "historical_head_time": False,
    }
    ref = store.put_evidence(
        b"issue body", side="external", revision="2026-11-01T00:00:00Z", kind="issue", metadata=metadata
    )
    assert ref.side == "external" and ref.revision != store.run.head
    for bad in (
        {},
        {**metadata, "captured_at": "today"},
        {**metadata, "historical_head_time": True},
        {**metadata, "source_url": "file:///private"},
    ):
        with pytest.raises(BindingError):
            store.put_evidence(b"issue body", side="external", revision="v1", kind="issue", metadata=bad)
