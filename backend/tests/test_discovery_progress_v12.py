"""Mandatory visible progress, native source references and restart memory."""

from __future__ import annotations

import json

import pytest
from test_discovery_workflow_v6 import batch, no_issue, run
from test_review_session_v5 import SessionModel, call, packet, receipt

from reviewforge.engine.hypothesis import HypothesisLedger


def book(protocol=12):
    return HypothesisLedger("run", "head", "digest", protocol_version=protocol)


def progress(**changes):
    return {
        "summary": "The delivered return changes the stored-value result; consumer behavior is not yet known.",
        "open_questions": ["Does the actual consumer require the original stored value?"],
        "resolved_questions": ["The changed return is present in fixed-head source."],
        "next_action": "Read the consumer before selecting the remaining review action.",
        "evidence_ids": [],
    } | changes


def tool_parameters(options, name):
    return next(tool["function"]["parameters"] for tool in options["tools"] if tool["function"]["name"] == name)


@pytest.mark.asyncio
async def test_two_native_turns_preserve_visible_progress_without_checkpoint_or_provider_reasoning():
    ledger = book()
    saved = []
    executed = []

    async def updated(current):
        saved.append(current.to_dict())

    async def execute(name, args):
        assert "review_progress" not in args
        assert saved[-1]["shared_workspace"]["notes"]
        executed.append((name, args))
        return "assert fetch(existing_id) == stored_value"

    def answer(messages, options, turn):
        data = packet(messages)
        assert [message.type for message in messages] == ["system", "human"] + (["human"] if turn > 1 else [])
        if turn == 1:
            for tool in options["tools"]:
                function = tool["function"]
                if function["name"] not in {"submit_assessments", "checkpoint_review_progress"}:
                    assert "review_progress" in function["parameters"]["required"]
            assert "review_progress" not in tool_parameters(options, "submit_assessments")["properties"]
            response = receipt(
                call("read_file", {"path": "consumer.py", "start": 1, "end": 1, "review_progress": progress()})
            )
            response.additional_kwargs["reasoning_content"] = "opaque-provider-reasoning-must-not-be-saved"
            return response
        notes = data["shared_review_workspace"]["notes"]
        assert len(notes) == 1 and notes[0]["summary"] == progress()["summary"]
        assert notes[0]["open_questions"] == progress()["open_questions"]
        assert notes[0]["trust"] == "untrusted-navigation-only"
        feedback = json.loads(messages[-1].content)["controller_receipt"]
        read = next(entry for entry in feedback if entry.get("action") == "read_file")
        assert read["review_progress_note_id"] == notes[0]["id"]
        result = read["result"]
        assert "fN.B/R/C" in result["reference_instruction"]
        assert result["observations"][0]["id"] == "obs_0"
        assert result["observations"][0]["source_delivery"] == "next-packet-rows"
        assert "excerpt" not in result["observations"][0]
        assert any(text == "assert fetch(existing_id) == stored_value" for _, text in data["rows"])
        assert "copy the exact reference IDs into assessment" not in json.dumps(result)
        assert "do not enumerate every listed ref" in data["discovery_reference_contract"]["required_source_sides"]
        assert "ALL pending" not in messages[0].content
        assert "does not need to finish the independent proof" in messages[0].content
        return batch(*map(no_issue, data["pending_units"]))

    model = SessionModel(handler=answer)
    await run(model, count=1, ledger=ledger, executor=execute, on_update=updated)
    assert len(model.calls) == 2 and len(executed) == 1 and len(ledger.no_issue_units) == 1
    assert not ledger.unresolved_units
    assert "opaque-provider-reasoning-must-not-be-saved" not in json.dumps(ledger.to_dict())
    assert not any(
        event.get("action") == "checkpoint_review_progress"
        for block in ledger.discovery_blocks.values()
        for event in block["session"]["events"]
    )


@pytest.mark.asyncio
async def test_saved_mandatory_progress_and_source_survive_provider_interruption_and_restart():
    ledger = book()

    def interrupted(messages, _options, turn):
        if turn == 1:
            return receipt(call("read_file", {"path": "consumer.py", "review_progress": progress()}))
        assert packet(messages)["shared_review_workspace"]["notes"][0]["summary"] == progress()["summary"]
        raise RuntimeError("offline interrupted provider request")

    initial = SessionModel(handler=interrupted)
    await run(initial, count=1, ledger=ledger)
    assert len(initial.calls) == 2 and ledger.unresolved_units
    restored = HypothesisLedger.from_dict(ledger.to_dict())

    def resumed(messages, *_):
        data = packet(messages)
        assert data["shared_review_workspace"]["notes"][0]["summary"] == progress()["summary"]
        assert data["shared_review_workspace"]["notes"][0]["next_action"] == progress()["next_action"]
        assert data["shared_review_workspace"]["local_observations"]
        assert data["session"]["turn"] == 3
        return batch(*map(no_issue, data["pending_units"]))

    continuation = SessionModel(handler=resumed)
    await run(continuation, count=1, ledger=restored)
    assert len(continuation.calls) == 1 and len(restored.no_issue_units) == 1 and not restored.unresolved_units


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad", [None, {"summary": "missing other fields"}, progress(open_questions=[7]), progress(actor="forged")]
)
async def test_missing_or_malformed_progress_cannot_execute_or_produce_clean_coverage(bad):
    ledger = book()
    executed = []

    async def execute(name, args):
        executed.append((name, args))
        return "unreachable source"

    def answer(messages, _options, turn):
        if turn > 1:
            assert "invalid-review-progress-schema" in messages[-1].content
        args = {"path": "consumer.py"}
        if bad is not None:
            args["review_progress"] = bad
        return receipt(call("read_file", args))

    model = SessionModel(handler=answer)
    await run(model, count=1, ledger=ledger, executor=execute)
    assert len(model.calls) == 2 and not executed
    assert not ledger.no_issue_units and not ledger.items and ledger.unresolved_units
    assert not ledger.shared_workspace["notes"]


@pytest.mark.asyncio
async def test_invalid_progress_evidence_id_is_rejected_before_source_and_can_be_corrected():
    ledger = book()
    executed = []

    async def execute(name, args):
        executed.append((name, args))
        return "assert fetch(existing_id) == stored_value"

    def answer(messages, _options, turn):
        if turn == 1:
            return receipt(
                call("read_file", {"path": "consumer.py", "review_progress": progress(evidence_ids=["ev_unknown"])})
            )
        if turn == 2:
            assert "invalid-review-progress-binding" in messages[-1].content
            assert not executed and not ledger.no_issue_units and not ledger.shared_workspace["notes"]
            return receipt(call("read_file", {"path": "consumer.py", "review_progress": progress()}))
        return batch(*map(no_issue, packet(messages)["pending_units"]))

    model = SessionModel(handler=answer)
    await run(model, count=1, ledger=ledger, executor=execute)
    assert len(model.calls) == 3 and len(executed) == 1 and not ledger.unresolved_units


@pytest.mark.asyncio
async def test_progress_wrapper_does_not_coerce_invalid_source_coordinates_past_broker_guard():
    executed = []

    async def execute(name, args):
        executed.append((name, args))
        return "unreachable"

    def answer(messages, _options, turn):
        if turn == 1:
            return receipt(
                call("read_file", {"path": "consumer.py", "start": True, "end": 2, "review_progress": progress()})
            )
        assert "invalid-source-tool-arguments" in messages[-1].content
        return batch(*map(no_issue, packet(messages)["pending_units"]))

    ledger = book()
    await run(SessionModel(handler=answer), count=1, ledger=ledger, executor=execute)
    assert not executed and len(ledger.no_issue_units) == 1


@pytest.mark.asyncio
async def test_workspace_queries_require_progress_and_preserve_directory_default():
    def answer(messages, options, turn):
        if turn == 1:
            query = tool_parameters(options, "query_review_workspace")
            assert query["properties"]["catalog"]["default"] == "directory"
            return receipt(
                call(
                    "query_review_workspace", {"paths": ["consumer.py"], "unit_ids": [], "review_progress": progress()}
                )
            )
        result = json.loads(messages[-1].content)["controller_receipt"][0]["result"]
        assert "evidence_directory" in result and "evidence_catalog" not in result
        assert packet(messages)["shared_review_workspace"]["notes"]
        return batch(*map(no_issue, packet(messages)["pending_units"]))

    ledger = book()
    await run(SessionModel(handler=answer), count=1, ledger=ledger)
    assert len(ledger.no_issue_units) == 1 and not ledger.unresolved_units


@pytest.mark.asyncio
async def test_eleven_keeps_its_original_optional_progress_and_native_read_receipt():
    def answer(messages, options, turn):
        data = packet(messages)
        assert "discovery_reference_contract" not in data
        assert "review_progress" not in tool_parameters(options, "read_file")["properties"]
        if turn == 1:
            return receipt(call("read_file", {"path": "consumer.py"}))
        assert not data["shared_review_workspace"]["notes"]
        result = json.loads(messages[-1].content)["controller_receipt"][0]["result"]
        assert isinstance(result, str) and "copy the exact reference IDs into assessment" in result
        return batch(*map(no_issue, data["pending_units"]))

    ledger = book(11)
    await run(SessionModel(handler=answer), count=1, ledger=ledger)
    assert len(ledger.no_issue_units) == 1 and not ledger.unresolved_units


@pytest.mark.asyncio
async def test_long_read_file_source_is_complete_and_delivered_once_in_next_rows():
    source = [f"native_source_line_{index:05d}=stored_value_{index:05d}" for index in range(240)]
    text = "\n".join(source)
    ledger = book()

    async def execute(_name, _args):
        return text

    def answer(messages, _options, turn):
        if turn == 1:
            return receipt(
                call(
                    "read_file",
                    {
                        "path": "long_consumer.py",
                        "start": 1,
                        "end": len(source),
                        "review_progress": progress(),
                    },
                )
            )
        data = packet(messages)
        body = "\n".join(message.content for message in messages)
        for line in source:
            assert sum(saved == line for _, saved in data["rows"]) == 1
            assert body.count(line) == 1
        feedback = json.loads(messages[-1].content)["controller_receipt"]
        native = next(item["result"] for item in feedback if item["action"] == "read_file")
        assert len(native["observations"]) > 1
        assert all("excerpt" not in observation for observation in native["observations"])
        stored = next(iter(ledger.discovery_blocks.values()))["observations"]
        assert "".join(observation["excerpt"] for observation in stored) == text
        return batch(*map(no_issue, data["pending_units"]))

    model = SessionModel(handler=answer)
    await run(model, count=1, ledger=ledger, executor=execute)
    assert len(model.calls) == 2 and not ledger.unresolved_units


@pytest.mark.asyncio
async def test_diff_not_projected_to_rows_retains_complete_raw_observations_in_receipt():
    text = "@@ -1 +1 @@\n-return old\n+return replacement\n"

    async def execute(_name, _args):
        return text

    def answer(messages, _options, turn):
        if turn == 1:
            return receipt(call("read_diff", {"path": "consumer.py", "review_progress": progress()}))
        feedback = json.loads(messages[-1].content)["controller_receipt"]
        native = next(item["result"] for item in feedback if item["action"] == "read_diff")
        assert "".join(item["excerpt"] for item in native["observations"]) == text
        return batch(*map(no_issue, packet(messages)["pending_units"]))

    ledger = book()
    await run(SessionModel(handler=answer), count=1, ledger=ledger, executor=execute)
    assert not ledger.unresolved_units
