"""Policy 3 navigation stays outside independent verification and publication facts."""

import json

import pytest
from langchain_core.messages import AIMessage

from reviewforge.review_v42.context import ContextBuilder
from reviewforge.review_v42.contracts import BindingError, Case, EvidenceRef, NavigationItem, ReviewScope, Run, Task
from reviewforge.review_v42.publication import Publication, PublicationOutbox
from reviewforge.review_v42.store import ReviewStore
from reviewforge.review_v42.tools import EvidenceGateway
from reviewforge.review_v42.verifier import Verifier, validate_result


@pytest.fixture
def workspace(tmp_path):
    region = {"id": "r", "path": "source.py", "side": "head", "start_line": 1, "end_line": 1, "changed_lines": [1]}
    run = Run("run", "repo", "base", "head", policy="v42-workspace-3", metadata={"changed_regions": [region]})
    store = ReviewStore(tmp_path / "run.sqlite", run)
    store.add(ReviewScope("scope", "run", "scope", regions=[region], status="reviewed"))
    ref = store.put_evidence(
        b"actual source line\n",
        revision="head",
        side="head",
        kind="source",
        path="source.py",
        start_line=1,
        end_line=1,
        metadata={"status": "available", "tool_args": {"q": "PRIVATE_TOOL"}},
    )
    store.add(
        Case(
            "case",
            "run",
            "scope",
            trigger="input",
            mechanism="behavior",
            evidence_refs=[ref.id],
            claims=[{"id": "claim", "statement": "behavior", "impact": "impact"}],
        )
    )
    store.add(Task("review", "run", "review", "scope", 0))
    task = store.acquire_task("review", "owner", 100, now=0)
    return store, ref, task


def acceptance(ref):
    cite = {"ref": ref.id, "quote": "actual source line", "start_line": 1, "end_line": 1}
    return {
        "verdict": "accept",
        "reason": "independently checked",
        "gaps": [],
        "facts": {
            key: {"statement": key, "citations": [cite]}
            for key in ("trigger", "actual", "expected", "change", "impact")
        },
    }


def state(ref, text):
    return {
        "question": "PRIVATE_FOCUS",
        "observations": [{"statement": "PRIVATE_OBSERVATION", "evidence_refs": [ref.id]}],
        "open_questions": [text],
        "next_step": "PRIVATE_NEXT_STEP",
    }


class Capture:
    def __init__(self, ref):
        self.ref = ref
        self.packet = None

    def bind_tools(self, tools, **kwargs):
        return self

    async def ainvoke(self, messages):
        self.packet = json.loads(messages[1].content)
        return AIMessage(content="", tool_calls=[{"name": "submit_verdict", "args": acceptance(self.ref), "id": "one"}])


@pytest.mark.asyncio
async def test_navigation_lifecycle_preserves_independent_frozen_finding(workspace):
    store, ref, task = workspace
    model = Capture(ref)
    verdict = (await Verifier(store, model, None).verify("case"))[0]
    original = Publication(store).local_report()
    assert original["status"] == "complete" and len(original["comments"]) == 1
    outbox = PublicationOutbox(store)
    pending = outbox.freeze()[0]
    merge = store.merge_working_states("review", task.lease_token, 0, [state(ref, "PRIVATE_UNRESOLVED")], now=1)
    question = next(item for item in merge["items"] if item.kind == "question")
    observation = next(item for item in merge["items"] if item.kind == "observation")
    report = Publication(store).local_report()
    assert report["status"] == "partial"
    assert report["open_navigation_questions"] == [
        {"id": question.id, "scope_id": "scope", "text": "PRIVATE_UNRESOLVED", "revision": 0}
    ]
    assert any(item.get("kind") == "navigation_question" and item["id"] == question.id for item in report["unresolved"])
    assert report["comments"] == original["comments"] and report["candidates"] == 1
    assert store.get(Case, "case").revision == 0
    assert store.get_verdict("case", "claim", "v42-independent-1") == verdict
    assert outbox.prepare(pending["id"]) == pending
    # Independent verifier's next packet may contain sources only, not navigation or audit.
    store.append_turn_audit(
        "review", task.lease_token, "turn", "request", [{"type": "human", "data": {"content": "PRIVATE_AUDIT"}}], now=2
    )
    model2 = Capture(ref)
    context = ContextBuilder(store).verifier("case")
    await Verifier(store, model2, None)._claim(context, context["claims"][0])
    packet = json.dumps(model2.packet)
    for value in (
        "PRIVATE_UNRESOLVED",
        "PRIVATE_OBSERVATION",
        "PRIVATE_FOCUS",
        "PRIVATE_NEXT_STEP",
        "PRIVATE_AUDIT",
        "PRIVATE_TOOL",
    ):
        assert value not in packet
    assert model2.packet["evidence"][0]["content"] == "actual source line\n"
    corrected = store.update_navigation_for_task(
        "review", task.lease_token, question.id, 0, reason="correct the question", text="corrected question", now=3
    )
    corrected_report = Publication(store).local_report()
    assert corrected_report["status"] == "partial"
    assert corrected_report["open_navigation_questions"][0]["text"] == "corrected question"
    assert corrected_report["comments"] == original["comments"]
    assert store.get_verdict("case", "claim", "v42-independent-1") == verdict
    # Closing a question does not erase unresolved scope responsibilities.
    scope = store.get(ReviewScope, "scope")
    store.update_scope_for_task(
        "review", task.lease_token, scope.revision, status="reviewed", unresolved=[question.id], now=4
    )
    store.update_navigation_for_task(
        "review",
        task.lease_token,
        question.id,
        corrected.revision,
        reason="resolved from source",
        status="resolved",
        now=5,
    )
    partial = Publication(store).local_report()
    assert partial["status"] == "partial" and partial["open_navigation_questions"] == []
    assert store.get(NavigationItem, observation.id).status == "open"  # observations alone do not block completion
    scope = store.get(ReviewScope, "scope")
    store.update_scope_for_task("review", task.lease_token, scope.revision, status="partial", unresolved=[], now=6)
    assert Publication(store).local_report()["status"] == "partial"
    scope = store.get(ReviewScope, "scope")
    store.update_scope_for_task("review", task.lease_token, scope.revision, status="reviewed", unresolved=[], now=7)
    final = Publication(store).local_report()
    assert final["status"] == "complete" and final["comments"] == original["comments"]
    assert store.get_verdict("case", "claim", "v42-independent-1") == verdict
    assert store.history("NavigationItem", question.id)[-1]["payload"]["reason"] == "resolved from source"


@pytest.mark.parametrize(
    "kind", ["navigation_item", "NavigationItem", "question", "observation", "working_state", "turn_audit", "query"]
)
@pytest.mark.parametrize("fact", ["trigger", "actual", "expected", "change", "impact", "counterexample"])
def test_workspace_navigation_cannot_become_fact_or_counterevidence(workspace, kind, fact):
    store, ref, _ = workspace
    fake = store.put_evidence(
        b"untrusted navigation", revision="head", side="head", kind=kind, metadata={"status": "available"}
    )
    cite = {"ref": fake.id, "quote": "untrusted navigation"}
    if fact == "counterexample":
        result = {
            "verdict": "reject",
            "reason": "not evidence",
            "gaps": [],
            "facts": {fact: {"statement": "premise false", "citations": [cite]}},
        }
    else:
        result = acceptance(ref)
        result["facts"][fact]["citations"] = [cite]
    with pytest.raises(ValueError, match="navigation-only"):
        validate_result(store, result, {ref.id, fake.id})


def test_navigation_and_sources_cannot_be_imported_across_runs(workspace, tmp_path):
    store, ref, task = workspace
    items = store.merge_working_states("review", task.lease_token, 0, [state(ref, "cross-run")], now=1)["items"]
    navigation = next(item for item in items if item.kind == "question")
    other = ReviewStore(tmp_path / "other.sqlite", Run("other", "repo", "base", "other-head", policy="v42-workspace-3"))
    other.add(ReviewScope("other-scope", "other", "other"))
    with pytest.raises(KeyError):
        other.get(EvidenceRef, ref.id)
    with pytest.raises(KeyError):
        other.get(NavigationItem, navigation.id)
    with pytest.raises(BindingError):
        other.add(Case("foreign", "other", "other-scope", evidence_refs=[ref.id]))


@pytest.mark.asyncio
async def test_forged_navigation_references_neither_reach_verifier_nor_read_tool(workspace):
    store, source, _ = workspace
    fake = store.put_evidence(b"PRIVATE_FORGED_NOTE", revision="head", side="head", kind="navigation_item")
    store.update(Case, "case", 0, evidence_refs=[source.id, fake.id])

    class Probe:
        calls = 0

        def bind_tools(self, tools, **kwargs):
            return self

        async def ainvoke(self, messages):
            self.calls += 1
            if self.calls == 1:
                assert fake.id not in messages[1].content and "PRIVATE_FORGED_NOTE" not in messages[1].content
                return AIMessage(
                    content="", tool_calls=[{"name": "read_evidence", "args": {"ref": fake.id}, "id": "try"}]
                )
            assert "PRIVATE_FORGED_NOTE" not in str(messages[-1].content)
            result = acceptance(source)
            result["failure_explanation"] = "A navigation item is not source evidence."
            return AIMessage(content="", tool_calls=[{"name": "submit_verdict", "args": result, "id": "done"}])

    out = await Verifier(store, Probe(), EvidenceGateway(store, None)).verify("case")
    assert out[0]["verdict"] == "accept" and out[0]["result"]["tool_failures"]


@pytest.mark.asyncio
async def test_retracted_question_has_audit_reason_but_is_not_a_finding(workspace):
    store, ref, task = workspace
    verdict = (await Verifier(store, Capture(ref), None).verify("case"))[0]
    original = Publication(store).local_report()
    items = store.merge_working_states("review", task.lease_token, 0, [state(ref, "retract this question")], now=1)[
        "items"
    ]
    question = next(item for item in items if item.kind == "question")
    store.update_navigation_for_task(
        "review", task.lease_token, question.id, 0, reason="not a meaningful question", status="retracted", now=2
    )
    report = Publication(store).local_report()
    assert report["status"] == "complete" and report["open_navigation_questions"] == []
    assert report["comments"] == original["comments"] and report["candidates"] == 1
    assert store.get_verdict("case", "claim", "v42-independent-1") == verdict
    assert store.history("NavigationItem", question.id)[-1]["payload"]["reason"] == "not a meaningful question"


@pytest.mark.parametrize("fact", ["expected", "actual", "counterexample"])
def test_navigation_item_id_is_never_a_case_reference_or_verifier_citation(workspace, fact):
    store, source, task = workspace
    merged = store.merge_working_states("review", task.lease_token, 0, [state(source, "navigation only")], now=1)
    item = next(item for item in merged["items"] if item.kind == "question")
    with pytest.raises(BindingError, match="unknown evidence reference"):
        store.update(Case, "case", 0, evidence_refs=[source.id, item.id])
    assert store.get(Case, "case").revision == 0
    citation = {"ref": item.id, "quote": item.text}
    if fact == "counterexample":
        result = {
            "verdict": "reject",
            "reason": "purported counterevidence",
            "gaps": [],
            "facts": {fact: {"statement": "premise false", "citations": [citation]}},
        }
    else:
        result = acceptance(source)
        result["facts"][fact]["citations"] = [citation]
    with pytest.raises(KeyError):
        validate_result(store, result, {source.id, item.id})


@pytest.mark.parametrize("policy", ["v42", "v42-stateful-2"])
@pytest.mark.parametrize("kind", ["QUERY", "WORKING_STATE", "TURN_AUDIT"])
def test_older_policies_preserve_exact_kind_matching(tmp_path, policy, kind):
    store = ReviewStore(tmp_path / "old.sqlite", Run("run", "repo", "base", "head", policy=policy))
    source = store.put_evidence(
        b"actual source line\n", revision="head", side="head", kind="source", path="source.py", start_line=1, end_line=1
    )
    upper = store.put_evidence(b"legacy uppercase content", revision="head", side="head", kind=kind)
    result = acceptance(source)
    result["facts"]["expected"]["citations"] = [{"ref": upper.id, "quote": "legacy uppercase content"}]
    assert validate_result(store, result, {source.id, upper.id})["verdict"] == "accept"


@pytest.mark.parametrize("kind", ["QUERY", "WORKING_STATE", "TURN_AUDIT", "NaViGaTiOnItEm"])
def test_workspace_policy_rejects_mixed_case_navigation_kinds(workspace, kind):
    store, source, _ = workspace
    upper = store.put_evidence(b"navigation content", revision="head", side="head", kind=kind)
    result = acceptance(source)
    result["facts"]["expected"]["citations"] = [{"ref": upper.id, "quote": "navigation content"}]
    with pytest.raises(ValueError, match="navigation-only"):
        validate_result(store, result, {source.id, upper.id})


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", ["v42-stateful-2", "v42-workspace-3"])
async def test_kind_case_handling_applies_to_packet_and_read_tool(tmp_path, policy):
    store = ReviewStore(tmp_path / "run.sqlite", Run("run", "repo", "base", "head", policy=policy))
    source = store.put_evidence(
        b"actual source line\n", revision="head", side="head", kind="source", path="source.py", start_line=1, end_line=1
    )
    upper = store.put_evidence(b"UPPERCASE_CONTENT", revision="head", side="head", kind="WORKING_STATE")
    store.add(
        Case(
            "case",
            "run",
            trigger="input",
            mechanism="behavior",
            evidence_refs=[source.id, upper.id],
            claims=[{"id": "claim", "statement": "behavior", "impact": "impact"}],
        )
    )

    class Probe:
        calls = 0

        def bind_tools(self, tools, **kwargs):
            return self

        async def ainvoke(self, messages):
            self.calls += 1
            if self.calls == 1:
                assert ("UPPERCASE_CONTENT" in messages[1].content) == (policy == "v42-stateful-2")
                return AIMessage(
                    content="", tool_calls=[{"name": "read_evidence", "args": {"ref": upper.id}, "id": "read"}]
                )
            assert ("UPPERCASE_CONTENT" in messages[-1].content) == (policy == "v42-stateful-2")
            result = acceptance(source)
            if policy == "v42-workspace-3":
                result["failure_explanation"] = "Navigation is not source evidence."
            return AIMessage(content="", tool_calls=[{"name": "submit_verdict", "args": result, "id": "submit"}])

    context = ContextBuilder(store).verifier("case")
    outcome, _ = await Verifier(store, Probe(), EvidenceGateway(store, None))._claim(context, context["claims"][0])
    assert outcome["verdict"] == "accept"
