"""Native candidate drafts preserve failed siblings without becoming evidence."""

import hashlib
import json

from langchain_core.messages import AIMessage
from test_v42_fair_runtime import PinnedGateway
from test_v42_stateful_reviewer import call

from reviewforge.review_v42.contracts import Case, ReviewScope, Run, Task
from reviewforge.review_v42.reviewer import Reviewer, reviewer_tools
from reviewforge.review_v42.shared_investigation import SharedInvestigations
from reviewforge.review_v42.store import ReviewStore
from reviewforge.review_v42.tools import EvidenceGateway


def board(tmp_path):
    region = {"id": "a", "path": "a.py", "side": "head", "start_line": 1, "end_line": 1, "changed_lines": [1]}
    run = Run(
        "run",
        "a/b",
        "base",
        "head",
        policy="v42-units-6",
        metadata={
            "changed_regions": [region],
            "workspace_digest": "head-snapshot",
            "base_workspace_digest": "base-snapshot",
            "shared_investigation": "shared-1",
            "candidate_draft_policy": "candidate-draft-1",
        },
    )
    store = ReviewStore(tmp_path / "board.sqlite", run)
    store.install_unit_plan(
        [ReviewScope("a", "run", "a", regions=[region])],
        ReviewScope("integration", "run", "integration", dependencies=["a"]),
    )
    SharedInvestigations(store)
    task = store.add(Task("review", "run", "review", "a", 0))
    task = store.acquire_task(task.id, "reviewer", 120)
    return store, task


def candidate(refs, *, impact="impact"):
    return {"trigger": "trigger " + impact, "behavior": "mechanism", "impact": impact, "evidence_refs": refs}


class Model:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.inputs = []

    def bind_tools(self, tools, **kwargs):
        self.tools = tools
        return self

    async def ainvoke(self, messages):
        self.inputs.append(list(messages))
        reply = next(self.replies)
        return reply(messages) if callable(reply) else reply


def response(*calls):
    return AIMessage(content="", tool_calls=list(calls))


def returned_ref(messages):
    return next(json.loads(m.content)["evidence_ref"] for m in messages if m.type == "tool")


async def test_invalid_siblings_are_durable_and_correctable(tmp_path):
    store, task = board(tmp_path)
    accepted = []

    def submit(messages):
        ref = returned_ref(messages)
        return response(
            call("submit_case", candidate([ref], impact="valid"), "good"),
            call("submit_case", candidate(["missing-1", "missing-2"], impact="first"), "bad1"),
            call("submit_case", candidate(["missing-3"], impact="second"), "bad2"),
            call("submit_case", candidate(["missing-4"], impact="third"), "bad3"),
        )

    def repair(messages):
        state = json.loads(messages[1].content)
        pending = state["candidate_drafts"]
        assert len(pending) == 3
        first = next(item for item in pending if item["origin_call_id"] == "bad1")
        errors = json.dumps(first["errors"])
        assert "missing-1" in errors and "missing-2" in errors
        ref = first["original_args"]["evidence_refs"]
        assert ref == ["missing-1", "missing-2"]
        source = next(item for item in store.list(Case) if item.claims[0]["impact"] == "valid").evidence_refs[0]
        return response(
            call(
                "submit_case",
                {
                    **candidate([source], impact="first"),
                    "draft_id": first["id"],
                    "expected_revision": first["revision"],
                },
                "repair",
            )
        )

    model = Model([response(call("read_source", {"path": "a.py"}, "read")), submit, repair, response(), response()])
    reviewer = Reviewer(
        store,
        model,
        EvidenceGateway(store, PinnedGateway()),
        task_id=task.id,
        lease_token=task.lease_token,
        on_case=accepted.append,
    )
    assert await reviewer.run("a") == "stalled"
    drafts = store.list_candidate_drafts("a")
    assert [item["status"] for item in drafts].count("promoted") == 2
    assert len(store.pending_candidate_drafts("a")) == 2
    assert len(store.list(Case)) == len(accepted) == 2
    assert not store.pending_shared_batches("a")


async def test_interruption_after_response_does_not_lose_the_native_candidate(tmp_path, monkeypatch):
    store, task = board(tmp_path)
    model = Model([response(call("submit_case", candidate(["unknown"]), "candidate"))])

    def interrupted(*args, **kwargs):
        raise OSError("simulated crash before staging")

    monkeypatch.setattr(store, "stage_candidate_batch", interrupted)
    reviewer = Reviewer(
        store, model, EvidenceGateway(store, PinnedGateway()), task_id=task.id, lease_token=task.lease_token
    )
    assert await reviewer.run("a") in {"partial", "error"}
    pending = store.pending_shared_batches("a")
    assert pending and "candidate" in json.dumps(pending)
    assert not store.list(Case)


async def test_interruption_after_promotion_leaves_pending_batch_and_preserves_case(tmp_path, monkeypatch):
    store, task = board(tmp_path)
    original = store.append_turn_audit

    def interrupted(task_id, token, turn_id, phase, messages, **kwargs):
        if phase.endswith(":good"):
            raise OSError("simulated crash after promotion before tool receipt")
        return original(task_id, token, turn_id, phase, messages, **kwargs)

    monkeypatch.setattr(store, "append_turn_audit", interrupted)
    model = Model(
        [
            response(call("read_source", {"path": "a.py"}, "read")),
            lambda messages: response(call("submit_case", candidate([returned_ref(messages)]), "good")),
        ]
    )
    reviewer = Reviewer(
        store, model, EvidenceGateway(store, PinnedGateway()), task_id=task.id, lease_token=task.lease_token
    )
    assert await reviewer.run("a") in {"partial", "error"}
    assert len(store.list(Case)) == 1
    assert store.list_candidate_drafts("a")[0]["status"] == "promoted"
    assert store.pending_shared_batches("a")
    retry = Model([])
    reviewer = Reviewer(
        store, retry, EvidenceGateway(store, PinnedGateway()), task_id=task.id, lease_token=task.lease_token
    )
    assert await reviewer.run("a") == "partial"
    assert not retry.inputs and len(store.list(Case)) == 1


async def test_malformed_native_batch_rejects_every_sibling_before_staging(tmp_path):
    store, task = board(tmp_path)
    duplicate = response(
        call("submit_case", candidate(["one"]), "same"), call("submit_case", candidate(["two"]), "same")
    )
    model = Model([duplicate, response(), response()])
    reviewer = Reviewer(
        store, model, EvidenceGateway(store, PinnedGateway()), task_id=task.id, lease_token=task.lease_token
    )
    assert await reviewer.run("a") == "stalled"
    assert not store.list_candidate_drafts("a")
    assert not store.list(Case)
    assert not store.pending_shared_batches("a")


async def test_explicit_retraction_preserves_history_without_creating_a_case(tmp_path):
    store, task = board(tmp_path)

    def retract(messages):
        draft = json.loads(messages[1].content)["candidate_drafts"][0]
        return response(
            call(
                "retract_candidate_draft",
                {
                    "draft_id": draft["id"],
                    "expected_revision": draft["revision"],
                    "reason": "Premise was incorrect",
                },
                "withdraw",
            )
        )

    model = Model([response(call("submit_case", candidate(["missing"]), "bad")), retract, response(), response()])
    reviewer = Reviewer(
        store, model, EvidenceGateway(store, PinnedGateway()), task_id=task.id, lease_token=task.lease_token
    )
    assert await reviewer.run("a") == "stalled"
    draft = store.list_candidate_drafts("a")[0]
    assert draft["status"] == "retracted" and draft["reason"] == "Premise was incorrect"
    assert draft["original_args"]["evidence_refs"] == ["missing"]
    assert not store.pending_candidate_drafts("a") and not store.list(Case)


async def test_parseable_invalid_candidate_stays_visible_with_all_validation_errors(tmp_path):
    store, task = board(tmp_path)
    incomplete = {"trigger": "original trigger", "behavior": "original behavior", "evidence_refs": ["x", "y"]}
    model = Model([response(call("submit_case", incomplete, "candidate")), response(), response()])
    reviewer = Reviewer(
        store, model, EvidenceGateway(store, PinnedGateway()), task_id=task.id, lease_token=task.lease_token
    )
    assert await reviewer.run("a") == "stalled"
    draft = store.pending_candidate_drafts("a")[0]
    assert draft["original_args"] == incomplete and draft["status"] == "repair_needed"
    errors = json.dumps(draft["errors"])
    assert all(value in errors for value in ("impact", '"x"', '"y"'))
    assert not store.list(Case)


async def test_same_response_read_cannot_make_a_guessed_reference_delivered(tmp_path):
    store, task = board(tmp_path)
    ref = store.put_evidence(
        b"fixed source",
        revision="head",
        side="head",
        kind="source",
        path="a.py",
        metadata={
            "workspace_digest": "head-snapshot",
            "status": "available",
            "tool_name": "read_source",
            "tool_args": {"path": "a.py"},
        },
    )
    model = Model(
        [
            response(
                call("read_evidence", {"ref": ref.id}, "read"), call("submit_case", candidate([ref.id]), "candidate")
            ),
            response(),
            response(),
        ]
    )
    reviewer = Reviewer(
        store, model, EvidenceGateway(store, PinnedGateway()), task_id=task.id, lease_token=task.lease_token
    )
    assert await reviewer.run("a") == "stalled"
    draft = store.pending_candidate_drafts("a")[0]
    assert draft["status"] == "repair_needed" and ref.id in json.dumps(draft["errors"])
    assert not store.list(Case)


async def test_submission_uses_attempt_creation_revision_for_idempotent_replay(tmp_path, monkeypatch):
    store, task = board(tmp_path)
    stage = store.stage_candidate_batch

    def replayed(*args, **kwargs):
        return [{**item, "revision": 1} for item in stage(*args, **kwargs)]

    monkeypatch.setattr(store, "stage_candidate_batch", replayed)
    model = Model([response(call("submit_case", candidate(["missing"]), "candidate")), response(), response()])
    reviewer = Reviewer(
        store, model, EvidenceGateway(store, PinnedGateway()), task_id=task.id, lease_token=task.lease_token
    )
    assert await reviewer.run("a") == "stalled"
    attempt = store.pending_candidate_drafts("a")[0]
    assert attempt["status"] == "repair_needed" and "missing" in json.dumps(attempt["errors"])


async def test_unknown_draft_id_keeps_full_attempt_open(tmp_path):
    store, task = board(tmp_path)
    proposed = {**candidate(["missing-a", "missing-b"]), "draft_id": "invented", "expected_revision": 0}
    model = Model([response(call("submit_case", proposed, "bad-target")), response(), response()])
    reviewer = Reviewer(
        store, model, EvidenceGateway(store, PinnedGateway()), task_id=task.id, lease_token=task.lease_token
    )
    assert await reviewer.run("a") == "stalled"
    attempt = store.pending_candidate_drafts("a")[0]
    assert attempt["original_args"] == proposed and attempt["status"] == "repair_needed"
    assert "invented" in json.dumps(attempt)
    assert not store.list(Case)


async def test_same_batch_conflicting_repairs_preserve_second_attempt(tmp_path):
    store, task = board(tmp_path)

    def conflicting(messages):
        old = json.loads(messages[1].content)["candidate_drafts"][0]
        frontier = store.list_frontier(task.id)
        ref = frontier[0].aliases[0]
        args = {**candidate([ref]), "draft_id": old["id"], "expected_revision": old["revision"]}
        return response(
            call("submit_case", args, "repair-one"),
            call("submit_case", {**args, "impact": "other claim"}, "repair-two"),
        )

    model = Model(
        [
            response(call("read_source", {"path": "a.py"}, "read")),
            response(call("submit_case", candidate(["missing"]), "initial")),
            conflicting,
            response(),
            response(),
        ]
    )
    reviewer = Reviewer(
        store, model, EvidenceGateway(store, PinnedGateway()), task_id=task.id, lease_token=task.lease_token
    )
    assert await reviewer.run("a") == "stalled"
    assert len(store.list(Case)) == 1
    attempts = store.pending_candidate_drafts("a")
    assert len(attempts) == 1 and attempts[0]["original_args"]["impact"] == "other claim"
    assert attempts[0]["status"] == "repair_needed"


async def test_wrong_target_requires_explicit_new_case_without_guessing_existing_case(tmp_path):
    store, task = board(tmp_path)

    def initial(messages):
        ref = returned_ref(messages)
        args = candidate([ref])
        return response(
            call("submit_case", args, "good"),
            call("submit_case", {**args, "draft_id": "invented", "expected_revision": 0}, "bad"),
        )

    def repair(messages):
        pending = json.loads(messages[1].content)["candidate_drafts"]
        attempt = next(item for item in pending if item["origin_call_id"] == "bad")
        assert attempt["requires_resolution"] is True
        return response(
            call(
                "submit_case",
                {
                    **attempt["current_args"],
                    "draft_id": attempt["id"],
                    "expected_revision": attempt["revision"],
                    "candidate_resolution": "new_case",
                },
                "explicit-new",
            )
        )

    model = Model([response(call("read_source", {"path": "a.py"}, "read")), initial, repair, response(), response()])
    reviewer = Reviewer(
        store, model, EvidenceGateway(store, PinnedGateway()), task_id=task.id, lease_token=task.lease_token
    )
    assert await reviewer.run("a") == "stalled"
    assert len(store.list(Case)) == 2
    assert not store.pending_candidate_drafts("a")


async def test_other_scope_target_remains_unchanged_and_attempt_stays_open(tmp_path):
    store, task = board(tmp_path)
    other = store.add(Task("other", "run", "review", "integration", 0))
    other = store.acquire_task(other.id, "integrator", 120)
    other_model = Model(
        [response(call("submit_case", candidate(["other-missing"]), "other-call")), response(), response()]
    )
    other_reviewer = Reviewer(
        store, other_model, EvidenceGateway(store, PinnedGateway()), task_id=other.id, lease_token=other.lease_token
    )
    assert await other_reviewer.run("integration") == "stalled"
    target = store.pending_candidate_drafts("integration")[0]
    args = {**candidate(["local-missing"]), "draft_id": target["id"], "expected_revision": target["revision"]}
    model = Model([response(call("submit_case", args, "wrong-scope")), response(), response()])
    reviewer = Reviewer(
        store, model, EvidenceGateway(store, PinnedGateway()), task_id=task.id, lease_token=task.lease_token
    )
    assert await reviewer.run("a") == "stalled"
    assert store.get_candidate_draft(target["id"]) == target
    pending = store.pending_candidate_drafts("a")
    assert len(pending) == 1 and pending[0]["original_args"] == args
    assert pending[0]["status"] == "repair_needed" and pending[0]["requires_resolution"] is True


async def test_wrong_target_needs_explicit_resolution_then_applies_to_selected_draft(tmp_path):
    store, task = board(tmp_path)

    def initial(messages):
        ref = returned_ref(messages)
        return response(
            call("submit_case", candidate(["missing"]), "target"),
            call("submit_case", {**candidate([ref]), "draft_id": "invented", "expected_revision": 0}, "wrong"),
        )

    def omit_resolution(messages):
        pending = json.loads(messages[1].content)["candidate_drafts"]
        source = next(item for item in pending if item["origin_call_id"] == "wrong")
        return response(
            call(
                "submit_case",
                {**source["current_args"], "draft_id": source["id"], "expected_revision": source["revision"]},
                "omitted",
            )
        )

    def choose_target(messages):
        assert not store.list(Case)
        pending = json.loads(messages[1].content)["candidate_drafts"]
        source = next(item for item in pending if item["origin_call_id"] == "wrong")
        target = next(item for item in pending if item["origin_call_id"] == "target")
        omitted = next(item for item in pending if item["origin_call_id"] == "omitted")
        return response(
            call(
                "submit_case",
                {
                    **source["current_args"],
                    "draft_id": source["id"],
                    "expected_revision": source["revision"],
                    "candidate_resolution": "apply_to_draft",
                    "target_draft_id": target["id"],
                    "target_expected_revision": target["revision"],
                },
                "apply",
            ),
            call(
                "retract_candidate_draft",
                {
                    "draft_id": omitted["id"],
                    "expected_revision": omitted["revision"],
                    "reason": "Duplicate attempt without a resolution",
                },
                "withdraw",
            ),
        )

    model = Model(
        [
            response(call("read_source", {"path": "a.py"}, "read")),
            initial,
            omit_resolution,
            choose_target,
            response(),
            response(),
        ]
    )
    reviewer = Reviewer(
        store, model, EvidenceGateway(store, PinnedGateway()), task_id=task.id, lease_token=task.lease_token
    )
    assert await reviewer.run("a") == "stalled"
    assert len(store.list(Case)) == 1
    assert not store.pending_candidate_drafts("a")


def test_draft_tools_only_exposed_when_enabled():
    old = {tool.name: tool for tool in reviewer_tools(units=True)}
    new = {tool.name: tool for tool in reviewer_tools(units=True, candidate_drafts=True)}
    assert "retract_candidate_draft" not in old and "retract_candidate_draft" in new
    assert "draft_id" not in old["submit_case"].args_schema.model_fields
    assert "draft_id" in new["submit_case"].args_schema.model_fields


def test_reused_native_call_id_is_fenced_by_turn_only_for_new_policy(tmp_path):
    store, task = board(tmp_path)
    reviewer = Reviewer(
        store, object(), EvidenceGateway(store, PinnedGateway()), task_id=task.id, lease_token=task.lease_token
    )
    reviewer._call_id = "same"
    reviewer._turn_id = "first"
    first = reviewer._decision_id()
    assert reviewer._decision_id() == first
    reviewer._turn_id = "second"
    assert reviewer._decision_id() != first
    del store.run.metadata["candidate_draft_policy"]
    legacy = "decision_" + hashlib.sha256(f"{task.id}:same".encode()).hexdigest()
    assert reviewer._decision_id() == legacy
    reviewer._turn_id = "first"
    assert reviewer._decision_id() == legacy
