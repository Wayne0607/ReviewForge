"""Independent verification delivers fixed sources with the verdict they support."""

import asyncio
import json
from collections import defaultdict

import pytest
from langchain_core.messages import AIMessage
from test_v42_decision_loop import step
from test_v42_integration import NativeModel, tool

from reviewforge.core.state import StateStore
from reviewforge.review_v42.adapter import PinnedToolGateway
from reviewforge.review_v42.context import ContextBuilder
from reviewforge.review_v42.contracts import (
    CLAIM_CYCLE_POLICY,
    Case,
    EvidenceRef,
    Question,
    ReviewScope,
    RevisionConflict,
    Run,
    Task,
    VerificationIncomplete,
)
from reviewforge.review_v42.publication import Publication, PublicationOutbox
from reviewforge.review_v42.runtime import ReviewRuntime
from reviewforge.review_v42.snapshot import snapshot_identity
from reviewforge.review_v42.store import ReviewStore
from reviewforge.review_v42.tools import EvidenceGateway
from reviewforge.review_v42.verifier import POLICY, Verifier

CONTEXT_SOURCES = {
    "sessions.py": "def repeat_session():\n    return session.participants\n",
    "README.md": "Only newly added participants receive a notice.\n",
    "config.py": "notification_channel = 'participants'\n",
}
HEAD_SOURCES = {"notify.py": "notify(original_emails)\n", **CONTEXT_SOURCES}
BASE_SOURCES = {"notify.py": "notify(new_emails)\n", **CONTEXT_SOURCES}
REGION = {"id": "notify", "path": "notify.py", "side": "head", "start_line": 1, "end_line": 1, "changed_lines": [1]}


class MemorySnapshot:
    source = "tarball"
    truncated = False

    def __init__(self, sha, files):
        self.head_sha = sha
        self.files = files

    def list_paths(self):
        return list(self.files)

    def read_bytes(self, path):
        return self.files[path].encode()


async def make_board(tmp_path, delivery="claim-delivery-1", **metadata):
    state = StateStore(repo="example/repo", pr_number=1, base_sha="base", head_sha="head")
    head = MemorySnapshot(state.head_sha, HEAD_SOURCES)
    base = MemorySnapshot(state.base_sha, BASE_SOURCES)
    run = Run(
        "run",
        state.repo,
        state.base_sha,
        state.head_sha,
        policy="v42-units-6",
        metadata={
            "changed_regions": [REGION],
            "workspace_digest": await snapshot_identity(head),
            "base_workspace_digest": await snapshot_identity(base),
            "shared_investigation": "shared-1",
            "candidate_draft_policy": "candidate-draft-1",
            "source_line_policy": "git-lf-1",
            "frontier_projection_policy": "source-line-union-1",
            "decision_cycle_policy": "decision-cycle-1",
            "verification_delivery_policy": delivery,
            **metadata,
        },
    )
    store = ReviewStore(tmp_path / "board.db", run)
    return store, EvidenceGateway(store, PinnedToolGateway(head, state, base_workspace=base, run=run))


def reviewer_script(*, offer_candidate):
    phases = defaultdict(int)
    seen = defaultdict(dict)

    def respond(messages, _turn):
        board = json.loads(messages[1].content)
        role = board["unit_role"]
        phase = phases[role]
        phases[role] += 1
        current = board["decision_items"][-1] if board["decision_items"] else None
        for message in messages:
            if message.type != "tool":
                continue
            receipt = json.loads(message.content)
            assert "error" not in receipt, receipt
            for item in receipt.get("results", []):
                source = item["result"]
                assert "error" not in source, source
                seen[role][source["path"]] = source["evidence_ref"]

        if phase == 0:
            return tool(
                "review_step",
                step(
                    "request_evidence",
                    {
                        "gap": "Notification behavior and contract",
                        "relevance": "Trace recipients to their source and contract",
                        "alternatives": "Check whether original or newly added participants get notified",
                        "operations": [{"name": "read_source", "args": {"path": path}} for path in HEAD_SOURCES],
                    },
                ),
            )

        refs = list(seen[role].values())
        if role == "primary" and offer_candidate:
            if phase == 1:
                return tool(
                    "review_step",
                    step(
                        "offer_candidate",
                        {
                            "candidate": {
                                "trigger": "Repeat a session with an existing participant",
                                "behavior": "The changed call passes every original email",
                                "impact": "An existing participant receives a duplicate notice",
                                "evidence_refs": refs,
                            }
                        },
                        current,
                        refs,
                    ),
                )
            phase -= 1
        if phase == 1:
            return tool(
                "review_step",
                step(
                    "close_question",
                    {
                        "question_id": current["question_id"],
                        "answer": "Read the notification call, caller, configuration and contract",
                        "evidence_refs": refs,
                    },
                    current,
                    refs,
                ),
            )
        assert phase == 2, board
        if role == "primary":
            payload = {
                "checked_region_ids": [REGION["id"]],
                "observations": [
                    {"statement": "Traced the notification call", "evidence_refs": refs, "region_ids": [REGION["id"]]}
                ],
                "finish": True,
            }
        else:
            payload = {
                "integration_receipts": [
                    {
                        "obligation_id": obligation["id"],
                        "disposition": "checked",
                        "evidence_refs": refs,
                        "reason": "Compared the call with the session and its notification contract",
                    }
                    for obligation in board["integration_obligations"]
                ],
                "finish": True,
            }
        return tool("review_step", step("complete_review", payload, current, refs))

    return NativeModel(respond)


def fact(statement, ref, quote, line=1):
    return {"statement": statement, "citations": [{"ref": ref, "quote": quote, "start_line": line, "end_line": line}]}


def accept_verdict(head, base):
    call = "notify(original_emails)"
    return {
        "verdict": "accept",
        "reason": "The changed notification call sends to participants outside the documented contract",
        "facts": {
            "trigger": fact(
                "Repeating a session reads the participant list", base["sessions.py"], "def repeat_session():"
            ),
            "actual": fact("The head call sends to the original emails", head["notify.py"], call),
            "expected": fact(
                "Only newly added participants are notified", base["README.md"], CONTEXT_SOURCES["README.md"].strip()
            ),
            "change": fact("The changed call passes every original email", head["notify.py"], call),
            "impact": fact("Participant notifications go to the original emails", head["notify.py"], call),
        },
        "gaps": [],
    }


def read_base(path):
    return tool("read_source", {"path": path, "side": "base"})


def reject_verdict(ref, quote, statement):
    return {
        "verdict": "reject",
        "reason": statement,
        "facts": {"counterexample": fact(statement, ref, quote)},
        "gaps": [],
    }


def claim_response(response):
    return AIMessage(content=json.dumps(response))


def claim_gap(path, *, side="base"):
    return claim_response(
        {
            "action": "request_evidence",
            "gap": {
                "fact": "counterexample",
                "question": "Does the original establish the claimed premise?",
                "source": {"side": side, "path": path},
            },
        }
    )


def claim_rejection(original, quote):
    decision = reject_verdict(original, quote, "The original disproves the premise")
    decision.pop("gaps")
    return claim_response({"action": "submit_verdict", **decision})


@pytest.mark.asyncio
async def test_claim_cycle_uses_the_shared_json_constraints_and_rejects_native_calls(tmp_path):
    from reviewforge.review_v42.response_protocol import JSON_CONSTRAINT, JSON_REQUEST_OPTIONS

    store, gateway = await make_board(tmp_path, CLAIM_CYCLE_POLICY, verification_response_constraint=JSON_CONSTRAINT)
    original = await gateway.execute("read_source", {"path": "notify.py"})
    case = store.add(
        Case(
            "case",
            store.run.id,
            "Notifications are sent",
            "New participant",
            "Recipients are passed",
            claims=[{"id": "claim", "statement": "Recipients are missing", "impact": "Nobody receives notice"}],
            evidence_refs=[original["evidence_ref"]],
        )
    )
    task = store.add(Task("verify", store.run.id, "verify", case.id, case.revision))
    lease = store.acquire_task(task.id, "verifier", 120)

    class ConstrainedModel(NativeModel):
        def bind_tools(self, *_args, **_kwargs):
            pytest.fail("claim delivery must use the message channel")

        async def ainvoke(self, messages, **options):
            assert options == JSON_REQUEST_OPTIONS
            return await super().ainvoke(messages)

    def decide(messages, turn):
        if turn == 1:
            return tool("shell", {"command": "unused"})
        assert json.loads(messages[1].content)["correction"] == "Return a JSON message, without tool calls."
        return claim_rejection(original["evidence_ref"], HEAD_SOURCES["notify.py"].strip())

    verifier = Verifier(store, ConstrainedModel(decide), gateway)
    result = await verifier.verify(case.id, task_id=task.id, lease_token=lease.lease_token)
    assert result[0]["verdict"] == "reject"
    assert store.get(Case, case.id).revision == case.revision


@pytest.mark.asyncio
async def test_claim_cycle_restarts_pending_source_and_preserves_prior_verdict(tmp_path, monkeypatch):
    store, gateway = await make_board(tmp_path, CLAIM_CYCLE_POLICY)
    head = await gateway.execute("read_source", {"path": "notify.py"})
    head_ref = head["evidence_ref"]
    case = store.add(
        Case(
            "case",
            store.run.id,
            trigger="Repeat a session",
            mechanism="Notify participants",
            claims=[
                {"id": "first", "statement": "No recipients are passed", "impact": "Nobody receives a notice"},
                {"id": "second", "statement": "The contract is silent", "impact": "Behavior is undefined"},
            ],
            evidence_refs=[head_ref],
        )
    )
    task = store.add(Task("verify", store.run.id, "verify", case.id, case.revision))
    lease = store.acquire_task(task.id, "before-restart", 120)
    read = gateway.execute

    async def interrupt(name, args):
        assert (name, args) == ("read_source", {"side": "base", "path": "README.md"})
        assert store.verification_gaps(case, "second")[0]["status"] == "pending"
        raise asyncio.CancelledError

    monkeypatch.setattr(gateway, "execute", interrupt)

    def initial(messages, _turn):
        packet = json.loads(messages[1].content)
        assert packet["claim"]["id"] == ("first" if _turn == 1 else "second")
        if _turn == 1:
            return claim_rejection(head_ref, HEAD_SOURCES["notify.py"].strip())
        assert store.get_verdict(case.id, "first", POLICY)["verdict"] == "reject"
        return claim_gap("README.md")

    with pytest.raises(asyncio.CancelledError):
        await Verifier(store, NativeModel(initial), gateway).verify(
            case.id, task_id=task.id, lease_token=lease.lease_token
        )
    first = store.get_verdict(case.id, "first", POLICY)
    store.release_task(task.id, lease.lease_token)
    monkeypatch.setattr(gateway, "execute", read)
    lease = store.acquire_task(task.id, "after-restart", 120)

    def resumed(messages, _turn):
        packet = json.loads(messages[1].content)
        assert packet["claim"]["id"] == "second" and packet["case_revision"] == case.revision
        (gap,) = packet["answered_source_requests"]
        assert gap["status"] == "supplied"
        base = next(source for source in packet["originals"] if source["id"] == gap["receipt_ref"])
        assert (base["side"], base["path"]) == ("base", "README.md")
        return claim_rejection(base["id"], CONTEXT_SOURCES["README.md"].strip())

    verifier = Verifier(store, NativeModel(resumed), gateway)
    result = await verifier.verify(case.id, task_id=task.id, lease_token=lease.lease_token)
    assert result[0] == first and result[1]["verdict"] == "reject"
    assert result[0]["evidence_refs"] == [head_ref]
    assert result[1]["result"]["cited_refs"] == [store.verification_gaps(case, "second")[0]["receipt_ref"]]
    assert len(verifier.llm._inputs) == 1
    assert store.get(Case, case.id) == case and not store.list(Question)


@pytest.mark.asyncio
async def test_claim_cycle_external_gap_survives_restart_without_a_verdict(tmp_path):
    store, gateway = await make_board(tmp_path, CLAIM_CYCLE_POLICY)
    case = store.add(
        Case(
            "case",
            store.run.id,
            trigger="Checkout",
            mechanism="Compare a cutoff",
            claims=[{"id": "policy", "statement": "The cutoff is wrong", "impact": "Eligible users are charged"}],
        )
    )
    task = store.add(Task("verify", store.run.id, "verify", case.id, case.revision))
    lease = store.acquire_task(task.id, "worker", 120)
    model = NativeModel(lambda _messages, _turn: claim_gap("Approved deployment cutoff", side="external"))
    verifier = Verifier(store, model, gateway)
    for _ in range(2):
        with pytest.raises(VerificationIncomplete, match="concrete source is unavailable"):
            await verifier.verify(case.id, task_id=task.id, lease_token=lease.lease_token)
        store.release_task(task.id, lease.lease_token)
        lease = store.acquire_task(task.id, "worker", 120)
    assert len(model._inputs) == 1 and store.get_verdict(case.id, "policy", POLICY) is None
    (gap,) = store.verification_gaps(case, "policy")
    assert gap["status"] == "unavailable" and gap["gap"]["source"]["path"] == "Approved deployment cutoff"


@pytest.mark.asyncio
@pytest.mark.parametrize("recover", [True, False])
async def test_unregistered_verifier_tools_allow_correction_but_cannot_sustain_a_loop(tmp_path, recover):
    store, gateway = await make_board(tmp_path)
    head = await gateway.execute("read_source", {"path": "notify.py"})
    ref = head["evidence_ref"]
    claim = {"id": "claim", "statement": "No recipients are passed", "impact": "Nobody receives a notice"}
    case = store.add(
        Case(
            "case",
            store.run.id,
            trigger="Repeat a session",
            mechanism="Notify recipients",
            claims=[claim],
            evidence_refs=[ref],
        )
    )

    def respond(messages, turn):
        assert "Use only the registered tools:" in messages[0].content
        if turn == 1:
            return tool("glob", {"path": "/unregistered", "pattern": "**/*"})
        receipt = json.loads(messages[-1].content)
        assert receipt["error"] == "unregistered verifier tool"
        assert "glob" not in receipt["available_tools"]
        assert "submit_verdict" in receipt["available_tools"]
        if recover:
            return tool(
                "submit_verdict", reject_verdict(ref, HEAD_SOURCES["notify.py"].strip(), "Recipients are passed")
            )
        assert turn == 2
        return tool("shell", {"command": "pwd"})

    model = NativeModel(respond)
    outcome, _observed = await Verifier(store, model, gateway)._claim(ContextBuilder(store).verifier(case.id), claim)
    assert len(model._inputs) == 2
    assert outcome["verdict"] == ("reject" if recover else "unresolved")
    assert outcome["tool_failures"] == []
    if not recover:
        assert "consecutive batches used only unregistered verifier tools" in outcome["gaps"][0]


@pytest.mark.asyncio
@pytest.mark.parametrize("interrupt_completion", [False, True], ids=["uninterrupted", "resume-after-delivery"])
async def test_three_equal_base_originals_deliver_with_verdict_without_reopening_review(
    tmp_path, monkeypatch, interrupt_completion
):
    store, gateway = await make_board(tmp_path)
    reviewer = reviewer_script(offer_candidate=True)
    before_case = before_scope = None
    base_refs = {}

    def respond(messages, turn):
        assert store.get(Case, before_case.id) == before_case
        assert store.get(ReviewScope, before_scope.id) == before_scope
        packet = json.loads(messages[1].content)
        assert packet["revision"] == 0
        head_refs = {source["ref"]["path"]: source["ref"]["id"] for source in packet["evidence"]}
        assert set(head_refs) == set(HEAD_SOURCES)
        if turn == 1:
            return AIMessage(
                content="",
                tool_calls=[
                    {"name": "read_source", "args": {"path": path, "side": "base"}, "id": f"base-{index}"}
                    for index, path in enumerate(CONTEXT_SOURCES)
                ],
            )
        assert turn == 2
        for message in messages:
            if message.type != "tool":
                continue
            source = json.loads(message.content)
            assert "error" not in source, source
            assert source["side"] == "base"
            base_refs[source["path"]] = source["evidence_ref"]
        assert set(base_refs) == set(CONTEXT_SOURCES)
        return tool("submit_verdict", accept_verdict(head_refs, base_refs))

    independent = NativeModel(respond)
    runtime = ReviewRuntime(store, reviewer, gateway, Verifier(store, independent, gateway))
    (primary,) = runtime.prepare([REGION])
    # Finish the primary before starting any worker, reproducing the pilot's delivery order.
    assert await runtime._review_scope(primary.id) == "reviewed", runtime._errors
    before_scope = store.get(ReviewScope, primary.id)
    (before_case,) = store.list(Case)
    assert before_case.revision == 0
    assert PublicationOutbox(store).freeze() == []

    if interrupt_completion:
        complete = store.complete_task

        def interrupt_verification(task_id, token, result, **kwargs):
            if store.get(Task, task_id).kind == "verify":
                assert store.get_verdict(before_case.id, before_case.claims[0]["id"], POLICY) is not None
                raise OSError("simulated interruption before verification task completion")
            return complete(task_id, token, result, **kwargs)

        monkeypatch.setattr(store, "complete_task", interrupt_verification)

    result = await runtime.run([REGION])

    assert store.get(Case, before_case.id) == before_case
    assert store.get(ReviewScope, primary.id) == before_scope
    assert store.pending_unit_cases() == []
    if interrupt_completion:
        assert result["status"] == "partial", result
        (pending,) = [task for task in store.list(Task) if task.kind == "verify"]
        assert (pending.status, pending.input_revision) == ("pending", 0)
    else:
        assert result["status"] == "completed", result

    head_originals = {ref.path: ref for ref in (store.get(EvidenceRef, rid) for rid in before_case.evidence_refs)}
    assert set(head_originals) == set(HEAD_SOURCES)
    for path, rid in base_refs.items():
        base, head = store.get(EvidenceRef, rid), head_originals[path]
        assert (base.side, base.revision, head.side, head.revision) == ("base", "base", "head", "head")
        assert (base.path, base.start_line, base.end_line, base.content_hash) == (
            head.path,
            head.start_line,
            head.end_line,
            head.content_hash,
        )
        assert base.id != head.id

    verdict = store.get_verdict(before_case.id, before_case.claims[0]["id"], POLICY)
    assert verdict and verdict["verdict"] == "accept" and verdict["case_revision"] == 0
    assert set(verdict["evidence_refs"]) == set(before_case.evidence_refs) | set(base_refs.values())
    assert set(verdict["delivery"]["case_evidence_refs"]) == set(before_case.evidence_refs)
    assert set(verdict["delivery"]["observed_evidence_refs"]) == set(verdict["evidence_refs"])
    assert len(verdict["source_digest"]) == 64
    calls = len(reviewer._inputs), len(independent._inputs)
    assert calls == (7, 2)
    # Resume from the persisted board with fresh runtime and verifier instances.
    reopened = ReviewStore(store.path, store.run)
    gateway = EvidenceGateway(reopened, gateway.gateway)
    resumed = ReviewRuntime(reopened, reviewer, gateway, Verifier(reopened, independent, gateway))
    assert (await resumed.run([REGION]))["status"] == "completed"
    assert (len(reviewer._inputs), len(independent._inputs)) == calls
    assert reopened.get_verdict(before_case.id, before_case.claims[0]["id"], POLICY) == verdict
    (verify_task,) = [task for task in reopened.list(Task) if task.kind == "verify"]
    assert (verify_task.status, verify_task.input_revision) == ("completed", 0)
    assert verify_task.result["verdicts"] == [verdict]
    report = Publication(reopened).local_report()
    assert report["status"] == "complete", report["unresolved"]
    assert [comment["anchor"] for comment in report["comments"]] == [{"path": "notify.py", "line": 1, "side": "RIGHT"}]
    assert len(PublicationOutbox(reopened).freeze()) == 1


@pytest.mark.asyncio
async def test_each_claim_can_cite_only_originals_presented_in_its_own_verification(tmp_path):
    store, gateway = await make_board(tmp_path)
    reviewer = reviewer_script(offer_candidate=False)
    base_refs = {}
    quotes = {
        "sessions.py": "def repeat_session():",
        "README.md": CONTEXT_SOURCES["README.md"].strip(),
    }
    first, second = quotes
    head = await gateway.execute("read_source", {"path": "notify.py"})
    future = await gateway.execute("read_source", {"path": second, "side": "base"})

    def respond(messages, _turn):
        packet = json.loads(messages[1].content)
        (claim,) = packet["claims"]
        path = claim["id"]
        assert [item["ref"]["id"] for item in packet["evidence"]] == [head["evidence_ref"]]
        receipts = [json.loads(message.content) for message in messages if message.type == "tool"]
        if not receipts:
            # A stored original still needs to be presented in this claim's own transcript.
            borrowed_ref = future["evidence_ref"] if path == first else base_refs[first]
            borrowed_path = second if path == first else first
            return tool("submit_verdict", reject_verdict(borrowed_ref, quotes[borrowed_path], "Borrowed proof"))
        if len(receipts) == 1:
            assert "not presented to this verifier" in receipts[0]["error"]
            return read_base(path)
        assert len(receipts) == 2 and receipts[-1]["side"] == "base"
        base_refs[path] = receipts[-1]["evidence_ref"]
        return tool("submit_verdict", reject_verdict(base_refs[path], quotes[path], "The original disproves the claim"))

    independent = NativeModel(respond)
    runtime = ReviewRuntime(store, reviewer, gateway, Verifier(store, independent, gateway))
    (primary,) = runtime.prepare([REGION])
    assert await runtime._review_scope(primary.id) == "reviewed", runtime._errors
    before_scope = store.get(ReviewScope, primary.id)
    case = store.add(
        Case(
            "case",
            store.run.id,
            scope_id=primary.id,
            trigger="Repeat a session",
            mechanism="Notify a participant",
            claims=[
                {"id": first, "statement": "The session lacks a repeat function", "impact": "Sessions cannot repeat"},
                {"id": second, "statement": "The contract is silent", "impact": "Behavior is undefined"},
            ],
            evidence_refs=[head["evidence_ref"]],
        )
    )

    result = await runtime.run([REGION])

    assert result["status"] == "completed", result
    assert store.get(ReviewScope, primary.id) == before_scope
    assert store.get(Case, case.id) == case
    assert len(independent._inputs) == 6
    assert base_refs[second] == future["evidence_ref"]
    decisions = [store.get_verdict(case.id, path, POLICY) for path in quotes]
    for path, decision in zip(quotes, decisions, strict=True):
        assert decision["verdict"] == "reject"
        expected_refs = {head["evidence_ref"], base_refs[path]}
        assert set(decision["evidence_refs"]) == expected_refs
        assert set(decision["delivery"]["observed_evidence_refs"]) == expected_refs
        assert decision["result"]["cited_refs"] == [base_refs[path]]
    assert decisions[0]["source_digest"] != decisions[1]["source_digest"]


@pytest.mark.asyncio
async def test_new_verifier_gap_advances_case_and_blocks_publication(tmp_path):
    store, gateway = await make_board(tmp_path)
    reviewer = reviewer_script(offer_candidate=True)
    gap = "Which participant groups are exempt from the notification policy?"
    independent = NativeModel(
        lambda *_: tool("submit_verdict", {"verdict": "needs_evidence", "reason": gap, "facts": {}, "gaps": [gap]})
    )
    verifier = Verifier(store, independent, gateway)
    runtime = ReviewRuntime(store, reviewer, gateway, verifier)
    (primary,) = runtime.prepare([REGION])
    assert await runtime._review_scope(primary.id) == "reviewed", runtime._errors
    (case,) = store.list(Case)
    (task,) = [task for task in store.list(Task) if task.kind == "verify"]
    task = store.acquire_task(task.id, "verifier", 120)

    with pytest.raises(RevisionConflict):
        await verifier.verify(case.id, task_id=task.id, lease_token=task.lease_token)

    revised = store.get(Case, case.id)
    assert revised.revision == 1
    assert revised.evidence_refs == case.evidence_refs
    (question_id,) = revised.question_ids
    question = store.get(Question, question_id)
    assert (question.statement, question.status, question.owner) == (gap, "open", "verifier")
    assert store.get(ReviewScope, primary.id).status == "pending"
    assert store.get_verdict(case.id, case.claims[0]["id"], POLICY) is None
    assert Publication(store).local_report()["status"] == "partial"
    assert PublicationOutbox(store).freeze() == []
