"""Offline tests of the native decision contract and actual source gateway."""

import json

import pytest
from langchain_core.messages import AIMessage
from test_v42_candidate_draft_reviewer import Model, board
from test_v42_fair_runtime import PinnedGateway
from test_v42_stateful_reviewer import call

from reviewforge.review_v42.contracts import Case, Question, ReviewScope
from reviewforge.review_v42.decision_loop import DecisionReviewer
from reviewforge.review_v42.store import ReviewStore
from reviewforge.review_v42.tools import EvidenceGateway


def step(action, payload, item=None, refs=None):
    return {
        "work_item_id": item["id"] if item else "",
        "expected_revision": item["revision"] if item else 0,
        "question": item["question"] if item else "What changed?",
        "judgment": "Reviewing fixed source",
        "evidence_refs": refs or [],
        "action": action,
        "payload": payload,
    }


def decision_store(tmp_path):
    original, task = board(tmp_path)
    run = original.run
    # Use a fresh database because run identity is immutable.
    run.metadata.update(
        decision_cycle_policy="decision-cycle-1",
        frontier_projection_policy="source-line-union-1",
        source_line_policy="git-lf-1",
    )
    store = ReviewStore(tmp_path / "decision.sqlite", run)
    original_plan = original.get_unit_plan()
    store.install_unit_plan(
        [original.get(ReviewScope, sid) for sid in original_plan.primary_scope_ids],
        original.get(ReviewScope, original_plan.integration_scope_id),
    )
    from reviewforge.review_v42.contracts import Task

    task = store.add(Task("review", run.id, "review", "a", 0))
    return store, store.acquire_task(task.id, "reviewer", 120)


def from_board(messages):
    return json.loads(messages[1].content)["decision_items"][0]


def last_ref(messages):
    for message in reversed(messages):
        if message.type == "tool":
            data = json.loads(message.content)
            if data.get("results"):
                return data["results"][0]["result"]["evidence_ref"]
    raise AssertionError("missing receipt")


async def finish_primary(store, task):
    def read(_):
        return AIMessage(
            content="",
            tool_calls=[
                call(
                    "review_step",
                    step(
                        "request_evidence",
                        {
                            "gap": "source",
                            "relevance": "changed behavior",
                            "alternatives": "confirm or refute",
                            "operations": [{"name": "read_source", "args": {"path": "a.py"}}],
                        },
                    ),
                    "r",
                )
            ],
        )

    def close(messages):
        item, ref = from_board(messages), last_ref(messages)
        return AIMessage(
            content="",
            tool_calls=[
                call(
                    "review_step",
                    step(
                        "close_question",
                        {"question_id": item["question_id"], "answer": "Checked source", "evidence_refs": [ref]},
                        item,
                        [ref],
                    ),
                    "c",
                )
            ],
        )

    def complete(messages):
        item = from_board(messages)
        ref = next(q for q in store.list(Question) if q.id == item["question_id"]).evidence_refs[0]
        payload = {
            "checked_region_ids": ["a"],
            "observations": [{"statement": "Checked source", "evidence_refs": [ref], "region_ids": ["a"]}],
            "unresolved_refs": [],
            "finish": True,
        }
        return AIMessage(
            content="", tool_calls=[call("review_step", step("complete_review", payload, item, [ref]), "f")]
        )

    model = Model([read, close, complete])
    reviewer = DecisionReviewer(
        store, model, EvidenceGateway(store, PinnedGateway()), task_id=task.id, lease_token=task.lease_token
    )
    assert await reviewer.run("a") == "reviewed", reviewer.error
    assert store.get(ReviewScope, "a").status == "reviewed"
    assert model.tools[0].name == "review_step"


async def test_read_close_and_complete_primary(tmp_path):
    store, task = decision_store(tmp_path)
    await finish_primary(store, task)


async def test_integration_uses_its_own_review_and_completion_gate(tmp_path):
    store, task = decision_store(tmp_path)
    await finish_primary(store, task)
    from reviewforge.review_v42.contracts import Task

    integration = store.add(Task("integration-task", store.run.id, "review", "integration", 0))
    integration = store.acquire_task(integration.id, "integrator", 120)

    def read(_):
        payload = {
            "gap": "source",
            "relevance": "integration",
            "alternatives": "confirm or refute",
            "operations": [{"name": "read_source", "args": {"path": "a.py"}}],
        }
        return AIMessage(content="", tool_calls=[call("review_step", step("request_evidence", payload), "ir")])

    def close(messages):
        item, ref = from_board(messages), last_ref(messages)
        return AIMessage(
            content="",
            tool_calls=[
                call(
                    "review_step",
                    step(
                        "close_question",
                        {"question_id": item["question_id"], "answer": "Checked integration", "evidence_refs": [ref]},
                        item,
                        [ref],
                    ),
                    "ic",
                )
            ],
        )

    def finish(messages):
        item = from_board(messages)
        ref = next(q for q in store.list(Question) if q.id == item["question_id"]).evidence_refs[0]
        obligations = json.loads(messages[1].content)["integration_obligations"]
        receipts = [
            {
                "obligation_id": o["id"],
                "disposition": "checked",
                "reason": "Compared sources",
                "evidence_refs": list(dict.fromkeys([*o["origin_evidence_refs"], ref])),
            }
            for o in obligations
        ]
        return AIMessage(
            content="",
            tool_calls=[
                call(
                    "review_step",
                    step("complete_review", {"integration_receipts": receipts, "finish": True}, item, [ref]),
                    "if",
                )
            ],
        )

    reviewer = DecisionReviewer(
        store,
        Model([read, close, finish]),
        EvidenceGateway(store, PinnedGateway()),
        task_id=integration.id,
        lease_token=integration.lease_token,
    )
    assert await reviewer.run("integration") == "reviewed", reviewer.error
    assert store.get(ReviewScope, "integration").status == "reviewed"


async def test_candidate_is_enqueued_before_scope_finishes(tmp_path):
    store, task = decision_store(tmp_path)
    accepted = []

    def read(_):
        payload = {
            "gap": "source",
            "relevance": "behavior",
            "alternatives": "confirm or refute",
            "operations": [{"name": "read_source", "args": {"path": "a.py"}}],
        }
        return AIMessage(content="", tool_calls=[call("review_step", step("request_evidence", payload), "r")])

    def candidate(messages):
        item, ref = from_board(messages), last_ref(messages)
        payload = {
            "candidate": {"trigger": "trigger", "behavior": "behavior", "impact": "impact", "evidence_refs": [ref]}
        }
        return AIMessage(
            content="", tool_calls=[call("review_step", step("offer_candidate", payload, item, [ref]), "case")]
        )

    def unresolved(messages):
        assert len(accepted) == 1
        item = from_board(messages)
        return AIMessage(
            content="",
            tool_calls=[
                call(
                    "review_step",
                    step(
                        "unresolved",
                        {"missing_fact": "contract", "attempted": "source", "recovery_condition": "contract available"},
                        item,
                    ),
                    "u",
                )
            ],
        )

    reviewer = DecisionReviewer(
        store,
        Model([read, candidate, unresolved]),
        EvidenceGateway(store, PinnedGateway()),
        on_case=accepted.append,
        task_id=task.id,
        lease_token=task.lease_token,
    )
    assert await reviewer.run("a") == "partial", reviewer.error
    assert len(store.list(Case)) == len(accepted) == 1
    assert store.get(ReviewScope, "a").status != "reviewed"


async def test_result_survives_interruption_before_exchange(tmp_path, monkeypatch):
    store, task = decision_store(tmp_path)
    payload = {
        "gap": "source",
        "relevance": "behavior",
        "alternatives": "confirm or refute",
        "operations": [{"name": "read_source", "args": {"path": "a.py"}}],
    }
    original_append = store.append_turn_audit

    def fail_exchange(task_id, token, turn_id, phase, messages, **kwargs):
        if phase == "exchange":
            raise OSError("interrupted before exchange")
        return original_append(task_id, token, turn_id, phase, messages, **kwargs)

    monkeypatch.setattr(store, "append_turn_audit", fail_exchange)
    gateway = PinnedGateway()
    first = DecisionReviewer(
        store,
        Model([AIMessage(content="", tool_calls=[call("review_step", step("request_evidence", payload), "r")])]),
        EvidenceGateway(store, gateway),
        task_id=task.id,
        lease_token=task.lease_token,
    )
    assert await first.run("a") == "partial"
    assert len(gateway.calls) == 1
    monkeypatch.setattr(store, "append_turn_audit", original_append)

    def unresolved(messages):
        item, ref = from_board(messages), last_ref(messages)
        assert ref
        return AIMessage(
            content="",
            tool_calls=[
                call(
                    "review_step",
                    step(
                        "unresolved",
                        {"missing_fact": "contract", "attempted": "source", "recovery_condition": "contract available"},
                        item,
                        [ref],
                    ),
                    "u",
                )
            ],
        )

    resumed = DecisionReviewer(
        store, Model([unresolved]), EvidenceGateway(store, gateway), task_id=task.id, lease_token=task.lease_token
    )
    assert await resumed.run("a") == "partial", resumed.error
    assert len(gateway.calls) == 1
    assert any(q.status == "unresolved" for q in store.list(Question))


async def test_focus_switch_keeps_old_question_but_projects_current_sources(tmp_path):
    store, task = decision_store(tmp_path)

    def read(path):
        return {
            "gap": "source",
            "relevance": "behavior",
            "alternatives": "confirm or refute",
            "operations": [{"name": "read_source", "args": {"path": path}}],
        }

    first = AIMessage(content="", tool_calls=[call("review_step", step("request_evidence", read("a.py")), "a")])

    def second(messages):
        arg = step("request_evidence", read("b.py"))
        arg["question"] = "Another question?"
        return AIMessage(content="", tool_calls=[call("review_step", arg, "b")])

    def close_second(messages):
        items = json.loads(messages[1].content)["decision_items"]
        item, ref = items[-1], last_ref(messages)
        return AIMessage(
            content="",
            tool_calls=[
                call(
                    "review_step",
                    step(
                        "close_question",
                        {"question_id": item["question_id"], "answer": "Checked", "evidence_refs": [ref]},
                        item,
                        [ref],
                    ),
                    "close-b",
                )
            ],
        )

    def inspect(messages):
        board = json.loads(messages[1].content)
        assert len(board["decision_items"]) == 2
        frontier = json.loads(messages[2].content)
        assert "BODY_b.py_" in json.dumps(frontier)
        assert "BODY_a.py_" not in json.dumps(frontier)
        item = board["decision_items"][0]
        return AIMessage(
            content="",
            tool_calls=[
                call(
                    "review_step",
                    step(
                        "unresolved",
                        {"missing_fact": "contract", "attempted": "source", "recovery_condition": "contract available"},
                        item,
                    ),
                    "unresolved-a",
                )
            ],
        )

    reviewer = DecisionReviewer(
        store,
        Model([first, second, close_second, inspect]),
        EvidenceGateway(store, PinnedGateway()),
        task_id=task.id,
        lease_token=task.lease_token,
    )
    assert await reviewer.run("a") == "partial", reviewer.error
    assert len([q for q in store.list(Question) if q.owner == task.id]) == 2


@pytest.mark.parametrize("rereads", [1, 9])
async def test_multiple_batches_preserve_originals_for_comparison(tmp_path, rereads):
    store, task = decision_store(tmp_path)

    def payload(path):
        return {
            "gap": "comparison",
            "relevance": "behavior",
            "alternatives": "confirm or refute",
            "operations": [{"name": "read_source", "args": {"path": path}}],
        }

    first = AIMessage(content="", tool_calls=[call("review_step", step("request_evidence", payload("a.py")), "a")])

    def second(messages):
        return AIMessage(
            content="",
            tool_calls=[call("review_step", step("request_evidence", payload("b.py"), from_board(messages)), "b")],
        )

    def compare(messages):
        assert "BODY_a.py_" in messages[2].content
        assert "BODY_b.py_" in messages[-1].content
        item = from_board(messages)
        return AIMessage(
            content="",
            tool_calls=[
                call(
                    "review_step",
                    step(
                        "unresolved",
                        {
                            "missing_fact": "contract",
                            "attempted": "both sources",
                            "recovery_condition": "contract available",
                        },
                        item,
                    ),
                    "u",
                )
            ],
        )

    reviewer = DecisionReviewer(
        store,
        Model([first, *([second] * rereads), compare]),
        EvidenceGateway(store, PinnedGateway()),
        task_id=task.id,
        lease_token=task.lease_token,
    )
    assert await reviewer.run("a") == "partial", reviewer.error


async def test_invalid_sibling_does_not_discard_valid_read(tmp_path):
    store, task = decision_store(tmp_path)
    payload = {
        "gap": "source",
        "relevance": "behavior",
        "alternatives": "confirm or refute",
        "operations": [{"name": "read_source", "args": {"path": "a.py"}}],
    }
    response = AIMessage(
        content="",
        tool_calls=[
            call("review_step", {"action": "invalid"}, "bad"),
            call("review_step", step("request_evidence", payload), "good"),
        ],
    )

    def check(messages):
        item, ref = from_board(messages), last_ref(messages)
        assert ref
        return AIMessage(
            content="",
            tool_calls=[
                call(
                    "review_step",
                    step(
                        "unresolved",
                        {"missing_fact": "contract", "attempted": "source", "recovery_condition": "contract"},
                        item,
                        [ref],
                    ),
                    "u",
                )
            ],
        )

    gateway = PinnedGateway()
    reviewer = DecisionReviewer(
        store,
        Model([response, check]),
        EvidenceGateway(store, gateway),
        task_id=task.id,
        lease_token=task.lease_token,
    )
    assert await reviewer.run("a") == "partial", reviewer.error
    assert len(gateway.calls) == 1
    assert any(q.status == "unresolved" for q in store.list(Question))


async def test_invalid_candidate_can_be_explicitly_retracted(tmp_path):
    store, task = decision_store(tmp_path)
    first = AIMessage(
        content="",
        tool_calls=[
            call(
                "review_step",
                step(
                    "offer_candidate",
                    {
                        "candidate": {
                            "trigger": "trigger",
                            "behavior": "behavior",
                            "impact": "impact",
                            "evidence_refs": ["missing"],
                        }
                    },
                ),
                "candidate",
            )
        ],
    )

    def retract(messages):
        board = json.loads(messages[1].content)
        draft = board["candidate_drafts"][0]
        # A rejected first submission does not create a work item.
        item = board["decision_items"][0] if board["decision_items"] else None
        payload = {
            "retract_draft": {"draft_id": draft["id"], "expected_revision": draft["revision"], "reason": "Wrong target"}
        }
        return AIMessage(
            content="", tool_calls=[call("review_step", step("offer_candidate", payload, item), "retract")]
        )

    def stop(messages):
        item = from_board(messages)
        payload = {"missing_fact": "contract", "attempted": "candidate", "recovery_condition": "contract"}
        return AIMessage(content="", tool_calls=[call("review_step", step("unresolved", payload, item), "u")])

    reviewer = DecisionReviewer(
        store,
        Model([first, retract, stop]),
        EvidenceGateway(store, PinnedGateway()),
        task_id=task.id,
        lease_token=task.lease_token,
    )
    assert await reviewer.run("a") == "partial", reviewer.error
    assert not store.pending_candidate_drafts("a")
    assert not store.list(Case)
