import asyncio
import json

import pytest
from langchain_core.messages import AIMessage
from test_v42_stateful_reviewer import ScriptedModel, call

from reviewforge.review_v42.contracts import Question, Run, Task
from reviewforge.review_v42.reviewer import QuestionAssistant, Reviewer
from reviewforge.review_v42.scope import compile_scopes
from reviewforge.review_v42.store import ReviewStore
from reviewforge.review_v42.tools import EvidenceGateway


def setup_store(tmp_path, policy="v42-frontier-5"):
    run = Run(
        id="r", repo="a/b", base="base", head="head", policy=policy, metadata={"workspace_digest": "sha256:snapshot"}
    )
    store = ReviewStore(tmp_path / "board.db", run)
    scope = compile_scopes(run, [{"id": "one", "path": "a.py"}])[0]
    store.add(scope)
    task = store.add(Task(id="task", run_id="r", kind="review", object_id=scope.id, input_revision=scope.revision))
    task = store.acquire_task(task.id, "tester", 120)
    return store, scope, task


class SearchGateway:
    def __init__(self):
        self.tool_specs = [
            {
                "type": "function",
                "function": {
                    "name": n,
                    "description": n,
                    "parameters": {"type": "object", "properties": {}, "additionalProperties": True},
                },
            }
            for n in ("search_code", "read_source", "read_diff")
        ]
        self.skipped = [f"ignored/{i:03}.bin" for i in range(314)]
        self.calls = []

    async def __call__(self, name, args):
        self.calls.append((name, args.copy()))
        if name == "search_code":
            offset = args.get("offset", 0)
            total = 105 if args["query"] == "q0" else 1
            matches = [
                {"path": f"src/{args['query']}/{i}.py", "line": i + 1, "text": f"SOURCE LINE {i}"}
                for i in range(offset, min(offset + 100, total))
            ]
            payload = {
                "query": args["query"],
                "glob": args.get("glob", ""),
                "matches": matches,
                "offset": offset,
                "next_offset": offset + 100 if offset + 100 < total else None,
                "total_matches": total,
                "skipped_paths": self.skipped,
                "aliases_resolved": False,
            }
            return {
                "content": json.dumps(payload),
                "revision": "head",
                "side": "head",
                "kind": "search",
                "path": "",
                "metadata": {
                    "workspace_digest": "sha256:snapshot",
                    "status": "available",
                    "navigation_only": True,
                    "complete": False,
                },
            }
        return {
            "content": f"FULL {name} {args['path']}",
            "revision": "head",
            "side": "head",
            "kind": "diff" if name == "read_diff" else "source",
            "path": args["path"],
            "metadata": {"status": "available", "navigation_only": name == "read_diff", "complete": True},
        }


def header(messages):
    return json.loads(messages[2].content)


async def test_search_frontier_indexes_preserve_latest_receipt_original_and_pagination(tmp_path):
    store, scope, task = setup_store(tmp_path)
    gateway = SearchGateway()

    class Model(ScriptedModel):
        async def ainvoke(self, messages):
            self.inputs.append(messages.copy())
            turn = len(self.inputs)
            if turn == 1:
                calls = [call("search_code", {"query": f"q{i}"}, f"s{i}") for i in range(6)]
                calls += [
                    call("search_code", {"query": "q0", "offset": 100}, "s6"),
                    call("read_diff", {"path": "a.py"}, "d"),
                    call("read_source", {"path": "a.py"}, "a"),
                ]
                return AIMessage(content="", tool_calls=calls)
            if turn == 2:
                assert not header(messages)["entries"]
                receipts = [json.loads(m.content) for m in messages if m.type == "tool"]
                self.receipts = receipts
                assert len(receipts) == 9
                assert json.loads(receipts[0]["content"])["skipped_paths"] == gateway.skipped
                assert json.loads(receipts[0]["content"])["next_offset"] == 100
                return AIMessage(content="", tool_calls=[call("read_source", {"path": "b.py"})])
            if turn == 3:
                entries = header(messages)["entries"]
                index = [e for e in entries if e["kind"] == "search_index"]
                assert len(index) == 7
                sources = [e["sources"][0] for e in index]
                assert {(x["tool_args"]["query"], x["offset"]) for x in sources} == {(f"q{i}", 0) for i in range(6)} | {
                    ("q0", 100)
                }
                assert all(x["skipped_count"] == 314 and not x["complete"] for x in sources)
                assert sources[0]["next_offset"] == 100 and len(sources[0]["matches"]) == 100
                assert "SOURCE LINE" not in json.dumps(index) and "ignored/" not in json.dumps(index)
                assert {(e["kind"], e["content"]) for e in entries if e["kind"] in {"source", "diff"}} == {
                    ("source", "FULL read_source a.py"),
                    ("diff", "FULL read_diff a.py"),
                }
                self.ref = sources[0]["ref"]
                assert store.read_evidence(self.ref).decode() == self.receipts[0]["content"]
                return AIMessage(
                    content="",
                    tool_calls=[
                        call(
                            "read_search_section",
                            {"ref": self.ref, "section": "skipped_paths", "offset": 0, "limit": 200},
                        )
                    ],
                )
            if turn == 4:
                result = json.loads(messages[-1].content)
                assert result["items"] == gateway.skipped[:200] and result["next_cursor"] == 200
                assert result["search_next_offset"] == 100 and result["complete"] is False
                return AIMessage(
                    content="",
                    tool_calls=[
                        call(
                            "read_search_section",
                            {"ref": self.ref, "section": "skipped_paths", "offset": 200, "limit": 200},
                        )
                    ],
                )
            if turn == 5:
                result = json.loads(messages[-1].content)
                assert result["items"] == gateway.skipped[200:] and result["next_cursor"] is None
                return AIMessage(content="", tool_calls=[call("read_evidence", {"ref": self.ref})])
            assert json.loads(messages[-1].content)["content"] == self.receipts[0]["content"]
            return AIMessage(content="", tool_calls=[call("finish_scope", {"checked_regions": ["one"]})])

    model = Model([])
    reviewer = Reviewer(store, model, EvidenceGateway(store, gateway), task_id=task.id, lease_token=task.lease_token)
    assert await reviewer.run(scope.id) == "reviewed"


async def test_unrecognized_search_retains_original_and_old_policy_unchanged(tmp_path):
    for policy in ("v42-frontier-4", "v42-frontier-5"):
        store, scope, task = setup_store(tmp_path / policy, policy)
        gateway = SearchGateway()
        original = gateway.__call__

        async def invalid(name, args):
            result = await original(name, args)
            if name == "search_code":
                result["metadata"]["workspace_digest"] = "other-snapshot"
            return result

        class Adapter:
            tool_specs = gateway.tool_specs

            async def __call__(self, name, args):
                return await invalid(name, args)

        model = ScriptedModel(
            [
                [call("search_code", {"query": "q1"})],
                [call("read_source", {"path": "a"})],
                [call("finish_scope", {"checked_regions": ["one"]})],
            ]
        )
        reviewer = Reviewer(
            store, model, EvidenceGateway(store, Adapter()), task_id=task.id, lease_token=task.lease_token
        )
        assert await reviewer.run(scope.id) == "reviewed"
        entry = next(e for e in header(model.inputs[2])["entries"] if e["kind"] == "search")
        assert json.loads(entry["content"])["skipped_paths"] == gateway.skipped


async def test_helper_can_page_search_and_correct_bad_ref(tmp_path):
    store, scope, task = setup_store(tmp_path)
    gateway = EvidenceGateway(store, SearchGateway())
    result = await gateway.execute("search_code", {"query": "q1"})
    ref = result["evidence_ref"]
    question = store.add_for_task(
        task.id, task.lease_token, Question(id="q", run_id="r", scope_id=scope.id, statement="Why?")
    )
    helper = store.add_for_task(
        task.id,
        task.lease_token,
        Task(id="helper", run_id="r", kind="question", object_id=question.id, input_revision=0),
    )
    helper = store.acquire_task(helper.id, "assistant", 120)
    model = ScriptedModel(
        [
            [call("read_search_section", {"ref": "missing", "section": "matches", "offset": 0, "limit": 1})],
            [call("read_search_section", {"ref": ref, "section": "matches", "offset": 0, "limit": 1})],
            [call("answer_question", {"status": "unresolved", "answer": "Need source", "evidence_refs": []})],
        ]
    )
    result = await QuestionAssistant(store, model, gateway).run(helper)
    assert result["status"] == "unresolved"
    page = json.loads(next(m.content for m in model.inputs[2] if m.type == "tool" and "items" in m.content))
    assert page["items"][0]["path"] == "src/q1/0.py" and page["navigation_only"] is True


async def test_index_resume_preserves_latest_exchange_and_rejects_invalid_sections(tmp_path):
    store, scope, task = setup_store(tmp_path)
    gateway = EvidenceGateway(store, SearchGateway())

    class Interrupted(ScriptedModel):
        async def ainvoke(self, messages):
            if len(self.inputs) == 2:
                raise asyncio.CancelledError()
            return await super().ainvoke(messages)

    model = Interrupted([[call("search_code", {"query": "q1"})], [call("read_source", {"path": "a.py"})]])
    reviewer = Reviewer(store, model, gateway, task_id=task.id, lease_token=task.lease_token)
    with pytest.raises(asyncio.CancelledError):
        await reviewer.run(scope.id)
    entries = store.list_frontier(task.id)
    ref = next(item.aliases[0] for item in entries if item.kind == "search")
    other = next(item.aliases[0] for item in entries if item.kind == "source")
    resumed = ScriptedModel(
        [
            [call("read_search_section", {"ref": "missing", "section": "matches", "offset": 0, "limit": 1})],
            [call("read_search_section", {"ref": other, "section": "matches", "offset": 0, "limit": 1})],
            [call("read_search_section", {"ref": ref, "section": "matches", "offset": 0, "limit": 1})],
            [call("finish_scope", {"checked_regions": ["one"]})],
        ]
    )
    reviewer = Reviewer(store, resumed, gateway, task_id=task.id, lease_token=task.lease_token)
    assert await reviewer.run(scope.id) == "reviewed"
    assert any(item["kind"] == "search_index" for item in header(resumed.inputs[0])["entries"])
    assert [m.type for m in resumed.inputs[0][-2:]] == ["ai", "tool"]
    assert not store.list(Question)
    assert json.loads(resumed.inputs[3][-1].content)["items"][0]["text"] == "SOURCE LINE 0"


async def test_corrupt_retained_search_fails_partial_on_recovery(tmp_path):
    store, scope, task = setup_store(tmp_path)
    gateway = EvidenceGateway(store, SearchGateway())

    class Interrupted(ScriptedModel):
        async def ainvoke(self, messages):
            if self.inputs:
                raise asyncio.CancelledError()
            return await super().ainvoke(messages)

    reviewer = Reviewer(
        store,
        Interrupted([[call("search_code", {"query": "q1"})]]),
        gateway,
        task_id=task.id,
        lease_token=task.lease_token,
    )
    with pytest.raises(asyncio.CancelledError):
        await reviewer.run(scope.id)
    entry = store.list_frontier(task.id)[0]
    (store.blob_dir / entry.content_hash).write_bytes(b"tampered")
    model = ScriptedModel([[call("finish_scope", {"checked_regions": ["one"]})]])
    reviewer = Reviewer(store, model, gateway, task_id=task.id, lease_token=task.lease_token)
    assert await reviewer.run(scope.id) == "partial"
    assert reviewer.error == "frontier_unavailable"
    assert not model.inputs
