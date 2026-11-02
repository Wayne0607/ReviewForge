"""Offline model scripts exercise the real unit-to-independent-verifier boundary."""

import json

import pytest
from langchain_core.messages import AIMessage
from test_v42_stateful_reviewer import Gateway, call

from reviewforge.review_v42.contracts import Case, ReviewScope, Run
from reviewforge.review_v42.publication import Publication, PublicationOutbox
from reviewforge.review_v42.runtime import ReviewRuntime
from reviewforge.review_v42.store import ReviewStore
from reviewforge.review_v42.tools import EvidenceGateway
from reviewforge.review_v42.verifier import Verifier


@pytest.mark.asyncio
async def test_audited_candidate_reaches_real_independent_verifier(tmp_path, monkeypatch):
    region = {"id": "one", "path": "a.py", "side": "head", "start_line": 1, "end_line": 1, "changed_lines": [1]}
    run = Run(
        "r",
        "a/b",
        "base",
        "head",
        policy="v42-units-6",
        metadata={
            "changed_regions": [region],
            "workspace_digest": "head-snapshot",
            "base_workspace_digest": "base-snapshot",
        },
    )
    store = ReviewStore(tmp_path / "board.db", run)
    new_ref = None
    read_new = False

    class Source(Gateway):
        async def __call__(self, name, args):
            return {
                "content": "send(original_emails)\nnotify(recipients)\nassert notified == newly_inserted\n",
                "revision": "head",
                "side": "head",
                "kind": "source",
                "path": args["path"],
                "start_line": 1,
                "end_line": 3,
                "metadata": {"workspace_digest": "head-snapshot", "status": "available"},
            }

    class ReviewerModel:
        def bind_tools(self, tools, **kwargs):
            return self

        async def ainvoke(self, messages):
            board = json.loads(messages[1].content)
            previous = [json.loads(m.content) for m in messages if m.type == "tool"]
            if read_new and new_ref and not any(x.get("evidence_ref") == new_ref for x in previous):
                return AIMessage(content="", tool_calls=[call("read_evidence", {"ref": new_ref}, "read-new")])
            if not previous:
                return AIMessage(content="", tool_calls=[call("read_source", {"path": "a.py"}, "read")])
            ref = new_ref if read_new else next(x["evidence_ref"] for x in previous if "evidence_ref" in x)
            if board["unit_role"] == "primary":
                calls = (
                    []
                    if store.list(Case)
                    else [
                        call(
                            "submit_case",
                            {
                                "trigger": "existing participant",
                                "behavior": "send original emails",
                                "impact": "duplicate notice",
                                "evidence_refs": [ref],
                            },
                            "case",
                        )
                    ]
                )
                return AIMessage(
                    content="",
                    tool_calls=[
                        *calls,
                        call(
                            "submit_unit_decision",
                            {
                                "checked_region_ids": ["one"],
                                "observations": [
                                    {"statement": "Inspected source", "evidence_refs": [ref], "region_ids": ["one"]}
                                ],
                                "unresolved_refs": [],
                                "finish": True,
                            },
                            "decision",
                        ),
                    ],
                )
            calls = [
                call(
                    "record_integration_receipt",
                    {
                        "obligation_id": o["id"],
                        "disposition": "checked",
                        "evidence_refs": [ref],
                        "reason": "Inspected source",
                    },
                    o["id"],
                )
                for o in board["integration_obligations"]
            ]
            return AIMessage(content="", tool_calls=[*calls, call("finish_integration", {}, "finish")])

    class IndependentModel:
        def bind_tools(self, tools, **kwargs):
            return self

        async def ainvoke(self, messages):
            packet = json.loads(messages[1].content)
            ref = packet["evidence"][0]["ref"]["id"]

            def fact(statement, quote, line):
                return {
                    "statement": statement,
                    "citations": [{"ref": ref, "quote": quote, "start_line": line, "end_line": line}],
                }

            return AIMessage(
                content="",
                tool_calls=[
                    call(
                        "submit_verdict",
                        {
                            "verdict": "accept",
                            "reason": "source-backed independent analysis",
                            "gaps": [],
                            "facts": {
                                "trigger": fact("original participants are sent", "send(original_emails)", 1),
                                "actual": fact("the original participants are sent", "send(original_emails)", 1),
                                "expected": fact(
                                    "only new participants should be notified", "assert notified == newly_inserted", 3
                                ),
                                "change": fact(
                                    "the changed call sends original participants", "send(original_emails)", 1
                                ),
                                "impact": fact("existing participants receive notification", "notify(recipients)", 2),
                            },
                        },
                        "verdict",
                    )
                ],
            )

    gateway = EvidenceGateway(store, Source())
    runtime = ReviewRuntime(store, ReviewerModel(), gateway, Verifier(store, IndependentModel(), gateway))
    result = await runtime.run([region])
    assert result["status"] == "completed", result
    case = store.list(Case)[0]
    verdict = store.get_verdict(case.id, case.claims[0]["id"], "v42-independent-1")
    assert verdict and verdict["verdict"] == "accept"
    report = Publication(store).local_report()
    assert report["status"] == "complete", report["unresolved"]
    assert len(report["comments"]) == 1
    with monkeypatch.context() as temporary:
        temporary.setattr(store, "pending_unit_cases", lambda **kwargs: [{"case_id": "unrelated", "scope_id": "other"}])
        assert len(Publication(store).local_report()["comments"]) == 1
        temporary.setattr(store, "pending_unit_cases", lambda **kwargs: None)
        assert Publication(store).local_report()["comments"] == []
    outbox = PublicationOutbox(store)
    original_comment = outbox.freeze()[0]
    source = store.put_evidence(
        b"send(original_emails)\nnotify(recipients)\nassert notified == newly_inserted\n# added context\n",
        revision="head",
        side="head",
        kind="source",
        path="a.py",
        start_line=1,
        end_line=4,
        metadata={"workspace_digest": "head-snapshot", "status": "available"},
    )
    scope = store.get(ReviewScope, case.scope_id)
    store.reopen_unit_for_evidence(
        scope.id, scope.revision, [source.id], "new source for this case", case_ids=[case.id]
    )
    new_ref = source.id
    assert store.get_verdict(case.id, case.claims[0]["id"], "v42-independent-1") is None
    assert Publication(store).local_report()["status"] == "partial"
    with pytest.raises(ValueError, match="permission"):
        outbox.prepare(original_comment["id"])
    stale_attempt = await runtime.run([region])
    assert stale_attempt["status"] == "partial", stale_attempt
    assert store.get_verdict(case.id, case.claims[0]["id"], "v42-independent-1")["verdict"] == "accept"
    pending_report = Publication(store).local_report()
    assert pending_report["status"] == "partial"
    assert pending_report["comments"] == []
    assert outbox.freeze() == []
    read_new = True
    resumed = await runtime.run([region])
    assert resumed["status"] == "completed", json.dumps(resumed, ensure_ascii=False)
    assert store.get_verdict(case.id, case.claims[0]["id"], "v42-independent-1")["verdict"] == "accept"
    assert Publication(store).local_report()["status"] == "complete"
    assert len(outbox.freeze()) == 1
