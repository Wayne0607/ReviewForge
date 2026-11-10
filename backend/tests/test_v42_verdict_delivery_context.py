"""Only original bytes presented in the current claim session can support it."""

import json

from test_v42_integration import NativeModel, tool

from reviewforge.review_v42.contracts import Case, ReviewScope, Run, Task
from reviewforge.review_v42.store import ReviewStore
from reviewforge.review_v42.tools import EvidenceGateway
from reviewforge.review_v42.verifier import Verifier


async def test_context_recovery_freezes_only_the_presented_source_page(tmp_path):
    metadata = {
        "workspace_digest": "snapshot",
        "base_workspace_digest": "base-snapshot",
        "shared_investigation": "shared-1",
        "candidate_draft_policy": "candidate-draft-1",
        "source_line_policy": "git-lf-1",
        "frontier_projection_policy": "source-line-union-1",
        "decision_cycle_policy": "decision-cycle-1",
        "verification_delivery_policy": "claim-delivery-1",
    }
    run = Run("run", "repo", "base", "head", policy="v42-units-6", metadata=metadata)
    store = ReviewStore(tmp_path / "board.db", run)
    original = store.put_evidence(
        b"trigger absent\nother behavior\n",
        revision="head",
        side="head",
        kind="source",
        path="a.py",
        start_line=4,
        end_line=5,
        metadata={"workspace_digest": "snapshot", "tool_name": "read_source", "tool_args": {"path": "a.py"}},
    )
    store.add(ReviewScope("scope", "run", "scope"))
    case = store.add(
        Case(
            "case",
            "run",
            "scope",
            trigger="The trigger exists",
            mechanism="A state change follows",
            claims=[{"id": "claim", "statement": "The state changes", "impact": "Incorrect state"}],
            evidence_refs=[original.id],
        )
    )
    store.add(Task("verify", "run", "verify", case.id, case.revision))
    lease = store.acquire_task("verify", "verifier", 120)

    class ContextLimitError(Exception):
        code = "context_length_exceeded"

    def reject(ref_id):
        cite = {"ref": ref_id, "quote": "trigger absent", "start_line": 4, "end_line": 4}
        return tool(
            "submit_verdict",
            {
                "verdict": "reject",
                "reason": "No matching trigger",
                "facts": {"counterexample": {"statement": "The trigger is absent", "citations": [cite]}},
                "gaps": [],
            },
        )

    page_id = None

    def reply(messages, turn):
        nonlocal page_id
        if turn == 1:
            assert json.loads(messages[1].content)["evidence"][0]["content"] == "trigger absent\nother behavior\n"
            raise ContextLimitError()
        if turn == 2:
            packet = json.loads(messages[1].content)
            assert packet["evidence_directory"][0]["id"] == original.id
            assert "trigger absent" not in messages[1].content
            return reject(original.id)
        if turn == 3:
            assert "not presented" in json.loads(messages[-1].content)["error"]
            return tool("read_evidence", {"ref": original.id, "start_line": 4, "end_line": 4})
        if turn == 4:
            receipt = json.loads(messages[-1].content)
            assert receipt["original_evidence_ref"] == original.id
            assert receipt["content"] == "trigger absent\n"
            page_id = receipt["evidence_ref"]
            return reject(original.id)
        assert turn == 5
        assert "not presented" in json.loads(messages[-1].content)["error"]
        return reject(page_id)

    model = NativeModel(reply)
    [verdict] = await Verifier(store, model, EvidenceGateway(store, None)).verify(
        case.id, task_id=lease.id, lease_token=lease.lease_token
    )

    assert verdict["verdict"] == "reject"
    assert verdict["result"]["cited_refs"] == [page_id]
    assert verdict["delivery"]["observed_evidence_refs"] == [page_id]
    assert verdict["delivery"]["case_evidence_refs"] == [original.id]
    assert store.get(Case, case.id) == case
