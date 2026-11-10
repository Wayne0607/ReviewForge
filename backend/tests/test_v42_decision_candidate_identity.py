"""Decision work items preserve explicit Case identity and verification readiness."""

import json

from langchain_core.messages import AIMessage
from test_v42_candidate_draft_reviewer import Model
from test_v42_decision_loop import decision_store, from_board, last_ref, step
from test_v42_fair_runtime import PinnedGateway
from test_v42_stateful_reviewer import call

from reviewforge.review_v42.contracts import Case, Task
from reviewforge.review_v42.decision_loop import DecisionReviewer
from reviewforge.review_v42.decision_state import DecisionState
from reviewforge.review_v42.tools import EvidenceGateway


def _response(action, payload, call_id, item=None, refs=None):
    return AIMessage(content="", tool_calls=[call("review_step", step(action, payload, item, refs), call_id)])


def _read():
    return _response(
        "request_evidence",
        {
            "gap": "source",
            "relevance": "changed behavior",
            "alternatives": "inspect the fixed source",
            "operations": [{"name": "read_source", "args": {"path": "a.py"}}],
        },
        "read",
    )


def _offer(ref, call_id, item, *, trigger="request token", behavior="uses seconds as milliseconds", **extra):
    fields = {"trigger": trigger, "behavior": behavior, "impact": "expiry too soon", "evidence_refs": [ref], **extra}
    return _response("offer_candidate", {"candidate": fields}, call_id, item, [ref])


def _finish(messages):
    return _response(
        "unresolved",
        {"missing_fact": "unrelated premise", "attempted": "read source", "recovery_condition": "get original"},
        "end",
        from_board(messages),
    )


def _receipt(messages):
    return json.loads(messages[-1].content)


def _replay(store, task, call_id):
    draft = next(row for row in store.list_candidate_drafts("a") if row["origin_call_id"] == call_id)
    return store.submit_candidate_draft(
        task.id,
        task.lease_token,
        draft["id"],
        0,
        turn_id=draft["origin_turn_id"],
        call_id=call_id,
        args=draft["original_args"],
    )


async def test_same_work_item_rephrasing_needs_disposition_but_distinct_cases_survive(tmp_path):
    store, task = decision_store(tmp_path)
    decisions = DecisionState(store)
    callbacks = []
    seen = {}

    def first(messages):
        seen["ref"] = last_ref(messages)
        return _offer(seen["ref"], "first", from_board(messages))

    def rephrase(messages):
        seen["original"] = _receipt(messages)["case_id"]
        return _offer(
            seen["ref"], "rephrase", from_board(messages), trigger="creating a token", behavior="expiry unit mismatch"
        )

    def withdraw(messages):
        blocked = _receipt(messages)
        assert blocked["status"] == "repair_needed" and blocked["requires_resolution"]
        assert blocked["errors"][0]["reason"] == "work_item_already_promoted_case"
        assert blocked["errors"][0]["case_ids"] == [seen["original"]]
        assert len(store.list(Case)) == len(decisions.pending_verifications()) == len(callbacks) == 1
        return _response(
            "offer_candidate",
            {
                "retract_draft": {
                    "draft_id": blocked["draft_id"],
                    "expected_revision": blocked["draft_revision"],
                    "reason": "The same token expiry mechanism was already offered.",
                }
            },
            "withdraw",
            from_board(messages),
        )

    def independent(messages):
        assert _receipt(messages)["status"] == "retracted"
        return _offer(
            seen["ref"], "independent", from_board(messages), trigger="rotate token", behavior="keeps old key"
        )

    def confirm_independent(messages):
        blocked = _receipt(messages)
        assert blocked["status"] == "repair_needed" and blocked["requires_resolution"]
        return _offer(
            seen["ref"],
            "confirm",
            from_board(messages),
            trigger="rotate token",
            behavior="keeps old key",
            draft_id=blocked["draft_id"],
            expected_revision=blocked["draft_revision"],
            candidate_resolution="new_case",
        )

    def different_item(messages):
        assert _receipt(messages)["case_changed"] is True
        assert len(store.list(Case)) == 2
        return _offer(seen["ref"], "different-item", None, trigger="refresh token", behavior="drops scope")

    def offer_update(messages):
        seen["unlisted"] = _receipt(messages)["case_id"]
        return _offer(seen["ref"], "update", from_board(messages), behavior="confuses seconds and millis")

    def select_unlisted(messages):
        blocked = _receipt(messages)
        assert blocked["requires_resolution"] and seen["unlisted"] not in blocked["errors"][0]["case_ids"]
        seen["guard"] = {"draft_id": blocked["draft_id"], "expected_revision": blocked["draft_revision"]}
        return _offer(
            seen["ref"],
            "unlisted",
            from_board(messages),
            case_id=seen["unlisted"],
            trigger="refresh token",
            behavior="confuses seconds and millis",
            **seen["guard"],
        )

    def withdraw_unlisted(messages):
        blocked = _receipt(messages)
        assert blocked["status"] == "repair_needed" and blocked["requires_resolution"]
        assert blocked["errors"] == [{"field": "draft_id", "reason": "case_not_listed_for_work_item"}]
        return _response(
            "offer_candidate",
            {
                "retract_draft": {
                    "draft_id": blocked["draft_id"],
                    "expected_revision": blocked["draft_revision"],
                    "reason": "The refresh case is unrelated to this draft.",
                }
            },
            "withdraw-unlisted",
            from_board(messages),
        )

    def select_listed(messages):
        return _offer(
            seen["ref"],
            "listed-wrong-trigger",
            from_board(messages),
            case_id=seen["original"],
            trigger="a rewritten trigger",
            behavior="confuses seconds and millis",
            **seen["guard"],
        )

    def fix_trigger(messages):
        blocked = _receipt(messages)
        assert blocked["draft_id"] == seen["guard"]["draft_id"]
        assert blocked["errors"] == [{"field": "trigger", "reason": "existing_trigger_immutable"}]
        return _offer(
            seen["ref"],
            "listed",
            from_board(messages),
            case_id=seen["original"],
            behavior="confuses seconds and millis",
            draft_id=blocked["draft_id"],
            expected_revision=blocked["draft_revision"],
        )

    model = Model(
        [
            _read(),
            first,
            rephrase,
            withdraw,
            independent,
            confirm_independent,
            different_item,
            offer_update,
            select_unlisted,
            withdraw_unlisted,
            select_listed,
            fix_trigger,
            _finish,
        ]
    )
    reviewer = DecisionReviewer(
        store,
        model,
        EvidenceGateway(store, PinnedGateway()),
        task_id=task.id,
        lease_token=task.lease_token,
        on_case=callbacks.append,
    )
    assert await reviewer.run("a") == "partial"
    cases = {case.trigger: case.id for case in store.list(Case)}
    assert set(cases) == {"request token", "rotate token", "refresh token"}
    assert {row["case_id"] for row in decisions.pending_verifications()} == set(cases.values())
    assert [(case.id, case.revision) for case in callbacks] == [
        (cases["request token"], 0),
        (cases["rotate token"], 0),
        (cases["refresh token"], 0),
        (cases["request token"], 1),
    ]
    assert not store.pending_candidate_drafts("a")
    items = decisions.list_items(task.id)
    with store._connect() as db:
        assert store._work_item_case_ids(db, items[0]["id"]) == sorted([cases["request token"], cases["rotate token"]])
        assert store._work_item_case_ids(db, items[1]["id"]) == [cases["refresh token"]]
    assert _replay(store, task, "rephrase")["status"] == "repair_needed"
    assert not store.pending_candidate_drafts("a")


async def test_exact_case_reuse_keeps_outbox_unchanged_and_genuine_revision_enqueues(tmp_path):
    store, task = decision_store(tmp_path)
    decisions = DecisionState(store)
    callbacks = []
    seen = {}

    def first(messages):
        seen["ref"] = last_ref(messages)
        return _offer(seen["ref"], "first", from_board(messages))

    def exact_with_pending(messages):
        result = _receipt(messages)
        assert result["case_changed"] is True
        seen["case_id"] = result["case_id"]
        seen["pending"] = decisions.pending_verifications()
        return _offer(seen["ref"], "exact-pending", from_board(messages))

    def exact_after_ack(messages):
        assert _receipt(messages)["case_changed"] is False
        assert decisions.pending_verifications() == seen["pending"]
        case = store.get(Case, seen["case_id"])
        verification = store.add(Task("verify-case", store.run.id, "verify", case.id, case.revision))
        decisions.ack_verification(case.id, case.revision, verification.id)
        assert decisions.pending_verifications() == []
        return _offer(seen["ref"], "exact-acked", from_board(messages))

    def revise(messages):
        result = _receipt(messages)
        assert result["case_id"] == seen["case_id"] and result["case_changed"] is False
        assert decisions.pending_verifications() == []
        assert len(callbacks) == 1
        replay = _replay(store, task, "exact-acked")
        assert replay["case_id"] == seen["case_id"] and replay["case_changed"] is False
        assert decisions.pending_verifications() == []
        return _offer(
            seen["ref"], "revise", from_board(messages), case_id=seen["case_id"], behavior="confuses seconds and millis"
        )

    model = Model([_read(), first, exact_with_pending, exact_after_ack, revise, _finish])
    reviewer = DecisionReviewer(
        store,
        model,
        EvidenceGateway(store, PinnedGateway()),
        task_id=task.id,
        lease_token=task.lease_token,
        on_case=callbacks.append,
    )
    assert await reviewer.run("a") == "partial"
    (case,) = store.list(Case)
    assert case.id == seen["case_id"] and case.revision == 1
    assert [(row.id, row.revision) for row in callbacks] == [(case.id, 0), (case.id, 1)]
    assert [(row["case_id"], row["case_revision"]) for row in decisions.pending_verifications()] == [(case.id, 1)]
