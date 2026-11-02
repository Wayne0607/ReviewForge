import pytest

from reviewforge.review_v42.contracts import Run
from reviewforge.review_v42.scope import compile_scopes


def test_units_are_stable_unique_and_keep_same_file_and_transactions():
    run = Run(id="r", repo="a/b", base="b", head="h", policy="v42-units-6")
    regions = [
        {"id": "a", "path": "same.py", "patch": "a", "transaction_id": "T"},
        {"id": "b", "path": "same.py", "patch": "b"},
        {"id": "c", "path": "other.py", "patch": "c", "transaction_id": "T"},
        {"id": "d", "path": "deleted.bin", "unsupported": True, "patch": ""},
    ]
    scopes = compile_scopes(run, regions)
    assert scopes == compile_scopes(run, list(reversed(regions)))
    assert {frozenset(r["id"] for r in s.regions) for s in scopes} == {frozenset({"a", "b", "c"}), frozenset({"d"})}
    assert {r["id"]: r for s in scopes for r in s.regions} == {r["id"]: r for r in regions}
    with pytest.raises(ValueError, match="duplicate"):
        compile_scopes(run, [regions[0], regions[0]])
    with pytest.raises(ValueError, match="transaction_id"):
        compile_scopes(run, [{"id": "x", "path": "x", "transaction_id": ""}])


@pytest.mark.parametrize("resolve_discovered", [True, False])
async def test_runtime_delivers_each_unit_then_separate_integration(tmp_path, resolve_discovered):
    import json

    from langchain_core.messages import AIMessage
    from test_v42_stateful_reviewer import Gateway, call

    from reviewforge.review_v42.runtime import ReviewRuntime
    from reviewforge.review_v42.store import ReviewStore

    regions = [
        {
            "id": "one",
            "path": "a.py",
            "side": "head",
            "start_line": 1,
            "end_line": 1,
            "changed_lines": [1],
            "patch": "+a",
        },
        {
            "id": "two",
            "path": "b.py",
            "side": "head",
            "start_line": 1,
            "end_line": 1,
            "changed_lines": [1],
            "patch": "+b",
        },
    ]
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
    store = ReviewStore(tmp_path / "board.db", run)

    class Model:
        def __init__(self):
            self.calls = []
            self.first_scope = None
            self.integration_refs = None

        def bind_tools(self, tools, **kwargs):
            self.tools = {tool["function"]["name"]: tool["function"] for tool in tools}
            return self

        async def ainvoke(self, messages):
            board = json.loads(messages[1].content)
            role = board["unit_role"]
            scope = board["scope"]
            system = messages[0].content
            assert "record_region_progress" not in system
            assert "record_region_progress" not in self.tools and "finish_scope" not in self.tools
            if role == "primary":
                assert {"submit_unit_decision", "register_unit_dependency"} <= self.tools.keys()
                assert "finish_integration" not in self.tools
                assert "entire region" in system and "same tool batch" in system
                assert "owned region's diff" in system and "pinned source side first" in system
                assert "current successful request" in self.tools["submit_unit_decision"]["description"]
                nested = self.tools["submit_unit_decision"]["parameters"]["properties"]["observations"]["items"]
                assert set(nested["required"]) == {"statement", "evidence_refs", "region_ids"}
                assert nested["additionalProperties"] is False
            else:
                assert {"record_integration_receipt", "finish_integration"} <= self.tools.keys()
                assert "submit_unit_decision" not in self.tools
                assert "separate cross-unit integration review" in system
            previous = [json.loads(m.content) for m in messages if m.type == "tool"]
            self.calls.append((role, scope["id"]))
            if role == "primary":
                if not previous:
                    path = scope["regions"][0]["path"]
                    other = "b.py" if path == "a.py" else "a.py"
                    if self.first_scope is None:
                        self.first_scope = scope["id"]
                    query = {"path": other, "scope_id": self.first_scope, "limit": 10}
                    return AIMessage(
                        content="",
                        tool_calls=[
                            call("read_source", {"path": path}, "own"),
                            call("read_source", {"path": other}, "cross"),
                            call("query_shared", query, "shared"),
                        ],
                    )
                own_ref = previous[0]["evidence_ref"]
                if scope["id"] != self.first_scope:
                    assert any(
                        item["type"] == "evidence" and self.first_scope in item["origin_scopes"]
                        for item in previous[2]["items"]
                    )
                region = scope["regions"][0]["id"]
                dependency = []
                if scope["id"] == self.first_scope:
                    target = next(x for x in board["primary_scope_ids"] if x != scope["id"])
                    dependency = [
                        call(
                            "register_unit_dependency",
                            {
                                "target_scope_id": target,
                                "description": "A caller passes data into the other changed unit",
                                "evidence_refs": [previous[0]["evidence_ref"], previous[1]["evidence_ref"]],
                            },
                            "dependency",
                        )
                    ]
                return AIMessage(
                    content="",
                    tool_calls=[
                        *dependency,
                        call(
                            "submit_unit_decision",
                            {
                                "checked_region_ids": [region],
                                "observations": [
                                    {
                                        "statement": "Inspected changed behavior",
                                        "evidence_refs": [own_ref],
                                        "region_ids": [region],
                                    }
                                ],
                                "unresolved_refs": [],
                                "finish": True,
                            },
                            "decision",
                        ),
                    ],
                )
            if not previous:
                return AIMessage(
                    content="",
                    tool_calls=[call("read_source", {"path": "a.py"}, "a"), call("read_source", {"path": "b.py"}, "b")],
                )
            refs = [x["evidence_ref"] for x in previous if "evidence_ref" in x]
            if not refs:
                return AIMessage(content="", tool_calls=[call("finish_integration", {}, "finish")])
            self.integration_refs = refs
            assert any(o["kind"] == "discovered" for o in board["integration_obligations"])
            calls = [
                call(
                    "record_integration_receipt",
                    {
                        "obligation_id": o["id"],
                        "disposition": "checked",
                        "evidence_refs": refs,
                        "reason": "Read and compared boundaries",
                    },
                    o["id"],
                )
                for o in board["integration_obligations"]
                if resolve_discovered or o["kind"] != "discovered"
            ]
            return AIMessage(content="", tool_calls=[*calls, call("finish_integration", {}, "finish")])

    class Verifier:
        pass

    class PinnedGateway(Gateway):
        async def __call__(self, name, args):
            result = await super().__call__(name, args)
            result["metadata"] = {"workspace_digest": "head-snapshot", "status": "available"}
            return result

    model = Model()
    runtime = ReviewRuntime(store, model, PinnedGateway(), Verifier())
    result = await runtime.run(regions)
    assert result["status"] == ("completed" if resolve_discovered else "partial"), result
    plan = store.get_unit_plan()
    assert len(plan.primary_scope_ids) == 2
    assert any(role == "integration" and scope_id == plan.integration_scope_id for role, scope_id in model.calls)
    assert len(store.list_integration_obligations()) >= 4
    assert all(row["status"] == "checked" for row in store.list_integration_obligations()) == resolve_discovered
    if not resolve_discovered:
        assert any(
            row["kind"] == "discovered" and row["status"] != "checked" for row in store.list_integration_obligations()
        )
        return
    old_count = len(model.calls)
    assert (await runtime.run(regions))["status"] == "completed"
    assert len(model.calls) == old_count


async def test_same_batch_source_cannot_fake_delivery_and_empty_integration_cannot_finish(tmp_path):
    import json

    from langchain_core.messages import AIMessage
    from test_v42_stateful_reviewer import Gateway, call

    from reviewforge.review_v42.runtime import ReviewRuntime
    from reviewforge.review_v42.store import ReviewStore
    from reviewforge.review_v42.tools import EvidenceGateway

    regions = [
        {
            "id": "one",
            "path": "a.py",
            "side": "head",
            "start_line": 1,
            "end_line": 1,
            "changed_lines": [1],
            "patch": "+a",
        }
    ]
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
    store = ReviewStore(tmp_path / "board.db", run)

    class PinnedGateway(Gateway):
        async def __call__(self, name, args):
            result = await super().__call__(name, args)
            result["metadata"] = {"workspace_digest": "head-snapshot", "status": "available"}
            return result

    gateway = EvidenceGateway(store, PinnedGateway())
    ref = (await gateway.execute("read_source", {"path": "a.py"}))["evidence_ref"]

    class Model:
        def __init__(self):
            self.primary = 0
            self.integration = 0

        def bind_tools(self, tools, **kwargs):
            return self

        async def ainvoke(self, messages):
            board = json.loads(messages[1].content)
            if board["unit_role"] == "primary":
                self.primary += 1
                decision = {
                    "checked_region_ids": ["one"],
                    "observations": [{"statement": "Inspected", "evidence_refs": [ref], "region_ids": ["one"]}],
                    "unresolved_refs": [],
                    "finish": True,
                }
                if self.primary == 1:
                    return AIMessage(
                        content="",
                        tool_calls=[
                            call("read_source", {"path": "a.py"}, "read"),
                            call("submit_unit_decision", decision, "premature"),
                        ],
                    )
                if self.primary == 2:
                    assert any("not delivered" in m.content for m in messages if m.type == "tool")
                    return AIMessage(content="", tool_calls=[call("submit_unit_decision", decision, "delivered")])
            self.integration += 1
            if self.integration == 1:
                return AIMessage(content="", tool_calls=[call("finish_scope", {"checked_regions": []}, "ordinary")])
            if self.integration == 2:
                assert any("unknown tool" in m.content for m in messages if m.type == "tool")
                return AIMessage(content="", tool_calls=[call("finish_integration", {}, "empty")])
            if self.integration == 3:
                assert "obligations" in messages[-1].content
                return AIMessage(content="", tool_calls=[call("read_source", {"path": "a.py"}, "read")])
            calls = [
                call(
                    "record_integration_receipt",
                    {
                        "obligation_id": o["id"],
                        "disposition": "checked",
                        "evidence_refs": [ref],
                        "reason": "Compared source",
                    },
                    o["id"],
                )
                for o in board["integration_obligations"]
            ]
            return AIMessage(content="", tool_calls=[*calls, call("finish_integration", {}, "finish")])

    model = Model()
    runtime = ReviewRuntime(store, model, gateway, object())
    result = await runtime.run(regions)
    assert result["status"] == "completed", result
    assert model.primary == 2 and model.integration == 4


async def test_unit_candidate_is_incremental_and_verified(tmp_path):
    import json

    from langchain_core.messages import AIMessage
    from test_v42_stateful_reviewer import Gateway, call

    from reviewforge.review_v42.contracts import Case
    from reviewforge.review_v42.runtime import ReviewRuntime
    from reviewforge.review_v42.store import ReviewStore

    regions = [
        {
            "id": "one",
            "path": "a.py",
            "side": "head",
            "start_line": 1,
            "end_line": 1,
            "changed_lines": [1],
            "patch": "+a",
        }
    ]
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
    store = ReviewStore(tmp_path / "board.db", run)

    class PinnedGateway(Gateway):
        async def __call__(self, name, args):
            result = await super().__call__(name, args)
            result["metadata"] = {"workspace_digest": "head-snapshot", "status": "available"}
            return result

    class Model:
        def bind_tools(self, tools, **kwargs):
            return self

        async def ainvoke(self, messages):
            board = json.loads(messages[1].content)
            previous = [json.loads(m.content) for m in messages if m.type == "tool"]
            if not previous:
                return AIMessage(content="", tool_calls=[call("read_source", {"path": "a.py"}, "read")])
            ref = next(x["evidence_ref"] for x in previous if "evidence_ref" in x)
            if board["unit_role"] == "primary":
                return AIMessage(
                    content="",
                    tool_calls=[
                        call(
                            "submit_case",
                            {"trigger": "trigger", "behavior": "mechanism", "impact": "impact", "evidence_refs": [ref]},
                            "case",
                        ),
                        call(
                            "submit_unit_decision",
                            {
                                "checked_region_ids": ["one"],
                                "observations": [
                                    {"statement": "Inspected", "evidence_refs": [ref], "region_ids": ["one"]}
                                ],
                                "unresolved_refs": [],
                                "finish": True,
                            },
                            "decision",
                        ),
                    ],
                )
            calls = [
                call(
                    "record_integration_receipt",
                    {
                        "obligation_id": o["id"],
                        "disposition": "checked",
                        "evidence_refs": [ref],
                        "reason": "Compared source",
                    },
                    o["id"],
                )
                for o in board["integration_obligations"]
            ]
            return AIMessage(content="", tool_calls=[*calls, call("finish_integration", {}, "finish")])

    class Verifier:
        policy = "v42-independent-1"

        async def verify(self, case_id, *, task_id, lease_token):
            case = store.get(Case, case_id)
            return [
                store.save_verdict(
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
            ]

    result = await ReviewRuntime(store, Model(), PinnedGateway(), Verifier()).run(regions)
    assert result["status"] == "completed", result
    assert len(store.list(Case)) == 1
    assert len(result["verdicts"]) == 1


async def test_provider_blocked_does_not_finish_units_or_integration(tmp_path):
    from test_v42_stateful_reviewer import Gateway

    from reviewforge.review_v42.runtime import ReviewRuntime
    from reviewforge.review_v42.store import ReviewStore

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
    store = ReviewStore(tmp_path / "board.db", run)

    class PaymentError(Exception):
        status_code = 402

    class Blocked:
        def bind_tools(self, tools, **kwargs):
            return self

        async def ainvoke(self, messages):
            raise PaymentError()

    result = await ReviewRuntime(store, Blocked(), Gateway(), object()).run(regions)
    assert result["status"] == "provider_blocked"
    assert all(item["status"] != "checked" for item in store.list_integration_obligations())


async def test_real_tool_failure_records_question_and_same_read_recovers(tmp_path):
    import json

    from langchain_core.messages import AIMessage
    from test_v42_stateful_reviewer import Gateway, call

    from reviewforge.review_v42.contracts import Question
    from reviewforge.review_v42.runtime import ReviewRuntime
    from reviewforge.review_v42.store import ReviewStore

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
    store = ReviewStore(tmp_path / "board.db", run)

    class FailingGateway(Gateway):
        def __init__(self):
            super().__init__()
            self.failed = False

        async def __call__(self, name, args):
            if not self.failed:
                self.failed = True
                return {
                    "content": "workspace unavailable",
                    "revision": "head",
                    "side": "head",
                    "kind": "failure",
                    "metadata": {"status": "unavailable", "reason": "snapshot unavailable"},
                }
            result = await super().__call__(name, args)
            result["metadata"] = {"workspace_digest": "head-snapshot", "status": "available"}
            return result

    class Model:
        def __init__(self):
            self.first = True

        def bind_tools(self, tools, **kwargs):
            return self

        async def ainvoke(self, messages):
            board = json.loads(messages[1].content)
            previous = [json.loads(m.content) for m in messages if m.type == "tool"]
            if not previous:
                return AIMessage(content="", tool_calls=[call("read_source", {"path": "a.py"}, "read")])
            if self.first:
                self.first = False
                assert "question_id" in previous[0] and "snapshot unavailable" in previous[0]["error"]
                return AIMessage(content="", tool_calls=[call("read_source", {"path": "a.py"}, "retry")])
            ref = next(x["evidence_ref"] for x in previous if "evidence_ref" in x)
            if board["unit_role"] == "primary":
                return AIMessage(
                    content="",
                    tool_calls=[
                        call(
                            "submit_unit_decision",
                            {
                                "checked_region_ids": ["one"],
                                "observations": [
                                    {"statement": "Inspected", "evidence_refs": [ref], "region_ids": ["one"]}
                                ],
                                "unresolved_refs": [],
                                "finish": True,
                            },
                            "decision",
                        )
                    ],
                )
            calls = [
                call(
                    "record_integration_receipt",
                    {"obligation_id": o["id"], "disposition": "checked", "evidence_refs": [ref], "reason": "Compared"},
                    o["id"],
                )
                for o in board["integration_obligations"]
            ]
            return AIMessage(content="", tool_calls=[*calls, call("finish_integration", {}, "finish")])

    result = await ReviewRuntime(store, Model(), FailingGateway(), object()).run(regions)
    assert result["status"] == "completed", result
    questions = store.list(Question)
    assert len(questions) == 1 and questions[0].owner == "tool_controller" and questions[0].status == "answered"


async def test_interrupted_unit_recovers_exact_exchange_and_lease(tmp_path):
    import asyncio
    import json

    from langchain_core.messages import AIMessage
    from test_v42_stateful_reviewer import Gateway, call

    from reviewforge.review_v42.contracts import Task
    from reviewforge.review_v42.runtime import ReviewRuntime
    from reviewforge.review_v42.store import ReviewStore

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
    store = ReviewStore(tmp_path / "board.db", run)

    class PinnedGateway(Gateway):
        async def __call__(self, name, args):
            result = await super().__call__(name, args)
            result["metadata"] = {"workspace_digest": "head-snapshot", "status": "available"}
            return result

    class Model:
        def __init__(self):
            self.interrupt = True
            self.primary_turns = 0

        def bind_tools(self, tools, **kwargs):
            return self

        async def ainvoke(self, messages):
            board = json.loads(messages[1].content)
            previous = [json.loads(m.content) for m in messages if m.type == "tool"]
            if board["unit_role"] == "primary":
                self.primary_turns += 1
                if not previous:
                    return AIMessage(content="", tool_calls=[call("read_source", {"path": "a.py"}, "read")])
                if self.interrupt:
                    self.interrupt = False
                    raise asyncio.CancelledError()
                assert [m.type for m in messages[-2:]] == ["ai", "tool"]
                ref = previous[0]["evidence_ref"]
                return AIMessage(
                    content="",
                    tool_calls=[
                        call(
                            "submit_unit_decision",
                            {
                                "checked_region_ids": ["one"],
                                "observations": [
                                    {"statement": "Inspected", "evidence_refs": [ref], "region_ids": ["one"]}
                                ],
                                "unresolved_refs": [],
                                "finish": True,
                            },
                            "decision",
                        )
                    ],
                )
            if not previous:
                return AIMessage(content="", tool_calls=[call("read_source", {"path": "a.py"}, "integration-read")])
            ref = previous[0]["evidence_ref"]
            calls = [
                call(
                    "record_integration_receipt",
                    {"obligation_id": o["id"], "disposition": "checked", "evidence_refs": [ref], "reason": "Compared"},
                    o["id"],
                )
                for o in board["integration_obligations"]
            ]
            return AIMessage(content="", tool_calls=[*calls, call("finish_integration", {}, "finish")])

    model, gateway = Model(), PinnedGateway()
    runtime = ReviewRuntime(store, model, gateway, object())
    with pytest.raises(asyncio.CancelledError):
        await runtime.run(regions)
    tasks = [t for t in store.list(Task) if t.kind == "review"]
    assert len(tasks) == 1 and tasks[0].status != "leased"
    assert len(gateway.calls) == 1
    assert (await runtime.run(regions))["status"] == "completed"
    assert len([args for name, args in gateway.calls if args["path"] == "a.py"]) == 2


async def test_verifier_new_evidence_reopens_completed_unit_and_integrates_in_same_run(tmp_path):
    import asyncio
    import json

    from langchain_core.messages import AIMessage
    from test_v42_stateful_reviewer import Gateway, call

    from reviewforge.review_v42.contracts import Case, ReviewScope, RevisionConflict
    from reviewforge.review_v42.runtime import ReviewRuntime
    from reviewforge.review_v42.store import ReviewStore

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
    store = ReviewStore(tmp_path / "board.db", run)

    class PinnedGateway(Gateway):
        async def __call__(self, name, args):
            result = await super().__call__(name, args)
            result["metadata"] = {"workspace_digest": "head-snapshot", "status": "available"}
            return result

    class Model:
        def __init__(self):
            self.reviewed_new = False
            self.integration_count = 0

        def bind_tools(self, tools, **kwargs):
            return self

        async def ainvoke(self, messages):
            board = json.loads(messages[1].content)
            previous = [json.loads(m.content) for m in messages if m.type == "tool"]
            if board["unit_role"] == "primary":
                cases = board["cases"]
                if not previous:
                    if cases:
                        self.reviewed_new = True
                        return AIMessage(
                            content="",
                            tool_calls=[call("read_evidence", {"ref": cases[0]["evidence_refs"][-1]}, "new-evidence")],
                        )
                    return AIMessage(content="", tool_calls=[call("read_source", {"path": "a.py"}, "read")])
                ref = next(row["evidence_ref"] for row in previous if "evidence_ref" in row)
                calls = (
                    []
                    if cases
                    else [
                        call(
                            "submit_case",
                            {"trigger": "trigger", "behavior": "mechanism", "impact": "impact", "evidence_refs": [ref]},
                            "case",
                        )
                    ]
                )
                calls.append(
                    call(
                        "submit_unit_decision",
                        {
                            "checked_region_ids": ["one"],
                            "observations": [
                                {"statement": "Checked after evidence", "evidence_refs": [ref], "region_ids": ["one"]}
                            ],
                            "unresolved_refs": [],
                            "finish": True,
                        },
                        "decision",
                    )
                )
                return AIMessage(content="", tool_calls=calls)
            self.integration_count += 1
            if not previous:
                return AIMessage(content="", tool_calls=[call("read_source", {"path": "a.py"}, "integration-read")])
            ref = next(row["evidence_ref"] for row in previous if "evidence_ref" in row)
            calls = [
                call(
                    "record_integration_receipt",
                    {"obligation_id": o["id"], "disposition": "checked", "evidence_refs": [ref], "reason": "Compared"},
                    o["id"],
                )
                for o in board["integration_obligations"]
            ]
            return AIMessage(content="", tool_calls=[*calls, call("finish_integration", {}, "finish")])

    class Verifier:
        policy = "v42-independent-1"

        def __init__(self):
            self.calls = 0

        async def verify(self, case_id, *, task_id, lease_token):
            self.calls += 1
            case = store.get(Case, case_id)
            if self.calls == 1:
                for _ in range(200):
                    if store.get(ReviewScope, case.scope_id).status == "reviewed":
                        break
                    await asyncio.sleep(0.01)
                assert store.get(ReviewScope, case.scope_id).status == "reviewed"
                ref = store.put_evidence(
                    b"new source\n",
                    revision="head",
                    side="head",
                    kind="source",
                    path="a.py",
                    metadata={"workspace_digest": "head-snapshot", "status": "available"},
                )
                store.record_verification_observations(
                    task_id, lease_token, case.revision, [], case.evidence_refs + [ref.id]
                )
                raise RevisionConflict("new evidence invalidates input")
            return [
                store.save_verdict(
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
            ]

    model, verifier = Model(), Verifier()
    result = await ReviewRuntime(store, model, PinnedGateway(), verifier).run(regions)
    assert result["status"] == "completed", result
    assert model.reviewed_new and model.integration_count == 2 and verifier.calls == 2


async def test_same_pending_evidence_state_is_not_retried_in_loop(tmp_path):
    from test_v42_stateful_reviewer import Gateway

    from reviewforge.review_v42.runtime import ReviewRuntime
    from reviewforge.review_v42.store import ReviewStore

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
    store = ReviewStore(tmp_path / "board.db", run)

    class Runtime(ReviewRuntime):
        calls = 0

        async def _review_scope(self, scope_id, **kwargs):
            self.calls += 1
            return "superseded"

    runtime = Runtime(store, object(), Gateway(), object())
    result = await runtime.run(regions)
    assert result["status"] == "partial" and runtime.calls == 1


async def test_integration_candidate_gap_gets_one_followup_without_new_evidence(tmp_path):
    import json

    from langchain_core.messages import AIMessage
    from test_v42_stateful_reviewer import Gateway, call

    from reviewforge.review_v42.contracts import Case
    from reviewforge.review_v42.runtime import ReviewRuntime
    from reviewforge.review_v42.store import ReviewStore

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
    store = ReviewStore(tmp_path / "board.db", run)

    class PinnedGateway(Gateway):
        async def __call__(self, name, args):
            result = await super().__call__(name, args)
            result["metadata"] = {"workspace_digest": "head-snapshot", "status": "available"}
            return result

    class Model:
        def __init__(self):
            self.followups = 0

        def bind_tools(self, tools, **kwargs):
            return self

        async def ainvoke(self, messages):
            board = json.loads(messages[1].content)
            previous = [json.loads(m.content) for m in messages if m.type == "tool"]
            if board["unit_role"] == "integration" and "Resolve these verifier gaps" in messages[0].content:
                self.followups += 1
                return AIMessage(content="", tool_calls=[call("finish_integration", {}, "finish-followup")])
            if not previous:
                return AIMessage(content="", tool_calls=[call("read_source", {"path": "a.py"}, "read")])
            ref = next(row["evidence_ref"] for row in previous if "evidence_ref" in row)
            if board["unit_role"] == "primary":
                return AIMessage(
                    content="",
                    tool_calls=[
                        call(
                            "submit_unit_decision",
                            {
                                "checked_region_ids": ["one"],
                                "observations": [
                                    {"statement": "Checked", "evidence_refs": [ref], "region_ids": ["one"]}
                                ],
                                "unresolved_refs": [],
                                "finish": True,
                            },
                            "decision",
                        )
                    ],
                )
            calls = [
                call(
                    "submit_case",
                    {
                        "trigger": "trigger",
                        "behavior": "cross-unit behavior",
                        "impact": "impact",
                        "evidence_refs": [ref],
                    },
                    "case",
                )
            ]
            calls.extend(
                call(
                    "record_integration_receipt",
                    {"obligation_id": o["id"], "disposition": "checked", "evidence_refs": [ref], "reason": "Compared"},
                    o["id"],
                )
                for o in board["integration_obligations"]
            )
            return AIMessage(content="", tool_calls=[*calls, call("finish_integration", {}, "finish")])

    class Verifier:
        policy = "v42-independent-1"
        calls = 0

        async def verify(self, case_id, *, task_id, lease_token):
            self.calls += 1
            case = store.get(Case, case_id)
            return [
                store.save_verdict(
                    case_id,
                    case.claims[0]["id"],
                    case.revision,
                    self.policy,
                    "needs_evidence",
                    case.evidence_refs,
                    {"gaps": ["Need a behavior contract"]},
                    task_id=task_id,
                    lease_token=lease_token,
                )
            ]

    model, verifier = Model(), Verifier()
    result = await ReviewRuntime(store, model, PinnedGateway(), verifier).run(regions)
    assert result["status"] == "partial"
    assert model.followups == 1 and verifier.calls == 1


def test_previous_frontier_policy_retains_original_prompt(tmp_path):
    from test_v42_frontier_reviewer import setup_frontier
    from test_v42_stateful_reviewer import Gateway

    from reviewforge.review_v42.reviewer import FRONTIER_PROMPT, PROMPT, WORKSPACE_PROMPT, Reviewer
    from reviewforge.review_v42.tools import EvidenceGateway

    store, scope, task = setup_frontier(tmp_path)
    reviewer = Reviewer(
        store, object(), EvidenceGateway(store, Gateway()), task_id=task.id, lease_token=task.lease_token
    )
    assert reviewer._context(scope.id)[0].content == PROMPT + WORKSPACE_PROMPT + FRONTIER_PROMPT
