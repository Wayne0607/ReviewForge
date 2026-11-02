"""Source pages, delivery and decisions respect the run-bound physical-line policy."""

import hashlib
import json

import pytest

from reviewforge.review_v42.contracts import BindingError, Case, EvidenceRef, ReviewScope, Run, Task
from reviewforge.review_v42.shared_investigation import SharedInvestigations
from reviewforge.review_v42.store import ReviewStore
from reviewforge.review_v42.tools import EvidenceGateway

CONTENT = "one\u2028inside\r\nsecond\rwithin\vtext\nlast".encode("utf-8")


@pytest.fixture
def board(tmp_path):
    regions = [
        {
            "id": name,
            "path": f"{name}.py",
            "side": "head",
            "start_line": 2,
            "end_line": 2,
            "base_start_line": 2,
            "base_end_line": 2,
            "changed_lines": [2],
            "patch": "",
        }
        for name in ("a", "b")
    ]
    run = Run(
        "run",
        "org/repo",
        "base",
        "head",
        policy="v42-units-6",
        metadata={
            "workspace_digest": "head-workspace",
            "base_workspace_digest": "base-workspace",
            "changed_regions": regions,
            "shared_investigation": "shared-1",
            "source_line_policy": "git-lf-1",
        },
    )
    store = ReviewStore(tmp_path / "board.sqlite", run)
    scopes = [ReviewScope(r["id"], run.id, r["id"], regions=[r]) for r in regions]
    store.install_unit_plan(scopes, ReviewScope("integration", run.id, "integration", dependencies=["a", "b"]))
    return store


async def fixed_source(store, *, side="head", path="a.py"):
    async def gateway(name, args):
        assert name == "read_source"
        return {
            "content": CONTENT,
            "revision": getattr(store.run, side),
            "side": side,
            "kind": "source",
            "path": path,
            "start_line": 1,
            "end_line": 3,
            "metadata": {"workspace_digest": "head-workspace", "complete": True, "status": "available"},
        }

    gateway = EvidenceGateway(store, gateway)
    original = await gateway.execute("read_source", {"path": path, "side": side})
    return gateway, original


def start(store, scope="a"):
    current = store.get(ReviewScope, scope)
    task = store.add(Task("task-" + scope, store.run.id, "review", scope, current.revision))
    return store.acquire_task(task.id, "worker", 100, now=0)


def audit(store, task, turn, ref, content, start_line, end_line):
    receipt = {"evidence_ref": ref.id, "content": content, "start_line": start_line, "end_line": end_line}
    store.append_turn_audit(
        task.id, task.lease_token, turn, "request", [{"type": "tool", "data": {"content": json.dumps(receipt)}}], now=1
    )
    response = [{"type": "ai", "data": {"content": "ok"}}]
    store.append_turn_audit(task.id, task.lease_token, turn, "response", response, now=1)
    store.append_turn_audit(task.id, task.lease_token, turn, "exchange", response, now=1)


@pytest.mark.parametrize("side", ["head", "base"])
async def test_git_physical_pages_preserve_bytes_offsets_and_frontier(board, side):
    store = board
    gateway, original = await fixed_source(store, side=side)
    ref = store.get(EvidenceRef, original["evidence_ref"])
    assert store.read_evidence(ref.id) == CONTENT
    assert ref.content_hash == hashlib.sha256(CONTENT).hexdigest()
    page = await gateway.execute("read_evidence", {"ref": ref.id, "start_line": 2, "end_line": 99})
    pageref = store.get(EvidenceRef, page["evidence_ref"])
    assert (pageref.start_line, pageref.end_line) == (2, 3)
    assert store.read_evidence(pageref.id) == b"second\rwithin\x0btext\nlast"
    assert pageref.metadata["parent_evidence_ref"] == ref.id
    nested = await gateway.execute("read_evidence", {"ref": pageref.id, "start_line": 3, "end_line": 3})
    assert store.read_evidence(nested["evidence_ref"]) == b"last"
    assert store.get(EvidenceRef, nested["evidence_ref"]).start_line == 3
    task = start(store)
    entry = store.pin_frontier_for_task(task.id, task.lease_token, ref.id, start_line=1, end_line=2, now=1)
    assert store.read_frontier_entry(task.id, entry.id) == "one\u2028inside\r\nsecond\rwithin\vtext\n".encode()
    last = store.read_frontier_page(task.id, entry.id, start_line=2, end_line=2)
    assert last["content"] == b"second\rwithin\x0btext\n" and last["start_line"] == 2
    assert pageref.side == side and pageref.revision == getattr(store.run, side)


async def test_partial_page_does_not_satisfy_reopened_source_delivery(board):
    store = board
    _, original = await fixed_source(store)
    ref = store.get(EvidenceRef, original["evidence_ref"])
    scope = store.get(ReviewScope, "a")
    store.reopen_unit_for_evidence(scope.id, scope.revision, [ref.id], "new source", region_ids=["a"])
    task = start(store)
    store.pin_frontier_for_task(task.id, task.lease_token, ref.id, now=1)
    audit(store, task, "partial", ref, "one\u2028inside\r\n", 1, 1)
    observation = [{"statement": "Checked the region", "evidence_refs": [ref.id], "region_ids": ["a"]}]
    scope = store.get(ReviewScope, "a")
    kwargs = dict(checked_region_ids=["a"], observations=observation, questions=[], cases=[], unresolved=[], now=2)
    with pytest.raises(BindingError, match="fully delivered"):
        store.commit_unit_decision(task.id, task.lease_token, scope.revision, "partial", turn_id="partial", **kwargs)
    audit(store, task, "complete", ref, CONTENT.decode(), 1, 3)
    result = store.commit_unit_decision(
        task.id, task.lease_token, scope.revision, "complete", turn_id="complete", **kwargs
    )
    assert result["scope"].checked_regions == scope.regions
    assert store.pending_unit_sources() == []


async def test_counterevidence_requires_full_git_line_ref_in_current_turn(board):
    store = board
    _, original = await fixed_source(store, path="b.py")
    ref = store.get(EvidenceRef, original["evidence_ref"])
    case = store.add(Case("case", store.run.id, scope_id="b", trigger="trigger", mechanism="mechanism"))
    task = start(store)
    store.pin_frontier_for_task(task.id, task.lease_token, ref.id, now=1)
    audit(store, task, "partial", ref, "one\u2028inside\r\n", 1, 1)
    with pytest.raises(BindingError, match="fully delivered"):
        store.register_case_counterevidence(
            task.id,
            task.lease_token,
            case.id,
            [ref.id],
            turn_id="partial",
            reason="possible conflict",
            expected_revision=0,
            now=2,
        )
    audit(store, task, "complete", ref, CONTENT.decode(), 1, 3)
    changed = store.register_case_counterevidence(
        task.id,
        task.lease_token,
        case.id,
        [ref.id],
        turn_id="complete",
        reason="possible conflict",
        expected_revision=0,
        now=2,
    )
    assert changed.counter_evidence_refs == [ref.id]
    assert {entry["scope_id"] for entry in store.pending_unit_sources()} == {"b"}


async def test_resolved_shared_observation_records_exact_physical_page(board):
    store = board
    gateway, original = await fixed_source(store)
    page = await gateway.execute("read_evidence", {"ref": original["evidence_ref"], "start_line": 2, "end_line": 2})
    ref = store.get(EvidenceRef, page["evidence_ref"])
    task = start(store)
    audit(store, task, "turn", ref, page["content"], 2, 2)
    shared = SharedInvestigations(store)
    item = shared.create(
        task.id, task.lease_token, "question", ["a"], ["a"], turn_id="turn", tool_call_id="create", now=2
    )
    item = shared.claim(task.id, task.lease_token, item["id"], item["revision"], "claim", operation_id="claim", now=2)
    resolved = shared.update(
        task.id,
        task.lease_token,
        item["id"],
        item["revision"],
        question="question",
        observations=[{"statement": "source examined", "evidence_refs": [ref.id]}],
        open_questions=[],
        status="resolved",
        reason="inspected",
        turn_id="turn",
        operation_id="resolve",
        now=2,
    )
    assert resolved["observations"][0]["delivered_ranges"][ref.id] == [[2, 2]]
    assert store.read_evidence(ref.id) == b"second\rwithin\x0btext\n"
    assert [item["ref_id"] for item in store.pending_unit_sources()] == [ref.id]


async def test_exact_derived_page_can_complete_affected_region_recheck(board):
    store = board
    gateway, original = await fixed_source(store)
    page = await gateway.execute("read_evidence", {"ref": original["evidence_ref"], "start_line": 2, "end_line": 2})
    ref = store.get(EvidenceRef, page["evidence_ref"])
    scope = store.get(ReviewScope, "a")
    store.reopen_unit_for_evidence(scope.id, scope.revision, [ref.id], "new page", region_ids=["a"])
    task = start(store)
    store.pin_frontier_for_task(task.id, task.lease_token, ref.id, now=1)
    audit(store, task, "page", ref, page["content"], 2, 2)
    scope = store.get(ReviewScope, "a")
    result = store.commit_unit_decision(
        task.id,
        task.lease_token,
        scope.revision,
        "page-rechecked",
        checked_region_ids=["a"],
        observations=[{"statement": "Inspected physical line", "evidence_refs": [ref.id], "region_ids": ["a"]}],
        questions=[],
        cases=[],
        unresolved=[],
        turn_id="page",
        now=2,
    )
    assert result["scope"].checked_regions == scope.regions
    assert store.pending_unit_sources() == []
    assert store.read_evidence(ref.id) == b"second\rwithin\x0btext\n"


async def test_exact_derived_page_can_be_counterevidence(board):
    store = board
    gateway, original = await fixed_source(store, path="b.py")
    page = await gateway.execute("read_evidence", {"ref": original["evidence_ref"], "start_line": 2, "end_line": 2})
    ref = store.get(EvidenceRef, page["evidence_ref"])
    case = store.add(Case("case", store.run.id, scope_id="b", trigger="trigger", mechanism="mechanism"))
    task = start(store)
    store.pin_frontier_for_task(task.id, task.lease_token, ref.id, now=1)
    audit(store, task, "page", ref, page["content"], 2, 2)
    changed = store.register_case_counterevidence(
        task.id,
        task.lease_token,
        case.id,
        [ref.id],
        turn_id="page",
        reason="possible conflict",
        expected_revision=0,
        now=2,
    )
    assert changed.counter_evidence_refs == [ref.id]
    assert store.read_evidence(ref.id) == b"second\rwithin\x0btext\n"


async def test_same_bytes_on_base_and_head_keep_distinct_page_identities(board):
    store = board
    head_gateway, head = await fixed_source(store, side="head")
    base_gateway, base = await fixed_source(store, side="base")
    head_page = await head_gateway.execute(
        "read_evidence", {"ref": head["evidence_ref"], "start_line": 1, "end_line": 1}
    )
    base_page = await base_gateway.execute(
        "read_evidence", {"ref": base["evidence_ref"], "start_line": 1, "end_line": 1}
    )
    assert store.read_evidence(head_page["evidence_ref"]) == store.read_evidence(base_page["evidence_ref"])
    assert head_page["evidence_ref"] != base_page["evidence_ref"]


def test_unconfigured_static_and_old_run_keep_splitlines_semantics(board):
    store = board
    raw = "A\u2028B\nC".encode()
    ref = EvidenceRef(
        "ref",
        store.run.id,
        store.run.repo,
        "head",
        "head",
        "source",
        hashlib.sha256(raw).hexdigest(),
        path="a.py",
        start_line=1,
        end_line=3,
    )
    assert ReviewStore._frontier_content(ref, raw, 2, 2) == b"B\n"
    assert ReviewStore._frontier_content(ref, raw, 2, 2, run=store.run) == b"C"
    old = Run("old", "org/repo", "base", "head")
    assert ReviewStore._frontier_content(ref, raw, 2, 2, run=old) == b"B\n"


@pytest.mark.parametrize(
    "raw,expected",
    [
        (b"a\nb\n", [b"a\n", b"b\n"]),
        (b"a\r\nb\r\n", [b"a\r\n", b"b\r\n"]),
        (b"a\rb\nc", [b"a\rb\n", b"c"]),
        ("a\u2028b\nc".encode(), ["a\u2028b\n".encode(), b"c"]),
        (b"a\x0bb\nc", [b"a\x0bb\n", b"c"]),
        (b"a\n\n", [b"a\n", b"\n"]),
        (b"a", [b"a"]),
    ],
)
async def test_each_explicit_page_preserves_exact_physical_line(board, raw, expected):
    store = board
    ref = store.put_evidence(
        raw,
        revision="head",
        side="head",
        kind="source",
        path="a.py",
        start_line=1,
        end_line=len(expected),
        metadata={"workspace_digest": "head-workspace"},
    )
    gateway = EvidenceGateway(store, None)
    for index, part in enumerate(expected, 1):
        page = await gateway.execute("read_evidence", {"ref": ref.id, "start_line": index, "end_line": index})
        assert store.read_evidence(page["evidence_ref"]) == part
        assert (page["start_line"], page["end_line"]) == (index, index)
    assert b"".join(expected) == raw


async def test_old_run_tool_page_keeps_legacy_interpretation(tmp_path):
    run = Run(
        "old",
        "org/repo",
        "base",
        "head",
        policy="v42-units-6",
        metadata={"workspace_digest": "old-workspace", "shared_investigation": "shared-1"},
    )
    store = ReviewStore(tmp_path / "old.sqlite", run)
    raw = "A\u2028B\nC".encode()
    ref = store.put_evidence(
        raw,
        revision="head",
        side="head",
        kind="source",
        path="a.py",
        start_line=1,
        end_line=3,
        metadata={"workspace_digest": "old-workspace"},
    )
    result = await EvidenceGateway(store, None).execute(
        "read_evidence", {"ref": ref.id, "start_line": 2, "end_line": 2}
    )
    assert store.read_evidence(result["evidence_ref"]) == b"B\n"
