"""Native candidate attempts remain independently correctable end to end."""

import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import ValidationError
from test_v42_candidate_draft_reviewer import Model
from test_v42_decision_completion_refs import NavigationGateway
from test_v42_decision_loop import decision_store, from_board, step
from test_v42_stateful_reviewer import call
from test_v42_stream_transport import ByteStream, client_with, final_usage, model_for, tool_end, tool_start

from reviewforge.review_v42.contracts import Case, Question
from reviewforge.review_v42.decision_loop import (
    Candidate,
    DecisionReviewer,
    InvalidDecisionReferencesError,
    ReviewStep,
    review_step_tool,
)
from reviewforge.review_v42.tools import EvidenceGateway


def test_native_schema_exposes_optional_candidate_fields_and_resolution_enum():
    schema = convert_to_openai_tool(review_step_tool())["function"]
    variants = schema["parameters"]["properties"]["payload"]["anyOf"]
    offer = next(row for row in variants if "candidate" in row.get("properties", {}))
    candidate = next(row for row in offer["properties"]["candidate"]["anyOf"] if row.get("type") == "object")
    assert set(candidate["required"]) == {"trigger", "behavior", "impact", "evidence_refs"}
    fields = candidate["properties"]
    assert "same-scope Question IDs" in fields["open_questions"]["description"]
    assert fields["case_id"]["minLength"] == 1
    assert fields["candidate_resolution"]["anyOf"][0]["enum"] == ["new_case", "apply_to_draft"]
    assert "For ordinary correction" in schema["description"]
    assert "Only an attempt marked requires_resolution" in schema["description"]


async def test_nested_candidate_schema_survives_native_stream_transport():
    requests = []
    stream = ByteStream([tool_start(), tool_end(), final_usage(), b"data: [DONE]\n\n"])
    async with client_with([stream], requests) as client:
        bound = model_for(client).bind_tools([review_step_tool()], tool_choice="auto")
        await bound.ainvoke([HumanMessage(content="Review")])
    sent = requests[0]["tools"][0]["function"]
    variants = sent["parameters"]["properties"]["payload"]["anyOf"]
    assert any("candidate" in row.get("properties", {}) for row in variants)
    assert requests[0]["tool_choice"] == "auto"


@pytest.mark.parametrize(
    "overrides",
    [
        {"case_id": ""},
        {"draft_id": ""},
        {"candidate_resolution": ""},
        {"target_draft_id": ""},
        {"draft_id": "d"},
        {"expected_revision": 0},
        {"candidate_resolution": "new_case"},
        {"draft_id": "d", "expected_revision": 0, "target_draft_id": "other"},
        {
            "draft_id": "d",
            "expected_revision": 0,
            "candidate_resolution": "apply_to_draft",
            "target_draft_id": "d",
            "target_expected_revision": 0,
        },
    ],
)
def test_invalid_optional_fields_do_not_silently_imply_intent(overrides):
    base = {"trigger": "t", "behavior": "b", "impact": "i", "evidence_refs": ["e"]}
    with pytest.raises(ValidationError):
        Candidate.model_validate({**base, **overrides})
    assert Candidate.model_validate(base)


def test_valid_candidate_paths_are_distinct():
    base = {"trigger": "t", "behavior": "b", "impact": "i", "evidence_refs": ["e"]}
    for choices in (
        {},
        {"draft_id": "d", "expected_revision": 1},
        {"draft_id": "d", "expected_revision": 1, "candidate_resolution": "new_case"},
        {
            "draft_id": "d",
            "expected_revision": 1,
            "candidate_resolution": "apply_to_draft",
            "target_draft_id": "other",
            "target_expected_revision": 2,
        },
    ):
        assert Candidate.model_validate({**base, **choices})


def test_question_links_require_existing_same_scope_ids(tmp_path):
    store, task = decision_store(tmp_path)
    store.add(Question("other-question", store.run.id, "Other question?", scope_id="integration"))
    store.add(Question("local-question", store.run.id, "Local question?", scope_id="a"))
    source = store.put_evidence(
        b"source",
        revision="head",
        side="head",
        kind="source",
        path="a.py",
        metadata={"workspace_digest": "head-snapshot", "tool_name": "read_source", "tool_args": {"path": "a.py"}},
    )
    args = step(
        "offer_candidate",
        {
            "candidate": {
                "trigger": "t",
                "behavior": "b",
                "impact": "i",
                "evidence_refs": [source.id],
                "open_questions": ["untracked text", "other-question"],
            }
        },
    )
    reviewer = DecisionReviewer(store, None, None, task_id=task.id, lease_token=task.lease_token)
    with pytest.raises(InvalidDecisionReferencesError) as caught:
        reviewer._validate_decision_refs(ReviewStep.model_validate(args))
    assert [(i["field"], i["reason"]) for i in caught.value.issues] == [
        ("payload.candidate.open_questions[0]", "unknown_or_wrong_scope"),
        ("payload.candidate.open_questions[1]", "unknown_or_wrong_scope"),
    ]
    args["payload"]["candidate"]["open_questions"] = ["local-question"]
    reviewer._validate_decision_refs(ReviewStep.model_validate(args))


async def test_repeated_identical_bad_attempts_do_not_create_fake_progress(tmp_path):
    store, task = decision_store(tmp_path)
    candidate = {
        "candidate": {
            "trigger": "trigger",
            "behavior": "behavior",
            "impact": "impact",
            "evidence_refs": ["made-up-ref"],
        }
    }
    response = AIMessage(content="", tool_calls=[call("review_step", step("offer_candidate", candidate), "bad")])
    reviewer = DecisionReviewer(
        store,
        Model([response, response]),
        EvidenceGateway(store, NavigationGateway()),
        task_id=task.id,
        lease_token=task.lease_token,
    )
    assert await reviewer.run("a") == "partial" and reviewer.error == "repeated_failed_batch"
    drafts = store.pending_candidate_drafts("a")
    assert len(drafts) == 2 and all(draft["status"] == "repair_needed" for draft in drafts)
    assert len({draft["id"] for draft in drafts}) == 2


async def test_corrupt_known_ref_cannot_be_masked_by_schema_and_unknown_id(tmp_path):
    store, task = decision_store(tmp_path)
    source = store.put_evidence(
        b"original",
        revision="head",
        side="head",
        kind="source",
        path="a.py",
        metadata={"workspace_digest": "head-snapshot", "tool_name": "read_source", "tool_args": {"path": "a.py"}},
    )
    (store.blob_dir / source.content_hash).write_bytes(b"corrupted")
    # Missing trigger and unknown ID are correctable, but cannot conceal the corrupt known source.
    candidate = {"candidate": {"behavior": "b", "impact": "i", "evidence_refs": ["missing", source.id]}}
    response = AIMessage(content="", tool_calls=[call("review_step", step("offer_candidate", candidate), "bad")])
    reviewer = DecisionReviewer(
        store,
        Model([response]),
        EvidenceGateway(store, NavigationGateway()),
        task_id=task.id,
        lease_token=task.lease_token,
    )
    assert await reviewer.run("a") == "partial"
    assert reviewer.error == "candidate_or_source_unavailable"
    drafts = store.pending_candidate_drafts("a")
    assert len(drafts) == 1 and drafts[0]["status"] == "pending_validation"
    assert not drafts[0]["errors"]


async def test_close_question_reports_undelivered_nested_source_and_can_correct(tmp_path):
    store, task = decision_store(tmp_path)
    unseen = store.put_evidence(
        b"unseen",
        revision="head",
        side="head",
        kind="source",
        path="unseen.py",
        metadata={"workspace_digest": "head-snapshot", "tool_name": "read_source", "tool_args": {"path": "unseen.py"}},
    )
    saved = {}

    def read(path, item=None):
        payload = {
            "gap": "source",
            "relevance": "behavior",
            "alternatives": "compare",
            "operations": [{"name": "read_source", "args": {"path": path}}],
        }
        return AIMessage(
            content="", tool_calls=[call("review_step", step("request_evidence", payload, item), "read-" + path)]
        )

    def bad_close(messages):
        saved["original"] = json.loads(messages[-1].content)["results"][0]["result"]["evidence_ref"]
        item = from_board(messages)
        payload = {"question_id": item["question_id"], "answer": "Checked", "evidence_refs": [unseen.id]}
        return AIMessage(
            content="", tool_calls=[call("review_step", step("close_question", payload, item), "close-bad")]
        )

    def correction(messages):
        feedback = json.loads(messages[-1].content)
        assert [(e["field"], e["evidence_ref"], e["kind"], e["reason"]) for e in feedback["reference_errors"]] == [
            ("payload.evidence_refs[0]", unseen.id, "source", "not_delivered")
        ]
        return read("b.py", from_board(messages))

    def close(messages):
        ref = json.loads(messages[-1].content)["results"][0]["result"]["evidence_ref"]
        item = from_board(messages)
        payload = {"question_id": item["question_id"], "answer": "Checked", "evidence_refs": [ref]}
        return AIMessage(
            content="", tool_calls=[call("review_step", step("close_question", payload, item, [ref]), "close")]
        )

    def complete(messages):
        payload = {
            "checked_region_ids": ["a"],
            "observations": [{"statement": "Checked", "evidence_refs": [saved["original"]], "region_ids": ["a"]}],
            "unresolved_refs": [],
            "finish": True,
        }
        return AIMessage(
            content="",
            tool_calls=[
                call(
                    "review_step",
                    step("complete_review", payload, from_board(messages), [saved["original"]]),
                    "complete",
                )
            ],
        )

    reviewer = DecisionReviewer(
        store,
        Model([read("a.py"), bad_close, correction, close, complete]),
        EvidenceGateway(store, NavigationGateway()),
        task_id=task.id,
        lease_token=task.lease_token,
    )
    assert await reviewer.run("a") == "reviewed", reviewer.error


async def test_native_candidate_failures_are_each_disposed_before_single_case_completion(tmp_path):
    store, task = decision_store(tmp_path)
    accepted, saved = [], {}
    other = store.put_evidence(
        b"other source\n",
        revision="head",
        side="head",
        kind="source",
        path="other.py",
        start_line=1,
        end_line=1,
        metadata={"workspace_digest": "head-snapshot", "tool_name": "read_source", "tool_args": {"path": "other.py"}},
    )
    read = {
        "gap": "source",
        "relevance": "behavior",
        "alternatives": "compare",
        "operations": [
            {"name": "read_source", "args": {"path": "a.py"}},
            {"name": "read_diff", "args": {"path": "a.py"}},
        ],
    }
    first = AIMessage(content="", tool_calls=[call("review_step", step("request_evidence", read), "read")])

    def candidate(messages, fields, ident, refs=None):
        return AIMessage(
            content="",
            tool_calls=[
                call(
                    "review_step",
                    step("offer_candidate", {"candidate": fields}, from_board(messages), refs or []),
                    ident,
                )
            ],
        )

    def first_offer(messages):
        results = json.loads(messages[-1].content)["results"]
        saved["source"], saved["diff"] = [entry["result"]["evidence_ref"] for entry in results]
        fields = {
            "trigger": "trigger",
            "behavior": "mechanism",
            "impact": "impact",
            "evidence_refs": [saved["source"], saved["diff"]],
            "open_questions": ["Is the contract otherwise defined?"],
        }
        return candidate(messages, fields, "first", [saved["source"], saved["diff"]])

    def missing_trigger(messages):
        feedback = json.loads(messages[-1].content)
        assert feedback["status"] == "repair_needed" and feedback["requires_resolution"] is False
        assert {error["reason"] for error in feedback["errors"]} == {"navigation_only", "unknown_or_wrong_scope"}
        assert {error["field"] for error in feedback["errors"]} == {
            "evidence_refs[1]",
            "payload.candidate.evidence_refs[1]",
            "payload.candidate.open_questions[0]",
        }
        saved["original"] = feedback
        fields = {
            "behavior": "mechanism",
            "impact": "impact",
            "evidence_refs": [saved["source"]],
            "draft_id": feedback["draft_id"],
            "expected_revision": feedback["draft_revision"],
        }
        return candidate(messages, fields, "missing-trigger", [saved["source"]])

    def unread_outer(messages):
        feedback = json.loads(messages[-1].content)
        assert feedback["status"] == "repair_needed" and feedback["requires_resolution"] is True
        assert feedback["preflight_target"]["draft_id"] == saved["original"]["draft_id"]
        assert any(
            error["field"] == "payload.candidate.trigger" and error["reason"] == "missing"
            for error in feedback["errors"]
        )
        saved["bad_shape"] = feedback
        fields = {
            "trigger": "trigger",
            "behavior": "mechanism",
            "impact": "impact",
            "evidence_refs": [saved["source"]],
            "draft_id": saved["original"]["draft_id"],
            "expected_revision": saved["original"]["draft_revision"],
        }
        return candidate(messages, fields, "unread-outer", [saved["source"], other.id])

    def retract_shape(messages):
        feedback = json.loads(messages[-1].content)
        assert feedback["status"] == "repair_needed" and feedback["requires_resolution"] is True
        assert any(
            error["field"] == "evidence_refs[1]"
            and error["evidence_ref"] == other.id
            and error["reason"] == "not_delivered"
            for error in feedback["errors"]
        )
        saved["unread"] = feedback
        item = from_board(messages)
        payload = {
            "retract_draft": {
                "draft_id": saved["bad_shape"]["draft_id"],
                "expected_revision": saved["bad_shape"]["draft_revision"],
                "reason": "Superseded invalid attempt",
            }
        }
        return AIMessage(
            content="",
            tool_calls=[
                call("review_step", step("offer_candidate", payload, item, [saved["source"]]), "retract-shape")
            ],
        )

    def retract_unread(messages):
        assert json.loads(messages[-1].content)["status"] == "retracted"
        payload = {
            "retract_draft": {
                "draft_id": saved["unread"]["draft_id"],
                "expected_revision": saved["unread"]["draft_revision"],
                "reason": "Superseded invalid attempt",
            }
        }
        return AIMessage(
            content="",
            tool_calls=[
                call(
                    "review_step",
                    step("offer_candidate", payload, from_board(messages), [saved["source"]]),
                    "retract-unread",
                )
            ],
        )

    def repair_original(messages):
        assert json.loads(messages[-1].content)["status"] == "retracted"
        fields = {
            "trigger": "trigger",
            "behavior": "mechanism",
            "impact": "impact",
            "evidence_refs": [saved["source"]],
            "draft_id": saved["original"]["draft_id"],
            "expected_revision": saved["original"]["draft_revision"],
        }
        return candidate(messages, fields, "repair", [saved["source"]])

    def close(messages):
        assert json.loads(messages[-1].content)["status"] == "promoted"
        assert len(accepted) == len(store.list(Case)) == 1
        item = from_board(messages)
        payload = {"question_id": item["question_id"], "answer": "Checked source", "evidence_refs": [saved["source"]]}
        return AIMessage(
            content="",
            tool_calls=[call("review_step", step("close_question", payload, item, [saved["source"]]), "close")],
        )

    def complete(messages):
        payload = {
            "checked_region_ids": ["a"],
            "observations": [{"statement": "Checked", "evidence_refs": [saved["source"]], "region_ids": ["a"]}],
            "unresolved_refs": [],
            "finish": True,
        }
        return AIMessage(
            content="",
            tool_calls=[
                call(
                    "review_step", step("complete_review", payload, from_board(messages), [saved["source"]]), "complete"
                )
            ],
        )

    reviewer = DecisionReviewer(
        store,
        Model(
            [
                first,
                first_offer,
                missing_trigger,
                unread_outer,
                retract_shape,
                retract_unread,
                repair_original,
                close,
                complete,
            ]
        ),
        EvidenceGateway(store, NavigationGateway()),
        on_case=accepted.append,
        task_id=task.id,
        lease_token=task.lease_token,
    )
    assert await reviewer.run("a") == "reviewed", reviewer.error
    assert len(accepted) == len(store.list(Case)) == 1
    assert not store.pending_candidate_drafts("a")
    assert any(q.status == "answered" for q in store.list(Question))
