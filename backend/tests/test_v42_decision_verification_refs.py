"""Decision-profile verifier observations distinguish navigation from Case evidence."""

import json
from dataclasses import asdict

import pytest
from langchain_core.messages import AIMessage

from reviewforge.review_v42.contracts import BindingError, Case, LeaseError, Question, ReviewScope, Run, Task
from reviewforge.review_v42.store import ReviewStore
from reviewforge.review_v42.tools import EvidenceGateway
from reviewforge.review_v42.verifier import Verifier


def make_board(tmp_path, *, decision=True):
    region = {"id": "a", "path": "a.py", "side": "head", "start_line": 1, "end_line": 2, "changed_lines": [1]}
    meta = {
        "workspace_digest": "snapshot",
        "base_workspace_digest": "base-snapshot",
        "changed_regions": [region],
        "shared_investigation": "shared-1",
        "candidate_draft_policy": "candidate-draft-1",
        "source_line_policy": "git-lf-1",
        "frontier_projection_policy": "source-line-union-1",
    }
    if decision:
        meta["decision_cycle_policy"] = "decision-cycle-1"
    store = ReviewStore(tmp_path / "board.db", Run("run", "repo", "base", "head", policy="v42-units-6", metadata=meta))
    store.install_unit_plan(
        [ReviewScope("a", "run", "a", regions=[region])], ReviewScope("i", "run", "i", dependencies=["a"])
    )
    source = store.put_evidence(
        b"bad\nresult\n",
        revision="head",
        side="head",
        kind="source",
        path="a.py",
        start_line=1,
        end_line=2,
        metadata={"workspace_digest": "snapshot", "tool_name": "read_source"},
    )
    case = Case(
        "case",
        "run",
        scope_id="a",
        trigger="x",
        mechanism="bad",
        claims=[{"id": "c", "statement": "bad", "impact": "loss"}],
        evidence_refs=[source.id],
    )
    store.add(case)
    store.add(Task("review", "run", "review", "a", 0))
    store.add(Task("verify", "run", "verify", "case", 0))
    lease = store.acquire_task("verify", "verifier", 120)
    return store, source, lease


def navigation(store, kind="search"):
    return store.put_evidence(
        b"navigation result\n",
        revision="head",
        side="head",
        kind=kind,
        path="a.py",
        metadata={
            "workspace_digest": "snapshot",
            "tool_name": "search_code" if kind == "search" else "read_diff",
            "navigation_only": True,
        },
    )


class ScriptedVerifierModel:
    def __init__(self, source):
        self.calls = 0
        self.source = source

    def bind_tools(self, tools, **kwargs):
        return self

    async def ainvoke(self, messages):
        self.calls += 1
        if self.calls == 1:
            call = {"name": "search_code", "args": {"query": "bad"}, "id": "s"}
        elif self.calls == 2:
            call = {"name": "read_diff", "args": {"path": "a.py"}, "id": "d"}
        else:
            cite = {"ref": self.source.id, "quote": "bad", "start_line": 1, "end_line": 1}
            call = {
                "name": "submit_verdict",
                "id": "v",
                "args": {
                    "verdict": "reject",
                    "reason": "premise disproved",
                    "facts": {"counterexample": {"statement": "trigger absent", "citations": [cite]}},
                    "gaps": [],
                },
            }
        return AIMessage(content="", tool_calls=[call])


class NavigationGateway:
    async def __call__(self, name, args):
        return {
            "content": "navigation result\n",
            "revision": "head",
            "side": "head",
            "kind": "search" if name == "search_code" else "diff",
            "path": "a.py",
            "metadata": {"workspace_digest": "snapshot", "navigation_only": True},
        }


async def test_real_verifier_navigation_does_not_change_case_or_scope(tmp_path):
    store, source, lease = make_board(tmp_path)
    before_scope = store.get(ReviewScope, "a")
    model = ScriptedVerifierModel(source)
    result = await Verifier(store, model, EvidenceGateway(store, NavigationGateway())).verify(
        "case", task_id="verify", lease_token=lease.lease_token
    )
    assert result[0]["verdict"] == "reject"
    assert model.calls == 3
    assert store.get(Case, "case").evidence_refs == [source.id]
    assert store.get(Case, "case").revision == 0
    assert store.get(ReviewScope, "a") == before_scope
    assert store.get(Task, "review").input_revision == 0
    assert store.get_verdict("case", "c", "v42-independent-1") is not None
    with store._connect() as db:
        events = [
            json.loads(row[0])
            for row in db.execute("SELECT payload FROM events WHERE action='verification_navigation'")
        ]
    assert len(events) == 1 and len(events[0]["evidence_refs"]) == 2
    assert {store.get(type(source), rid).kind for rid in events[0]["evidence_refs"]} == {"search", "diff"}
    assert (
        await Verifier(store, model, EvidenceGateway(store, NavigationGateway())).verify(
            "case", task_id="verify", lease_token=lease.lease_token
        )
    ) == result
    assert model.calls == 3


def test_new_source_or_gap_still_invalidates_case_and_responsibility(tmp_path):
    store, source, lease = make_board(tmp_path)
    nav = navigation(store)
    original = store.put_evidence(
        b"new\nsource\n",
        revision="head",
        side="head",
        kind="source",
        path="a.py",
        start_line=1,
        end_line=2,
        metadata={"workspace_digest": "snapshot", "tool_name": "read_source"},
    )
    before = store.get(ReviewScope, "a")
    case = store.record_verification_observations("verify", lease.lease_token, 0, [], [source.id, nav.id, original.id])
    assert case.revision == 1 and case.evidence_refs == [source.id, original.id]
    assert store.get(ReviewScope, "a").revision == before.revision + 1
    assert store.pending_unit_cases()[0]["case_id"] == case.id


def test_old_profile_retains_legacy_navigation_semantics(tmp_path):
    store, source, lease = make_board(tmp_path, decision=False)
    nav = navigation(store)
    case = store.record_verification_observations("verify", lease.lease_token, 0, [], [source.id, nav.id])
    assert case.revision == 1 and nav.id in case.evidence_refs


@pytest.mark.parametrize("bad", ["missing", "corrupt", "foreign"])
def test_all_navigation_refs_validated_before_filter(tmp_path, bad):
    store, source, lease = make_board(tmp_path)
    nav = navigation(store)
    if bad == "missing":
        bad_id = "ev_missing"
    elif bad == "corrupt":
        (store.blob_dir / nav.content_hash).write_bytes(b"tampered")
        bad_id = nav.id
    else:
        payload = asdict(nav)
        payload["run_id"] = "foreign"
        with store._connect() as db:
            db.execute("UPDATE objects SET payload=? WHERE kind='EvidenceRef' AND id=?", (json.dumps(payload), nav.id))
        bad_id = nav.id
    with pytest.raises((BindingError, OSError)):
        store.record_verification_observations("verify", lease.lease_token, 0, [], [source.id, bad_id])
    assert store.get(Case, "case").revision == 0


def test_expired_lease_and_missing_question_remain_guarded(tmp_path):
    store, source, lease = make_board(tmp_path)
    nav = navigation(store)
    with pytest.raises(LeaseError):
        store.record_verification_observations(
            "verify", lease.lease_token, 0, [], [source.id, nav.id], now=lease.lease_expires
        )
    q = Question("q", "run", "missing rule", scope_id="a", case_id="case")
    case = store.record_verification_observations("verify", lease.lease_token, 0, [q], [source.id, nav.id])
    assert case.revision == 1 and case.question_ids == ["q"] and nav.id not in case.evidence_refs


def test_corrupt_question_interpretation_ref_cannot_be_hidden(tmp_path):
    store, source, lease = make_board(tmp_path)
    nav = navigation(store)
    (store.blob_dir / nav.content_hash).write_bytes(b"tampered")
    question = Question(
        "q",
        "run",
        "missing rule",
        scope_id="a",
        case_id="case",
        interpretations=[{"id": "i", "text": "hypothesis", "evidence_refs": [nav.id]}],
    )
    with pytest.raises(BindingError):
        store.record_verification_observations("verify", lease.lease_token, 0, [question], [source.id])
    assert store.get(Case, "case").revision == 0
    assert store.list(Question) == []
