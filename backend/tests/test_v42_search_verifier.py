"""Search projections remain navigation in the frontier-5 independent verifier."""

import json

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from reviewforge.review_v42.context import ContextBuilder
from reviewforge.review_v42.contracts import Case, ReviewScope, Run, Task
from reviewforge.review_v42.publication import Publication, PublicationOutbox
from reviewforge.review_v42.store import ReviewStore
from reviewforge.review_v42.tools import EvidenceGateway
from reviewforge.review_v42.verifier import Verifier, validate_result


@pytest.fixture
def board(tmp_path):
    region = {"id": "r", "path": "source.py", "side": "head", "start_line": 1, "end_line": 1, "changed_lines": [1]}
    run = Run(
        "run",
        "repo",
        "base",
        "head",
        policy="v42-frontier-5",
        metadata={"changed_regions": [region], "workspace_digest": "workspace"},
    )
    store = ReviewStore(tmp_path / "run.sqlite", run)
    store.add(ReviewScope("scope", "run", "scope", regions=[region], status="reviewed"))
    source = store.put_evidence(
        b"actual source line\n",
        revision="head",
        side="head",
        kind="source",
        path="source.py",
        start_line=1,
        end_line=1,
        metadata={"status": "available"},
    )
    document = {
        "query": "needle",
        "glob": "",
        "matches": [],
        "offset": 0,
        "next_offset": None,
        "total_matches": 0,
        "skipped_paths": ["PRIVATE_SKIPPED_PATH"],
        "aliases_resolved": False,
    }
    search = store.put_evidence(
        json.dumps(document).encode(),
        revision="head",
        side="head",
        kind="search",
        metadata={
            "status": "available",
            "complete": False,
            "navigation_only": True,
            "tool_name": "search_code",
            "tool_args": {"query": "needle"},
            "workspace_digest": "workspace",
        },
    )
    store.add(
        Case(
            "case",
            "run",
            "scope",
            trigger="input",
            mechanism="behavior",
            evidence_refs=[source.id, search.id],
            claims=[{"id": "claim", "statement": "behavior", "impact": "impact"}],
        )
    )
    store.add(Task("review", "run", "review", "scope", 0))
    task = store.acquire_task("review", "owner", 100, now=0)
    return store, source, search, task


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


def reject(ref, quote):
    return {
        "verdict": "reject",
        "reason": "purported proof of absence",
        "gaps": [],
        "facts": {"counterexample": {"statement": "not reachable", "citations": [{"ref": ref.id, "quote": quote}]}},
    }


@pytest.mark.asyncio
async def test_incomplete_last_page_stays_navigation_and_frozen_verdict_survives_pin_release(board):
    store, source, search, task = board
    entry = store.pin_frontier_for_task("review", task.lease_token, search.id, now=1)
    builder = ContextBuilder(store)
    index = builder.search_index(entry)
    assert index["kind"] == "search_index"
    assert index["sources"][0]["next_offset"] is None and index["sources"][0]["complete"] is False
    section = builder.search_section(search.id, "skipped_paths", offset=0, limit=1)
    assert section["items"] == ["PRIVATE_SKIPPED_PATH"] and section["complete"] is False
    with pytest.raises(ValueError, match="navigation-only"):
        validate_result(store, reject(search, "PRIVATE_SKIPPED_PATH"), {source.id, search.id})

    class Model:
        def bind_tools(self, tools, **kwargs):
            return self

        async def ainvoke(self, messages):
            packet = json.loads(messages[1].content)
            assert [item["ref"]["id"] for item in packet["evidence"]] == [source.id]
            assert "PRIVATE_SKIPPED_PATH" not in messages[1].content and search.id not in messages[1].content
            assert entry.id not in messages[1].content and "never evidence of absence" in messages[0].content
            return AIMessage(
                content="", tool_calls=[{"name": "submit_verdict", "id": "ok", "args": acceptance(source)}]
            )

    verdict = (await Verifier(store, Model(), None).verify("case"))[0]
    assert verdict["verdict"] == "accept"
    original = Publication(store).local_report()
    assert original["status"] == "complete"
    outbox = PublicationOutbox(store)
    pending = outbox.freeze()[0]
    store.release_frontier_for_task(
        "review",
        task.lease_token,
        [entry.id],
        "navigation no longer needed",
        expected_revision=store.frontier_revision("review"),
        now=2,
    )
    assert store.get_verdict("case", "claim", "v42-independent-1") == verdict
    assert Publication(store).local_report()["comments"] == original["comments"]
    assert outbox.prepare(pending["id"]) == pending


@pytest.mark.parametrize(
    "form",
    [
        "complete_search",
        "search_index",
        "search_section",
        "source_search_tool",
        "source_navigation_marker",
        "source_index_body",
        "source_section_body",
    ],
)
@pytest.mark.parametrize("fact", ["actual", "expected", "counterexample"])
def test_search_or_disguised_projection_cannot_be_fact_or_counterevidence(board, form, fact):
    store, source, _, _ = board
    metadata = {"status": "available", "complete": True}
    content, kind = '"needle"', form
    if form == "complete_search":
        kind, metadata["navigation_only"] = "search", True
    elif form in {"search_index", "search_section"}:
        kind = form
    elif form == "source_search_tool":
        kind, metadata["tool_name"] = "source", "search_code"
    elif form == "source_navigation_marker":
        kind, metadata["navigation_only"] = "source", True
    elif form == "source_index_body":
        kind, content = (
            "source",
            json.dumps({"kind": "search_index", "entry_id": "x", "sources": [], "marker": "needle"}),
        )
    else:
        kind, content = (
            "source",
            json.dumps(
                {
                    "kind": "search_section",
                    "navigation_only": True,
                    "ref": "x",
                    "section": "matches",
                    "offset": 0,
                    "items": [],
                    "content_hash": "hash",
                    "marker": "needle",
                }
            ),
        )
    ref = store.put_evidence(content.encode(), revision="head", side="head", kind=kind, metadata=metadata)
    if fact == "counterexample":
        result = reject(ref, "needle")
    else:
        result = acceptance(source)
        result["facts"][fact]["citations"] = [{"ref": ref.id, "quote": "needle"}]
    with pytest.raises(ValueError, match="navigation-only"):
        validate_result(store, result, {source.id, ref.id})


def test_regular_json_source_with_similar_wording_is_not_rejected(board):
    store, source, _, _ = board
    ref = store.put_evidence(
        b'{"kind":"search_index","meaning":"regular source"}',
        revision="head",
        side="head",
        kind="source",
        path="config.json",
    )
    result = acceptance(source)
    result["facts"]["expected"]["citations"] = [{"ref": ref.id, "quote": "regular source"}]
    assert validate_result(store, result, {source.id, ref.id})["verdict"] == "accept"


@pytest.mark.asyncio
async def test_upstream_search_cannot_be_read_by_guessing_its_id(board):
    store, source, search, _ = board

    class Probe:
        calls = 0

        def bind_tools(self, tools, **kwargs):
            return self

        async def ainvoke(self, messages):
            self.calls += 1
            if self.calls == 1:
                assert search.id not in messages[1].content
                return AIMessage(
                    content="", tool_calls=[{"name": "read_evidence", "id": "read", "args": {"ref": search.id}}]
                )
            assert "PRIVATE_SKIPPED_PATH" not in str(messages[-1].content)
            result = acceptance(source)
            result["failure_explanation"] = "The upstream navigation record is not evidence."
            return AIMessage(content="", tool_calls=[{"name": "submit_verdict", "id": "done", "args": result}])

    out = await Verifier(store, Probe(), EvidenceGateway(store, None)).verify("case")
    assert out[0]["verdict"] == "accept" and out[0]["result"]["tool_failures"]


@pytest.mark.parametrize("policy", ["v42", "v42-stateful-2", "v42-workspace-3", "v42-frontier-4"])
def test_previous_policies_keep_original_search_citation_semantics(tmp_path, policy):
    store = ReviewStore(tmp_path / "old.sqlite", Run("old", "repo", "base", "head", policy=policy))
    source = store.put_evidence(
        b"actual source line\n", revision="head", side="head", kind="source", path="source.py", start_line=1, end_line=1
    )
    search = store.put_evidence(
        b"needle", revision="head", side="head", kind="search", metadata={"status": "available", "complete": True}
    )
    result = acceptance(source)
    result["facts"]["expected"]["citations"] = [{"ref": search.id, "quote": "needle"}]
    assert validate_result(store, result, {source.id, search.id})["verdict"] == "accept"


@pytest.mark.asyncio
async def test_verifier_can_search_independently_without_using_results_as_proof(board):
    store, source, _, _ = board

    class Gateway:
        async def __call__(self, name, args):
            return {
                "content": '{"matches":[],"skipped_paths":["own search gap"]}',
                "revision": "head",
                "side": "head",
                "kind": "search",
                "metadata": {"status": "available", "navigation_only": True, "complete": False},
            }

    class IndependentSearch:
        calls = 0

        def bind_tools(self, tools, **kwargs):
            return self

        async def ainvoke(self, messages):
            self.calls += 1
            if self.calls == 1:
                return AIMessage(
                    content="", tool_calls=[{"name": "search_code", "id": "search", "args": {"query": "x"}}]
                )
            if self.calls == 2:
                receipt = json.loads(next(m.content for m in reversed(messages) if isinstance(m, ToolMessage)))
                return AIMessage(
                    content="",
                    tool_calls=[{"name": "read_evidence", "id": "read", "args": {"ref": receipt["evidence_ref"]}}],
                )
            assert "own search gap" in str(messages[-1].content)
            return AIMessage(
                content="", tool_calls=[{"name": "submit_verdict", "id": "done", "args": acceptance(source)}]
            )

    out = await Verifier(store, IndependentSearch(), EvidenceGateway(store, Gateway())).verify("case")
    assert out[0]["verdict"] == "accept" and out[0]["result"]["tool_failures"] == []
