import json

from langchain_core.messages import AIMessage

from reviewforge.review_delivery.contracts import Binding
from reviewforge.review_delivery.runtime import DeliveryRuntime
from reviewforge.review_delivery.store import DeliveryStore
from reviewforge.review_delivery.tools import spec


def call(name, args, n):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": str(n)}])


class Script:
    def __init__(self, steps):
        self.steps = iter(steps)
        self.calls = 0

    def bind_tools(self, tools, **kwargs):
        return self

    async def ainvoke(self, messages):
        self.calls += 1
        step = next(self.steps)
        return step(messages) if callable(step) else step


class Gateway:
    tool_specs = [spec("read_source", "Read original source", {"path": {"type": "string"}}, ["path"])]

    async def __call__(self, name, args):
        return {
            "kind": "source",
            "side": "head",
            "revision": "head",
            "path": args["path"],
            "start_line": 1,
            "end_line": 1,
            "metadata": {"status": "available"},
            "content": "Required policy: B must reject unsafe calls.\n"
            if args["path"] == "policy.md"
            else "def code(): pass\n",
        }


def last_source(messages):
    return json.loads(messages[-1].content)["source_id"]


def source(store):
    return store.list_sources()[0]["id"]


def make_store(tmp_path):
    return DeliveryStore(tmp_path / "run.db", Binding("run", "org/repo", 1, "base", "head", {}, {}))


async def test_independent_verdicts_research_and_restart(tmp_path):
    store = make_store(tmp_path)
    lead = Script(
        [
            call("read_source", {"path": "a.py"}, 1),
            call("delegate_research", {"question": "What calls code?"}, 2),
            lambda _: call(
                "submit_finding", {"claim": {"title": "A", "body": "trigger A"}, "source_ids": [source(store)]}, 3
            ),
            lambda _: call(
                "submit_finding", {"claim": {"title": "B", "body": "trigger B"}, "source_ids": [source(store)]}, 4
            ),
            call("finish_review", {"summary": "reviewed", "gaps": []}, 5),
        ]
    )
    helper = Script(
        [
            call("read_source", {"path": "a.py"}, 1),
            lambda m: call("finish_research", {"answer": "calls found", "source_ids": [last_source(m)], "gaps": []}, 2),
        ]
    )
    verifier = Script(
        [
            call("read_saved_source", {"source_id": "wrong"}, 1),
            lambda _: call("read_saved_source", {"source_id": source(store)}, 2),
            lambda _: call(
                "submit_verdict",
                {"verdict": "accept", "source_ids": [source(store)], "reason": "attempted refutation"},
                3,
            ),
            lambda _: call("read_saved_source", {"source_id": source(store)}, 4),
            lambda _: call(
                "submit_verdict",
                {
                    "verdict": "needs_evidence",
                    "source_ids": [source(store)],
                    "reason": "rule missing",
                    "missing_fact": "required policy",
                    "attempted": ["repository policy"],
                    "recovery_condition": "supply policy",
                },
                5,
            ),
        ]
    )
    deliveries = []

    async def publish():
        deliveries.append([x["finding_id"] for x in store.list_outbox()])

    runtime = DeliveryRuntime(store, Gateway(), lead, verifier, publish, helper)
    result = await runtime.run("Review change")
    assert result["status"] == "partial"
    assert [f["state"] for f in store.list_findings()] == ["accept", "needs_evidence"]
    assert len(store.list_outbox()) == 1
    assert any(len(x) == 1 for x in deliveries)
    assert store.list_research()[0]["result"]["answer"] == "calls found"
    sessions = store.list_sessions()
    assert len({s["id"] for s in sessions}) == len(sessions) == 4
    assert all(source(store) in store.delivered_sources(s["id"]) for s in sessions)
    # Resume completed sessions without resampling either accepted or blocked judgment.
    counts = lead.calls, verifier.calls, helper.calls
    assert (await runtime.run("Review change"))["status"] == "partial"
    assert (lead.calls, verifier.calls, helper.calls) == counts
    blocked = store.list_findings()[1]
    runtime.models["verifier"] = Script(
        [
            call("read_source", {"path": "policy.md"}, 1),
            lambda messages: call(
                "submit_verdict",
                {
                    "verdict": "accept",
                    "source_ids": [last_source(messages)],
                    "reason": "policy supplied; refutation attempted",
                },
                2,
            ),
        ]
    )
    await runtime.resume_finding(blocked["id"])
    assert store.get_finding(blocked["id"])["revision"] == blocked["revision"]
    assert len(store.list_outbox()) == 2
    assert (await runtime.run("Review change"))["status"] == "complete"


async def test_delivery_only_after_successful_next_request(tmp_path):
    store = make_store(tmp_path)

    class Failing(Script):
        async def ainvoke(self, messages):
            if self.calls:
                raise RuntimeError("offline")
            return await super().ainvoke(messages)

    lead = Failing([call("read_source", {"path": "a.py"}, 1)])
    runtime = DeliveryRuntime(store, Gateway(), lead, Script([]))
    import pytest

    with pytest.raises(RuntimeError, match="offline"):
        await runtime.run("Review change")
    session = store.list_sessions()[0]["id"]
    assert store.list_sources()
    assert store.delivered_sources(session) == []
    # The persisted pending request contains the source; success records its delivery.
    runtime.models["lead"] = Script([call("finish_review", {"summary": "done", "gaps": []}, 2)])
    assert (await runtime.run("Review change"))["status"] == "complete"
    assert source(store) in store.delivered_sources(session)


async def test_cross_batch_originals_are_present_in_actual_model_request(tmp_path):
    store = make_store(tmp_path)

    class DistinctGateway(Gateway):
        async def __call__(self, name, args):
            result = await super().__call__(name, args)
            result["content"] = "original " + args["path"]
            return result

    def compare(messages):
        contents = [str(m.content) for m in messages]
        assert any("original a.py" in x for x in contents)
        assert any("original b.py" in x for x in contents)
        assert any("compare before and after" in x for x in contents)
        return call("finish_review", {"summary": "compared", "gaps": []}, 5)

    def retain_and_read(messages):
        return AIMessage(
            content="",
            tool_calls=[
                {"name": "retain_source", "args": {"source_id": last_source(messages)}, "id": "retain"},
                {"name": "read_source", "args": {"path": "b.py"}, "id": "read"},
            ],
        )

    lead = Script(
        [
            call("read_source", {"path": "a.py"}, 1),
            retain_and_read,
            lambda messages: AIMessage(
                content="",
                tool_calls=[
                    {"name": "retain_source", "args": {"source_id": last_source(messages)}, "id": "retain-b"},
                    {
                        "name": "set_progress",
                        "args": {"focus": "compare before and after", "next_steps": "compare"},
                        "id": "focus",
                    },
                ],
            ),
            compare,
        ]
    )
    result = await DeliveryRuntime(store, DistinctGateway(), lead, Script([])).run("Review")
    assert result["status"] == "complete"


async def test_verifier_cannot_cite_source_only_delivered_to_lead(tmp_path):
    store = make_store(tmp_path)
    lead = Script(
        [
            call("read_source", {"path": "a.py"}, 1),
            lambda _: call("submit_finding", {"claim": {"title": "A", "body": "A"}, "source_ids": [source(store)]}, 2),
            call("finish_review", {"summary": "done", "gaps": []}, 3),
        ]
    )

    def correction(messages):
        assert "source not delivered" in str(messages[-1].content)
        return call("read_saved_source", {"source_id": source(store)}, 2)

    verifier = Script(
        [
            lambda _: call(
                "submit_verdict", {"verdict": "accept", "source_ids": [source(store)], "reason": "no refutation"}, 1
            ),
            correction,
            lambda _: call(
                "submit_verdict",
                {"verdict": "accept", "source_ids": [source(store)], "reason": "attempted refutation"},
                3,
            ),
        ]
    )
    await DeliveryRuntime(store, Gateway(), lead, verifier).run("Review")
    assert len(store.list_verdicts()) == 1
    assert store.list_verdicts()[0]["reason"] == "attempted refutation"


async def test_notes_are_marked_read_only_through_successful_snapshot(tmp_path):
    store = make_store(tmp_path)
    helper = Script(
        [
            call("add_note", {"text": "first observation"}, 1),
            call("finish_research", {"answer": "done", "source_ids": [], "gaps": []}, 2),
        ]
    )

    def concurrent_note(messages):
        lead_id = next(s["id"] for s in store.list_sessions() if s["role"] == "lead")
        task = store.list_research()[0]["id"]
        assert "first observation" in str(messages[-1].content)
        store.add_note(task, lead_id, "later observation")
        return call("finish_review", {"summary": "done", "gaps": []}, 3)

    lead = Script(
        [
            call("delegate_research", {"question": "Check callers"}, 1),
            call("read_notes", {}, 2),
            concurrent_note,
        ]
    )
    await DeliveryRuntime(store, Gateway(), lead, Script([]), research_model=helper).run("Review")
    lead_id = next(s["id"] for s in store.list_sessions() if s["role"] == "lead")
    assert store.changed_note_tasks(lead_id) == [store.list_research()[0]["id"]]


async def test_new_counterevidence_reopens_only_its_finding(tmp_path):
    store = make_store(tmp_path)
    lead = Script(
        [
            call("read_source", {"path": "a.py"}, 1),
            lambda m: call("submit_finding", {"claim": {"title": "A", "body": "A"}, "source_ids": [last_source(m)]}, 2),
            call("read_source", {"path": "counter.py"}, 3),
            lambda m: call(
                "challenge_finding",
                {
                    "finding_id": store.list_findings()[0]["id"],
                    "reason": "counterexample",
                    "source_ids": [last_source(m)],
                },
                4,
            ),
            call("finish_review", {"summary": "done", "gaps": []}, 5),
        ]
    )

    def read_counter(messages):
        context = json.loads(messages[1].content)
        assert context["revision_reason"] == "counterexample"
        return call("read_saved_source", {"source_id": context["lead_source_ids"][-1]}, 3)

    verifier = Script(
        [
            lambda _: call("read_saved_source", {"source_id": source(store)}, 1),
            lambda _: call(
                "submit_verdict", {"verdict": "accept", "source_ids": [source(store)], "reason": "no counterexample"}, 2
            ),
            read_counter,
            lambda m: call(
                "submit_verdict",
                {
                    "verdict": "reject",
                    "source_ids": [json.loads(m[-1].content)["id"]],
                    "reason": "counterexample refutes claim",
                },
                4,
            ),
        ]
    )
    await DeliveryRuntime(store, Gateway(), lead, verifier).run("Review")
    assert len(store.list_findings()) == 1
    assert store.list_findings()[0]["revision"] == 2
    assert store.list_findings()[0]["state"] == "reject"


async def test_finding_index_survives_multiple_batches_and_progress_history_is_readable(tmp_path):
    store = make_store(tmp_path)

    def check_index_and_record_progress(messages):
        working = next(m.content for m in messages if str(m.content).startswith("Current session working set"))
        payload = json.loads(working.split(": ", 1)[1])
        finding = store.list_findings()[0]
        assert payload["finding_index"] == [{"id": finding["id"], "title": "Persistent A", "state": "accept"}]
        assert payload["findings"] == []
        assert "sensitive full finding body" not in working
        return call("set_progress", {"focus": "unresolved region A", "next_steps": "inspect policy"}, 5)

    def check_history(messages):
        history = json.loads(messages[-1].content)["history"]
        assert [item["focus"] for item in history] == ["unresolved region A", "region B"]
        return call("finish_review", {"summary": "done", "gaps": []}, 8)

    lead = Script(
        [
            call("read_source", {"path": "a.py"}, 1),
            lambda m: call(
                "submit_finding",
                {
                    "claim": {"title": "Persistent A", "body": "sensitive full finding body"},
                    "source_ids": [last_source(m)],
                },
                2,
            ),
            call("read_source", {"path": "b.py"}, 3),
            call("read_source", {"path": "c.py"}, 4),
            check_index_and_record_progress,
            call("set_progress", {"focus": "region B", "next_steps": "inspect callers"}, 6),
            call("read_progress", {}, 7),
            check_history,
        ]
    )
    verifier = Script(
        [
            lambda _: call("read_saved_source", {"source_id": source(store)}, 1),
            lambda _: call(
                "submit_verdict",
                {"verdict": "accept", "source_ids": [source(store)], "reason": "refutation attempted"},
                2,
            ),
        ]
    )
    result = await DeliveryRuntime(store, Gateway(), lead, verifier).run("Review")
    assert result["status"] == "complete"
    assert len(store.list_findings()) == 1
