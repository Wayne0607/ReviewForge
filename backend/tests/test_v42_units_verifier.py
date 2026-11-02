"""Independent evidence and unit-note boundaries for the units-6 policy."""

import json

import pytest
from langchain_core.messages import AIMessage

from reviewforge.review_v42.contracts import Case, ReviewScope, Run, Task
from reviewforge.review_v42.store import ReviewStore
from reviewforge.review_v42.verifier import POLICY, Verifier, validate_result


@pytest.fixture
def board(tmp_path):
    store = ReviewStore(
        tmp_path / "units.sqlite",
        Run("run", "repo", "base", "head", policy="v42-units-6", metadata={"workspace_digest": "workspace"}),
    )
    producer = store.put_evidence(
        b"send(original_emails)\n",
        revision="head",
        side="head",
        kind="source",
        path="producer.py",
        start_line=1,
        end_line=1,
        metadata={
            "workspace_digest": "workspace",
            "status": "available",
            "reviewer_confirmation": "PRIVATE_PRODUCER_VERDICT",
        },
    )
    consumer = store.put_evidence(
        b"for email in recipients: notify(email)\n",
        revision="head",
        side="head",
        kind="source",
        path="consumer.py",
        start_line=1,
        end_line=1,
        metadata={"workspace_digest": "workspace"},
    )
    contract = store.put_evidence(
        b"assert notified == newly_inserted\n",
        revision="head",
        side="head",
        kind="source",
        path="test_notification.py",
        start_line=1,
        end_line=1,
        metadata={"workspace_digest": "workspace"},
    )
    store.add(
        Case(
            "case",
            "run",
            trigger="input contains an existing participant",
            mechanism="notification sends original inputs",
            claims=[
                {
                    "id": "claim",
                    "statement": "existing participant receives a new invitation",
                    "impact": "duplicate notification",
                }
            ],
            evidence_refs=[producer.id, consumer.id, contract.id],
        )
    )
    return store, producer, consumer, contract


def fact(statement, ref, quote):
    return {"statement": statement, "citations": [{"ref": ref.id, "quote": quote, "start_line": 1, "end_line": 1}]}


def acceptance(producer, consumer, contract):
    return {
        "verdict": "accept",
        "reason": "independent cross-file analysis",
        "gaps": [],
        "facts": {
            "trigger": fact("existing participant is included in the input", producer, "send(original_emails)"),
            "actual": fact(
                "the consumer notifies the unfiltered list", consumer, "for email in recipients: notify(email)"
            ),
            "expected": fact(
                "only newly inserted participants are notified", contract, "assert notified == newly_inserted"
            ),
            "change": fact("the changed call sends the original list", producer, "send(original_emails)"),
            "impact": fact("an existing participant is notified", consumer, "for email in recipients: notify(email)"),
        },
    }


@pytest.mark.asyncio
async def test_independent_cross_file_source_inputs_and_unit_notes_are_separate(board):
    store, producer, consumer, contract = board
    note = store.put_evidence(b"PRIVATE_UPSTREAM_UNIT_NOTE", revision="head", side="head", kind="unit_receipt")
    disguised = store.put_evidence(
        b"PRIVATE_DISGUISED_NOTE",
        revision="head",
        side="head",
        kind="source",
        metadata={"tool_name": "record_integration_receipt"},
    )
    search = store.put_evidence(
        b"PRIVATE_UPSTREAM_SEARCH",
        revision="head",
        side="head",
        kind="search",
        metadata={"status": "available", "navigation_only": True, "complete": True},
    )
    store.update(
        Case, "case", 0, evidence_refs=[producer.id, consumer.id, contract.id, note.id, search.id, disguised.id]
    )

    class Model:
        def bind_tools(self, tools, **kwargs):
            return self

        async def ainvoke(self, messages):
            packet = json.loads(messages[1].content)
            rendered = messages[1].content
            for value in (
                "PRIVATE_UPSTREAM_UNIT_NOTE",
                "PRIVATE_UPSTREAM_SEARCH",
                "PRIVATE_DISGUISED_NOTE",
                "PRIVATE_PRODUCER_VERDICT",
                note.id,
                search.id,
                disguised.id,
            ):
                assert value not in rendered
            assert {item["ref"]["id"] for item in packet["evidence"]} == {producer.id, consumer.id, contract.id}
            return AIMessage(
                content="",
                tool_calls=[{"name": "submit_verdict", "id": "1", "args": acceptance(producer, consumer, contract)}],
            )

    outcome = (await Verifier(store, Model(), None).verify("case"))[0]
    assert outcome["verdict"] == "accept"
    assert {c["ref"] for c in outcome["result"]["facts"]["actual"]["citations"]} == {consumer.id}


@pytest.mark.parametrize(
    "kind",
    [
        "unit",
        "work_unit",
        "unit_plan",
        "unit_receipt",
        "integration",
        "integration_task",
        "integration_receipt",
        "obligation",
        "boundary_obligation",
        "interaction_obligation",
        "navigation_item",
        "frontier_entry",
        "search_index",
        "search_section",
        "query",
    ],
)
@pytest.mark.parametrize("fact_name", ["expected", "actual", "counterexample"])
def test_unit_and_integration_records_cannot_be_cited_as_proof(board, kind, fact_name):
    store, producer, consumer, contract = board
    note = store.put_evidence(
        b"private unit note", revision="head", side="head", kind=kind, metadata={"status": "available"}
    )
    cite = {"ref": note.id, "quote": "private unit note"}
    if fact_name == "counterexample":
        result = {
            "verdict": "reject",
            "reason": "alleged refutation",
            "gaps": [],
            "facts": {"counterexample": {"statement": "premise false", "citations": [cite]}},
        }
    else:
        result = acceptance(producer, consumer, contract)
        result["facts"][fact_name]["citations"] = [cite]
    with pytest.raises(ValueError, match="navigation-only"):
        validate_result(store, result, {producer.id, consumer.id, contract.id, note.id})


@pytest.mark.parametrize(
    "tool",
    [
        "submit_unit_decision",
        "record_integration_receipt",
        "add_integration_obligation",
        "finish_integration",
        "register_unit_dependency",
    ],
)
def test_unit_tool_receipt_disguised_as_source_cannot_be_cited(board, tool):
    store, producer, consumer, contract = board
    note = store.put_evidence(
        b"claimed proof", revision="head", side="head", kind="source", metadata={"tool_name": tool}
    )
    result = acceptance(producer, consumer, contract)
    result["facts"]["expected"]["citations"] = [{"ref": note.id, "quote": "claimed proof"}]
    with pytest.raises(ValueError, match="navigation-only"):
        validate_result(store, result, {producer.id, consumer.id, contract.id, note.id})


def test_positive_counterevidence_is_bound_to_real_cross_file_source(board):
    store, producer, consumer, contract = board
    correct = store.put_evidence(
        b"send(newly_inserted)\n",
        revision="head",
        side="head",
        kind="source",
        path="producer.py",
        start_line=1,
        end_line=1,
        metadata={"workspace_digest": "workspace"},
    )
    result = {
        "verdict": "reject",
        "reason": "the concrete premise is disproved",
        "gaps": [],
        "facts": {
            "counterexample": fact("the changed call sends only new participants", correct, "send(newly_inserted)")
        },
    }
    assert validate_result(store, result, {producer.id, consumer.id, contract.id, correct.id})["verdict"] == "reject"


def test_same_head_different_workspace_cannot_supply_proof(board):
    store, producer, consumer, contract = board
    impostor = store.put_evidence(
        b"assert notified == newly_inserted\n",
        revision="head",
        side="head",
        kind="source",
        path="test_notification.py",
        start_line=1,
        end_line=1,
        metadata={"workspace_digest": "another workspace"},
    )
    result = acceptance(producer, consumer, contract)
    result["facts"]["expected"] = fact("incorrect workspace", impostor, "assert notified == newly_inserted")
    with pytest.raises(ValueError, match="workspace mismatch"):
        validate_result(store, result, {producer.id, consumer.id, contract.id, impostor.id})


@pytest.mark.asyncio
async def test_unrelated_unit_navigation_does_not_invalidate_frozen_verdict(board):
    store, producer, consumer, contract = board
    store.add(ReviewScope("other-unit", "run", "other"))

    class Model:
        def bind_tools(self, tools, **kwargs):
            return self

        async def ainvoke(self, messages):
            return AIMessage(
                content="",
                tool_calls=[{"name": "submit_verdict", "id": "1", "args": acceptance(producer, consumer, contract)}],
            )

    await Verifier(store, Model(), None).verify("case")
    original = store.get_verdict("case", "claim", POLICY)
    assert original and original["verdict"] == "accept"
    store.add(Task("navigation-task", "run", "review", "other-unit", 0))
    task = store.acquire_task("navigation-task", "reviewer", 300)
    merged = store.merge_working_states(
        task.id,
        task.lease_token,
        0,
        [
            {
                "question": "other question",
                "next_step": "inspect",
                "open_questions": ["unrelated note"],
                "observations": [],
            }
        ],
    )
    item = merged["items"][0]
    store.update_navigation_for_task(
        task.id, task.lease_token, item.id, item.revision, text="corrected unrelated note", reason="correction"
    )
    assert store.get_verdict("case", "claim", POLICY) == original


@pytest.mark.parametrize("old_policy", ["v42", "v42-stateful-2", "v42-workspace-3", "v42-frontier-4", "v42-frontier-5"])
def test_new_unit_note_kind_does_not_change_old_policy_semantics(tmp_path, old_policy):
    store = ReviewStore(tmp_path / "old.sqlite", Run("old", "repo", "base", "head", policy=old_policy))
    producer = store.put_evidence(
        b"send(original_emails)\n", revision="head", side="head", kind="source", path="p", start_line=1, end_line=1
    )
    consumer = store.put_evidence(
        b"for email in recipients: notify(email)\n",
        revision="head",
        side="head",
        kind="source",
        path="c",
        start_line=1,
        end_line=1,
    )
    contract = store.put_evidence(
        b"assert notified == newly_inserted\n",
        revision="head",
        side="head",
        kind="source",
        path="t",
        start_line=1,
        end_line=1,
    )
    note = store.put_evidence(b"legacy literal", revision="head", side="head", kind="unit_receipt")
    result = acceptance(producer, consumer, contract)
    result["facts"]["expected"]["citations"] = [{"ref": note.id, "quote": "legacy literal"}]
    assert validate_result(store, result, {producer.id, consumer.id, contract.id, note.id})["verdict"] == "accept"
