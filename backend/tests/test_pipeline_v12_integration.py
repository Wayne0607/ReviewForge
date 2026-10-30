"""Native fake-model integration for visible discovery progress, resume and blind proof."""

from __future__ import annotations

import asyncio
import copy
import json
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage
from test_discovery_workflow_v6 import batch, lead, no_issue
from test_discovery_workflow_v7 import inputs
from test_review_session_v5 import SessionModel, call, packet, receipt

from reviewforge.core.config import PipelineV4Config
from reviewforge.core.events import EventBus
from reviewforge.engine import pipeline_v4
from reviewforge.engine.hypothesis import HypothesisLedger, HypothesisStatus

FIRST = "VISIBLE_PROGRESS_FIRST_CONSUMER_READ"
SECOND = "VISIBLE_PROGRESS_SECOND_CONSUMER_READ"


def progress(summary):
    return {
        "summary": summary,
        "open_questions": ["Does the consumer require the stored value?"],
        "resolved_questions": [],
        "next_action": "Read the concrete consumer and compare its supported input.",
        "evidence_ids": [],
    }


def runtime(model):
    return SimpleNamespace(_model_router=SimpleNamespace(get_llm=lambda *_: model), _events=EventBus(), _db=None)


def local_reference(observations, path):
    return next(
        reference
        for observation in observations
        if observation.get("path") == path
        for reference in observation["evidence"]
    )


def blind_reference(observations, path):
    return next(
        reference
        for observation in observations
        if observation.get("path") == path
        for reference in observation["references"]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("restore_after_first_read", [False, True])
async def test_v12_progress_is_visible_across_reads_restore_streamed_investigation_and_blind_gate(
    monkeypatch,
    restore_after_first_read,
):
    ledger = HypothesisLedger("run", "head", "digest", protocol_version=12)
    review_state, changes, pack = inputs(2)
    pack.workspace_digest = "digest"
    snapshots, source_calls, events = [], [], []
    interrupted = False
    investigation_seen = False
    blind_seen = False

    async def execute(name, args):
        source_calls.append((name, copy.deepcopy(args)))
        if name == "read_diff":
            return review_state.file_diffs[args["path"]]
        if args.get("path") == "consumer_first.py":
            return "assert lookup(stored_id) == old_value\n"
        if args.get("path") == "consumer_second.py":
            return "stored_id = persist(old_value)\n"
        return "return new_value\n"

    monkeypatch.setattr(pipeline_v4, "build_workspace_executor", lambda *_args, **_kwargs: execute)

    async def persist(_runtime, current):
        nonlocal interrupted
        snapshots.append(copy.deepcopy(current.to_dict()))
        saved_first_source = any(
            observation.get("path") == "consumer_first.py"
            for block in current.discovery_blocks.values()
            for observation in block.get("observations", [])
        )
        if restore_after_first_read and not interrupted and saved_first_source:
            interrupted = True
            raise asyncio.CancelledError()

    monkeypatch.setattr(pipeline_v4, "_persist_ledger", persist)

    def answer(messages, _options, _turn):
        nonlocal investigation_seen, blind_seen
        data = packet(messages)
        if "case_id" in data:
            events.append("investigation")
            investigation_seen = True
            assert FIRST in str(data["shared_review_workspace_untrusted"]["notes"])
            assert SECOND in str(data["shared_review_workspace_untrusted"]["notes"])
            expected_ref = local_reference(data["observations"], "consumer_first.py")
            actual_ref = local_reference(data["observations"], "file0.py")
            return receipt(
                call(
                    "submit_confirmed",
                    {
                        "case_id": data["case_id"],
                        "reason": "The concrete source establishes conflicting behavior.",
                        "expected": "The consumer expects the stored old_value for stored_id.",
                        "actual": "The changed return yields new_value.",
                        "expected_evidence": [expected_ref],
                        "actual_evidence": [actual_ref],
                    },
                )
            )
        if "items" in data:
            assert FIRST not in json.dumps(data) and SECOND not in json.dumps(data)
            assert "review_progress" not in json.dumps(data)
            item = data["items"][0]
            if "candidate" not in item:
                events.append("blind")
                blind_seen = True
                assert investigation_seen and "shared_review_workspace" not in item
                actual_ref = blind_reference(item["observations"], "file0.py")
                expected_ref = blind_reference(item["observations"], "consumer_first.py")
                return AIMessage(
                    content=json.dumps(
                        {
                            "audits": [
                                {
                                    "hypothesis_id": item["hypothesis_id"],
                                    "execution": [
                                        {"statement": "The operation returns new_value.", "evidence": [actual_ref]}
                                    ],
                                    "contracts": [
                                        {
                                            "kind": "test",
                                            "statement": "The consumer expects the stored old_value.",
                                            "evidence": [expected_ref],
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
        units = data["pending_units"]
        view = data["shared_review_workspace"]
        assert "evidence_catalog" not in view and "evidence_directory" in view
        read_paths = {row["path"] for row in data["read_history_navigation_only"] if row["status"] == "success"}
        if "consumer_first.py" not in read_paths:
            return receipt(
                call(
                    "read_file",
                    {
                        "path": "consumer_first.py",
                        "start": 1,
                        "end": 1,
                        "review_progress": progress(FIRST),
                    },
                )
            )
        assert FIRST in str(view["notes"]), "The previous visible read explanation must be in the next input."
        assert "assert lookup(stored_id) == old_value" in str(data["rows"])
        if "consumer_second.py" not in read_paths:
            return receipt(
                call(
                    "read_file",
                    {
                        "path": "consumer_second.py",
                        "start": 1,
                        "end": 1,
                        "review_progress": progress(FIRST + "\n" + SECOND),
                    },
                )
            )
        assert SECOND in str(view["notes"])
        if len(units) == 2:
            events.append("submit-open")
            return batch(lead(units[0]))
        assert investigation_seen, "The accepted OPEN case must be investigated before the final discovery request."
        events.append("last-discovery")
        return batch(*map(no_issue, units))

    model = SessionModel(handler=answer)
    current_runtime = runtime(model)
    config = PipelineV4Config(protocol_version=12, provider_max_attempts=1)
    assert config.protocol_version == 12
    arguments = (current_runtime, review_state, changes, pack)
    if restore_after_first_read:
        with pytest.raises(asyncio.CancelledError):
            await pipeline_v4._run_llm_stages(*arguments, ledger, SimpleNamespace(), config, "en", {})
        assert FIRST in str(snapshots[-1]["shared_workspace"])
        ledger = HypothesisLedger.from_dict(json.loads(json.dumps(snapshots[-1])))
        assert ledger.protocol_version == 12 and ledger.unresolved_units
    publication = await pipeline_v4._run_llm_stages(
        *arguments,
        ledger,
        SimpleNamespace(),
        config,
        "en",
        {},
        resume=restore_after_first_read,
    )
    assert blind_seen and events.index("submit-open") < events.index("investigation") < events.index("last-discovery")
    assert len(ledger.items) == 1 and len(ledger.no_issue_units) == 1 and not ledger.unresolved_units, (
        ledger.unresolved_units,
        [(record["units"], record["workflow"]["feedback"]) for record in ledger.discovery_blocks.values()],
    )
    case = next(iter(ledger.items.values()))
    assert case.status == HypothesisStatus.CONFIRMED and case.attempts == 1
    assert ledger.publication_reviews[case.id]["decision"] == "unknown" and not publication.comments
    assert all(FIRST not in str(record) and SECOND not in str(record) for record in ledger.evidence_records.values())
    assert sum(args.get("path") == "consumer_first.py" for name, args in source_calls if name == "read_file") == 1
    count = len(model.calls)
    frozen = HypothesisLedger.from_dict(ledger.to_dict())
    await pipeline_v4._run_llm_stages(*arguments, frozen, SimpleNamespace(), config, "en", {}, resume=True)
    assert len(model.calls) == count and frozen.protocol_version == 12


def test_new_default_does_not_upgrade_saved_protocols():
    for protocol in range(1, 13):
        original = HypothesisLedger("run", "head", "digest", protocol_version=protocol)
        restored = HypothesisLedger.from_dict(original.to_dict())
        assert restored.protocol_version == protocol
    assert PipelineV4Config().protocol_version == 13
