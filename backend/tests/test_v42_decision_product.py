"""The decision profile is an immutable opt-in at the actual product boundary."""

import json
import sqlite3
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage
from test_v42_decision_loop import step
from test_v42_integration import NativeModel, product, tool
from test_v42_units_product import FixedBaseGitHub, TwoFileWorkspace, two_file_state

from reviewforge.review_v42.contracts import Run
from reviewforge.review_v42.decision_loop import complete_scope_tool, review_step_tool
from reviewforge.review_v42.runtime import ReviewRuntime


def test_native_tool_exposes_nested_completion_contract():
    from langchain_core.utils.function_calling import convert_to_openai_tool

    sent = convert_to_openai_tool(review_step_tool())["function"]
    for fields in (
        "observations:[{statement:string,evidence_refs:[string],region_ids:[string]}]",
        "relations:[{target_scope_id:string,description:string,evidence_refs:[string]}]",
        "integration_relations:[{unit_ids:[string],description:string,evidence_refs:[string]}]",
        "integration_receipts:[{obligation_id:string,disposition:checked|unresolved,evidence_refs:[string],reason:string}]",
    ):
        assert fields in sent["description"]
    scope = convert_to_openai_tool(complete_scope_tool())["function"]["parameters"]
    assert set(scope["required"]) == {"scope_id", "expected_scope_revision", "payload"}
    assert "observations" in scope["properties"]["payload"]["properties"]


@pytest.mark.parametrize(
    "variable,value",
    [
        ("REVIEWFORGE_V42_DECISION_CYCLE", "wrong"),
        ("REVIEWFORGE_V42_SHARED_INVESTIGATION", "wrong"),
        ("REVIEWFORGE_V42_CANDIDATE_DRAFT", "wrong"),
        ("REVIEWFORGE_V42_FRONTIER_PROJECTION", "wrong"),
        ("REVIEWFORGE_V42_QUESTION_ASSISTANT", "question-delivery-1"),
        ("REVIEWFORGE_V42_VERIFICATION_DELIVERY", "wrong"),
    ],
)
async def test_invalid_profile_fails_before_workspace_or_model(tmp_path, monkeypatch, variable, value):
    monkeypatch.setenv("REVIEWFORGE_V42_DECISION_CYCLE", "decision-cycle-1")
    monkeypatch.setenv(variable, value)
    model = NativeModel(lambda *_: pytest.fail("invalid profile cannot call a provider"))
    obj, _ = product(tmp_path, monkeypatch, model, model, TwoFileWorkspace(), policy=None)

    async def forbidden(*_):
        pytest.fail("invalid profile cannot capture a workspace")

    obj._gateway.workspace_for = forbidden
    with pytest.raises(ValueError, match="decision cycle|decision-cycle"):
        await obj.run(two_file_state())
    assert not list(tmp_path.rglob("board.sqlite"))


@pytest.mark.parametrize(
    "key,value",
    [
        ("decision_cycle_policy", None),
        ("decision_cycle_policy", "wrong"),
        ("shared_investigation", None),
        ("candidate_draft_policy", "wrong"),
        ("frontier_projection_policy", None),
        ("source_line_policy", "wrong"),
        ("question_assistant_policy", None),
    ],
)
def test_runtime_rejects_incoherent_profile_without_gateway(key, value):
    metadata = {
        "decision_cycle_policy": "decision-cycle-1",
        "shared_investigation": "shared-1",
        "candidate_draft_policy": "candidate-draft-1",
        "frontier_projection_policy": "source-line-union-1",
        "source_line_policy": "git-lf-1",
        key: value,
    }
    store = SimpleNamespace(run=Run("run", "repo", "base", "head", policy="v42-units-6", metadata=metadata))
    with pytest.raises(ValueError, match="decision cycle"):
        ReviewRuntime(store, None, object(), object())


async def test_single_option_binds_dependencies_and_preserves_old_identity(tmp_path, monkeypatch):
    for variable in (
        "DECISION_CYCLE",
        "SHARED_INVESTIGATION",
        "CANDIDATE_DRAFT",
        "FRONTIER_PROJECTION",
        "QUESTION_ASSISTANT",
        "UNIT_PLAN",
        "UNIT_SCHEDULER",
        "VERIFICATION_DELIVERY",
    ):
        monkeypatch.delenv("REVIEWFORGE_V42_" + variable, raising=False)
    model = NativeModel(lambda *_: AIMessage(content="No decision yet"))
    obj, _ = product(tmp_path, monkeypatch, model, model, TwoFileWorkspace(), policy=None)
    obj._gateway._github = FixedBaseGitHub()
    old = await obj.run(two_file_state())
    monkeypatch.setenv("REVIEWFORGE_V42_DECISION_CYCLE", "decision-cycle-1")
    new = await obj.run(two_file_state())
    metadata = new["review_v42_report"]["run"]["metadata"]
    assert metadata["decision_cycle_policy"] == "decision-cycle-1"
    assert metadata["shared_investigation"] == "shared-1"
    assert metadata["candidate_draft_policy"] == "candidate-draft-1"
    assert metadata["frontier_projection_policy"] == "source-line-union-1"
    assert "unit_plan" not in metadata and "question_assistant_policy" not in metadata
    assert new["run_id"] != old["run_id"]
    assert "decision_cycle_policy" not in old["review_v42_report"]["run"]["metadata"]
    assert (await obj.run(two_file_state()))["run_id"] == new["run_id"]
    monkeypatch.setenv("REVIEWFORGE_V42_VERIFICATION_DELIVERY", "claim-delivery-1")
    delivery = await obj.run(two_file_state())
    assert delivery["review_v42_report"]["run"]["metadata"]["verification_delivery_policy"] == "claim-delivery-1"
    assert delivery["run_id"] not in {old["run_id"], new["run_id"]}
    assert (await obj.run(two_file_state()))["run_id"] == delivery["run_id"]
    monkeypatch.delenv("REVIEWFORGE_V42_DECISION_CYCLE")
    with pytest.raises(ValueError, match="verification delivery requires decision-cycle-1"):
        await obj.run(two_file_state())
    monkeypatch.delenv("REVIEWFORGE_V42_VERIFICATION_DELIVERY")
    assert (await obj.run(two_file_state()))["run_id"] == old["run_id"]


@pytest.mark.parametrize("candidate", [False, True])
@pytest.mark.parametrize("bad_source_args", [False, True])
async def test_real_product_checks_two_units_and_independent_integration(
    tmp_path, monkeypatch, candidate, bad_source_args
):
    monkeypatch.setenv("REVIEWFORGE_V42_DECISION_CYCLE", "decision-cycle-1")
    phases, refs = defaultdict(int), {}
    visited = set()
    rejected_completion = set()
    premature_scopes = set()
    rejected_retraction = set()

    def review(messages, _turn):
        board = json.loads(messages[1].content)
        scope, role = board["scope"], board["unit_role"]
        key = scope["id"]
        phase = phases[key]
        phases[key] += 1
        visited.add((key, role))
        item = board["decision_items"][-1] if board["decision_items"] else None
        offer = candidate and role == "primary" and scope["regions"][0]["path"] == "a.py"
        assert not any(q["owner"] == "tool_controller" for q in board["questions"])
        for message in messages:
            if message.type == "tool":
                receipt = json.loads(message.content)
                if (receipt.get("errors") or [{}])[0].get("reason") == "revision_changed":
                    assert key not in rejected_completion
                    assert receipt["errors"][0]["current_revision"] == scope["revision"]
                    assert "work_item_id" not in receipt
                    assert len(board["decision_items"]) == 1
                    rejected_completion.add(key)
                elif (receipt.get("errors") or [{}])[0].get("reason") == "open_work_items":
                    blockers = {entry["reason"]: entry["ids"] for entry in receipt["errors"]}
                    assert blockers == {
                        "open_work_items": [item["id"]],
                        "open_questions": [item["question_id"]],
                        "unchecked_regions": [region["id"] for region in scope["regions"]],
                    }
                    assert len(board["decision_items"]) == 1
                    assert receipt["scope_revision"] == scope["revision"]
                    premature_scopes.add(key)
                elif (receipt.get("errors") or [{}])[0].get("reason") == "draft_revision_changed":
                    (draft,) = board["candidate_drafts"]
                    assert receipt["draft_id"] == draft["draft_id"]
                    assert receipt["draft_revision"] == draft["draft_revision"]
                    assert receipt["draft_status"] == draft["status"] == "repair_needed"
                    assert receipt["work_item_revision"] == item["work_item_revision"] != receipt["draft_revision"]
                    assert receipt["errors"][0]["field"] == "payload.retract_draft.draft_revision"
                    rejected_retraction.add(key)
                elif offer and phase == 5 + int(bad_source_args) and "error" in receipt:
                    assert receipt["status"] == "repair_needed"
                else:
                    assert "error" not in receipt, receipt
                for result in receipt.get("results", []):
                    value = result["result"]
                    if "error" in value:
                        assert bad_source_args and phase == 1, value
                        assert "InvalidEvidenceRequestError" in value["error"]
                        assert "question_id" not in value
                    if "evidence_ref" in value:
                        refs.setdefault(key, {})[value["path"]] = value["evidence_ref"]
        if bad_source_args and phase == 0:
            return tool(
                "review_step",
                step(
                    "request_evidence",
                    {
                        "gap": "Caller references",
                        "relevance": "Trace impact",
                        "alternatives": "Used or unused",
                        "operations": [{"name": "find_references", "args": {"query": "value"}}],
                    },
                ),
            )
        if bad_source_args:
            phase -= 1
        if key in premature_scopes:
            phase -= 1
        if not candidate and not bad_source_args and role == "primary" and phase == 1 and key not in premature_scopes:
            return tool(
                "complete_scope", {"scope_id": key, "expected_scope_revision": scope["revision"], "payload": {}}
            )
        if phase == 0:
            paths = ["a.py", "b.py"] if role == "integration" else [scope["regions"][0]["path"]]
            payload = {
                "gap": "Original behavior",
                "relevance": "Check changed units and consumers",
                "alternatives": "Confirm regression or refute premise",
                "operations": [{"name": "read_source", "args": {"path": p}} for p in paths],
            }
            return tool("review_step", step("request_evidence", payload, item))
        own_refs = list(refs[key].values())
        if offer and phase in {2, 3, 4}:
            assert item["status"] == "answered"
            if phase == 3:
                receipt = json.loads(messages[-1].content)
                assert receipt["case_changed"] is True
                assert item["case_ids"] == [receipt["case_id"]]
            if phase == 4:
                assert json.loads(messages[-1].content)["case_changed"] is False
            return tool(
                "review_step",
                step(
                    "offer_candidate",
                    {
                        "candidate": {
                            "trigger": "Import the value",
                            "behavior": "The imported value is absent" if phase < 4 else "The value is missing",
                            "impact": "Consumers cannot import it",
                            "evidence_refs": own_refs,
                        }
                    },
                    item,
                    own_refs,
                ),
            )
        if phase == 1:
            return tool(
                "review_step",
                step(
                    "close_question",
                    {
                        "question_id": item["question_id"],
                        "answer": "Inspected the original definition and usage",
                        "evidence_refs": own_refs,
                    },
                    item,
                    own_refs,
                ),
            )
        if offer and phase in {5, 6}:
            receipt = json.loads(messages[-1].content)
            if phase == 5:
                assert receipt["requires_resolution"]
                assert receipt["errors"][0]["case_ids"] == item["case_ids"]
            else:
                assert key in rejected_retraction
            return tool(
                "review_step",
                step(
                    "offer_candidate",
                    {
                        "retract_draft": {
                            "draft_id": receipt["draft_id"],
                            "draft_revision": item["work_item_revision"] if phase == 5 else receipt["draft_revision"],
                            "reason": "This is the same finding with different wording",
                        }
                    },
                    item,
                    own_refs,
                ),
            )
        if offer and phase == 7:
            assert item["status"] == "answered"
            assert json.loads(messages[-1].content)["status"] == "retracted"
        if role == "integration":
            obligations = board["integration_obligations"]
            assert obligations and any(len(o["unit_ids"]) == 2 for o in obligations)
            payload = {
                "integration_receipts": [
                    {
                        "obligation_id": o["id"],
                        "disposition": "checked",
                        "evidence_refs": own_refs,
                        "reason": "Compared the definition with the consuming import",
                    }
                    for o in obligations
                ],
                "finish": True,
            }
        else:
            ids = [r["id"] for r in scope["regions"]]
            payload = {
                "checked_region_ids": ids,
                "observations": [
                    {"statement": "Checked fixed original source", "region_ids": ids, "evidence_refs": own_refs}
                ],
                "unresolved_refs": [],
                "finish": True,
            }
        return tool(
            "complete_scope",
            {
                "scope_id": key,
                "expected_scope_revision": scope["revision"] + int(key not in rejected_completion),
                "payload": payload,
            },
        )

    def verify(messages, _turn):
        packet = json.loads(next(m.content for m in messages if m.type == "human"))
        assert "decision_items" not in packet
        reference = next(e["ref"]["id"] for e in packet["evidence"] if e["ref"]["path"] == "a.py")
        return tool(
            "submit_verdict",
            {
                "verdict": "reject",
                "reason": "The claimed missing definition exists",
                "gaps": [],
                "facts": {
                    "counterexample": {
                        "statement": "The value is defined; the missing definition premise is false",
                        "citations": [{"ref": reference, "quote": "value = 0", "start_line": 1, "end_line": 1}],
                    }
                },
            },
        )

    reviewer, verifier = NativeModel(review), NativeModel(verify)
    obj, _ = product(tmp_path, monkeypatch, reviewer, verifier, TwoFileWorkspace(), policy=None)
    obj._gateway._github = FixedBaseGitHub()
    result = await obj.run(two_file_state())
    report = result["review_v42_report"]
    assert result["status"] == "completed", report["runtime"]
    assert len([v for v in visited if v[1] == "primary"]) == 2
    assert len([v for v in visited if v[1] == "integration"]) == 1
    assert report["metrics"]["coverage"]["checked_regions"] == 2
    assert rejected_completion == {key for key, _role in visited}
    assert len(rejected_retraction) == int(candidate)
    assert result["comments_sent"] == 0 and result["local_only"]
    assert len(verifier._inputs) == int(candidate)
    if candidate:
        assert report["verified"][0]["verdict"] == "reject"
    with sqlite3.connect(Path(result["report_path"]).parent / "board.sqlite") as db:
        assert db.execute("SELECT count(*) FROM decision_verification_queue").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM decision_items").fetchone()[0] == 3
        completions = [
            json.loads(payload) for (payload,) in db.execute("SELECT payload FROM decision_steps WHERE item_id=''")
        ]
        assert {
            step["scope_id"] for step in completions if step.get("disposition") == "reviewed"
        } == rejected_completion
        assert {step["scope_id"] for step in completions if step["status"] == "pending"} == premature_scopes
    calls = len(reviewer._inputs), len(verifier._inputs)
    again = await obj.run(two_file_state())
    assert again["status"] == "completed" and again["run_id"] == result["run_id"]
    assert (len(reviewer._inputs), len(verifier._inputs)) == calls
