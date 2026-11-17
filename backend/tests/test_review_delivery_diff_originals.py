import json
from types import SimpleNamespace

import pytest

from reviewforge.core.state import StateStore
from reviewforge.review_delivery.contracts import Binding
from reviewforge.review_delivery.excerpts import original_ranges
from reviewforge.review_delivery.runtime import DeliveryRuntime
from reviewforge.review_delivery.store import DeliveryStore
from reviewforge.review_v42.adapter import PinnedToolGateway
from reviewforge.review_v42.snapshot_diff import pinned_regions
from tests.test_review_delivery_context import Script, call, finish, workspace
from tests.test_review_delivery_entry import BASE, HEAD, Workspace


async def setup(tmp_path):
    base = Workspace(BASE, b"result = 1\nother = 2\n")
    head = Workspace(HEAD, b"result = 0\nother = None\n")
    run = SimpleNamespace(metadata={"source_line_policy": "git-lf-1"})
    fixed = await pinned_regions(base, head, run=run)
    state = StateStore(
        repo="org/repo",
        head_sha=HEAD,
        base_sha=BASE,
        file_diffs={"logic.py": "\n".join(region["patch"] for region in fixed["regions"])},
    )
    gateway = PinnedToolGateway(head, state, base_workspace=base, run=run)
    store = DeliveryStore(tmp_path / "run.db", Binding("run", "org/repo", 1, BASE, HEAD, {}, {}))
    return store, gateway, fixed["regions"]


@pytest.mark.parametrize("native", [False, True])
async def test_diff_delivers_independent_originals_once_without_promoting_navigation(tmp_path, native):
    store, gateway, regions = await setup(tmp_path)

    def compare(messages):
        sources = workspace(messages)["retained_sources"]
        assert {source["snapshot_side"] for source in sources} == {"base", "head"}
        text = "\n".join(str(message.content) for message in messages)
        assert text.count("result = 1") == 2  # one diff line, one original
        assert text.count("result = 0") == 2
        receipt = json.loads(messages[-1].content)
        assert receipt["kind"] == "diff" and receipt["metadata"]["navigation_only"]
        assert len(receipt["originals"]) == 2 and not receipt["evidence_gaps"]
        assert all("content" not in source and "content_in_working_set" in source for source in receipt["originals"])
        assert len({receipt["source_id"], *(source["source_id"] for source in receipt["originals"])}) == 3
        assert all(not store.delivered_sources(session["id"]) for session in store.list_sessions())
        return finish()

    lead = Script([call("read_diff", {"path": "logic.py"}), compare])
    policy = {"lead": {"native_history": native}}
    result = await DeliveryRuntime(
        store, gateway, lead, Script([]), changed_regions=regions, conversation_protocols=policy
    ).run("Compare the change")
    assert result["status"] == "complete"
    session = store.list_sessions()[0]["id"]
    assert len(store.delivered_sources(session)) == 3
    assert sorted(source["kind"] for source in store.list_sources()) == ["navigation", "original", "original"]
    before = lead.calls
    assert (await DeliveryRuntime(store, gateway, lead, Script([]), changed_regions=regions).run("Compare"))[
        "status"
    ] == "complete"
    assert lead.calls == before


async def test_diff_reuses_own_covering_original_but_does_not_grant_another_session_delivery(tmp_path):
    store, gateway, regions = await setup(tmp_path)

    def compare(messages):
        sources = workspace(messages)["retained_sources"]
        assert len(sources) == 2
        assert len(store.list_sources()) == 3
        return finish()

    lead = Script(
        [call("read_source", {"path": "logic.py", "side": "base"}), call("read_diff", {"path": "logic.py"}), compare]
    )
    runtime = DeliveryRuntime(store, gateway, lead, Script([]), changed_regions=regions)
    await runtime.run("Compare")
    separate = store.create_session("research")
    assert store.delivered_sources(separate["id"]) == []
    assert runtime._working_set(separate["id"], "research")[0]["retained_sources"] == []
    assert len(store.delivered_sources(store.list_sessions()[0]["id"])) == 3


@pytest.mark.parametrize("failure", ["unavailable", "wrong_revision"])
async def test_companion_failure_preserves_navigation_and_concrete_evidence_gap(tmp_path, failure):
    store, gateway, regions = await setup(tmp_path)

    class Failing:
        tool_specs = gateway.tool_specs

        async def __call__(self, name, args):
            if name == "read_source" and args.get("side") == "base":
                if failure == "unavailable":
                    raise RuntimeError("base workspace unavailable")
                return {**(await gateway(name, args)), "revision": "unbound"}
            return await gateway(name, args)

    def compare(messages):
        receipt = json.loads(messages[-1].content)
        assert receipt["kind"] == "diff" and "@@" in receipt["content"]
        assert len(receipt["originals"]) == len(receipt["evidence_gaps"]) == 1
        assert receipt["evidence_gaps"][0]["side"] == "base"
        assert {source["snapshot_side"] for source in workspace(messages)["retained_sources"]} == {"head"}
        return call("finish_review", {"summary": "base unavailable", "gaps": [json.dumps(receipt["evidence_gaps"][0])]})

    result = await DeliveryRuntime(
        store,
        Failing(),
        Script([call("read_diff", {"path": "logic.py"}), compare]),
        Script([]),
        changed_regions=regions,
    ).run("Compare")
    assert result["status"] == "partial"
    assert store.list_verdicts() == []


async def test_companion_originals_are_not_delivered_before_a_successful_model_response(tmp_path):
    store, gateway, regions = await setup(tmp_path)

    def disconnect(messages):
        assert len(workspace(messages)["retained_sources"]) == 2
        raise RuntimeError("connection interrupted")

    with pytest.raises(RuntimeError, match="connection interrupted"):
        await DeliveryRuntime(
            store,
            gateway,
            Script([call("read_diff", {"path": "logic.py"}), disconnect]),
            Script([]),
            changed_regions=regions,
        ).run("Compare")
    session = store.list_sessions()[0]["id"]
    assert len(store.list_sources()) == 3
    assert not store.delivered_sources(session)
    assert (
        await DeliveryRuntime(store, gateway, Script([finish()]), Script([]), changed_regions=regions).run("Compare")
    )["status"] == "complete"
    assert len(store.delivered_sources(session)) == 3


def test_ranges_merge_and_preserve_empty_and_unsupported_sides():
    regions = [
        {"id": "one", "path": "x", "patch": "@@ -2,3 +2,2 @@"},
        {"id": "two", "path": "x", "patch": "@@ -4,2 +3,2 @@"},
        {"id": "add", "path": "new", "patch": "@@ -0,0 +1,2 @@"},
        {"id": "delete", "path": "deleted", "patch": "@@ -1,2 +0,0 @@", "side": "base"},
        {"id": "binary", "path": "binary", "patch": "", "unsupported": True, "reason": "binary"},
    ]
    assert original_ranges(regions, "x") == (
        [
            {"path": "x", "side": "base", "start_line": 2, "end_line": 5},
            {"path": "x", "side": "head", "start_line": 2, "end_line": 4},
        ],
        [],
    )
    assert [item["side"] for item in original_ranges(regions, "new")[0]] == ["head"]
    assert [item["side"] for item in original_ranges(regions, "deleted")[0]] == ["base"]
    assert original_ranges(regions, "binary")[1][0]["reason"] == "binary"
    assert original_ranges(regions, "unchanged") == ([], [])
