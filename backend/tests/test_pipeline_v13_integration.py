"""Ordered progress survives a second local note in one real native response."""

from __future__ import annotations

import asyncio
import copy
import json
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage
from test_discovery_workflow_v6 import batch, lead, no_issue
from test_discovery_workflow_v7 import inputs
from test_pipeline_v12_integration import blind_reference, local_reference, runtime
from test_review_session_v5 import SessionModel, call, packet, receipt

from reviewforge.core.config import PipelineV4Config
from reviewforge.engine import pipeline_v4
from reviewforge.engine.hypothesis import HypothesisLedger, HypothesisStatus
from reviewforge.engine.review_workspace import SharedReviewWorkspace

FIRST = "JOURNAL_FIRST_TOOL_KEYS_CHECKED"
RESOLVED = "JOURNAL_KEYS_PRESENCE_RESOLVED"
SECOND = "JOURNAL_SECOND_TOOL_OTHER_LOCAL_QUESTION"
QUESTION = "JOURNAL_NEW_CONSUMER_QUESTION"
RELATED = "RELATED_TASK_PREVIOUS_FINDING"
CORRECTION = "RELATED_TASK_LATER_CORRECTION"
UNRELATED = "UNRELATED_TASK_PRIVATE_FINDING"


def read_call(path, *, first):
    return call(
        "read_file",
        {
            "path": path,
            "start": 1,
            "end": 1,
            "review_progress": {
                "summary": FIRST if first else SECOND,
                "open_questions": [] if first else [QUESTION],
                "resolved_questions": [RESOLVED] if first else [],
                "next_action": "Inspect the separate consumer requirement.",
                "evidence_ids": [],
            },
        },
        identity="first-source" if first else "second-source",
    )


def assert_journal(view, *, both):
    history = view["progress_journal"]
    assert UNRELATED not in str(view)
    assert RELATED in str(history) and CORRECTION in str(history)
    assert all(group["trust"] == "untrusted-navigation-history" for group in history)
    related = next(group for group in history if group["task_id"] == "related-previous-task")
    assert [event["summary"] for event in related["events"]] == [RELATED, CORRECTION]
    assert related["events"][0]["resolved_questions"] == ["An earlier check."]
    assert related["events"][1]["resolved_questions"] == []
    assert related["events"][1]["open_questions"] == ["Recheck the earlier interpretation."]
    assert next(note for note in view["notes"] if note["task_id"] == "related-previous-task")["summary"] == CORRECTION
    events = [event for group in history for event in group["events"] if event["summary"] in {FIRST, SECOND}]
    assert events and events[0]["summary"] == FIRST
    assert events[0]["resolved_questions"] == [RESOLVED]
    if both:
        assert [event["summary"] for event in events] == [FIRST, SECOND]
        assert events[0]["sequence"] < events[1]["sequence"]
        assert events[1]["resolved_questions"] == [] and events[1]["open_questions"] == [QUESTION]
        current = [note for note in view["notes"] if note["summary"] in {FIRST, SECOND}]
        assert len(current) == 1 and current[0]["summary"] == SECOND
        assert current[0]["resolved_questions"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("interrupt_after_reads", [0, 1, 2])
async def test_two_tools_local_progress_survives_later_input_resume_streaming_and_blind_proof(
    monkeypatch,
    interrupt_after_reads,
):
    ledger = HypothesisLedger("run13", "head", "digest", protocol_version=13)
    shared = SharedReviewWorkspace(ledger)
    for summary in (RELATED, CORRECTION):
        shared.publish_progress(
            task_id="related-previous-task",
            actor="discovery",
            paths=["file0.py"],
            summary=summary,
            resolved_questions=["An earlier check."] if summary == RELATED else [],
            open_questions=[] if summary == RELATED else ["Recheck the earlier interpretation."],
        )
    shared.publish_progress(
        task_id="unrelated-task",
        actor="discovery",
        paths=["unrelated/private.py"],
        summary=UNRELATED,
        resolved_questions=["Private unrelated conclusion."],
    )
    review_state, changes, pack = inputs(2)
    pack.workspace_digest = "digest"
    snapshots, source_calls, events = [], [], []
    interrupted, investigation_seen, blind_seen, open_seen = False, False, False, False

    async def execute(name, args):
        source_calls.append((name, copy.deepcopy(args)))
        if name == "read_diff":
            return review_state.file_diffs[args["path"]]
        return {
            "consumer_first.py": "assert lookup(stored_id) == old_value\n",
            "consumer_second.py": "stored_id = persist(old_value)\n",
        }.get(args.get("path"), "return new_value\n")

    monkeypatch.setattr(pipeline_v4, "build_workspace_executor", lambda *_args, **_kwargs: execute)

    async def persist(_runtime, current):
        nonlocal interrupted, open_seen
        snapshots.append(copy.deepcopy(current.to_dict()))
        open_seen = open_seen or any(item.status == HypothesisStatus.OPEN for item in current.items.values())
        read_paths = {
            observation.get("path")
            for block in current.discovery_blocks.values()
            for observation in block.get("observations", [])
            if observation.get("status") == "success"
        }
        count = len(read_paths & {"consumer_first.py", "consumer_second.py"})
        if interrupt_after_reads and not interrupted and count >= interrupt_after_reads:
            interrupted = True
            raise asyncio.CancelledError()

    monkeypatch.setattr(pipeline_v4, "_persist_ledger", persist)

    def answer(messages, _options, _turn):
        nonlocal investigation_seen, blind_seen
        data = packet(messages)
        if "case_id" in data:
            events.append("investigation")
            investigation_seen = True
            assert_journal(data["shared_review_workspace_untrusted"], both=True)
            return receipt(
                call(
                    "submit_confirmed",
                    {
                        "case_id": data["case_id"],
                        "reason": "The concrete source establishes conflicting behavior.",
                        "expected": "The consumer expects old_value for stored_id.",
                        "actual": "The return yields new_value.",
                        "expected_evidence": [local_reference(data["observations"], "consumer_first.py")],
                        "actual_evidence": [local_reference(data["observations"], "file0.py")],
                    },
                )
            )
        if "items" in data:
            text = json.dumps(data)
            assert not any(
                marker in text for marker in (FIRST, RESOLVED, SECOND, QUESTION, RELATED, CORRECTION, UNRELATED)
            )
            assert "progress_journal" not in text and "review_progress" not in text
            item = data["items"][0]
            if "candidate" not in item:
                blind_seen = True
                events.append("blind")
                return AIMessage(
                    content=json.dumps(
                        {
                            "audits": [
                                {
                                    "hypothesis_id": item["hypothesis_id"],
                                    "execution": [
                                        {
                                            "statement": "The operation returns new_value.",
                                            "evidence": [blind_reference(item["observations"], "file0.py")],
                                        }
                                    ],
                                    "contracts": [
                                        {
                                            "kind": "test",
                                            "statement": "The consumer expects old_value.",
                                            "evidence": [blind_reference(item["observations"], "consumer_first.py")],
                                        }
                                    ],
                                    "gaps": [],
                                }
                            ]
                        }
                    )
                )
            return AIMessage(
                content=json.dumps(
                    {
                        "reviews": [
                            {
                                "hypothesis_id": item["candidate"]["id"],
                                "decision": "unknown",
                                "reason": "The sources do not yet establish an introduced regression.",
                                "missing_obligations": ["introduced_regression"],
                            }
                        ]
                    }
                )
            )
        events.append("discovery")
        view = data["shared_review_workspace"]
        assert UNRELATED not in str(view)
        assert RELATED in str(view["progress_journal"]) and CORRECTION in str(view["progress_journal"])
        paths = {row["path"] for row in data["read_history_navigation_only"] if row["status"] == "success"}
        if "consumer_first.py" not in paths:
            # Reproduce the real failure: the second call does not restate the first call's resolved conclusion.
            return receipt(read_call("consumer_first.py", first=True), read_call("consumer_second.py", first=False))
        assert_journal(view, both="consumer_second.py" in paths)
        if "consumer_second.py" not in paths:
            return receipt(read_call("consumer_second.py", first=False))
        units = data["pending_units"]
        if len(units) == 2:
            events.append("submit-open")
            return batch(lead(units[0]))
        assert investigation_seen and open_seen
        events.append("last-discovery")
        return batch(*map(no_issue, units))

    model = SessionModel(handler=answer)
    arguments = (runtime(model), review_state, changes, pack)
    config = PipelineV4Config(provider_max_attempts=1)
    assert config.protocol_version == 13
    if interrupt_after_reads:
        with pytest.raises(asyncio.CancelledError):
            await pipeline_v4._run_llm_stages(*arguments, ledger, SimpleNamespace(), config, "en", {})
        ledger = HypothesisLedger.from_dict(json.loads(json.dumps(snapshots[-1])))
        assert ledger.protocol_version == 13
    publication = await pipeline_v4._run_llm_stages(
        *arguments,
        ledger,
        SimpleNamespace(),
        config,
        "en",
        {},
        resume=bool(interrupt_after_reads),
    )
    assert blind_seen and events.index("submit-open") < events.index("investigation") < events.index("last-discovery")
    assert len(ledger.items) == len(ledger.no_issue_units) == 1 and not ledger.unresolved_units
    case = next(iter(ledger.items.values()))
    assert case.status == HypothesisStatus.CONFIRMED and case.attempts == 1
    assert ledger.publication_reviews[case.id]["decision"] == "unknown" and not publication.comments
    for path in ("consumer_first.py", "consumer_second.py"):
        assert sum(args.get("path") == path for name, args in source_calls if name == "read_file") == 1
    assert not any(
        marker in str(ledger.evidence_records) for marker in (FIRST, RESOLVED, SECOND, QUESTION, RELATED, UNRELATED)
    )
    assert_journal(
        SharedReviewWorkspace(ledger).view(paths=["file0.py"], role="investigation", catalog="directory"), both=True
    )
    for role in ("proof", "source_audit"):
        blind_view = SharedReviewWorkspace(ledger).view(paths=["file0.py"], role=role)
        assert "progress_journal" not in blind_view and "notes" not in blind_view
    count = len(model.calls)
    restored = HypothesisLedger.from_dict(ledger.to_dict())
    await pipeline_v4._run_llm_stages(*arguments, restored, SimpleNamespace(), config, "en", {}, resume=True)
    assert len(model.calls) == count


@pytest.mark.parametrize("protocol", range(1, 14))
def test_old_ledger_protocol_and_old_workspace_shape_do_not_upgrade_with_default13(protocol):
    ledger = HypothesisLedger("run", "head", "digest", protocol_version=protocol)
    if protocol >= 11:
        workspace = SharedReviewWorkspace(ledger)
        workspace.publish_progress(task_id="old", actor="discovery", paths=["file.py"], summary="A prior local note.")
        view = workspace.view(paths=["file.py"], role="discovery")
        assert ("progress_journal" in view) is (protocol >= 13)
    restored = HypothesisLedger.from_dict(ledger.to_dict())
    assert restored.protocol_version == protocol and PipelineV4Config().protocol_version == 13
