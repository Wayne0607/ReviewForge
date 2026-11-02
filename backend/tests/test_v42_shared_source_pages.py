"""Shared investigations can cite a controller-derived, fully delivered source page."""

import hashlib
import json

import pytest
from langchain_core.messages import AIMessage
from test_v42_stateful_reviewer import Gateway, ScriptedModel, call

from reviewforge.review_v42.contracts import BindingError, EvidenceRef, ReviewScope, Run, Task
from reviewforge.review_v42.reviewer import Reviewer
from reviewforge.review_v42.runtime import ReviewRuntime
from reviewforge.review_v42.shared_investigation import SharedInvestigations
from reviewforge.review_v42.store import ReviewStore
from reviewforge.review_v42.tools import EvidenceGateway, InvalidEvidenceRequestError


def board(tmp_path, *, shared=True):
    region = {
        "id": "changed",
        "path": "a.py",
        "side": "head",
        "start_line": 2,
        "end_line": 3,
        "base_start_line": 2,
        "base_end_line": 3,
        "changed_lines": [2, 3],
        "patch": "@@ change",
    }
    metadata = {"changed_regions": [region], "workspace_digest": "ws", "base_workspace_digest": "base-ws"}
    if shared:
        metadata["shared_investigation"] = "shared-1"
    run = Run("r", "org/repo", "base", "head", policy="v42-units-6", metadata=metadata)
    store = ReviewStore(tmp_path / "board.db", run)
    scope = ReviewRuntime(store, None, None, None).prepare([region])[0]
    return store, scope


def source(store, *, side="head", path="a.py", metadata=None, content=b"one\ntwo\nthree\nfour\n"):
    return store.put_evidence(
        content,
        revision=getattr(store.run, side),
        side=side,
        kind="source",
        path=path,
        start_line=1,
        end_line=4,
        environment="env",
        metadata=metadata
        if metadata is not None
        else {
            "workspace_digest": "ws",
            "status": "available",
            "complete": True,
            "tool_name": "read_source",
            "tool_args": {"path": path, "side": side},
        },
    )


async def test_page_identity_origin_eof_subpages_and_non_source(tmp_path):
    store, _ = board(tmp_path)
    gateway = EvidenceGateway(store, Gateway())
    original = source(store)
    page = await gateway.execute("read_evidence", {"ref": original.id, "start_line": 2, "end_line": 30})
    ref = store.get(EvidenceRef, page["evidence_ref"])
    assert page["original_evidence_ref"] == original.id
    assert page["requested_end_line"] == 30 and page["actual_end_line"] == 4
    assert page["complete"] is False and page["next_line"] is None
    assert page["content"] == "two\nthree\nfour\n" and (ref.start_line, ref.end_line) == (2, 4)
    assert ref.content_hash == hashlib.sha256(page["content"].encode()).hexdigest()
    assert (ref.repo, ref.revision, ref.side, ref.path, ref.environment) == (
        original.repo,
        original.revision,
        original.side,
        original.path,
        original.environment,
    )
    assert ref.metadata["parent_evidence_ref"] == original.id
    assert ref.metadata["tool_name"] == "read_source" and ref.metadata["workspace_digest"] == "ws"
    assert ref.metadata["complete"] is True
    assert (await gateway.execute("read_evidence", {"ref": original.id}))["content"] == "one\ntwo\nthree\nfour\n"
    nested = await gateway.execute("read_evidence", {"ref": ref.id, "start_line": 3, "end_line": 3})
    assert nested["original_evidence_ref"] == ref.id and nested["content"] == "three\n"
    nested_ref = store.get(EvidenceRef, nested["evidence_ref"])
    assert (nested_ref.start_line, nested_ref.end_line) == (3, 3)
    assert nested_ref.metadata["parent_evidence_ref"] == ref.id
    with pytest.raises(InvalidEvidenceRequestError):
        await gateway.execute("read_evidence", {"ref": ref.id, "start_line": 1, "end_line": 1})
    same = await gateway.execute("read_evidence", {"ref": original.id, "start_line": 2, "end_line": 4})
    assert same["evidence_ref"] == ref.id
    other = source(store, side="base", path="b.py")
    other_page = await gateway.execute("read_evidence", {"ref": other.id, "start_line": 2, "end_line": 4})
    other_ref = store.get(EvidenceRef, other_page["evidence_ref"])
    assert other_ref.content_hash == ref.content_hash and other_ref.id != ref.id
    assert (other_ref.side, other_ref.revision, other_ref.path) == ("base", "base", "b.py")
    diff = store.put_evidence(b"one\ntwo\n", revision="head", side="head", kind="diff", path="a.py")
    assert (await gateway.execute("read_evidence", {"ref": diff.id, "start_line": 1, "end_line": 1}))[
        "evidence_ref"
    ] == diff.id
    external = store.put_evidence(
        b"external\nsource\n",
        revision="snapshot",
        side="external",
        kind="source",
        path="external",
        metadata={
            "source_url": "https://example.test/source",
            "captured_at": "2026-01-01T00:00:00+00:00",
            "historical_head_time": False,
        },
    )
    result = await gateway.execute("read_evidence", {"ref": external.id, "start_line": 1, "end_line": 1})
    assert result["evidence_ref"] == external.id and "original_evidence_ref" not in result


@pytest.mark.parametrize(
    "metadata",
    [
        {"workspace_digest": "wrong"},
        {"workspace_digest": "ws", "complete": False},
        {"workspace_digest": "ws", "navigation_only": True},
        {"workspace_digest": "ws", "status": "error"},
        {"workspace_digest": "ws", "tool_name": "search_code"},
    ],
)
async def test_unusable_source_cannot_be_minted_as_page(tmp_path, metadata):
    store, _ = board(tmp_path)
    parent = source(store, metadata=metadata)
    with pytest.raises(BindingError, match="available, complete"):
        await EvidenceGateway(store, Gateway()).execute(
            "read_evidence", {"ref": parent.id, "start_line": 2, "end_line": 3}
        )
    assert store.list(EvidenceRef) == [parent]


async def test_old_mode_keeps_parent_page_semantics(tmp_path):
    store, _ = board(tmp_path, shared=False)
    parent = source(store)
    result = await EvidenceGateway(store, Gateway()).execute(
        "read_evidence", {"ref": parent.id, "start_line": 2, "end_line": 30}
    )
    assert result["evidence_ref"] == parent.id and "original_evidence_ref" not in result
    assert result["end_line"] == 4 and len(store.list(EvidenceRef)) == 1


async def test_corrupt_parent_does_not_create_page(tmp_path):
    store, _ = board(tmp_path)
    parent = source(store)
    (store.blob_dir / parent.content_hash).write_bytes(b"corrupted")
    with pytest.raises(BindingError, match="hash mismatch"):
        await EvidenceGateway(store, Gateway()).execute(
            "read_evidence", {"ref": parent.id, "start_line": 2, "end_line": 3}
        )
    assert store.list(EvidenceRef) == [parent]


async def test_delivered_page_can_resolve_a_shared_investigation(tmp_path):
    store, scope = board(tmp_path)
    parent = source(store)
    task = store.add(Task("shared-task", store.run.id, "review", scope.id, scope.revision))
    task = store.acquire_task(task.id, "worker", 120, now=0)
    store.append_turn_audit(task.id, task.lease_token, "create", "request", [], now=1)
    store.append_turn_audit(
        task.id, task.lease_token, "create", "response", [{"type": "ai", "data": {"content": ""}}], now=1
    )
    shared = SharedInvestigations(store)
    item = shared.create(
        task.id,
        task.lease_token,
        "What happens here?",
        [scope.id],
        ["changed"],
        turn_id="create",
        tool_call_id="call",
        now=1,
    )
    item = shared.claim(
        task.id, task.lease_token, item["id"], item["revision"], "investigate", operation_id="claim", now=2
    )
    page = await EvidenceGateway(store, Gateway()).execute(
        "read_evidence", {"ref": parent.id, "start_line": 2, "end_line": 3}
    )
    store.pin_frontier_for_task(task.id, task.lease_token, page["evidence_ref"], start_line=2, end_line=3, now=3)
    request = [{"type": "tool", "data": {"content": json.dumps(page)}}]
    store.append_turn_audit(task.id, task.lease_token, "page", "request", request, now=3)
    store.append_turn_audit(
        task.id, task.lease_token, "page", "response", [{"type": "ai", "data": {"content": ""}}], now=3
    )
    result = shared.update(
        task.id,
        task.lease_token,
        item["id"],
        item["revision"],
        question="What happens here?",
        observations=[{"statement": "Inspected source page", "evidence_refs": [page["evidence_ref"]]}],
        open_questions=[],
        status="resolved",
        reason="Source page establishes the observation",
        turn_id="page",
        operation_id="resolve",
        now=4,
    )
    assert result["status"] == "resolved"
    assert result["observations"][0]["delivered_ranges"][page["evidence_ref"]] == [[2, 3]]


async def test_reviewer_pins_page_and_audits_source_before_region_decision(tmp_path):
    store, scope = board(tmp_path)
    parent = source(store)
    task = store.add(Task("task", store.run.id, "review", scope.id, scope.revision))
    task = store.acquire_task(task.id, "worker", 120)

    class Model(ScriptedModel):
        async def ainvoke(self, messages):
            self.inputs.append(messages.copy())
            if len(self.inputs) == 1:
                return AIMessage(
                    content="",
                    tool_calls=[
                        call(
                            "read_evidence",
                            {
                                "ref": parent.id,
                                "start_line": 2,
                                "end_line": 3,
                            },
                        )
                    ],
                )
            receipt = json.loads(next(m.content for m in messages if m.type == "tool"))
            assert receipt["original_evidence_ref"] == parent.id
            return AIMessage(
                content="",
                tool_calls=[
                    call(
                        "submit_unit_decision",
                        {
                            "checked_region_ids": ["changed"],
                            "observations": [
                                {
                                    "statement": "Inspected both changed lines.",
                                    "evidence_refs": [receipt["evidence_ref"]],
                                    "region_ids": ["changed"],
                                }
                            ],
                            "unresolved_refs": [],
                            "finish": True,
                        },
                    )
                ],
            )

    model = Model([])
    reviewer = Reviewer(store, model, EvidenceGateway(store, Gateway()), task_id=task.id, lease_token=task.lease_token)
    assert await reviewer.run(scope.id) == "reviewed"
    entry = store.list_frontier(task.id)[0]
    ref = store.get(EvidenceRef, entry.aliases[0])
    assert ref.id != parent.id and (ref.start_line, ref.end_line) == (2, 3)
    assert store.read_frontier_entry(task.id, entry.id) == b"two\nthree\n"
    assert store.get(ReviewScope, scope.id).checked_regions == scope.regions
    audit = store.read_turn_audit(task.id)
    assert any(row["phase"] == "request" and ref.id in json.dumps(row["messages"]) for row in audit)

    changed_parent = source(store, content=b"one\nupdated\nthree\nfour\n")
    new_page = await EvidenceGateway(store, Gateway()).execute(
        "read_evidence", {"ref": changed_parent.id, "start_line": 2, "end_line": 3}
    )
    new_ref = new_page["evidence_ref"]
    reviewed = store.get(ReviewScope, scope.id)
    reopened = store.reopen_unit_for_evidence(scope.id, reviewed.revision, [new_ref], "new related source")
    assert reopened.status == "pending" and not reopened.checked_regions
    replacement = store.add(Task("replacement", store.run.id, "review", scope.id, reopened.revision))
    replacement = store.acquire_task(replacement.id, "worker-2", 120)

    class Recheck(ScriptedModel):
        async def ainvoke(self, messages):
            self.inputs.append(messages.copy())
            if len(self.inputs) == 1:
                return AIMessage(content="", tool_calls=[call("read_evidence", {"ref": new_ref})])
            receipt = json.loads(next(m.content for m in messages if m.type == "tool"))
            assert receipt["evidence_ref"] == new_ref and receipt["content"] == "updated\nthree\n"
            return AIMessage(
                content="",
                tool_calls=[
                    call(
                        "submit_unit_decision",
                        {
                            "checked_region_ids": ["changed"],
                            "observations": [
                                {
                                    "statement": "Rechecked both changed lines against new source.",
                                    "evidence_refs": [new_ref],
                                    "region_ids": ["changed"],
                                }
                            ],
                            "unresolved_refs": [],
                            "finish": True,
                        },
                    )
                ],
            )

    model = Recheck([])
    reviewer = Reviewer(
        store, model, EvidenceGateway(store, Gateway()), task_id=replacement.id, lease_token=replacement.lease_token
    )
    assert await reviewer.run(scope.id) == "reviewed"
    assert store.pending_unit_sources() == []
