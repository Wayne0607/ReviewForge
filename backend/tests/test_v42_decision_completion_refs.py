"""Completion feedback identifies navigation without relaxing source gates."""

import json

import pytest
from langchain_core.messages import AIMessage
from test_v42_candidate_draft_reviewer import Model
from test_v42_decision_loop import decision_store, finish_primary, from_board, step
from test_v42_fair_runtime import PinnedGateway
from test_v42_stateful_reviewer import call

from reviewforge.review_v42.contracts import EvidenceRef, ReviewScope, Task
from reviewforge.review_v42.decision_loop import (
    DECISION_PROMPT,
    DecisionReviewer,
    InvalidCompletionReferencesError,
    ReviewCompletion,
    review_step_tool,
)
from reviewforge.review_v42.tools import EvidenceGateway


class NavigationGateway(PinnedGateway):
    def __init__(self):
        super().__init__()
        self.tool_specs.extend(
            [
                {
                    "type": "function",
                    "function": {
                        "name": "read_diff",
                        "description": "diff",
                        "parameters": {
                            "type": "object",
                            "properties": {"path": {"type": "string"}},
                            "required": ["path"],
                            "additionalProperties": False,
                        },
                    },
                },
                {
                    "type": "function",
                    "function": {
                        "name": "search_code",
                        "description": "search",
                        "parameters": {
                            "type": "object",
                            "properties": {"query": {"type": "string"}},
                            "required": ["query"],
                            "additionalProperties": False,
                        },
                    },
                },
            ]
        )

    async def __call__(self, name, args):
        if name == "read_source":
            return await super().__call__(name, args)
        self.calls.append((name, args.copy()))
        return {
            "content": "navigation result",
            "revision": "head",
            "side": "head",
            "kind": "diff" if name == "read_diff" else "search",
            "path": args.get("path", ""),
            "metadata": {
                "workspace_digest": "head-snapshot",
                "status": "available",
                "navigation_only": name == "search_code",
            },
        }


def test_completion_tool_explains_primary_and_integration_ownership():
    description = review_step_tool().description
    assert "another member of primary_scope_ids" in description
    assert "never its own ID or the integration scope" in description
    assert "Only the integration role submits integration_relations and integration_receipts" in description
    assert "never the integration scope" in DECISION_PROMPT


def create_ref(store, kind, *, metadata=None):
    return store.put_evidence(
        (kind + " contents").encode(),
        revision="head",
        side="head",
        kind=kind,
        path="a.py",
        metadata={"workspace_digest": "head-snapshot", **(metadata or {})},
    )


def test_all_completion_fields_report_exact_invalid_reference_locations(tmp_path):
    store, task = decision_store(tmp_path)
    source = create_ref(store, "source")
    diff = create_ref(store, "diff")
    search = create_ref(store, "search")
    marked = create_ref(store, "custom", metadata={"navigation_only": True})
    payload = ReviewCompletion.model_validate(
        {
            "observations": [
                {"statement": "checked", "evidence_refs": [source.id, diff.id, "missing"], "region_ids": ["a"]}
            ],
            "relations": [{"target_scope_id": "other", "description": "related", "evidence_refs": [search.id]}],
            "integration_relations": [{"unit_ids": ["a"], "description": "related", "evidence_refs": [marked.id]}],
            "integration_receipts": [
                {"obligation_id": "ob", "disposition": "checked", "reason": "checked", "evidence_refs": [diff.id]}
            ],
        }
    )
    reviewer = DecisionReviewer(store, None, None, task_id=task.id, lease_token=task.lease_token)
    with pytest.raises(InvalidCompletionReferencesError) as caught:
        reviewer._validate_completion_refs(payload)
    issues = caught.value.issues
    assert [(i["field"], i["evidence_ref"], i["kind"], i["reason"]) for i in issues] == [
        ("payload.observations[0].evidence_refs[1]", diff.id, "diff", "navigation_only"),
        ("payload.observations[0].evidence_refs[2]", "missing", None, "unknown_reference"),
        ("payload.relations[0].evidence_refs[0]", search.id, "search", "navigation_only"),
        ("payload.integration_relations[0].evidence_refs[0]", marked.id, "custom", "navigation_only"),
        ("payload.integration_receipts[0].evidence_refs[0]", diff.id, "diff", "navigation_only"),
    ]
    assert all("original source" in i["message"] for i in issues if i["reason"] == "navigation_only")


async def test_model_corrects_mixed_navigation_and_completes(tmp_path):
    store, task = decision_store(tmp_path)
    saved = {}
    request = {
        "gap": "source",
        "relevance": "behavior",
        "alternatives": "compare",
        "operations": [
            {"name": "read_source", "args": {"path": "a.py"}},
            {"name": "read_diff", "args": {"path": "a.py"}},
            {"name": "search_code", "args": {"query": "a"}},
        ],
    }
    first = AIMessage(content="", tool_calls=[call("review_step", step("request_evidence", request), "read")])

    def close(messages):
        results = json.loads(messages[-1].content)["results"]
        saved["refs"] = [entry["result"]["evidence_ref"] for entry in results]
        item = from_board(messages)
        return AIMessage(
            content="",
            tool_calls=[
                call(
                    "review_step",
                    step(
                        "close_question",
                        {"question_id": item["question_id"], "answer": "Checked", "evidence_refs": [saved["refs"][0]]},
                        item,
                        [saved["refs"][0]],
                    ),
                    "close",
                )
            ],
        )

    def completion(messages, refs):
        item = from_board(messages)
        payload = {
            "checked_region_ids": ["a"],
            "observations": [{"statement": "Checked", "evidence_refs": refs, "region_ids": ["a"]}],
            "unresolved_refs": [],
            "finish": True,
        }
        return AIMessage(
            content="",
            tool_calls=[call("review_step", step("complete_review", payload, item, [saved["refs"][0]]), "complete")],
        )

    def invalid(messages):
        return completion(messages, [*saved["refs"], "unknown-id"])

    def partially_corrected(messages):
        feedback = json.loads(messages[-1].content)
        assert [(e["field"], e["evidence_ref"], e["kind"], e["reason"]) for e in feedback["reference_errors"]] == [
            ("payload.observations[0].evidence_refs[1]", saved["refs"][1], "diff", "navigation_only"),
            ("payload.observations[0].evidence_refs[2]", saved["refs"][2], "search", "navigation_only"),
            ("payload.observations[0].evidence_refs[3]", "unknown-id", None, "unknown_reference"),
        ]
        return completion(messages, [saved["refs"][0], saved["refs"][2], "unknown-id"])

    def corrected(messages):
        feedback = json.loads(messages[-1].content)
        assert [issue["evidence_ref"] for issue in feedback["reference_errors"]] == [saved["refs"][2], "unknown-id"]
        return completion(messages, [saved["refs"][0]])

    reviewer = DecisionReviewer(
        store,
        Model([first, close, invalid, partially_corrected, corrected]),
        EvidenceGateway(store, NavigationGateway()),
        task_id=task.id,
        lease_token=task.lease_token,
    )
    assert await reviewer.run("a") == "reviewed", reviewer.error
    assert store.get(ReviewScope, "a").status == "reviewed"


async def test_integration_can_locate_originals_without_using_navigation_as_proof(tmp_path):
    store, task = decision_store(tmp_path)
    await finish_primary(store, task)
    integration = store.add(Task("integration-task", store.run.id, "review", "integration", 0))
    integration = store.acquire_task(integration.id, "integrator", 120)
    refs = {}
    gap = {"gap": "integration behavior", "relevance": "changed unit", "alternatives": "inspect original"}

    def locate(_messages):
        payload = {
            **gap,
            "operations": [
                {"name": "read_diff", "args": {"path": "a.py"}},
                {"name": "search_code", "args": {"query": "value"}},
            ],
        }
        return AIMessage(content="", tool_calls=[call("review_step", step("request_evidence", payload), "locate")])

    def read_original(messages):
        results = json.loads(messages[-1].content)["results"]
        refs["diff"], refs["search"] = [entry["result"]["evidence_ref"] for entry in results]
        payload = {
            **gap,
            "navigation_refs": [refs["search"], "stale-navigation-hint"],
            "operations": [{"name": "read_source", "args": {"path": "a.py"}}],
        }
        return AIMessage(
            content="",
            tool_calls=[
                call("review_step", step("request_evidence", payload, from_board(messages), [refs["diff"]]), "read")
            ],
        )

    def close(messages, *, include_navigation):
        item = from_board(messages)
        if include_navigation:
            assert item["judgments"][-1]["navigation_refs"] == [refs["diff"], refs["search"]]
            assert item["judgments"][-1]["evidence_refs"] == []
            results = json.loads(messages[-1].content)["results"]
            refs["original"] = results[0]["result"]["evidence_ref"]
        else:
            feedback = json.loads(messages[-1].content)
            assert feedback["reference_errors"][0]["reason"] == "navigation_only"
        evidence = [refs["original"], refs["diff"]] if include_navigation else [refs["original"]]
        payload = {"question_id": item["question_id"], "answer": "Checked the original", "evidence_refs": evidence}
        return AIMessage(
            content="", tool_calls=[call("review_step", step("close_question", payload, item, evidence), "close")]
        )

    def finish(messages):
        board = json.loads(messages[1].content)
        receipts = [
            {
                "obligation_id": o["id"],
                "disposition": "checked",
                "evidence_refs": [refs["original"]],
                "reason": "Checked original",
            }
            for o in board["integration_obligations"]
        ]
        args = {
            "scope_id": "integration",
            "expected_scope_revision": board["scope"]["revision"],
            "payload": {"integration_receipts": receipts, "finish": True},
        }
        return AIMessage(content="", tool_calls=[call("complete_scope", args, "finish")])

    gateway = NavigationGateway()
    reviewer = DecisionReviewer(
        store,
        Model(
            [
                locate,
                read_original,
                lambda m: close(m, include_navigation=True),
                lambda m: close(m, include_navigation=False),
                finish,
            ]
        ),
        EvidenceGateway(store, gateway),
        task_id=integration.id,
        lease_token=integration.lease_token,
    )
    assert await reviewer.run("integration") == "reviewed", reviewer.error
    assert gateway.calls == [
        ("read_diff", {"path": "a.py"}),
        ("search_code", {"query": "value"}),
        ("read_source", {"path": "a.py"}),
    ]


async def test_damaged_source_still_fails_original_gate(tmp_path):
    store, task = decision_store(tmp_path)
    saved = {}
    request = {
        "gap": "source",
        "relevance": "behavior",
        "alternatives": "compare",
        "operations": [{"name": "read_source", "args": {"path": "a.py"}}],
    }
    first = AIMessage(content="", tool_calls=[call("review_step", step("request_evidence", request), "read")])

    def close(messages):
        saved["ref"] = json.loads(messages[-1].content)["results"][0]["result"]["evidence_ref"]
        item = from_board(messages)
        return AIMessage(
            content="",
            tool_calls=[
                call(
                    "review_step",
                    step(
                        "close_question",
                        {"question_id": item["question_id"], "answer": "Checked", "evidence_refs": [saved["ref"]]},
                        item,
                        [saved["ref"]],
                    ),
                    "close",
                )
            ],
        )

    def damage(messages):
        item = from_board(messages)
        ref = store.get(EvidenceRef, saved["ref"])
        (store.blob_dir / ref.content_hash).unlink()
        payload = {
            "checked_region_ids": ["a"],
            "observations": [{"statement": "Checked", "evidence_refs": [ref.id], "region_ids": ["a"]}],
            "unresolved_refs": [],
            "finish": True,
        }
        return AIMessage(
            content="", tool_calls=[call("review_step", step("complete_review", payload, item, [ref.id]), "damaged")]
        )

    reviewer = DecisionReviewer(
        store,
        Model([first, close, damage]),
        EvidenceGateway(store, NavigationGateway()),
        task_id=task.id,
        lease_token=task.lease_token,
    )
    assert await reviewer.run("a") == "partial"
    result = next(row for row in store.read_turn_audit(task.id) if row["phase"].endswith(":damaged"))
    feedback = json.loads(result["messages"][0]["data"]["content"])
    assert "FileNotFoundError" in feedback["error"]
    assert "reference_errors" not in feedback
