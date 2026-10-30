"""Local progress journals guide independent handoff without becoming proof."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from test_discovery_progress_v12 import progress, tool_parameters
from test_discovery_submission import fixture
from test_discovery_workflow_v6 import batch, lead, no_issue, run
from test_review_session_v5 import SessionModel, call, packet, receipt

from reviewforge.engine.hypothesis import HypothesisLedger
from reviewforge.engine.review_context import ReviewContext


def book(protocol=13):
    return HypothesisLedger("run", "head", "digest", protocol_version=protocol)


def fragments():
    return (
        progress(
            summary="Consumer return boundary inspected.",
            resolved_questions=["The consumer accepts the stored-value return boundary."],
            next_action="Inspect the unrelated optional serializer boundary.",
        ),
        progress(
            summary="Serializer boundary inspected as a separate local topic.",
            resolved_questions=["The serializer preserves the input value."],
            next_action="Submit the independently ready return unit.",
        ),
    )


def two_reads():
    first, second = fragments()
    return receipt(
        call("read_file", {"path": "consumer.py", "review_progress": first}),
        call("read_file", {"path": "serializer.py", "review_progress": second}),
    )


@pytest.mark.asyncio
async def test_local_second_tool_fragment_does_not_hide_first_topic_before_incremental_submission():
    first, second = fragments()

    def answer(messages, options, turn):
        data = packet(messages)
        if turn == 1:
            schema = json.dumps(tool_parameters(options, "read_file"))
            assert "progress_journal" in schema and "ev_..." in schema and "never obs_N" in schema
            return two_reads()
        view = data["shared_review_workspace"]
        assert view["notes"][0]["resolved_questions"] == second["resolved_questions"]
        journal = view["progress_journal"]
        assert len(journal) == 1 and journal[0]["trust"] == "untrusted-navigation-history"
        events = journal[0]["events"]
        assert [event["sequence"] for event in events] == [1, 2]
        assert events[0]["resolved_questions"] == first["resolved_questions"]
        assert events[1]["resolved_questions"] == second["resolved_questions"]
        assert all(event["trust"] == "untrusted-navigation-only" for event in events)
        assert "CURRENT LOCAL FRAGMENT" in messages[0].content
        assert "not an automatically\nmerged truth set" in messages[0].content
        assert "initial_context_gaps_navigation_only" in data and "unchecked_navigation_only" not in data
        assert data["initial_context_gaps_navigation_only"]["live_coverage"] is False
        assert "not a current unfinished checklist" in messages[0].content
        assert len(data["pending_units"]) == (2 if turn == 2 else 1)
        return batch(no_issue(data["pending_units"][0]))

    model = SessionModel(handler=answer)
    ledger = book()
    await run(model, count=2, ledger=ledger)
    assert len(model.calls) == 3 and len(ledger.no_issue_units) == 2 and not ledger.unresolved_units
    assert not ledger.items


@pytest.mark.asyncio
async def test_ordered_topics_and_specific_correction_survive_restart_before_open_handoff():
    first, second = fragments()
    ledger = book()

    def interrupted(messages, _options, turn):
        if turn == 1:
            response = two_reads()
            response.additional_kwargs["reasoning_content"] = "opaque-reasoning-not-part-of-the-journal"
            return response
        assert len(packet(messages)["shared_review_workspace"]["progress_journal"][0]["events"]) == 2
        raise RuntimeError("offline provider interrupted")

    await run(SessionModel(handler=interrupted), count=1, ledger=ledger)
    restored = HypothesisLedger.from_dict(ledger.to_dict())
    correction = progress(
        summary="Correction of event 1 consumer proposition only: compatibility remains unverified.",
        open_questions=["Does the consumer require the previous return value?"],
        resolved_questions=[],
        next_action="Hand off the source-supported inconsistency with its missing consumer contract.",
    )

    def continued(messages, _options, turn):
        view = packet(messages)["shared_review_workspace"]
        events = view["progress_journal"][0]["events"]
        if turn == 1:
            assert [event["sequence"] for event in events] == [1, 2]
            assert events[0]["resolved_questions"] == first["resolved_questions"]
            return receipt(call("read_file", {"path": "contract.py", "review_progress": correction}))
        assert [event["sequence"] for event in events] == [1, 2, 3]
        assert events[0]["resolved_questions"] == first["resolved_questions"]
        assert events[1]["resolved_questions"] == second["resolved_questions"]
        assert events[2]["summary"] == correction["summary"]
        assert view["notes"][0]["resolved_questions"] == []
        assert "there is no global ordering between owners" in messages[0].content
        return batch(lead(packet(messages)["pending_units"][0]))

    model = SessionModel(handler=continued)
    await run(model, count=1, ledger=restored)
    assert len(model.calls) == 2 and len(restored.items) == 1 and not restored.unresolved_units
    assert not restored.no_issue_units
    candidate = next(iter(restored.items.values()))
    assert candidate.status.value == "open" and candidate.open_question
    assert "progress_journal" not in candidate.discovery_basis
    assert all(observation.tool == "read_file" for observation in candidate.observations)
    assert "opaque-reasoning-not-part-of-the-journal" not in json.dumps(restored.to_dict())


@pytest.mark.asyncio
async def test_prompt_matches_explicitly_supplied_isolated_test_capability_without_running_it():
    state, _, _, _ = fixture(1)
    state.head_sha = "head"

    async def execute(*_args):
        pytest.fail("the model did not request any execution")

    # The production broker obtains this same capability from the pinned
    # executor's ReviewContext, including during discovery.
    execute.review_context = ReviewContext(
        SimpleNamespace(head_sha="head", digest="digest"), state, protocol_version=13
    )

    def answer(messages, options, _turn):
        schema = tool_parameters(options, "run_repository_test")
        assert "review_progress" in schema["required"]
        assert "When run_repository_test\nis supplied" in messages[0].content
        assert "Never execute source on the review host" in messages[0].content
        assert "Discovery has no execution tool" not in messages[0].content
        assert "Never execute tests or claim a runtime result" not in messages[0].content
        return batch(no_issue(packet(messages)["pending_units"][0]))

    ledger = book()
    await run(SessionModel(handler=answer), count=1, ledger=ledger, executor=execute)
    assert len(ledger.no_issue_units) == 1 and not ledger.unresolved_units


@pytest.mark.asyncio
async def test_twelve_keeps_its_current_only_view_and_frozen_prompt_schema_descriptions():
    second = fragments()[1]

    def answer(messages, options, turn):
        data = packet(messages)
        assert "progress_journal" not in data["shared_review_workspace"]
        assert "CURRENT LOCAL FRAGMENT" not in messages[0].content
        assert "Discovery has no execution tool" in messages[0].content
        assert "initial_context_gaps_navigation_only" not in data and "unchecked_navigation_only" in data
        assert "progress_journal" not in json.dumps(tool_parameters(options, "read_file"))
        if turn == 1:
            return two_reads()
        assert data["shared_review_workspace"]["notes"][0]["resolved_questions"] == second["resolved_questions"]
        return batch(no_issue(data["pending_units"][0]))

    ledger = book(12)
    await run(SessionModel(handler=answer), count=1, ledger=ledger)
    assert len(ledger.no_issue_units) == 1 and not ledger.unresolved_units


@pytest.mark.asyncio
async def test_audit_observation_id_is_not_an_immutable_progress_evidence_id():
    executed = []

    async def execute(name, args):
        executed.append((name, args))
        return "unreachable"

    def answer(messages, _options, turn):
        if turn == 1:
            return receipt(
                call("read_file", {"path": "consumer.py", "review_progress": progress(evidence_ids=["obs_0"])})
            )
        assert "invalid-review-progress-binding" in messages[-1].content
        assert not executed and not packet(messages)["shared_review_workspace"]["progress_journal"]
        return batch(no_issue(packet(messages)["pending_units"][0]))

    ledger = book()
    await run(SessionModel(handler=answer), count=1, ledger=ledger, executor=execute)
    assert not executed and len(ledger.no_issue_units) == 1
