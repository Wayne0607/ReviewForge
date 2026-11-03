"""Candidate-draft policy is explicitly bound to a new product run."""

import asyncio
import json

import pytest
from langchain_core.messages import AIMessage
from test_v42_fair_runtime import PinnedGateway, finish_unit, receipts
from test_v42_integration import NativeModel, product
from test_v42_question_runtime import Verifier
from test_v42_stateful_reviewer import call
from test_v42_units_product import FixedBaseGitHub, TwoFileWorkspace, two_file_state

from reviewforge.review_v42.contracts import Case, Run
from reviewforge.review_v42.runtime import ReviewRuntime
from reviewforge.review_v42.shared_investigation import SharedInvestigations
from reviewforge.review_v42.store import ReviewStore


@pytest.mark.parametrize("value,shared", [("invalid", "shared-1"), ("candidate-draft-1", "")])
async def test_invalid_draft_option_fails_before_provider_call(tmp_path, monkeypatch, value, shared):
    monkeypatch.setenv("REVIEWFORGE_V42_CANDIDATE_DRAFT", value)
    if shared:
        monkeypatch.setenv("REVIEWFORGE_V42_SHARED_INVESTIGATION", shared)
    else:
        monkeypatch.delenv("REVIEWFORGE_V42_SHARED_INVESTIGATION", raising=False)
    model = NativeModel(lambda *_: pytest.fail("invalid configuration cannot call a provider"))
    obj, _ = product(tmp_path, monkeypatch, model, model, TwoFileWorkspace(), policy=None)
    with pytest.raises(ValueError, match="unsupported v4.2 candidate draft policy"):
        await obj.run(two_file_state())
    assert not model._inputs


async def test_opt_in_is_bound_to_new_identity_and_old_run_is_not_upgraded(tmp_path, monkeypatch):
    monkeypatch.setenv("REVIEWFORGE_V42_SHARED_INVESTIGATION", "shared-1")
    monkeypatch.delenv("REVIEWFORGE_V42_CANDIDATE_DRAFT", raising=False)
    model = NativeModel(lambda *_: AIMessage(content="No decision yet"))
    obj, _ = product(tmp_path, monkeypatch, model, model, TwoFileWorkspace(), policy=None)
    obj._gateway._github = FixedBaseGitHub()
    old = await obj.run(two_file_state())
    assert "candidate_draft_policy" not in old["review_v42_report"]["run"]["metadata"]
    monkeypatch.setenv("REVIEWFORGE_V42_CANDIDATE_DRAFT", "candidate-draft-1")
    new = await obj.run(two_file_state())
    assert new["run_id"] != old["run_id"]
    assert new["review_v42_report"]["run"]["metadata"]["candidate_draft_policy"] == "candidate-draft-1"
    assert (await obj.run(two_file_state()))["run_id"] == new["run_id"]


async def test_candidate_batch_survives_partial_and_resumes_with_independent_verification(tmp_path):
    region = {"id": "one", "path": "a.py", "side": "head", "start_line": 1, "end_line": 1, "changed_lines": [1]}
    run = Run(
        "r",
        "repo",
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
    store = ReviewStore(tmp_path / "board.db", run)
    SharedInvestigations(store)

    class CandidateModel:
        ref = None
        repair = False
        observed = []

        def bind_tools(self, tools, **kwargs):
            return self

        async def ainvoke(self, messages):
            board = json.loads(messages[1].content)
            tool_messages = [json.loads(message.content) for message in messages if message.type == "tool"]
            for item in tool_messages:
                if item.get("evidence_ref"):
                    self.ref = item["evidence_ref"]
            if board["unit_role"] == "integration":
                if not tool_messages or not any(item.get("evidence_ref") for item in tool_messages):
                    return AIMessage(content="", tool_calls=[call("read_source", {"path": "a.py"}, "integrate-read")])
                refs = [item["evidence_ref"] for item in tool_messages if item.get("evidence_ref")]
                return AIMessage(
                    content="",
                    tool_calls=[
                        *receipts(board["integration_obligations"], refs),
                        call("finish_integration", {}, "integrate-finish"),
                    ],
                )
            drafts = board.get("candidate_drafts", [])
            pending = [item for item in drafts if item.get("status") in {"pending_validation", "repair_needed"}]
            if not self.ref:
                return AIMessage(content="", tool_calls=[call("read_source", {"path": "a.py"}, "read")])
            if not store.list(Case) and not drafts:
                calls = []
                for index in range(4):
                    args = {
                        "trigger": f"trigger {index}",
                        "behavior": f"behavior {index}",
                        "impact": "impact",
                        "evidence_refs": [self.ref] if index == 0 else [f"missing-{index}"],
                    }
                    calls.append(call("submit_case", args, f"case-{index}"))
                return AIMessage(content="", tool_calls=calls)
            if pending:
                self.observed.append([(item["id"], item["status"]) for item in pending])
                if self.repair:
                    corrections = []
                    for index, item in enumerate(pending):
                        if item["origin_call_id"] == "case-1":
                            corrections.append(
                                call(
                                    "submit_case",
                                    {
                                        **item["current_args"],
                                        "evidence_refs": [self.ref],
                                        "draft_id": item["id"],
                                        "expected_revision": item["revision"],
                                    },
                                    "correct-case",
                                )
                            )
                        else:
                            corrections.append(
                                call(
                                    "retract_candidate_draft",
                                    {
                                        "draft_id": item["id"],
                                        "expected_revision": item["revision"],
                                        "reason": "Duplicate of reviewed behavior",
                                    },
                                    f"retract-{index}",
                                )
                            )
                    return AIMessage(content="", tool_calls=corrections)
            return AIMessage(content="", tool_calls=[finish_unit("one", self.ref)])

    class RecordingVerifier(Verifier):
        seen = []

        async def verify(self, case_id, **kwargs):
            self.seen.append(case_id)
            return await super().verify(case_id, **kwargs)

    model, verifier = CandidateModel(), RecordingVerifier(store)
    runtime = ReviewRuntime(store, model, PinnedGateway(), verifier)
    first = await asyncio.wait_for(runtime.run([region]), 15)
    assert first["status"] == "partial", first
    assert len(store.list(Case)) == 1
    assert len(store.pending_candidate_drafts()) == 3
    assert model.observed and len(model.observed[0]) == 3
    assert verifier.revisions
    model.repair = True
    resumed = await asyncio.wait_for(runtime.run([region]), 15)
    assert resumed["status"] == "completed", json.dumps(resumed, ensure_ascii=False)
    assert store.pending_candidate_drafts() == []
    assert len(store.list(Case)) == 2
    assert set(verifier.seen) == {case.id for case in store.list(Case)}


async def test_promoted_before_receipt_recovers_verifier_but_keeps_batch_partial(tmp_path, monkeypatch):
    region = {"id": "one", "path": "a.py", "side": "head", "start_line": 1, "end_line": 1, "changed_lines": [1]}
    run = Run(
        "r",
        "repo",
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
    store = ReviewStore(tmp_path / "board.db", run)
    SharedInvestigations(store)
    append = store.append_turn_audit

    def crash_before_receipt(task_id, token, turn_id, phase, messages, **kwargs):
        if phase.endswith(":candidate"):
            raise OSError("lost process before the tool receipt")
        return append(task_id, token, turn_id, phase, messages, **kwargs)

    class Discover:
        def bind_tools(self, tools, **kwargs):
            return self

        async def ainvoke(self, messages):
            tool = next((json.loads(message.content) for message in messages if message.type == "tool"), None)
            if not tool:
                return AIMessage(content="", tool_calls=[call("read_source", {"path": "a.py"}, "read")])
            return AIMessage(
                content="",
                tool_calls=[
                    call(
                        "submit_case",
                        {
                            "trigger": "trigger",
                            "behavior": "behavior",
                            "impact": "impact",
                            "evidence_refs": [tool["evidence_ref"]],
                        },
                        "candidate",
                    )
                ],
            )

    class UnavailableVerifier:
        policy = "v42-independent-1"

        async def verify(self, *args, **kwargs):
            raise RuntimeError("simulated verifier outage")

    monkeypatch.setattr(store, "append_turn_audit", crash_before_receipt)
    first = await asyncio.wait_for(
        ReviewRuntime(store, Discover(), PinnedGateway(), UnavailableVerifier()).run([region]), 15
    )
    assert first["status"] == "partial" and len(store.list(Case)) == 1
    monkeypatch.setattr(store, "append_turn_audit", append)

    class NoReplay:
        def bind_tools(self, tools, **kwargs):
            return self

        async def ainvoke(self, messages):
            pytest.fail("unfinished native submission must not be replayed")

    verifier = Verifier(store)
    resumed = await asyncio.wait_for(ReviewRuntime(store, NoReplay(), PinnedGateway(), verifier).run([region]), 15)
    assert resumed["status"] == "partial" and verifier.revisions
    assert len(store.list(Case)) == 1
    scope_id = store.list(Case)[0].scope_id
    assert store.pending_shared_batches(scope_id)
