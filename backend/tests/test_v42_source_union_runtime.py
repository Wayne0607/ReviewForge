"""Opt-in source projection is bound to a fresh run and real source delivery."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage
from test_v42_fair_runtime import receipts
from test_v42_integration import NativeModel, product
from test_v42_stateful_reviewer import call
from test_v42_units_product import FixedBaseGitHub, TwoFileWorkspace, two_file_state

import reviewforge.review_v42.integration as integration
from reviewforge.review_v42.contracts import Run
from reviewforge.review_v42.runtime import ReviewRuntime
from reviewforge.review_v42.shared_investigation import SharedInvestigations
from reviewforge.review_v42.store import ReviewStore


@pytest.mark.parametrize("value,shared", [("invalid", "shared-1"), ("source-line-union-1", "")])
async def test_invalid_product_option_fails_before_provider_or_board(tmp_path, monkeypatch, value, shared):
    monkeypatch.setenv("REVIEWFORGE_V42_FRONTIER_PROJECTION", value)
    if shared:
        monkeypatch.setenv("REVIEWFORGE_V42_SHARED_INVESTIGATION", shared)
    else:
        monkeypatch.delenv("REVIEWFORGE_V42_SHARED_INVESTIGATION", raising=False)
    model = NativeModel(lambda *_: pytest.fail("invalid option must not call the provider"))
    obj, _ = product(tmp_path, monkeypatch, model, model, TwoFileWorkspace(), policy=None)
    with pytest.raises(ValueError, match="unsupported v4.2 frontier projection policy"):
        await obj.run(two_file_state())
    assert not model._inputs and not list(tmp_path.rglob("board.sqlite"))


async def test_product_rejects_union_with_incompatible_kernel(tmp_path, monkeypatch):
    monkeypatch.setenv("REVIEWFORGE_V42_FRONTIER_PROJECTION", "source-line-union-1")
    monkeypatch.setenv("REVIEWFORGE_V42_SHARED_INVESTIGATION", "shared-1")
    monkeypatch.setattr(integration, "EXECUTION_POLICY", "v42-frontier-5")
    model = NativeModel(lambda *_: pytest.fail("incompatible policy cannot call the provider"))
    obj, _ = product(tmp_path, monkeypatch, model, model, TwoFileWorkspace(), policy=None)
    with pytest.raises(ValueError, match="unsupported v4.2"):
        await obj.run(two_file_state())
    assert not model._inputs and not list(tmp_path.rglob("board.sqlite"))


@pytest.mark.parametrize(
    "policy,metadata",
    [
        (
            "v42-units-6",
            {
                "frontier_projection_policy": "invalid",
                "shared_investigation": "shared-1",
                "source_line_policy": "git-lf-1",
            },
        ),
        ("v42-units-6", {"frontier_projection_policy": "source-line-union-1", "source_line_policy": "git-lf-1"}),
        ("v42-units-6", {"frontier_projection_policy": "source-line-union-1", "shared_investigation": "shared-1"}),
        (
            "v42-frontier-5",
            {
                "frontier_projection_policy": "source-line-union-1",
                "shared_investigation": "shared-1",
                "source_line_policy": "git-lf-1",
            },
        ),
    ],
)
def test_runtime_rejects_invalid_projection_before_dispatch(policy, metadata):
    store = SimpleNamespace(run=Run("r", "repo", "base", "head", policy=policy, metadata=metadata))
    with pytest.raises(ValueError, match="unsupported v4.2 frontier projection policy"):
        ReviewRuntime(store, None, object(), object())


async def test_product_option_is_identity_bound_and_does_not_upgrade_old_run(tmp_path, monkeypatch):
    monkeypatch.setenv("REVIEWFORGE_V42_SHARED_INVESTIGATION", "shared-1")
    monkeypatch.delenv("REVIEWFORGE_V42_FRONTIER_PROJECTION", raising=False)
    model = NativeModel(lambda *_: AIMessage(content="No decision yet"))
    obj, _ = product(tmp_path, monkeypatch, model, model, TwoFileWorkspace(), policy=None)
    obj._gateway._github = FixedBaseGitHub()
    old = await obj.run(two_file_state())
    assert "frontier_projection_policy" not in old["review_v42_report"]["run"]["metadata"]
    monkeypatch.setenv("REVIEWFORGE_V42_FRONTIER_PROJECTION", "source-line-union-1")
    new = await obj.run(two_file_state())
    assert new["run_id"] != old["run_id"]
    metadata = new["review_v42_report"]["run"]["metadata"]
    assert metadata["frontier_projection_policy"] == "source-line-union-1"
    assert metadata["source_line_policy"] == "git-lf-1" and metadata["shared_investigation"] == "shared-1"
    assert (await obj.run(two_file_state()))["run_id"] == new["run_id"]


@pytest.mark.parametrize("enable_union", [False, True])
async def test_runtime_can_use_union_delivery_for_owned_region(tmp_path, enable_union):
    region = {"id": "one", "path": "a.py", "side": "head", "start_line": 1, "end_line": 5, "changed_lines": [1]}
    metadata = {
        "changed_regions": [region],
        "workspace_digest": "head-snapshot",
        "base_workspace_digest": "base-snapshot",
        "shared_investigation": "shared-1",
        "source_line_policy": "git-lf-1",
    }
    if enable_union:
        metadata["frontier_projection_policy"] = "source-line-union-1"
    run = Run(
        "r",
        "repo",
        "base",
        "head",
        policy="v42-units-6",
        metadata=metadata,
    )
    store = ReviewStore(tmp_path / "board.db", run)
    SharedInvestigations(store)

    class Gateway:
        tool_specs = [
            {
                "type": "function",
                "function": {
                    "name": "read_source",
                    "description": "Read original source",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "start_line": {"type": "integer"},
                            "end_line": {"type": "integer"},
                        },
                        "required": ["path"],
                    },
                },
            }
        ]

        async def __call__(self, name, args):
            lines = ["one\n", "two\n", "three\n", "four\n", "five\n"] if args["path"] == "a.py" else ["other\n"]
            start, end = args.get("start_line", 1), min(args.get("end_line", len(lines)), len(lines))
            return {
                "content": "".join(lines[start - 1 : end]),
                "revision": "head",
                "side": "head",
                "kind": "source",
                "path": args["path"],
                "start_line": start,
                "end_line": end,
                "metadata": {"workspace_digest": "head-snapshot", "status": "available"},
            }

    class Model:
        primary_turn = 0
        refs = []
        union_seen = False

        def bind_tools(self, tools, **kwargs):
            return self

        async def ainvoke(self, messages):
            board = json.loads(messages[1].content)
            recent = [json.loads(m.content) for m in messages if m.type == "tool"]
            if board["unit_role"] == "integration":
                refs = [item["evidence_ref"] for item in recent if item.get("evidence_ref")]
                if not refs:
                    return AIMessage(content="", tool_calls=[call("read_source", {"path": "a.py"}, "int-read")])
                return AIMessage(
                    content="",
                    tool_calls=[
                        *receipts(board["integration_obligations"], refs),
                        call("finish_integration", {}, "int-finish"),
                    ],
                )
            for item in recent:
                if item.get("path") == "a.py" and item.get("evidence_ref"):
                    self.refs.append(item["evidence_ref"])
            self.primary_turn += 1
            if self.primary_turn == 1:
                return AIMessage(
                    content="",
                    tool_calls=[call("read_source", {"path": "a.py", "start_line": 1, "end_line": 3}, "first")],
                )
            if self.primary_turn == 2:
                return AIMessage(
                    content="",
                    tool_calls=[call("read_source", {"path": "a.py", "start_line": 3, "end_line": 5}, "second")],
                )
            if self.primary_turn == 3:
                return AIMessage(content="", tool_calls=[call("read_source", {"path": "b.py"}, "other")])
            frontier = next(
                json.loads(m.content) for m in messages if m.type == "human" and '"frontier_revision"' in m.content
            )
            unions = [
                entry
                for entry in frontier["entries"]
                if entry.get("kind") == "source_union" and entry["path"] == "a.py"
            ]
            if enable_union:
                assert len(unions) == 1 and unions[0]["content"] == "one\ntwo\nthree\nfour\nfive\n"
                assert {item["ref_id"] for item in unions[0]["mappings"]} == set(self.refs)
                assert {(item["start_line"], item["end_line"]) for item in unions[0]["mappings"]} == {(1, 3), (3, 5)}
            else:
                assert not unions
                originals = [
                    entry for entry in frontier["entries"] if entry.get("kind") == "source" and entry["path"] == "a.py"
                ]
                assert {entry["content"] for entry in originals} == {"one\ntwo\nthree\n", "three\nfour\nfive\n"}
            assert len(recent) == 1 and recent[0]["path"] == "b.py" and recent[0]["content"] == "other\n"
            self.union_seen = True
            return AIMessage(
                content="",
                tool_calls=[
                    call(
                        "submit_unit_decision",
                        {
                            "checked_region_ids": ["one"],
                            "observations": [
                                {
                                    "statement": "Read both original pages",
                                    "evidence_refs": self.refs,
                                    "region_ids": ["one"],
                                }
                            ],
                            "unresolved_refs": [],
                            "finish": True,
                        },
                        "finish",
                    )
                ],
            )

    model = Model()
    result = await asyncio.wait_for(ReviewRuntime(store, model, Gateway(), object()).run([region]), 15)
    assert result["status"] == "completed", result
    assert model.union_seen
    assert {store.read_evidence(ref).decode() for ref in model.refs} == {"one\ntwo\nthree\n", "three\nfour\nfive\n"}
