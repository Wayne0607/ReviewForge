"""Offline contract tests across the pinned adapter, evidence gateway and reviewer."""

import json
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage

from reviewforge.review_v42.adapter import PinnedToolGateway
from reviewforge.review_v42.contracts import EvidenceRef, Question, Run
from reviewforge.review_v42.reviewer import Reviewer
from reviewforge.review_v42.scope import compile_scopes
from reviewforge.review_v42.snapshot_diff import pinned_regions
from reviewforge.review_v42.store import ReviewStore
from reviewforge.review_v42.tools import EvidenceGateway


class Snapshot:
    source = "tarball"
    truncated = False

    def __init__(self, revision, files):
        self.head_sha = revision
        self.files = files

    def list_paths(self):
        return list(self.files)

    def read_bytes(self, path):
        return self.files[path]


class ScriptedModel:
    def __init__(self, turns):
        self.turns = iter(turns)
        self.inputs = []

    def bind_tools(self, tools, **kwargs):
        return self

    async def ainvoke(self, messages):
        self.inputs.append(messages.copy())
        return next(self.turns)


def invoke(name, arguments, ident):
    return {"name": name, "args": arguments, "id": ident}


async def make_review(tmp_path):
    base = Snapshot("base", {"same.ts": b"first\r\nsecond\r\n", "changed.ts": b"old\n"})
    head = Snapshot("head", {"same.ts": b"first\r\nsecond\r\n", "changed.ts": b"new\n"})
    fixed = await pinned_regions(base, head)
    regions = fixed["regions"]
    assert regions and all(region["path"] == "changed.ts" and not region["unsupported"] for region in regions)
    diffs = {path: "\n".join(r["patch"] for r in regions if r["path"] == path) for path in {r["path"] for r in regions}}
    state = SimpleNamespace(
        repo="a/b", head_sha="head", base_sha="base", linked_issues=[], files_changed=list(diffs), file_diffs=diffs
    )
    run = Run(id="r", repo="a/b", base="base", head="head", metadata={"changed_regions": regions})
    store = ReviewStore(tmp_path / "board.db", run)
    scope = compile_scopes(run, regions)[0]
    store.add(scope)
    return base, head, state, store, scope


async def test_fixed_snapshot_queries_complete_without_false_unresolved(tmp_path):
    base, head, state, store, scope = await make_review(tmp_path)
    gateway = EvidenceGateway(store, PinnedToolGateway(head, state, base_workspace=base))
    calls = [
        invoke("read_source", {"path": "same.ts", "start_line": 1, "end_line": 80}, "source"),
        invoke("read_diff", {"path": "same.ts"}, "unchanged"),
        invoke("read_source", {"path": "missing.ts"}, "missing"),
        invoke("read_source", {"path": "same.ts", "start_line": 3, "end_line": 80}, "beyond"),
    ]
    model = ScriptedModel(
        [
            AIMessage(content="", tool_calls=calls),
            AIMessage(
                content="",
                tool_calls=[invoke("finish_scope", {"checked_regions": [r["id"] for r in scope.regions]}, "finish")],
            ),
        ]
    )
    assert await Reviewer(store, model, gateway).run(scope.id) == "reviewed"
    assert not store.list(Question)
    receipts = {m.tool_call_id: json.loads(m.content) for m in model.inputs[-1] if m.type == "tool"}
    source = receipts["source"]
    assert source["kind"] == "source" and source["content"] == "first\r\nsecond\r\n"
    assert (source["start_line"], source["end_line"]) == (1, 2)
    assert source["metadata"]["requested_end_line"] == 80
    assert source["metadata"]["file_lines"] == 2 and source["metadata"]["at_eof"] is True
    assert store.read_evidence(source["evidence_ref"]) == b"first\r\nsecond\r\n"
    for ident, status in (("unchanged", "not_changed"), ("missing", "not_found"), ("beyond", "out_of_range")):
        receipt = receipts[ident]
        assert receipt["kind"] == "query" and receipt["metadata"]["status"] == status
        assert receipt["revision"] == "head" and receipt["metadata"]["navigation_only"] is True
        assert store.get(EvidenceRef, receipt["evidence_ref"]).kind == "query"
    assert json.loads(receipts["unchanged"]["content"])["present"] == {"base": True, "head": True}
    assert json.loads(receipts["beyond"]["content"])["file_lines"] == 2


@pytest.mark.parametrize(
    ("fault", "reason"),
    [
        ("closed", "complete fixed tarball workspace is required"),
        ("tampered", "fixed workspace contents changed"),
        ("missing_base", "diff absence requires complete fixed base and head snapshots"),
    ],
)
async def test_snapshot_faults_remain_unresolved_with_diagnostics(tmp_path, fault, reason):
    base, head, state, store, scope = await make_review(tmp_path)
    if fault == "closed":
        head._closed = True
    elif fault == "tampered":
        head.files["same.ts"] = b"tampered\n"
    adapter = PinnedToolGateway(head, state, base_workspace=None if fault == "missing_base" else base)
    model = ScriptedModel(
        [
            AIMessage(content="", tool_calls=[invoke("read_diff", {"path": "same.ts"}, "failed")]),
            AIMessage(
                content="",
                tool_calls=[invoke("finish_scope", {"checked_regions": [r["id"] for r in scope.regions]}, "finish")],
            ),
        ]
    )
    assert await Reviewer(store, model, EvidenceGateway(store, adapter)).run(scope.id) == "partial"
    question = store.list(Question)[0]
    assert question.owner == "tool_controller" and question.status == "unresolved"
    receipt = json.loads(next(m.content for m in model.inputs[-1] if m.type == "tool"))
    assert receipt["question_id"] == question.id and reason in receipt["error"]
    assert not store.list(EvidenceRef)
