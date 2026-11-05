"""Validate the same custom source schemas the decision model receives."""

import json

import pytest
from langchain_core.messages import AIMessage
from test_v42_candidate_draft_reviewer import Model
from test_v42_decision_loop import decision_store, from_board, step
from test_v42_stateful_reviewer import call

from reviewforge.review_v42.contracts import BindingError, Question
from reviewforge.review_v42.decision_loop import DecisionReviewer
from reviewforge.review_v42.tools import EvidenceGateway, InvalidEvidenceRequestError


class Gateway:
    def __init__(self, schema, *, error=None):
        self.tool_specs = [
            {
                "type": "function",
                "function": {"name": "find_references", "description": "Find references", "parameters": schema},
            }
        ]
        self.calls = []
        self.error = error

    async def __call__(self, name, args):
        self.calls.append((name, args))
        if self.error:
            raise self.error
        return {
            "content": "reference to " + args["symbol"],
            "revision": "head",
            "side": "head",
            "kind": "search",
            "metadata": {"workspace_digest": "head-snapshot", "status": "available", "navigation_only": True},
        }


SCHEMA = {
    "type": "object",
    "properties": {
        "symbol": {"type": "string"},
        "side": {"type": "string", "enum": ["base", "head"]},
        "offset": {"type": "integer", "minimum": 0},
        "filters": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["symbol"],
    "additionalProperties": False,
}


@pytest.mark.parametrize(
    "args",
    [
        {"query": "idle_deadline"},
        {"symbol": "idle_deadline", "unexpected": True},
        {"symbol": 123},
        {"symbol": "x", "side": "unrelated"},
        {"symbol": "x", "offset": -1},
        {"symbol": "x", "offset": True},
        {"symbol": "x", "filters": ["valid", 42]},
    ],
)
async def test_custom_schema_rejects_arguments_without_invoking_gateway(args):
    gateway = Gateway(SCHEMA)
    reviewer = DecisionReviewer(None, None, gateway)
    with pytest.raises(InvalidEvidenceRequestError):
        await reviewer._read_operation({"name": "find_references", "args": args}, reviewer._operations())
    assert not gateway.calls


async def test_nested_json_schema_reference_is_enforced():
    schema = {
        "$defs": {"filter": {"type": "string", "enum": ["a", "b"]}},
        "type": "object",
        "properties": {"symbol": {"type": "string"}, "filters": {"type": "array", "items": {"$ref": "#/$defs/filter"}}},
        "required": ["symbol"],
        "additionalProperties": False,
    }
    gateway = Gateway(schema)
    reviewer = DecisionReviewer(None, None, gateway)
    with pytest.raises(InvalidEvidenceRequestError):
        await reviewer._read_operation(
            {"name": "find_references", "args": {"symbol": "x", "filters": ["c"]}}, reviewer._operations()
        )
    assert not gateway.calls


async def test_invalid_schema_is_not_reported_as_bad_model_arguments():
    gateway = Gateway({"type": "unsupported"})
    reviewer = DecisionReviewer(None, None, gateway)
    with pytest.raises(BindingError):
        await reviewer._read_operation({"name": "find_references", "args": {"symbol": "x"}}, reviewer._operations())
    assert not gateway.calls


@pytest.mark.parametrize(
    "args",
    [
        {"ref": "r", "section": "matches", "offset": "0", "limit": 1},
        {"ref": "r", "section": "unknown", "offset": 0, "limit": 1},
        {"ref": "r", "section": "matches", "offset": -1, "limit": 1},
    ],
)
async def test_builtin_search_section_errors_are_correctable(args):
    gateway = Gateway(SCHEMA)
    reviewer = DecisionReviewer(None, None, gateway)
    with pytest.raises(InvalidEvidenceRequestError):
        await reviewer._read_operation({"name": "read_search_section", "args": args}, reviewer._operations())
    assert not gateway.calls


def request(args, item=None):
    return step(
        "request_evidence",
        {
            "gap": "references",
            "relevance": "callers",
            "alternatives": "confirm or refute",
            "operations": [{"name": "find_references", "args": args}],
        },
        item,
    )


def stop(item):
    payload = {"missing_fact": "contract", "attempted": "references", "recovery_condition": "contract available"}
    return AIMessage(content="", tool_calls=[call("review_step", step("unresolved", payload, item), "stop")])


async def test_incorrect_custom_argument_is_corrected_without_tool_failure(tmp_path):
    store, task = decision_store(tmp_path)
    gateway = Gateway(SCHEMA)
    first = AIMessage(content="", tool_calls=[call("review_step", request({"query": "idle_deadline"}), "bad")])

    def correct(messages):
        result = json.loads(messages[-1].content)["results"][0]["result"]
        assert "InvalidEvidenceRequestError" in result["error"]
        assert "question_id" not in result
        assert not [q for q in store.list(Question) if q.owner == "tool_controller"]
        return AIMessage(
            content="",
            tool_calls=[call("review_step", request({"symbol": "idle_deadline"}, from_board(messages)), "correct")],
        )

    def check(messages):
        result = json.loads(messages[-1].content)["results"][0]["result"]
        assert result["content"] == "reference to idle_deadline"
        return stop(from_board(messages))

    reviewer = DecisionReviewer(
        store,
        Model([first, correct, check]),
        EvidenceGateway(store, gateway),
        task_id=task.id,
        lease_token=task.lease_token,
    )
    assert await reviewer.run("a") == "partial", reviewer.error
    assert gateway.calls == [("find_references", {"symbol": "idle_deadline"})]
    assert not [q for q in store.list(Question) if q.owner == "tool_controller"]


async def test_actual_gateway_failure_remains_unresolved(tmp_path):
    store, task = decision_store(tmp_path)
    gateway = Gateway(SCHEMA, error=OSError("source unavailable"))
    first = AIMessage(content="", tool_calls=[call("review_step", request({"symbol": "idle_deadline"}), "io")])

    def check(messages):
        result = json.loads(messages[-1].content)["results"][0]["result"]
        assert "OSError" in result["error"] and result["question_id"]
        return stop(from_board(messages))

    reviewer = DecisionReviewer(
        store, Model([first, check]), EvidenceGateway(store, gateway), task_id=task.id, lease_token=task.lease_token
    )
    assert await reviewer.run("a") == "partial", reviewer.error
    failures = [q for q in store.list(Question) if q.owner == "tool_controller"]
    assert len(failures) == 1 and failures[0].status == "unresolved"
