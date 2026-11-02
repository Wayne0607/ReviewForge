"""Independent verification boundaries for the task-level source frontier."""

import json

import pytest
from langchain_core.messages import AIMessage

from reviewforge.review_v42.contracts import BindingError, Case, EvidenceRef, ReviewScope, Run, Task
from reviewforge.review_v42.publication import Publication, PublicationOutbox
from reviewforge.review_v42.store import ReviewStore
from reviewforge.review_v42.verifier import Verifier, validate_result


@pytest.fixture
def board(tmp_path):
    region = {"id": "r", "path": "source.py", "side": "head", "start_line": 1, "end_line": 1, "changed_lines": [1]}
    run = Run("run", "repo", "base", "head", policy="v42-frontier-4", metadata={"changed_regions": [region]})
    store = ReviewStore(tmp_path / "state.sqlite", run)
    store.add(ReviewScope("scope", "run", "scope", regions=[region], status="reviewed"))
    source = store.put_evidence(
        b"actual source line\n",
        revision="head",
        side="head",
        kind="source",
        path="source.py",
        start_line=1,
        end_line=1,
        metadata={"status": "available", "reviewer_confirmation": "PRIVATE_REVIEW"},
    )
    other = store.put_evidence(
        b"PRIVATE_OTHER_FRONTIER\n",
        revision="head",
        side="head",
        kind="source",
        path="other.py",
        start_line=1,
        end_line=1,
    )
    store.add(
        Case(
            "case",
            "run",
            "scope",
            trigger="input",
            mechanism="behavior",
            evidence_refs=[source.id],
            claims=[{"id": "claim", "statement": "behavior", "impact": "impact"}],
        )
    )
    store.add(Task("review", "run", "review", "scope", 0))
    task = store.acquire_task("review", "owner", 100, now=0)
    return store, source, other, task


def acceptance(ref):
    citation = {"ref": ref.id, "quote": "actual source line", "start_line": 1, "end_line": 1}
    return {
        "verdict": "accept",
        "reason": "independently verified",
        "gaps": [],
        "facts": {
            key: {"statement": key, "citations": [citation]}
            for key in ("trigger", "actual", "expected", "change", "impact")
        },
    }


class Capture:
    def __init__(self, source):
        self.source = source
        self.packet = None
        self.tools = None

    def bind_tools(self, tools, **kwargs):
        self.tools = tools
        return self

    async def ainvoke(self, messages):
        self.packet = json.loads(messages[1].content)
        return AIMessage(
            content="", tool_calls=[{"name": "submit_verdict", "args": acceptance(self.source), "id": "one"}]
        )


@pytest.mark.asyncio
async def test_frontier_changes_preserve_frozen_verdict_and_only_case_source_is_shared(board):
    store, source, other, task = board
    entry = store.pin_frontier_for_task("review", task.lease_token, source.id, now=1)
    hidden = store.pin_frontier_for_task("review", task.lease_token, other.id, now=2)
    assert store.read_frontier_entry("review", entry.id) == b"actual source line\n"
    model = Capture(source)
    verdict = (await Verifier(store, model, None).verify("case"))[0]
    assert verdict["verdict"] == "accept"
    packet = json.dumps(model.packet)
    assert "PRIVATE_OTHER_FRONTIER" not in packet and "PRIVATE_REVIEW" not in packet
    assert entry.id not in packet and hidden.id not in packet and other.id not in packet
    assert model.packet["evidence"][0]["content"] == "actual source line\n"
    assert model.packet["evidence"][0]["ref"]["id"] == source.id
    assert all(getattr(tool, "name", "") not in {"list_frontier", "read_frontier_entry"} for tool in model.tools)
    original = Publication(store).local_report()
    assert original["status"] == "complete" and len(original["comments"]) == 1
    outbox = PublicationOutbox(store)
    pending = outbox.freeze()[0]
    case_revision = store.get(Case, "case").revision
    scope_revision = store.get(ReviewScope, "scope").revision
    store.release_frontier_for_task(
        "review",
        task.lease_token,
        [entry.id],
        "no longer needed",
        expected_revision=store.frontier_revision("review"),
        now=3,
    )
    assert store.get(Case, "case").revision == case_revision
    assert store.get(ReviewScope, "scope").revision == scope_revision
    assert store.get_verdict("case", "claim", "v42-independent-1") == verdict
    assert Publication(store).local_report()["comments"] == original["comments"]
    assert outbox.prepare(pending["id"]) == pending
    store.pin_frontier_for_task("review", task.lease_token, source.id, now=4)
    assert store.get_verdict("case", "claim", "v42-independent-1") == verdict
    assert Publication(store).local_report()["comments"] == original["comments"]


@pytest.mark.asyncio
async def test_frontier_id_is_not_case_evidence_or_verifier_citation(board):
    store, source, _, task = board
    entry = store.pin_frontier_for_task("review", task.lease_token, source.id, now=1)
    with pytest.raises(BindingError, match="unknown evidence reference"):
        store.update(Case, "case", 0, evidence_refs=[source.id, entry.id])
    assert store.get(Case, "case").revision == 0
    result = acceptance(source)
    result["facts"]["expected"]["citations"] = [{"ref": entry.id, "quote": "actual source line"}]
    with pytest.raises(KeyError):
        validate_result(store, result, {source.id, entry.id})


@pytest.mark.parametrize(
    "kind", ["frontier", "frontier_entry", "FrontierEntry", "FRONTIER", "NavigationItem", "query", "turn_audit"]
)
@pytest.mark.parametrize("fact", ["actual", "expected", "counterexample"])
def test_frontier_and_navigation_cannot_be_laundered_as_evidence(board, kind, fact):
    store, source, _, _ = board
    fake = store.put_evidence(b"not source", revision="head", side="head", kind=kind, metadata={"status": "available"})
    cite = {"ref": fake.id, "quote": "not source"}
    if fact == "counterexample":
        result = {
            "verdict": "reject",
            "reason": "purported refutation",
            "gaps": [],
            "facts": {fact: {"statement": "premise false", "citations": [cite]}},
        }
    else:
        result = acceptance(source)
        result["facts"][fact]["citations"] = [cite]
    with pytest.raises(ValueError, match="navigation-only"):
        validate_result(store, result, {source.id, fake.id})


@pytest.mark.asyncio
async def test_policy4_open_navigation_and_capacity_failure_remain_partial(board):
    store, source, _, task = board
    verdict = (await Verifier(store, Capture(source), None).verify("case"))[0]
    state = {"question": "check", "observations": [], "open_questions": ["missing behavior"], "next_step": "inspect"}
    merged = store.merge_working_states("review", task.lease_token, 0, [state], now=1)
    nav = next(item for item in merged["items"] if item.kind == "question")
    report = Publication(store).local_report()
    assert report["status"] == "partial" and report["open_navigation_questions"][0]["id"] == nav.id
    assert store.get_verdict("case", "claim", "v42-independent-1") == verdict
    store.update_navigation_for_task("review", task.lease_token, nav.id, 0, reason="resolved", status="resolved", now=2)
    scope = store.get(ReviewScope, "scope")
    store.update_scope_for_task(
        "review", task.lease_token, scope.revision, status="partial", unresolved=["context capacity unavailable"], now=3
    )
    assert Publication(store).local_report()["status"] == "partial"


def test_snapshot_side_and_other_runs_still_rejected(board, tmp_path):
    store, source, _, _ = board
    base = store.put_evidence(
        b"actual source line\n", revision="base", side="base", kind="source", path="source.py", start_line=1, end_line=1
    )
    result = acceptance(source)
    result["facts"]["actual"]["citations"] = [
        {"ref": base.id, "quote": "actual source line", "start_line": 1, "end_line": 1}
    ]
    with pytest.raises(ValueError, match="head source"):
        validate_result(store, result, {source.id, base.id})
    foreign = ReviewStore(
        tmp_path / "other.sqlite", Run("other", "repo", "base", "another-head", policy="v42-frontier-4")
    )
    with pytest.raises(KeyError):
        foreign.get(EvidenceRef, source.id)
    with pytest.raises(BindingError):
        foreign.add(Case("foreign", "other", evidence_refs=[source.id]))


@pytest.mark.parametrize("policy", ["v42", "v42-stateful-2", "v42-workspace-3"])
def test_previous_policy_does_not_gain_frontier_kind_restriction(tmp_path, policy):
    store = ReviewStore(tmp_path / "old.sqlite", Run("run", "repo", "base", "head", policy=policy))
    source = store.put_evidence(
        b"actual source line\n", revision="head", side="head", kind="source", path="source.py", start_line=1, end_line=1
    )
    other = store.put_evidence(b"literal source", revision="head", side="head", kind="frontier_entry")
    result = acceptance(source)
    result["facts"]["expected"]["citations"] = [{"ref": other.id, "quote": "literal source"}]
    assert validate_result(store, result, {source.id, other.id})["verdict"] == "accept"


@pytest.mark.asyncio
async def test_capacity_recovery_indexes_case_sources_only_and_stays_partial(board):
    store, source, other, task = board
    own = store.pin_frontier_for_task("review", task.lease_token, source.id, now=1)
    hidden = store.pin_frontier_for_task("review", task.lease_token, other.id, now=2)

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
                assert [entry["id"] for entry in packet["evidence_directory"]] == [source.id]
                text = json.dumps(packet)
                for marker in ("PRIVATE_OTHER_FRONTIER", "PRIVATE_REVIEW", own.id, hidden.id, other.id):
                    assert marker not in text
            raise ContextLimitError()

    out = await Verifier(store, CapacityModel(), None).verify("case")
    assert out[0]["verdict"] == "unresolved"
    assert Publication(store).local_report()["status"] == "partial"
