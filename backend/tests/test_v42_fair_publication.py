"""Publication and verifier fences with concurrent cooperative review units."""

import asyncio
import json

import pytest
from langchain_core.messages import AIMessage
from test_v42_stateful_reviewer import Gateway, call

from reviewforge.review_v42.contracts import BindingError, Case, LeaseError, ReviewScope, Run, Task
from reviewforge.review_v42.publication import Publication, PublicationOutbox
from reviewforge.review_v42.runtime import ReviewRuntime
from reviewforge.review_v42.store import ReviewStore
from reviewforge.review_v42.tools import EvidenceGateway
from reviewforge.review_v42.verifier import Verifier


@pytest.mark.asyncio
async def test_active_units_keep_candidate_and_publication_fences_independent(tmp_path):
    regions = [
        {"id": ident, "path": path, "side": "head", "start_line": 1, "end_line": 1, "changed_lines": [1]}
        for ident, path in (("a", "a.py"), ("b", "b.py"))
    ]
    store = ReviewStore(
        tmp_path / "fair.db",
        Run(
            "r",
            "repo",
            "base",
            "head",
            policy="v42-units-6",
            metadata={
                "changed_regions": regions,
                "workspace_digest": "head-snapshot",
                "base_workspace_digest": "base-snapshot",
                "unit_scheduler": "cooperative-1",
            },
        ),
    )
    delivered = asyncio.Event()
    continued_reads = asyncio.Event()
    unit_ids = {}
    counts = {"a": 0, "b": 0}

    class Source(Gateway):
        async def __call__(self, name, args):
            await asyncio.sleep(0.005)
            return {
                "content": "send(original)\nnotify(recipient)\nassert only_new\n",
                "revision": "head",
                "side": "head",
                "kind": "source",
                "path": args["path"],
                "start_line": 1,
                "end_line": 3,
                "metadata": {"workspace_digest": "head-snapshot", "status": "available"},
            }

    class ReviewModel:
        def bind_tools(self, tools, **kwargs):
            return self

        async def ainvoke(self, messages):
            board = json.loads(messages[1].content)
            assert board["unit_role"] == "primary"
            scope = board["scope"]
            identity = scope["regions"][0]["id"]
            counts[identity] += 1
            if identity == "a" and counts[identity] >= 2:
                continued_reads.set()
            receipts = [json.loads(m.content) for m in messages if m.type == "tool"]
            if identity == "b" and counts[identity] == 2:
                ref = next(x["evidence_ref"] for x in receipts if "evidence_ref" in x)
                return AIMessage(
                    content="",
                    tool_calls=[
                        call(
                            "register_unit_dependency",
                            {
                                "target_scope_id": unit_ids["a"],
                                "description": "producer and consumer interact",
                                "evidence_refs": [ref],
                            },
                            "dependency",
                        ),
                        call(
                            "submit_case",
                            {
                                "trigger": "existing recipient",
                                "behavior": "send original",
                                "impact": "duplicate notice",
                                "evidence_refs": [ref],
                            },
                            "candidate",
                        ),
                    ],
                )
            path = "b.py" if identity == "b" and counts[identity] == 1 else f"{identity}-lookup-{counts[identity]}.py"
            return AIMessage(
                content="", tool_calls=[call("read_source", {"path": path}, f"read-{identity}-{counts[identity]}")]
            )

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
                            "reason": "independently examined",
                            "gaps": [],
                            "facts": {
                                "trigger": fact("original recipient is present", "send(original)", 1),
                                "actual": fact("original recipient is sent", "send(original)", 1),
                                "expected": fact("only new recipients should be sent", "assert only_new", 3),
                                "change": fact("changed call sends original", "send(original)", 1),
                                "impact": fact("recipient is notified", "notify(recipient)", 2),
                            },
                        },
                        "verdict",
                    )
                ],
            )

    class NotifyingVerifier(Verifier):
        async def verify(self, *args, **kwargs):
            result = await super().verify(*args, **kwargs)
            delivered.set()
            return result

    gateway = EvidenceGateway(store, Source())
    runtime = ReviewRuntime(store, ReviewModel(), gateway, NotifyingVerifier(store, IndependentModel(), gateway))
    for scope in runtime.prepare(regions):
        unit_ids[scope.regions[0]["id"]] = scope.id
    running = asyncio.create_task(runtime.run(regions))
    try:
        await asyncio.wait_for(delivered.wait(), 10)
        await asyncio.wait_for(continued_reads.wait(), 10)
        assert counts["a"] >= 2 and counts["b"] >= 2 and not running.done()
        case = store.list(Case)[0]
        assert store.get_verdict(case.id, case.claims[0]["id"], "v42-independent-1")["verdict"] == "accept"
        scopes = {s.id: s for s in store.list(ReviewScope)}
        assert scopes[unit_ids["a"]].checked_regions == []
        assert scopes[unit_ids["b"]].checked_regions == []
        obligations = store.list_integration_obligations()
        assert any(x["kind"] == "discovered" and not x["valid"] for x in obligations)
        report = Publication(store).local_report()
        assert report["status"] == "partial" and len(report["comments"]) == 1
        outbox = PublicationOutbox(store)
        frozen = outbox.freeze()[0]

        leases = {t.object_id: t for t in store.list(Task) if t.kind == "review" and t.status == "leased"}
        assert {unit_ids["a"], unit_ids["b"]} <= leases.keys()
        with pytest.raises(LeaseError):
            store.pin_frontier_for_task(
                leases[unit_ids["a"]].id, leases[unit_ids["b"]].lease_token, case.evidence_refs[0]
            )

        # A pinned ref, without a successful request/response in this lease,
        # cannot forge an inspected region while a sibling unit is active.
        unread = store.put_evidence(
            b"unread source\n",
            revision="head",
            side="head",
            kind="source",
            path="a.py",
            start_line=1,
            end_line=1,
            metadata={"workspace_digest": "head-snapshot"},
        )
        task_a = leases[unit_ids["a"]]
        store.pin_frontier_for_task(task_a.id, task_a.lease_token, unread.id)
        scope_a = store.get(ReviewScope, unit_ids["a"])
        with pytest.raises((BindingError, LeaseError)):
            store.commit_unit_decision(
                task_a.id,
                task_a.lease_token,
                scope_a.revision,
                "forged-decision",
                checked_region_ids=["a"],
                observations=[{"statement": "claimed inspection", "evidence_refs": [unread.id], "region_ids": ["a"]}],
                questions=[],
                cases=[],
                unresolved=[],
                finish=True,
                turn_id="not-delivered",
            )
        assert store.get(ReviewScope, unit_ids["a"]).checked_regions == []

        evidence = store.put_evidence(
            b"send(original)\nnotify(recipient)\nassert only_new\n# new\n",
            revision="head",
            side="head",
            kind="source",
            path="b.py",
            start_line=1,
            end_line=4,
            metadata={"workspace_digest": "head-snapshot"},
        )
        before = store.get(ReviewScope, case.scope_id)
        store.reopen_unit_for_evidence(before.id, before.revision, [evidence.id], "new evidence", case_ids=[case.id])
        with pytest.raises(ValueError, match="permission"):
            outbox.prepare(frozen["id"])
        assert store.get_verdict(case.id, case.claims[0]["id"], "v42-independent-1") is None
        delivered.clear()
        runtime._enqueue(store.get(Case, case.id))
        await asyncio.wait_for(delivered.wait(), 10)
        assert store.get_verdict(case.id, case.claims[0]["id"], "v42-independent-1")["verdict"] == "accept"
        assert Publication(store).local_report()["status"] == "partial"
        assert outbox.freeze() == []
    finally:
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)
