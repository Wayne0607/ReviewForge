import json

import pytest

from reviewforge.review_v42.contracts import BindingError, ReviewScope, Run, Task
from reviewforge.review_v42.decision_state import DecisionState
from reviewforge.review_v42.store import ReviewStore


@pytest.fixture
def board(tmp_path):
    region = {"id": "a", "path": "a.py", "side": "head", "start_line": 1, "end_line": 2, "changed_lines": [1]}
    run = Run(
        "r",
        "org/repo",
        "base",
        "head",
        policy="v42-units-6",
        metadata={
            "workspace_digest": "snapshot",
            "base_workspace_digest": "base-snapshot",
            "changed_regions": [region],
            "shared_investigation": "shared-1",
            "candidate_draft_policy": "candidate-draft-1",
            "source_line_policy": "git-lf-1",
            "frontier_projection_policy": "source-line-union-1",
            "decision_cycle_policy": "decision-cycle-1",
        },
    )
    store = ReviewStore(tmp_path / "state.db", run)
    store.install_unit_plan(
        [ReviewScope("a", "r", "a", regions=[region])], ReviewScope("i", "r", "i", dependencies=["a"])
    )
    store.add(Task("review", "r", "review", "a", 0))
    token = store.acquire_task("review", "worker", 100, now=0).lease_token
    return store, token


def audit(store, token, turn, calls, messages=()):
    store.append_turn_audit("review", token, turn, "request", list(messages), now=2)
    store.append_turn_audit(
        "review", token, turn, "response", [{"type": "ai", "data": {"content": "", "tool_calls": calls}}], now=2
    )


def call(args, id="c"):
    return {"name": "review_step", "id": id, "args": args, "type": "tool_call"}


def initial():
    return {
        "work_item_id": "",
        "expected_revision": 0,
        "question": "What happens?",
        "judgment": "",
        "evidence_refs": [],
        "action": "request_evidence",
        "payload": {
            "gap": "behavior",
            "relevance": "impact",
            "alternatives": "compare",
            "operations": [{"name": "read_source", "args": {"path": "a.py"}}],
        },
    }


def test_outbox_and_nested_delivery_are_bound_to_successful_next_turn(board):
    store, token = board
    state = DecisionState(store)
    args = initial()
    audit(store, token, "one", [call(args)])
    step = state.stage("review", token, "one", "c", args, now=2)
    assert state.stage("review", token, "one", "c", args, now=2) == step
    op = step["operations"][0]
    ref = store.put_evidence(
        b"one\ntwo\n",
        revision="head",
        side="head",
        kind="source",
        path="a.py",
        start_line=1,
        end_line=2,
        metadata={"workspace_digest": "snapshot", "tool_name": "read_source", "tool_args": {"path": "a.py"}},
    )
    store.pin_frontier_for_task("review", token, ref.id, now=2)
    receipt = {"evidence_ref": ref.id, "content": "one\ntwo\n", "start_line": 1, "end_line": 2}
    state.claim_operation("review", token, step["id"], op["id"], now=2)
    state.record_result("review", token, step["id"], op["id"], receipt, now=2)
    with store._connect() as db, pytest.raises(BindingError):
        store._require_delivered(db, "review", token, "one", {ref.id})
    wrapper = {"step_id": step["id"], "results": [{"operation_id": op["id"], "result": receipt}]}
    msg = {"type": "tool", "data": {"content": json.dumps(wrapper)}}
    audit(store, token, "two", [], [msg])
    with store._connect() as db:
        assert ref.id in store._require_delivered(db, "review", token, "two", {ref.id})
    state.mark_consumed("review", token, step["id"], "two", now=2)
    assert state.pending("review") == []
    reopened = DecisionState(ReviewStore(store.path, store.run))
    assert reopened.current("review")["question"] == "What happens?"


def test_rejects_forged_sibling_and_unknown_outcome_cannot_be_replayed(board):
    store, token = board
    state = DecisionState(store)
    args = initial()
    audit(store, token, "one", [call(args)])
    step = state.stage("review", token, "one", "c", args, now=2)
    op = step["operations"][0]
    state.claim_operation("review", token, step["id"], op["id"], now=2)
    state.mark_unknown("review", token, step["id"], op["id"], "write outcome unknown", now=2)
    with pytest.raises(ValueError):
        state.record_result("review", token, step["id"], op["id"], {}, now=2)
    assert state.pending("review")[0]["operations"][0]["status"] == "unknown"


def test_candidate_decision_and_verification_readiness_are_atomic(board):
    store, token = board
    state = DecisionState(store)
    ref = store.put_evidence(
        b"one\ntwo\n",
        revision="head",
        side="head",
        kind="source",
        path="a.py",
        start_line=1,
        end_line=2,
        metadata={"workspace_digest": "snapshot", "tool_name": "read_source", "tool_args": {"path": "a.py"}},
    )
    store.pin_frontier_for_task("review", token, ref.id, now=2)
    receipt = {"evidence_ref": ref.id, "content": "one\ntwo\n", "start_line": 1, "end_line": 2}
    candidate = {"trigger": "action", "behavior": "wrong result", "impact": "lost work", "evidence_refs": [ref.id]}
    args = {
        "work_item_id": "",
        "expected_revision": 0,
        "question": "What happens?",
        "judgment": "wrong result",
        "evidence_refs": [ref.id],
        "action": "offer_candidate",
        "payload": {"candidate": candidate},
    }
    audit(store, token, "candidate", [call(args)], [{"type": "tool", "data": {"content": json.dumps(receipt)}}])
    step = state.stage("review", token, "candidate", "c", args, now=2)
    draft = store.stage_candidate_batch("review", token, "candidate", now=2)[0]
    result = store.submit_candidate_draft(
        "review", token, draft["id"], 0, turn_id="candidate", call_id="c", args=candidate, now=2
    )
    assert result["status"] == "promoted"
    with store._connect() as db:
        queued = db.execute("SELECT case_id,step_id,case_revision FROM decision_verification_queue").fetchone()
        decision = json.loads(db.execute("SELECT payload FROM decision_steps WHERE id=?", (step["id"],)).fetchone()[0])
    assert queued == (result["case_id"], step["id"], result["case_revision"])
    assert decision["disposition"] == "promoted"


def test_recovery_delivers_recorded_source_to_new_lease(board):
    store, token = board
    state = DecisionState(store)
    args = initial()
    audit(store, token, "one", [call(args)])
    step = state.stage("review", token, "one", "c", args, now=2)
    op = step["operations"][0]
    ref = store.put_evidence(
        b"one\ntwo\n",
        revision="head",
        side="head",
        kind="source",
        path="a.py",
        start_line=1,
        end_line=2,
        metadata={"workspace_digest": "snapshot", "tool_name": "read_source", "tool_args": {"path": "a.py"}},
    )
    state.claim_operation("review", token, step["id"], op["id"], now=2)
    result = {"evidence_ref": ref.id, "content": "one\ntwo\n", "start_line": 1, "end_line": 2}
    state.record_result("review", token, step["id"], op["id"], result, now=2)
    store.release_task("review", token, now=3)
    new_token = store.acquire_task("review", "replacement", 100, now=4).lease_token
    wrapper = {"step_id": step["id"], "results": [{"operation_id": op["id"], "result": result}]}
    audit(store, new_token, "two", [], [{"type": "tool", "data": {"content": json.dumps(wrapper)}}])
    with store._connect() as db:
        assert ref.id in store._require_delivered(db, "review", new_token, "two", {ref.id})
    state.mark_consumed("review", new_token, step["id"], "two", now=4)
    with pytest.raises(ValueError):
        state.claim_operation("review", token, step["id"], op["id"], now=4)


def test_ack_requires_durable_matching_verify_task(board):
    store, token = board
    state = DecisionState(store)
    with store._connect() as db:
        db.execute("INSERT INTO decision_verification_queue VALUES ('case1','step1',3)")
    with pytest.raises(BindingError):
        state.ack_verification("case1", 3, "verify")
    assert len(state.pending_verifications()) == 1


def test_stale_readiness_requires_current_or_equivalent_frozen_task(board):
    from dataclasses import asdict

    from reviewforge.review_v42.contracts import Case

    store, _ = board
    state = DecisionState(store)
    case = Case("case1", "r", scope_id="a", trigger="go", mechanism="bad", revision=2)
    task = Task(
        "verify",
        "r",
        "verify",
        "case1",
        1,
        status="completed",
        result={
            "case_input": {
                "trigger": "old",
                "mechanism": "bad",
                "claims": [],
                "evidence_refs": [],
                "counter_evidence_refs": [],
            }
        },
    )
    with store._connect() as db:
        db.execute("INSERT INTO objects VALUES ('Case',?,?,?)", (case.id, case.revision, json.dumps(asdict(case))))
        db.execute("INSERT INTO objects VALUES ('Task',?,?,?)", (task.id, task.revision, json.dumps(asdict(task))))
        db.execute("INSERT INTO decision_verification_queue VALUES ('case1','step1',1)")
    with pytest.raises(BindingError):
        state.ack_verification("case1", 1, "verify")
    task.result["case_input"]["trigger"] = "go"
    with store._connect() as db:
        db.execute("UPDATE objects SET payload=? WHERE kind='Task' AND id='verify'", (json.dumps(asdict(task)),))
    state.ack_verification("case1", 1, "verify")
    assert state.pending_verifications() == []


def test_nested_result_integrity_is_checked_per_operation(board):
    store, token = board
    state = DecisionState(store)
    args = initial()
    args["payload"]["operations"].append({"name": "read_source", "args": {"path": "b.py"}})
    audit(store, token, "one", [call(args)])
    step = state.stage("review", token, "one", "c", args, now=2)
    refs = []
    results = []
    for index, op in enumerate(step["operations"]):
        path = "a.py" if index == 0 else "b.py"
        ref = store.put_evidence(
            b"content\n",
            revision="head",
            side="head",
            kind="source",
            path=path,
            start_line=1,
            end_line=1,
            metadata={"workspace_digest": "snapshot", "tool_name": "read_source", "tool_args": {"path": path}},
        )
        receipt = {"evidence_ref": ref.id, "content": "content\n", "start_line": 1, "end_line": 1}
        state.claim_operation("review", token, step["id"], op["id"], now=2)
        state.record_result("review", token, step["id"], op["id"], receipt, now=2)
        refs.append(ref)
        results.append({"operation_id": op["id"], "result": receipt})
    results[1] = {**results[1], "result": {**results[1]["result"], "content": "forged\n"}}
    wrapper = {"step_id": step["id"], "results": results}
    audit(store, token, "two", [], [{"type": "tool", "data": {"content": json.dumps(wrapper)}}])
    with store._connect() as db:
        assert refs[0].id in store._require_delivered(db, "review", token, "two", {refs[0].id})
        with pytest.raises(BindingError):
            store._require_delivered(db, "review", token, "two", {refs[1].id})


def test_retract_draft_and_decision_disposition_are_atomic(board):
    store, token = board
    state = DecisionState(store)
    candidate = {"trigger": "action", "behavior": "bad", "impact": "loss", "evidence_refs": ["missing"]}
    args = {
        "work_item_id": "",
        "expected_revision": 0,
        "question": "What happens?",
        "judgment": "",
        "evidence_refs": [],
        "action": "offer_candidate",
        "payload": {"candidate": candidate},
    }
    audit(store, token, "candidate", [call(args)])
    first = state.stage("review", token, "candidate", "c", args, now=2)
    draft = store.stage_candidate_batch("review", token, "candidate", now=2)[0]
    failed = store.submit_candidate_draft(
        "review", token, draft["id"], 0, turn_id="candidate", call_id="c", args=candidate, now=2
    )
    assert failed["status"] == "repair_needed"
    retract = {"draft_id": failed["id"], "expected_revision": failed["revision"], "reason": "mistaken candidate"}
    args = {
        **args,
        "work_item_id": first["work_item_id"],
        "expected_revision": first["revision"],
        "payload": {"retract_draft": retract},
    }
    audit(store, token, "withdraw", [call(args, "w")])
    step = state.stage("review", token, "withdraw", "w", args, now=2)
    result = store.retract_candidate_draft(
        "review",
        token,
        retract["draft_id"],
        retract["expected_revision"],
        turn_id="withdraw",
        call_id="w",
        reason=retract["reason"],
        now=2,
    )
    assert result["status"] == "retracted"
    with store._connect() as db:
        decision = json.loads(db.execute("SELECT payload FROM decision_steps WHERE id=?", (step["id"],)).fetchone()[0])
    assert decision["disposition"] == "retracted"
