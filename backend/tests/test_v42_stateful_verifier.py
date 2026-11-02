"""Independent verifier trust boundaries for v42-stateful-2."""

import json

import pytest
from langchain_core.messages import AIMessage

from reviewforge.review_v42.context import ContextBuilder
from reviewforge.review_v42.contracts import BindingError, Case, EvidenceRef, ReviewScope, Run, Task
from reviewforge.review_v42.publication import Publication
from reviewforge.review_v42.store import ReviewStore
from reviewforge.review_v42.tools import EvidenceGateway
from reviewforge.review_v42.verifier import Verifier, validate_result


@pytest.fixture
def board(tmp_path):
    region = {"id": "r", "path": "source.py", "side": "head", "start_line": 1, "end_line": 1, "changed_lines": [1]}
    run = Run("run", "repo", "base", "head", policy="v42-stateful-2", metadata={"changed_regions": [region]})
    store = ReviewStore(tmp_path / "state.sqlite", run)
    store.add(ReviewScope("scope", "run", "scope", regions=[region], status="reviewed"))
    ref = store.put_evidence(
        b"actual source line\n",
        revision="head",
        side="head",
        kind="source",
        path="source.py",
        start_line=1,
        end_line=1,
        metadata={
            "status": "available",
            "reviewer_confirmation": "PRIVATE_VERDICT_MARKER",
            "tool_args": {"query": "PRIVATE_QUERY_MARKER"},
        },
    )
    store.add(
        Case(
            "case",
            "run",
            "scope",
            trigger="input x",
            mechanism="actual behavior",
            claims=[
                {
                    "id": "claim",
                    "statement": "actual behavior",
                    "impact": "impact",
                }
            ],
            evidence_refs=[ref.id],
        )
    )
    store.add(Task("review", "run", "review", "scope", 0))
    task = store.acquire_task("review", "owner", 100, now=0)
    return store, ref, task


def work(ref, message):
    return {
        "question": message,
        "observations": [{"statement": message, "evidence_refs": [ref.id]}],
        "open_questions": [message],
        "next_step": message,
    }


def acceptance(ref):
    cite = {"ref": ref.id, "quote": "actual source line", "start_line": 1, "end_line": 1}
    return {
        "verdict": "accept",
        "reason": "independently checked",
        "gaps": [],
        "facts": {
            name: {"statement": name, "citations": [cite]}
            for name in ("trigger", "actual", "expected", "change", "impact")
        },
    }


class CaptureModel:
    def __init__(self, ref):
        self.ref = ref
        self.packet = None

    def bind_tools(self, tools, **kwargs):
        return self

    async def ainvoke(self, messages):
        self.packet = json.loads(messages[1].content)
        return AIMessage(content="", tool_calls=[{"name": "submit_verdict", "args": acceptance(self.ref), "id": "x"}])


@pytest.mark.asyncio
async def test_working_state_and_audit_are_not_verifier_inputs_or_evidence(board, tmp_path):
    store, ref, task = board
    store.update_scope_for_task("review", task.lease_token, 0, now=1, working_state=work(ref, "PRIVATE_WORKING_MARKER"))
    audit = store.append_turn_audit(
        "review",
        task.lease_token,
        "turn",
        "request",
        [{"type": "human", "data": {"content": "PRIVATE_AUDIT_MARKER"}}],
        now=2,
    )
    model = CaptureModel(ref)
    verdict = (await Verifier(store, model, None).verify("case"))[0]
    rendered = json.dumps(model.packet, ensure_ascii=False)
    for text in (
        "PRIVATE_WORKING_MARKER",
        "PRIVATE_AUDIT_MARKER",
        "PRIVATE_VERDICT_MARKER",
        "PRIVATE_QUERY_MARKER",
        "PRIVATE_CONFIDENCE_MARKER",
    ):
        assert text not in rendered
    assert model.packet["evidence"][0]["content"] == "actual source line\n"
    assert verdict["verdict"] == "accept"
    before = Publication(store).local_report()["comments"]
    case_revision = store.get(Case, "case").revision
    store.update_scope_for_task("review", task.lease_token, 1, now=3, working_state=work(ref, "NEW_NAVIGATION_ONLY"))
    assert store.get(Case, "case").revision == case_revision
    assert store.get_verdict("case", "claim", "v42-independent-1") == verdict
    assert Publication(store).local_report()["comments"] == before
    assert [item.id for item in store.list(EvidenceRef)] == [ref.id]
    with pytest.raises(KeyError):
        store.read_evidence(audit["content_hash"])
    gateway = EvidenceGateway(store, None)
    assert (await gateway.execute("read_evidence", {"ref": ref.id}))["content"] == "actual source line\n"
    assert store.get(EvidenceRef, ref.id).run_id == "run"
    assert store.get(EvidenceRef, ref.id).revision == "head"
    with pytest.raises(BindingError):
        store.put_evidence(b"old", revision="base", side="head", kind="source")
    other = ReviewStore(tmp_path / "other.sqlite", Run("other", "repo", "base", "other-head"))
    with pytest.raises(KeyError):
        other.read_evidence(ref.id)


@pytest.mark.parametrize("kind", ["query", "working_state", "turn_audit"])
@pytest.mark.parametrize("fact", ["trigger", "actual", "expected", "change", "impact", "counterexample"])
def test_navigation_and_audit_cannot_be_laundered_into_any_verdict(board, kind, fact):
    store, source, _ = board
    suspect = store.put_evidence(
        b"not independent source", revision="head", side="head", kind=kind, metadata={"status": "available"}
    )
    citation = {"ref": suspect.id, "quote": "not independent source"}
    if fact == "counterexample":
        result = {
            "verdict": "reject",
            "reason": "purported counterevidence",
            "gaps": [],
            "facts": {fact: {"statement": "premise is false", "citations": [citation]}},
        }
    else:
        result = acceptance(source)
        result["facts"][fact]["citations"] = [citation]
    with pytest.raises(ValueError, match="navigation-only"):
        validate_result(store, result, {source.id, suspect.id})


@pytest.mark.asyncio
async def test_capacity_recovery_does_not_leak_reviewer_metadata(board):
    store, ref, task = board
    store.update_scope_for_task("review", task.lease_token, 0, now=1, working_state=work(ref, "PRIVATE_WORKING_MARKER"))

    class ContextLimitError(Exception):
        code = "context_length_exceeded"

    class CapacityModel:
        calls = 0

        def bind_tools(self, tools, **kwargs):
            return self

        async def ainvoke(self, messages):
            self.calls += 1
            if self.calls == 2:
                packet = json.loads(messages[1].content)
                rendered = json.dumps(packet)
                assert "PRIVATE_WORKING_MARKER" not in rendered
                assert "PRIVATE_VERDICT_MARKER" not in rendered
                assert "PRIVATE_QUERY_MARKER" not in rendered
                assert packet["evidence_directory"][0]["id"] == ref.id
            raise ContextLimitError()

    context = ContextBuilder(store).verifier("case")
    result, _ = await Verifier(store, CapacityModel(), None)._claim(context, context["claims"][0])
    assert result["verdict"] == "unresolved"


@pytest.mark.asyncio
async def test_fabricated_note_refs_are_not_forwarded_or_readable_by_verifier(board):
    store, source, _ = board
    note = store.put_evidence(b"PRIVATE_PACKAGED_WORKING_MARKER", revision="head", side="head", kind="working_state")
    audit = store.put_evidence(b"PRIVATE_PACKAGED_AUDIT_MARKER", revision="head", side="head", kind="turn_audit")
    store.update(Case, "case", 0, evidence_refs=[source.id, note.id, audit.id])

    class ProbeModel:
        calls = 0

        def bind_tools(self, tools, **kwargs):
            return self

        async def ainvoke(self, messages):
            self.calls += 1
            if self.calls == 1:
                text = messages[1].content
                assert "PRIVATE_PACKAGED_WORKING_MARKER" not in text
                assert "PRIVATE_PACKAGED_AUDIT_MARKER" not in text
                assert note.id not in text and audit.id not in text
                return AIMessage(
                    content="", tool_calls=[{"name": "read_evidence", "id": "try", "args": {"ref": note.id}}]
                )
            assert "PRIVATE_PACKAGED_WORKING_MARKER" not in str(messages[-1].content)
            result = acceptance(source)
            result["failure_explanation"] = "The blocked navigation note is not source evidence."
            return AIMessage(content="", tool_calls=[{"name": "submit_verdict", "id": "final", "args": result}])

    verdict = await Verifier(store, ProbeModel(), EvidenceGateway(store, None)).verify("case")
    assert verdict[0]["verdict"] == "accept"
    assert verdict[0]["result"]["tool_failures"]
