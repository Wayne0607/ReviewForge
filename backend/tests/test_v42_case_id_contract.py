import asyncio
import json

import pytest
from langchain_core.messages import AIMessage
from test_v42_stateful_reviewer import Gateway, call

from reviewforge.review_v42.contracts import Case, Question, Run
from reviewforge.review_v42.reviewer import Reviewer
from reviewforge.review_v42.runtime import ReviewRuntime
from reviewforge.review_v42.scope import compile_scopes
from reviewforge.review_v42.store import ReviewStore
from reviewforge.review_v42.tools import EvidenceGateway


def setup_run(tmp_path):
    regions = [{"id": "one", "path": "a.py", "side": "head", "start_line": 1, "end_line": 1, "changed_lines": [1]}]
    run = Run(
        id="r",
        repo="a/b",
        base="base",
        head="head",
        policy="v42-units-6",
        metadata={
            "changed_regions": regions,
            "workspace_digest": "head-snapshot",
            "base_workspace_digest": "base-snapshot",
        },
    )
    return ReviewStore(tmp_path / "board.db", run), regions


class PinnedGateway(Gateway):
    async def __call__(self, name, args):
        result = await super().__call__(name, args)
        result["metadata"] = {"workspace_digest": "head-snapshot", "status": "available"}
        return result


def finish_unit(ref):
    return call(
        "submit_unit_decision",
        {
            "checked_region_ids": ["one"],
            "observations": [{"statement": "Inspected source", "evidence_refs": [ref], "region_ids": ["one"]}],
            "unresolved_refs": [],
            "finish": True,
        },
        "finish-unit",
    )


def finish_integration(board, ref):
    calls = [
        call(
            "record_integration_receipt",
            {
                "obligation_id": row["id"],
                "disposition": "checked",
                "evidence_refs": [ref],
                "reason": "Compared original source",
            },
            row["id"],
        )
        for row in board["integration_obligations"]
    ]
    return AIMessage(content="", tool_calls=[*calls, call("finish_integration", {}, "finish-integration")])


async def test_invented_case_id_feedback_then_source_bound_case_reaches_verifier(tmp_path):
    store, regions = setup_run(tmp_path)
    verified = asyncio.Event()

    class Verifier:
        policy = "v42-independent-1"

        async def verify(self, case_id, *, task_id, lease_token):
            case = store.get(Case, case_id)
            assert case.evidence_refs == [model.ref]
            result = store.save_verdict(
                case_id,
                case.claims[0]["id"],
                case.revision,
                self.policy,
                "reject",
                case.evidence_refs,
                {"reason": "counterexample"},
                task_id=task_id,
                lease_token=lease_token,
            )
            verified.set()
            return [result]

    class Model:
        def __init__(self):
            self.step = 0

        def bind_tools(self, tools, **kwargs):
            specs = {tool["function"]["name"]: tool["function"] for tool in tools}
            desc = specs["submit_case"]["parameters"]["properties"]["case_id"]["description"]
            assert "Omit for a new case" in desc and "returned ID" in desc
            return self

        async def ainvoke(self, messages):
            board = json.loads(messages[1].content)
            assert "omit case_id or question_id" in messages[0].content
            previous = [json.loads(m.content) for m in messages if m.type == "tool"]
            if board["unit_role"] == "integration":
                if not previous:
                    return AIMessage(content="", tool_calls=[call("read_source", {"path": "a.py"}, "integration-read")])
                return finish_integration(board, previous[0]["evidence_ref"])
            self.step += 1
            if self.step == 1:
                return AIMessage(content="", tool_calls=[call("read_source", {"path": "a.py"}, "read")])
            if self.step == 2:
                self.ref = previous[0]["evidence_ref"]
                args = {
                    "case_id": "case_auth_admin_and_owner",
                    "trigger": "trigger",
                    "behavior": "mechanism",
                    "impact": "impact",
                    "evidence_refs": [self.ref],
                }
                return AIMessage(content="", tool_calls=[call("submit_case", args, "bad-case")])
            if self.step == 3:
                assert "Unknown case_id" in previous[0]["error"] and "Omit case_id" in previous[0]["error"]
                assert not store.list(Case)
                args = {"trigger": "trigger", "behavior": "mechanism", "impact": "impact", "evidence_refs": [self.ref]}
                return AIMessage(content="", tool_calls=[call("submit_case", args, "new-case")])
            if self.step == 4:
                self.case_id = previous[0]["case_id"]
                assert self.case_id != "case_auth_admin_and_owner"
                return AIMessage(
                    content="",
                    tool_calls=[
                        call(
                            "submit_case",
                            {
                                "case_id": self.case_id,
                                "trigger": "trigger",
                                "behavior": "mechanism",
                                "impact": "impact",
                                "evidence_refs": [self.ref],
                            },
                            "update-case",
                        )
                    ],
                )
            assert previous[0]["case_id"] == self.case_id
            await asyncio.wait_for(verified.wait(), 3)
            return AIMessage(content="", tool_calls=[finish_unit(self.ref)])

    model = Model()
    result = await asyncio.wait_for(ReviewRuntime(store, model, PinnedGateway(), Verifier()).run(regions), 6)
    assert result["status"] == "completed", result
    assert len(store.list(Case)) == 1 and verified.is_set()


async def test_invented_question_id_feedback_then_create_and_update_with_returned_id(tmp_path):
    store, regions = setup_run(tmp_path)

    class Model:
        def __init__(self):
            self.step = 0

        def bind_tools(self, tools, **kwargs):
            specs = {tool["function"]["name"]: tool["function"] for tool in tools}
            assert (
                "Omit for a new question"
                in specs["save_question"]["parameters"]["properties"]["question_id"]["description"]
            )
            return self

        async def ainvoke(self, messages):
            board = json.loads(messages[1].content)
            previous = [json.loads(m.content) for m in messages if m.type == "tool"]
            if board["unit_role"] == "integration":
                if not previous:
                    return AIMessage(content="", tool_calls=[call("read_source", {"path": "a.py"}, "integration-read")])
                return finish_integration(board, previous[0]["evidence_ref"])
            self.step += 1
            if self.step == 1:
                return AIMessage(content="", tool_calls=[call("read_source", {"path": "a.py"}, "read")])
            if self.step == 2:
                self.ref = previous[0]["evidence_ref"]
                return AIMessage(
                    content="",
                    tool_calls=[
                        call(
                            "save_question",
                            {"question_id": "q_invented", "statement": "What does the source show?"},
                            "bad-question",
                        )
                    ],
                )
            if self.step == 3:
                assert "Unknown question_id" in previous[0]["error"] and "Omit question_id" in previous[0]["error"]
                assert not store.list(Question)
                return AIMessage(
                    content="",
                    tool_calls=[call("save_question", {"statement": "What does the source show?"}, "new-question")],
                )
            if self.step == 4:
                self.question_id = previous[0]["question_id"]
                assert self.question_id != "q_invented"
                return AIMessage(
                    content="",
                    tool_calls=[
                        call(
                            "save_question",
                            {
                                "question_id": self.question_id,
                                "statement": "What does the source show?",
                                "status": "answered",
                                "answer": "The source shows the relevant behavior.",
                                "evidence_refs": [self.ref],
                            },
                            "answer-question",
                        )
                    ],
                )
            assert previous[0]["question_id"] == self.question_id
            return AIMessage(content="", tool_calls=[finish_unit(self.ref)])

    result = await asyncio.wait_for(ReviewRuntime(store, Model(), PinnedGateway(), object()).run(regions), 6)
    assert result["status"] == "completed", result
    questions = store.list(Question)
    assert len(questions) == 1 and questions[0].status == "answered"


def test_legacy_unknown_ids_still_raise_keyerror(tmp_path):
    run = Run(id="legacy", repo="a/b", base="base", head="head", policy="v42-frontier-4")
    store = ReviewStore(tmp_path / "board.db", run)
    scope = compile_scopes(run, [{"id": "one", "path": "a.py"}])[0]
    store.add(scope)
    ref = store.put_evidence(b"source", revision="head", side="head", kind="source", path="a.py")
    reviewer = Reviewer(store, object(), EvidenceGateway(store, PinnedGateway()))
    with pytest.raises(KeyError):
        reviewer._case(
            scope, {"case_id": "unknown", "trigger": "x", "behavior": "y", "impact": "z", "evidence_refs": [ref.id]}
        )
    with pytest.raises(KeyError):
        reviewer._question(scope, {"question_id": "unknown", "statement": "why?"})
