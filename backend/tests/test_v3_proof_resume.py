"""Real SQLite/outbox recovery preserves terminal proof decisions and new facts."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

from reviewforge.core.config import PipelineV4Config
from reviewforge.core.database import Database
from reviewforge.core.events import EventBus
from reviewforge.core.state import StateStore
from reviewforge.engine.editor import Editor, render_review_body
from reviewforge.engine.hypothesis import (
    ContractAssessment,
    EvidenceCitation,
    Hypothesis,
    HypothesisLedger,
    HypothesisStatus,
    Mechanism,
    Observation,
    Site,
)
from reviewforge.engine.pipeline_v4 import _publication_coverage, publication_payload, run_hypothesis_pipeline
from reviewforge.engine.proof_gate import OBLIGATIONS, ProofGate
from reviewforge.engine.semantic_diff import compile_semantic_changeset
from reviewforge.tools.workspace import PRHeadWorkspace, WorkspaceInfo


class NoNetworkModel(BaseChatModel):
    calls: list = Field(default_factory=list)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls.append(messages)
        raise AssertionError("No model request expected during this pipeline recovery")

    @property
    def _llm_type(self):
        return "proof-resume-no-network-fixture"


class ReceiptModel(NoNetworkModel):
    decision: str = "accepted"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        import json

        self.calls.append(messages)
        if self.decision == "provider-error":
            raise RuntimeError("fixture transport failure")
        items = json.loads(messages[-1].content)["items"]
        reviews = []
        for packet in items:
            identity = packet["candidate"]["id"]
            review = {
                "hypothesis_id": identity,
                "decision": self.decision,
                "reason": "Original candidate review",
                "obligations": {},
                "counterevidence": None,
                "missing_obligations": [],
            }
            if self.decision == "accepted":
                review["obligations"] = {
                    name: {
                        "statement": "Established from the supplied source",
                        "evidence": [f"{identity}/obs_0:e1", f"{identity}/obs_1:e1"],
                    }
                    for name in OBLIGATIONS
                }
            elif self.decision == "refuted":
                review["counterevidence"] = {
                    "premise": "mechanism",
                    "statement": "The source protects access",
                    "evidence": [f"{identity}/obs_0:e1"],
                }
            else:
                review["reason"] = "The actual caller binding is not in the saved evidence"
                review["missing_obligations"] = ["supported_input"]
            reviews.append(review)
        return ChatResult(
            generations=[
                ChatGeneration(
                    message=AIMessage(
                        content=json.dumps({"reviews": reviews}),
                        usage_metadata={"input_tokens": 100, "output_tokens": 100, "total_tokens": 200},
                    )
                )
            ]
        )


def proposed(unit_id, identity="h_first", mechanism=Mechanism.NULL_PATH):
    sources = [
        Observation("obs_0", "read_file", "app.py", "app.py", (1, 2), "head", "a", "    return value.id\n", "success"),
        Observation(
            "obs_1",
            "read_file",
            "caller.py",
            "caller.py",
            (1, 1),
            "head",
            "b",
            "# The receiver is optional and must return an empty result.\n",
            "success",
        ),
    ]
    return Hypothesis(
        id=identity,
        identity=f"{unit_id}::{mechanism.value}::lookup",
        unit_id=unit_id,
        mechanism=mechanism,
        claim=f"Original {identity} missing receiver dereference",
        trigger="A supported absent receiver",
        impact="The lookup raises instead of returning the empty result",
        open_question="Is None supported?",
        refutation="Source checks the receiver",
        sites=[Site("app.py", 2, "return value.id")],
        severity="warning",
        source="generator",
        status=HypothesisStatus.CONFIRMED,
        evidence_strength="strong",
        observations=sources,
        assessment=ContractAssessment(
            "The optional receiver must return an empty result",
            "The missing receiver raises on access",
            "conflict",
            [EvidenceCitation("obs_1", sources[1].excerpt)],
            [EvidenceCitation("obs_0", sources[0].excerpt)],
        ),
    )


async def scenario(tmp_path, monkeypatch, decision):
    source = "def lookup(value):\n    return value.id\nenabled = True\n"
    (tmp_path / "app.py").write_text(source, encoding="utf-8")
    info = WorkspaceInfo("owner/repo", "owner/repo", "head", tmp_path, 1, len(source), "workspace", False, "tarball")
    workspace = PRHeadWorkspace(info, None, fallback_repo="owner/repo", temp_dir=tmp_path)
    state = StateStore(
        repo="owner/repo",
        pr_number=1,
        head_sha="head",
        file_diffs={"app.py": "@@ -0,0 +1,3 @@\n+def lookup(value):\n+    return value.id\n+enabled = True\n"},
        impact_manifest={
            "files": [
                {
                    "path": "app.py",
                    "changed_symbols": [{"name": "lookup", "start_line": 1, "end_line": 2, "added_lines": [1, 2]}],
                }
            ]
        },
    )
    unit = compile_semantic_changeset(state).units[0]
    book = state.ledger = HypothesisLedger("run", "head", "workspace", protocol_version=3)
    book.upsert(proposed(unit.id))
    review_model = ReceiptModel(decision=decision if decision != "budget-exhausted" else "accepted")
    if decision == "budget-exhausted":
        book.request_budget = {"limit": 1}
    await ProofGate(review_model).run(book)
    if decision in {"provider-error", "output-error"}:
        await ProofGate(review_model).run(book)  # persist the actual attempt cap
    database = Database(tmp_path / "outbox.sqlite")
    await database.connect()
    await database.create_run("run", "owner/repo", 1, "head")
    posts = []

    async def invoke(name, params, *args, **kwargs):
        posts.append(params)
        return {
            "delivered_indexes": list(range(len(params["comments"]))),
            "review": {"id": len(posts)},
            "compatibility": False,
        }

    no_requests = NoNetworkModel()
    fake = SimpleNamespace(
        _db=database,
        _events=EventBus(),
        _gateway=SimpleNamespace(workspace_for=AsyncMock(return_value=workspace), invoke=invoke),
        _pipeline_v4_config=PipelineV4Config(mode="hypothesis", protocol_version=3),
        _model_router=SimpleNamespace(get_llm=lambda *_: no_requests),
    )
    generator = AsyncMock(
        return_value=SimpleNamespace(
            accepted=0, dropped_unanchored=0, dropped_invalid=0, dropped_overflow=0, blocks=0, failed_blocks=0
        )
    )
    monkeypatch.setattr("reviewforge.engine.pipeline_v4.HypothesisGenerator.run", generator)
    return state, fake, database, posts, no_requests, review_model, generator, unit.id


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["refuted", "unknown", "provider-error", "output-error", "budget-exhausted"])
async def test_terminal_proof_does_not_repeat_summary_or_resume_model_stages(tmp_path, monkeypatch, decision):
    state, fake, database, posts, no_requests, review_model, generator, _ = await scenario(
        tmp_path, monkeypatch, decision
    )
    try:
        first = await run_hypothesis_pipeline(fake, state)
        assert len(posts) == 1 and not posts[0]["comments"]
        original = await database.get_v4_publication("run")
        await database.close()
        await database.connect()
        state.ledger = await database.load_hypothesis_ledger("run")
        second = await run_hypothesis_pipeline(fake, state)
        third = await run_hypothesis_pipeline(fake, state)
        assert len(posts) == 1 and len(await database.list_v4_publications("run")) == 1
        assert (await database.get_v4_publication("run"))["payload"] == original["payload"]
        assert generator.await_count == 1 and not no_requests.calls
        assert all(health.completed for health in (first, second, third)) == (decision in {"refuted", "unknown"})
        if decision in {"provider-error", "output-error", "budget-exhausted"}:
            assert second.publication.failures == 1 and not second.publication.retryable
        assert len(review_model.calls) == (
            2 if decision in {"provider-error", "output-error"} else 0 if decision == "budget-exhausted" else 1
        )
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_last_provider_retry_does_not_create_duplicate_unchanged_summary(tmp_path, monkeypatch):
    state, fake, database, posts, no_requests, _, generator, _ = await scenario(tmp_path, monkeypatch, "provider-error")
    # Exercise both attempts through the actual pipeline and SQLite outbox.
    state.ledger.publication_reviews.clear()
    failing = ReceiptModel(decision="provider-error")
    fake._model_router.get_llm = lambda *_: failing
    try:
        # First execution fails once and publishes the unverified status summary.
        health = await run_hypothesis_pipeline(fake, state)
        assert not health.completed and health.publication.retryable
        assert len(posts) == 1 and len(failing.calls) == 1
        health = await run_hypothesis_pipeline(fake, state)
        assert not health.completed and not health.publication.retryable
        assert len(posts) == 1 and len(failing.calls) == 2
        await run_hypothesis_pipeline(fake, state)
        assert len(posts) == 1 and len(failing.calls) == 2
        assert len(await database.list_v4_publications("run")) == 1
        assert generator.await_count == 1 and not no_requests.calls
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_new_accepted_candidate_and_changed_accepted_facts_are_supplemented_once(tmp_path, monkeypatch):
    state, fake, database, posts, no_requests, _, generator, unit_id = await scenario(tmp_path, monkeypatch, "accepted")
    try:
        assert (await run_hypothesis_pipeline(fake, state)).completed
        assert len(posts) == 1 and len(posts[0]["comments"]) == 1
        first_payload = (await database.get_v4_publication("run"))["payload"]
        state.ledger.upsert(proposed(unit_id, "h_new", Mechanism.ERROR_PATH))
        await ProofGate(ReceiptModel()).run(state.ledger)
        assert (await run_hypothesis_pipeline(fake, state)).completed
        assert len(posts) == 2 and "h_new" in posts[1]["comments"][0]["body"]
        assert "h_first" not in posts[1]["comments"][0]["body"]
        assert (await run_hypothesis_pipeline(fake, state)).completed and len(posts) == 2
        first = next(item for item in state.ledger.items.values() if item.id == "h_first")
        first.sites.append(Site("app.py", 3, "enabled = True"))
        await ProofGate(ReceiptModel()).run(state.ledger)
        assert (await run_hypothesis_pipeline(fake, state)).completed
        assert len(posts) == 3 and posts[2]["comments"] == [] and "app.py:3" in posts[2]["body"]
        assert (await run_hypothesis_pipeline(fake, state)).completed and len(posts) == 3
        assert generator.await_count == 1 and not no_requests.calls
        assert (await database.get_v4_publication("run"))["payload"] == first_payload
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_undelivered_current_proof_outbox_reconciles_before_new_work(tmp_path, monkeypatch):
    state, fake, database, posts, no_requests, _, generator, _ = await scenario(tmp_path, monkeypatch, "accepted")
    try:
        publication = await Editor(no_requests).run(state.ledger, None)
        body = render_review_body(publication, state.ledger)
        payload, rejected = publication_payload(state, publication, review_body=body)
        payload.update(rejected=rejected, coverage=_publication_coverage(state.ledger, publication, payload))
        await database.prepare_v4_publication("run", "head", payload)
        await database.claim_v4_publication("run")
        assert (await run_hypothesis_pipeline(fake, state)).completed
        assert len(posts) == 1 and posts[0]["reconcile_only"] is True
        assert (await database.get_v4_publication("run"))["status"] == "delivered"
        assert (await run_hypothesis_pipeline(fake, state)).completed and len(posts) == 1
        assert len(await database.list_v4_publications("run")) == 1
        assert not no_requests.calls and generator.await_count == 0
    finally:
        await database.close()
