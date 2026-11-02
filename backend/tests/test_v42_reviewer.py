import json

import pytest
from langchain_core.messages import AIMessage

from reviewforge.review_v42.contracts import Case, EvidenceRef, Question, Run
from reviewforge.review_v42.reviewer import Reviewer
from reviewforge.review_v42.scope import compile_scopes
from reviewforge.review_v42.store import ReviewStore
from reviewforge.review_v42.tools import EvidenceGateway, ToolUnavailableError
from reviewforge.review_v42.verifier import _citation


class Model:
    def __init__(self, turns):
        self.turns, self.inputs = iter(turns), []

    def bind_tools(self, tools, **kwargs):
        return self

    async def ainvoke(self, messages):
        self.inputs.append(messages.copy())
        item = next(self.turns)
        if isinstance(item, BaseException):
            raise item
        return item


def call(name, args, ident="c"):
    return {"name": name, "args": args, "id": ident}


def ai(*calls):
    return AIMessage(content="", tool_calls=list(calls))


def setup(tmp_path):
    store = ReviewStore(tmp_path / "state.db", Run(id="r", repo="x/y", base="base", head="head"))
    scope = compile_scopes(store.run, [{"id": "one", "path": "a.py"}])[0]
    store.add(scope)
    return store, scope


async def source(name, args):
    return {"content": "source", "revision": "head", "side": "head", "kind": "source", "path": "a.py"}


async def test_empty_review_and_partial_are_distinct(tmp_path):
    for checked, expected in [(["one"], "reviewed"), ([], "partial")]:
        store, scope = setup(tmp_path / expected)
        model = Model([ai(call("finish_scope", {"checked_regions": checked}))])
        assert await Reviewer(store, model, EvidenceGateway(store, source)).run(scope.id) == expected
        assert not store.list(Case)


async def test_valid_case_survives_bad_sibling_and_is_not_duplicated(tmp_path):
    store, scope = setup(tmp_path)
    ref = store.put_evidence(b"source", revision="head", side="head", kind="source", path="a.py")
    good = {"trigger": "x", "behavior": "bad", "impact": "lost", "evidence_refs": [ref.id]}
    model = Model(
        [
            ai(
                call("submit_case", good, "1"),
                call("submit_case", {"trigger": "x"}, "2"),
                call("finish_scope", {"checked_regions": ["one"]}, "3"),
            ),
            ai(call("submit_case", good, "4")),
            ai(call("finish_scope", {"checked_regions": ["one"]}, "5")),
        ]
    )
    assert await Reviewer(store, model, EvidenceGateway(store, source)).run(scope.id) == "reviewed"
    assert len(store.list(Case)) == 1


async def test_repeated_receipt_stalls(tmp_path):
    store, scope = setup(tmp_path)
    read = call("read_diff", {"path": "a.py"})
    assert (
        await Reviewer(store, Model([ai(read), ai(read), ai(read)]), EvidenceGateway(store, source)).run(scope.id)
        == "stalled"
    )


async def test_unavailable_tool_and_402(tmp_path):
    store, scope = setup(tmp_path)

    async def bad(name, args):
        return {
            "content": "workspace is closed",
            "revision": "head",
            "side": "head",
            "kind": "failure",
            "metadata": {"status": "unavailable"},
        }

    model = Model([ai(call("read_diff", {"path": "a.py"})), ai(call("finish_scope", {"checked_regions": ["one"]}))])
    assert await Reviewer(store, model, EvidenceGateway(store, bad)).run(scope.id) == "partial"
    question = store.list(Question)[0]
    assert question.status == "unresolved"
    receipt = json.loads(next(m.content for m in model.inputs[-1] if m.type == "tool"))
    assert receipt["question_id"] == question.id and "workspace is closed" in receipt["error"]
    store2, scope2 = setup(tmp_path / "another")

    class BlockedError(Exception):
        status_code = 402

    assert (
        await Reviewer(store2, Model([BlockedError()]), EvidenceGateway(store2, source)).run(scope2.id)
        == "provider_blocked"
    )


@pytest.mark.parametrize("status", ["not_found", "not_changed", "out_of_range"])
async def test_negative_queries_remain_navigational_and_do_not_block_review(tmp_path, status):
    store, scope = setup(tmp_path)

    async def negative(name, args):
        return {
            "content": "Verified snapshot query result",
            "revision": "head",
            "side": "head",
            "kind": "query",
            "path": args["path"],
            "metadata": {"status": status, "navigation_only": True, "complete": True},
        }

    model = Model([ai(call("read_diff", {"path": "other.py"})), ai(call("finish_scope", {"checked_regions": ["one"]}))])
    assert await Reviewer(store, model, EvidenceGateway(store, negative)).run(scope.id) == "reviewed"
    assert not store.list(Question)
    receipt = json.loads(next(m.content for m in model.inputs[-1] if m.type == "tool"))
    assert receipt["kind"] == "query" and receipt["revision"] == "head" and receipt["path"] == "other.py"
    assert receipt["metadata"]["status"] == status
    ref = store.get(EvidenceRef, receipt["evidence_ref"])
    assert store.read_evidence(ref.id) == b"Verified snapshot query result"
    with pytest.raises(ValueError, match="navigation-only"):
        _citation(store, {"ref": ref.id, "quote": "Verified snapshot"})


async def test_unavailable_reason_and_question_are_visible_without_false_resolution(tmp_path):
    store, scope = setup(tmp_path)
    responses = iter(["unavailable", "not_found", "available"])

    async def gateway(name, args):
        status = next(responses)
        return {
            "content": "workspace is closed" if status == "unavailable" else "snapshot result",
            "revision": "head",
            "side": "head",
            "kind": {"unavailable": "failure", "not_found": "query", "available": "source"}[status],
            "path": args["path"],
            "metadata": {"status": status, "navigation_only": status == "not_found"},
        }

    read = call("read_source", {"path": "a.py", "revision": "head"})

    class InspectingModel(Model):
        async def ainvoke(self, messages):
            if len(self.inputs) == 1:
                receipt = json.loads(next(m.content for m in messages if m.type == "tool"))
                question = store.list(Question)[0]
                assert receipt["question_id"] == question.id
                assert "workspace is closed" in receipt["error"]
                assert question.status == "unresolved"
            if len(self.inputs) == 2:
                assert store.list(Question)[0].status == "unresolved"
            return await super().ainvoke(messages)

    model = InspectingModel([ai(read), ai(read), ai(read), ai(call("finish_scope", {"checked_regions": ["one"]}))])
    assert await Reviewer(store, model, EvidenceGateway(store, gateway)).run(scope.id) == "reviewed"
    assert store.list(Question)[0].status == "answered"


async def test_failure_preserves_structured_reason_and_exception_identity(tmp_path):
    store, _ = setup(tmp_path)

    async def unavailable(name, args):
        return {
            "content": "untrusted receipt body",
            "revision": "head",
            "side": "head",
            "kind": "test",
            "metadata": {"status": "unavailable", "original_receipt": {"reason": "runtime missing"}},
        }

    with pytest.raises(ToolUnavailableError) as failure:
        await EvidenceGateway(store, unavailable).execute("run_check", {"path": "test.py"})
    assert failure.value.name == "run_check" and failure.value.status == "unavailable"
    assert failure.value.reason == "runtime missing" and "untrusted receipt body" not in str(failure.value)
    assert not store.list(EvidenceRef)


async def test_negative_result_without_navigation_provenance_is_not_accepted(tmp_path):
    store, scope = setup(tmp_path)

    async def bad(name, args):
        return {
            "content": "not found",
            "revision": "head",
            "side": "head",
            "kind": "source",
            "metadata": {"status": "not_found"},
        }

    model = Model([ai(call("read_diff", {"path": "a.py"})), ai(call("finish_scope", {"checked_regions": ["one"]}))])
    assert await Reviewer(store, model, EvidenceGateway(store, bad)).run(scope.id) == "partial"
    assert store.list(Question)[0].status == "unresolved"
    assert not store.list(EvidenceRef)


async def test_gateway_native_tool_schemas_and_arguments(tmp_path):
    store, scope = setup(tmp_path)
    received = []

    class Gateway:
        tool_specs = [
            {
                "type": "function",
                "function": {
                    "name": "read_source",
                    "parameters": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}, "side": {"type": "string"}},
                        "required": ["path", "side"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "read_knowledge",
                    "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "run_check",
                    "parameters": {
                        "type": "object",
                        "properties": {"runtime": {"type": "string"}, "path": {"type": "string"}},
                        "required": ["runtime", "path"],
                    },
                },
            },
        ]

        async def __call__(self, name, args):
            received.append((name, args))
            return {"content": "source", "revision": "head", "side": "head", "kind": "source", "path": "a.py"}

    class AssertingModel(Model):
        def bind_tools(self, tools, **kwargs):
            raw = [tool for tool in tools if isinstance(tool, dict)]
            assert raw == Gateway.tool_specs
            return self

    model = AssertingModel(
        [
            ai(call("read_source", {"path": "a.py", "side": "head"})),
            ai(call("read_knowledge", {"path": "README.md"})),
            ai(call("run_check", {"runtime": "python", "path": "tests/test_a.py"})),
            ai(call("finish_scope", {"checked_regions": ["one"]})),
        ]
    )
    assert await Reviewer(store, model, EvidenceGateway(store, Gateway())).run(scope.id) == "reviewed"
    assert received == [
        ("read_source", {"path": "a.py", "side": "head"}),
        ("read_knowledge", {"path": "README.md"}),
        ("run_check", {"runtime": "python", "path": "tests/test_a.py"}),
    ]


async def test_unsupported_region_cannot_become_clean_by_assertion(tmp_path):
    store, scope = setup(tmp_path)
    scope = store.update(
        type(scope), scope.id, scope.revision, regions=[{"id": "one", "path": "image.bin", "unsupported": True}]
    )
    model = Model([ai(call("finish_scope", {"checked_regions": ["one"]}))])
    assert await Reviewer(store, model, EvidenceGateway(store, source)).run(scope.id) == "partial"
