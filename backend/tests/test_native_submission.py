"""Use the real SDK conversion: a typed dict cannot reveal erased duplicate keys."""

import copy
import json
from types import SimpleNamespace

import httpx
import pytest
from test_investigator_typed_closure import Model, close, wire_submission, worker
from test_openai_compat import _model, _tool_response

from reviewforge.engine.native_submission import inspect_tool_calls, strict_tool_calls
from reviewforge.engine.openai_compat import ReasoningContentChatOpenAI


def response(arguments):
    raw = _tool_response(reasoning_content="", call_id="submit", argument=1)
    call = raw["choices"][0]["message"]["tool_calls"][0]
    call["function"] = {"name": "submit_investigation", "arguments": arguments}
    return raw


def converted(arguments):
    return _model()._create_chat_result(response(arguments)).generations[0].message


def test_real_adapter_preserves_raw_arguments_with_deep_copy_and_no_request_replay():
    raw = response(json.dumps(wire_submission()))
    model = _model()
    message = model._create_chat_result(raw).generations[0].message
    assert strict_tool_calls(message) == message.tool_calls
    before = copy.deepcopy(message.additional_kwargs["reviewforge_raw_tool_calls"])
    raw["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = "changed"
    assert message.additional_kwargs["reviewforge_raw_tool_calls"] == before
    payload = model._get_request_payload([message])
    assert "reviewforge_raw_tool_calls" not in json.dumps(payload)
    assert json.loads(payload["messages"][0]["tool_calls"][0]["function"]["arguments"]) == wire_submission()


@pytest.mark.parametrize(
    "arguments",
    [
        '{"unit_id":"foreign","unit_id":"target"}',
        '{"outer":{"source_ref":"foreign","source_ref":"allowed"}}',
        '{"value":NaN}',
        '{"value":Infinity}',
    ],
)
def test_sdk_erased_duplicates_or_non_json_numbers_cannot_licence_native_output(arguments):
    message = converted(arguments)
    assert message.tool_calls  # The SDK's permissive parse accepted this object.
    with pytest.raises(ValueError):
        strict_tool_calls(message)
    valid, errors = inspect_tool_calls(message)
    assert not valid and errors


@pytest.mark.asyncio
async def test_actual_closure_rejects_duplicate_comparison_even_after_sdk_typed_parse():
    arguments = json.dumps(wire_submission()).replace(
        '"assessment_comparison": "conflict"',
        '"assessment_comparison":"compatible","assessment_comparison":"conflict"',
    )
    message = converted(arguments)
    assert message.tool_calls[0]["args"]["assessment_comparison"] == "conflict"
    result = await close(worker(Model(reply=message)))
    assert result.verdict == "unknown" and result.outcome == "output-error"


def test_partial_native_stream_keeps_only_raw_verified_complete_calls():
    raw = response(json.dumps(wire_submission()))
    calls = raw["choices"][0]["message"]["tool_calls"]
    broken = copy.deepcopy(calls[0])
    broken["id"] = "broken"
    broken["function"]["arguments"] = '{"assessment_comparison":'
    calls.append(broken)
    message = _model()._create_chat_result(raw).generations[0].message
    valid, errors = inspect_tool_calls(message)
    assert len(valid) == 1 and valid[0]["id"] == "submit"
    assert errors and message.invalid_tool_calls
    with pytest.raises(ValueError):
        strict_tool_calls(message)


def test_duplicate_parsed_identity_cannot_hide_a_different_unconsumed_raw_call():
    message = converted(json.dumps(wire_submission()))
    first = copy.deepcopy(message.tool_calls[0])
    message.tool_calls = [first, copy.deepcopy(first)]
    second_raw = copy.deepcopy(message.additional_kwargs["reviewforge_raw_tool_calls"][0])
    second_raw["id"] = "unconsumed"
    message.additional_kwargs["reviewforge_raw_tool_calls"].append(second_raw)
    valid, errors = inspect_tool_calls(message)
    assert valid == [first] and "unmatched-raw-tool-calls" in errors
    with pytest.raises(ValueError):
        strict_tool_calls(message)


@pytest.mark.parametrize("change", ["name", "id", "args", "raw-type", "ambiguous-id"])
def test_raw_and_parsed_argument_mismatch_is_not_replaced_or_repaired(change):
    message = converted(json.dumps(wire_submission()))
    original = copy.deepcopy(message.tool_calls)
    raw = message.additional_kwargs["reviewforge_raw_tool_calls"]
    if change in {"name", "args"}:
        raw[0]["function"]["name" if change == "name" else "arguments"] = (
            "different" if change == "name" else '{"untrusted":"different"}'
        )
    elif change == "id":
        raw[0]["id"] = "other"
    elif change == "raw-type":
        message.additional_kwargs["reviewforge_raw_tool_calls"] = {"invalid": True}
    else:
        raw.append(copy.deepcopy(raw[0]))
    with pytest.raises(ValueError):
        strict_tool_calls(message)
    assert message.tool_calls == original


def test_already_structured_provider_and_plain_text_keep_their_existing_interfaces():
    structured = SimpleNamespace(tool_calls=[{"name": "submit", "args": {"x": 1}, "id": "one"}])
    assert strict_tool_calls(structured) == structured.tool_calls
    assert strict_tool_calls(SimpleNamespace(content="text", tool_calls=[])) == []
    raw = {"model": "offline", "choices": [{"message": {"role": "assistant", "content": "plain", "tool_calls": None}}]}
    message = _model()._create_chat_result(raw).generations[0].message
    assert "reviewforge_raw_tool_calls" not in message.additional_kwargs
    assert strict_tool_calls(message) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("duplicate", [False, True])
async def test_http_sdk_to_actual_closure_preserves_uniqueness_without_network(duplicate):
    requests = []
    arguments = json.dumps(wire_submission())
    if duplicate:
        arguments = arguments.replace(
            '"assessment_comparison": "conflict"',
            '"assessment_comparison":"compatible","assessment_comparison":"conflict"',
        )
    reply = response(arguments) | {
        "object": "chat.completion",
        "created": 1,
        "usage": {"prompt_tokens": 120, "completion_tokens": 80, "total_tokens": 200},
    }

    def handle(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=reply)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        model = ReasoningContentChatOpenAI(
            api_key="offline-test-only",
            base_url="https://example.invalid/v1",
            model="offline-fixture",
            max_retries=0,
            http_async_client=client,
        )
        result = await close(worker(model))
    assert len(requests) == 1 and requests[0]["tool_choice"] == "required"
    assert requests[0]["tools"][0]["function"]["name"] == "submit_investigation"
    assert result.verdict == ("unknown" if duplicate else "confirmed")
    assert result.outcome == ("output-error" if duplicate else "resolved")
