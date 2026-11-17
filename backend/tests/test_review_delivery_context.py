import itertools
import json

import pytest
from langchain_core.messages import AIMessage

from reviewforge.review_delivery.contracts import Binding
from reviewforge.review_delivery.runtime import DeliveryRuntime
from reviewforge.review_delivery.store import DeliveryStore
from reviewforge.review_delivery.tools import spec


def call(name, args):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": "call"}])


def workspace(messages):
    state = {"retained_sources": []}
    for message in messages:
        if not isinstance(message.content, str):
            continue
        if message.content.startswith("Current session working set (notes are navigation): "):
            state = json.loads(message.content.split(": ", 1)[1])
        elif message.content.startswith("Session workspace update (navigation only): "):
            delta = json.loads(message.content.split(": ", 1)[1])
            sources = {item["id"]: item for item in state["retained_sources"]}
            sources.update({item["id"]: item for item in delta.pop("retained_sources_added", [])})
            for identity in delta.pop("released_source_ids", []):
                sources.pop(identity, None)
            for key in delta.pop("unset_fields", []):
                state.pop(key, None)
            state.update(delta)
            state["retained_sources"] = list(sources.values())
    return state


def finish():
    return call("finish_review", {"summary": "done", "gaps": []})


class Script:
    def __init__(self, steps):
        self.steps = iter(steps)
        self.calls = 0
        self.warnings = 0

    def bind_tools(self, *args, **kwargs):
        return self

    async def ainvoke(self, messages):
        self.calls += 1
        self.warnings += "repeated_reading" in workspace(messages)
        step = next(self.steps)
        return step(messages) if callable(step) else step


class Gateway:
    tool_specs = [
        spec("read_source", "Read a fixed original", {"path": {"type": "string"}}, ["path"]),
    ]

    async def __call__(self, name, args):
        return {
            "kind": "source",
            "side": "base" if args["path"].startswith("base") else "head",
            "revision": "base" if args["path"].startswith("base") else "head",
            "path": args["path"],
            "start_line": 1,
            "end_line": 1,
            "metadata": {"status": "available"},
            "content": "UNIQUE ORIGINAL " + args["path"] + "\n",
        }


def store_at(tmp_path):
    return DeliveryStore(tmp_path / "review.db", Binding("run", "org/repo", 1, "base", "head", {}, {}))


async def test_originals_survive_cross_batch_comparison_without_model_memory_calls(tmp_path):
    store = store_at(tmp_path)

    def compare(messages):
        assert {s["snapshot_side"] for s in workspace(messages)["retained_sources"]} == {"base", "head"}
        text = "\n".join(str(m.content) for m in messages)
        assert text.count("UNIQUE ORIGINAL base.py") == 1
        assert text.count("UNIQUE ORIGINAL head.py") == 1
        return finish()

    lead = Script([call("read_source", {"path": "base.py"}), call("read_source", {"path": "head.py"}), compare])
    assert (await DeliveryRuntime(store, Gateway(), lead, Script([])).run("Compare base and head"))[
        "status"
    ] == "complete"
    session = store.list_sessions()[0]["id"]
    assert len(store.delivered_sources(session)) == 2
    assert not any(t["payload"]["type"] in {"retain", "progress"} for t in store.list_turns(session))


async def test_residency_restart_release_and_delivery_do_not_depend_on_live_process_state(tmp_path):
    store = store_at(tmp_path)

    def fail(messages):
        assert "UNIQUE ORIGINAL base.py" in messages[2].content
        raise RuntimeError("network unavailable")

    lead = Script([call("read_source", {"path": "base.py"}), fail])
    with pytest.raises(RuntimeError, match="network unavailable"):
        await DeliveryRuntime(store, Gateway(), lead, Script([])).run("Compare")
    session = store.list_sessions()[0]["id"]
    assert store.delivered_sources(session) == []
    source = store.list_sources()[0]["id"]
    store.close()
    store = store_at(tmp_path)

    def release(messages):
        assert len(workspace(messages)["retained_sources"]) == 2
        return call("release_source", {"source_id": source})

    def after_release(messages):
        assert "UNIQUE ORIGINAL base.py" not in "\n".join(str(m.content) for m in messages)
        assert "UNIQUE ORIGINAL head.py" in messages[2].content
        return finish()

    resumed = Script([call("read_source", {"path": "head.py"}), release, after_release])
    assert (await DeliveryRuntime(store, Gateway(), resumed, Script([])).run("Compare"))["status"] == "complete"
    assert len(store.delivered_sources(session)) == 2
    assert len(store.list_sources()) == 2


async def test_navigation_is_not_automatically_resident_or_promoted_to_evidence(tmp_path):
    store = store_at(tmp_path)

    class Navigation(Gateway):
        async def __call__(self, name, args):
            value = await super().__call__(name, args)
            return {
                **value,
                "kind": "query",
                "content": "UNTRUSTED NAVIGATION",
                "metadata": {"status": "available", "navigation_only": True},
            }

    def check(messages):
        assert workspace(messages)["retained_sources"] == []
        assert "UNTRUSTED NAVIGATION" not in "\n".join(str(m.content) for m in messages)
        history = workspace(messages)["read_history_navigation_only"]
        assert history[0]["tool"] == "read_source" and history[0]["args"] == {"path": "query"}
        assert history[0]["sources"][0]["kind"] == "navigation"
        assert history[0]["ok"] is True
        return finish()

    lead = Script([call("read_source", {"path": "query"}), call("list_sources", {}), check])
    assert (await DeliveryRuntime(store, Navigation(), lead, Script([])).run("Review"))["status"] == "complete"
    assert store.list_sources()[0]["kind"] == "navigation"


async def test_successful_alternating_reads_warn_then_halt_durably_and_recover_with_new_material(tmp_path):
    store = store_at(tmp_path)
    lead = Script(itertools.cycle([call("read_source", {"path": "base.py"}), call("read_source", {"path": "head.py"})]))
    runtime = DeliveryRuntime(store, Gateway(), lead, Script([]))
    result = await runtime.run("Compare")
    assert result["status"] == "partial" and "after correction" in result["reason"]
    assert lead.warnings >= 1
    assert store.list_verdicts() == store.list_outbox() == []
    before = lead.calls
    assert (await runtime.run("Compare"))["status"] == "partial"
    assert lead.calls == before
    store.close()
    store = store_at(tmp_path)
    resumed = Script([])
    assert (await DeliveryRuntime(store, Gateway(), resumed, Script([])).run("Compare"))["status"] == "partial"
    assert resumed.calls == 0
    store.put_source("original", "head", b"New authoritative policy", {"path": "new-policy.md", "revision": "head"})
    resumed = Script([finish()])
    assert (await DeliveryRuntime(store, Gateway(), resumed, Script([])).run("Compare"))["status"] == "complete"


async def test_correction_allows_new_reads_and_has_no_distinct_read_quota(tmp_path):
    store = store_at(tmp_path)
    steps = [call("read_source", {"path": "base.py"}), call("read_source", {"path": "base.py"})]
    steps += [call("read_source", {"path": f"source-{n}.py"}) for n in range(15)]
    steps.append(finish())
    lead = Script(steps)
    assert (await DeliveryRuntime(store, Gateway(), lead, Script([])).run("Review"))["status"] == "complete"
    assert lead.warnings == 1
    assert len(store.list_sources()) == 16


async def test_reading_cycle_cannot_erase_or_resample_a_previously_accepted_finding(tmp_path):
    store = store_at(tmp_path)

    def original_id():
        return store.list_sources()[0]["id"]

    lead = Script(
        itertools.chain(
            [
                call("read_source", {"path": "head.py"}),
                lambda _: call(
                    "submit_finding",
                    {
                        "claim": {"title": "Defect", "body": "Concrete trigger and impact"},
                        "source_ids": [original_id()],
                    },
                ),
            ],
            itertools.repeat(call("read_source", {"path": "head.py"})),
        )
    )
    verifier = Script(
        [
            lambda _: call("read_saved_source", {"source_id": original_id()}),
            lambda _: call(
                "submit_verdict", {"verdict": "accept", "source_ids": [original_id()], "reason": "Refutation checked"}
            ),
        ]
    )
    runtime = DeliveryRuntime(store, Gateway(), lead, verifier)
    assert (await runtime.run("Review"))["status"] == "partial"
    accepted = store.list_findings()[0]
    assert accepted["state"] == "accept" and len(store.list_outbox()) == 1
    count = lead.calls, verifier.calls
    assert (await runtime.run("Review"))["status"] == "partial"
    assert (lead.calls, verifier.calls) == count
    assert store.list_findings()[0] == accepted and len(store.list_verdicts()) == 1


async def test_diagnostic_execution_is_not_mistaken_for_cached_source_rereading(tmp_path):
    store = store_at(tmp_path)

    class Checks(Gateway):
        tool_specs = [*Gateway.tool_specs, spec("run_check", "Execute diagnostic", {}, [])]

        async def __call__(self, name, args):
            result = await super().__call__("read_source", {"path": "test.py"})
            return {**result, "kind": "test", "content": "Test passed"}

    lead = Script([*(call("run_check", {}) for _ in range(8)), finish()])
    assert (await DeliveryRuntime(store, Checks(), lead, Script([])).run("Verify"))["status"] == "complete"
    assert lead.warnings == 0


async def test_visible_new_observations_allow_rereading_without_requiring_set_progress(tmp_path):
    store = store_at(tmp_path)
    steps = []
    for observation in [
        "Check time units",
        "Check caller conversion",
        "Check default duration",
        "Check comparison boundary",
    ]:
        message = call("read_source", {"path": "base.py"})
        message.content = observation
        steps.append(message)
    lead = Script([*steps, finish()])
    assert (await DeliveryRuntime(store, Gateway(), lead, Script([])).run("Compare"))["status"] == "complete"
    assert lead.warnings == 0


async def test_recombining_old_reads_cannot_evade_cycle_detection(tmp_path):
    store = store_at(tmp_path)

    def batch(paths):
        return AIMessage(
            content="",
            tool_calls=[{"name": "read_source", "args": {"path": p}, "id": str(n)} for n, p in enumerate(paths)],
        )

    lead = Script(
        [
            batch(["base.py"]),
            batch(["head.py"]),
            batch(["base.py", "head.py"]),
            batch(["base.py"]),
            batch(["head.py", "base.py", "base.py"]),
        ]
    )
    result = await DeliveryRuntime(store, Gateway(), lead, Script([])).run("Compare")
    assert result["status"] == "partial" and "after correction" in result["reason"]
    assert lead.warnings == 1
    session = store.list_sessions()[0]["id"]
    history = DeliveryRuntime(store, Gateway(), lead, Script([]))._working_set(session, "lead")[0]
    assert len(history["read_history_navigation_only"]) == 2
    assert len(history["retained_sources"]) == 2
