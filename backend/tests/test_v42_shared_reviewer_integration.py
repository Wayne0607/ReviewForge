"""Exercise shared investigation tools through the real reviewer and runtime."""

import asyncio
import json

import pytest
from langchain_core.messages import AIMessage
from test_v42_fair_runtime import PinnedGateway, finish_unit, receipts
from test_v42_stateful_reviewer import call

from reviewforge.review_v42.contracts import ReviewScope, Run
from reviewforge.review_v42.reviewer import reviewer_tools
from reviewforge.review_v42.runtime import ReviewRuntime
from reviewforge.review_v42.store import ReviewStore


def test_shared_tools_are_explicitly_opt_in():
    old = {tool.name for tool in reviewer_tools(units=True)}
    new = {tool.name for tool in reviewer_tools(units=True, shared_investigation=True)}
    assert "create_shared_investigation" not in old
    assert {
        "create_shared_investigation",
        "claim_shared_investigation",
        "update_shared_investigation",
        "query_shared_investigations",
        "query_shared_cases",
        "register_case_counterevidence",
    } <= new
    with pytest.raises(ValueError, match="require unit review"):
        reviewer_tools(shared_investigation=True)


async def test_shared_investigation_is_created_claimed_resolved_and_reviewed(tmp_path):
    regions = [{"id": "a", "path": "a.py", "side": "head", "start_line": 1, "end_line": 1, "changed_lines": [1]}]
    run = Run(
        id="shared-run",
        repo="a/b",
        base="base",
        head="head",
        policy="v42-units-6",
        metadata={
            "changed_regions": regions,
            "workspace_digest": "head-snapshot",
            "base_workspace_digest": "base-snapshot",
            "unit_plan": "single-owner-1",
            "shared_investigation": "shared-1",
        },
    )
    store = ReviewStore(tmp_path / "board.sqlite", run)

    class Model:
        def __init__(self):
            self.primary_step = 0
            self.integration_step = 0
            self.item = None
            self.ref = None
            self.saw_preview = False

        def bind_tools(self, tools, **kwargs):
            names = {item["function"]["name"] if isinstance(item, dict) else item.name for item in tools}
            assert {"create_shared_investigation", "query_shared_cases"} <= names
            return self

        async def ainvoke(self, messages):
            board = json.loads(messages[1].content)
            previous = [json.loads(message.content) for message in messages if message.type == "tool"]
            if board["unit_role"] == "integration":
                self.integration_step += 1
                if self.integration_step == 1:
                    return AIMessage(content="", tool_calls=[call("read_source", {"path": "a.py"}, "integration-read")])
                ref = next(item["evidence_ref"] for item in previous if "evidence_ref" in item)
                return AIMessage(
                    content="",
                    tool_calls=[
                        *receipts(board["integration_obligations"], [ref]),
                        call("finish_integration", {}, "end"),
                    ],
                )

            self.primary_step += 1
            if self.primary_step == 1:
                return AIMessage(
                    content="",
                    tool_calls=[
                        call(
                            "create_shared_investigation",
                            {
                                "question": "Does the changed behavior satisfy its contract?",
                                "scope_ids": [board["scope"]["id"]],
                                "region_ids": ["a"],
                            },
                            "create",
                        ),
                        call("read_source", {"path": "a.py"}, "source"),
                    ],
                )
            if self.primary_step == 2:
                self.item = next(item for item in previous if item.get("type") == "shared_investigation")
                self.ref = next(item["evidence_ref"] for item in previous if "evidence_ref" in item)
                preview = board["shared_investigations"]
                self.saw_preview = preview["total"] == 1 and preview["trust"] == "untrusted_navigation"
                return AIMessage(
                    content="",
                    tool_calls=[
                        call(
                            "claim_shared_investigation",
                            {
                                "investigation_id": self.item["id"],
                                "expected_revision": self.item["revision"],
                                "reason": "Investigate",
                            },
                            "claim",
                        )
                    ],
                )
            if self.primary_step == 3:
                self.item = next(item for item in previous if item.get("type") == "shared_investigation")
                return AIMessage(
                    content="",
                    tool_calls=[
                        call(
                            "update_shared_investigation",
                            {
                                "investigation_id": self.item["id"],
                                "expected_revision": self.item["revision"],
                                "question": "Does the changed behavior satisfy its contract?",
                                "observations": [{"statement": "Read the pinned source", "evidence_refs": [self.ref]}],
                                "open_questions": [],
                                "status": "resolved",
                                "reason": "The remaining question was investigated",
                            },
                            "resolve",
                        )
                    ],
                )
            assert self.primary_step == 4
            assert any(item.get("status") == "resolved" for item in previous)
            return AIMessage(content="", tool_calls=[finish_unit("a", self.ref)])

    class BoundedGateway(PinnedGateway):
        async def __call__(self, name, args):
            result = await super().__call__(name, args)
            result.update(start_line=1, end_line=1)
            return result

    model = Model()
    result = await asyncio.wait_for(ReviewRuntime(store, model, BoundedGateway(), object()).run(regions), 10)
    assert result["status"] == "completed", result
    assert model.saw_preview and model.primary_step == 4
    assert all(scope.status == "reviewed" for scope in store.list(ReviewScope))
