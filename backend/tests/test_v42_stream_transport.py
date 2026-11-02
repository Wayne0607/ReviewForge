"""Offline tests for the v4.2 native SSE transport; no provider calls."""

from __future__ import annotations

import json

import httpx
import openai
import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from reviewforge.core.config import LLMConfig
from reviewforge.engine.model_router import ModelRouter
from reviewforge.engine.native_submission import inspect_tool_calls
from reviewforge.engine.openai_compat import ReasoningContentChatOpenAI, V42StreamingChatOpenAI
from reviewforge.engine.provider_retry import RetryChatLLM, RetryPolicy


def frame(*, delta=None, finish=None, usage=None, index=0):
    payload = {"id": "id-1", "model": "model-1", "choices": []}
    if delta is not None or finish is not None:
        payload["choices"] = [{"index": index, "delta": delta or {}, "finish_reason": finish}]
    if usage is not None:
        payload["usage"] = usage
    return ("data: " + json.dumps(payload, ensure_ascii=False) + "\n\n").encode()


def tool_start(arguments='{"x":'):
    return frame(
        delta={
            "role": "assistant",
            "tool_calls": [
                {
                    "index": 0,
                    "id": "call-a",
                    "type": "function",
                    "function": {"name": "inspect_source", "arguments": arguments},
                }
            ],
        }
    )


def tool_end(arguments="1}"):
    return frame(delta={"tool_calls": [{"index": 0, "function": {"arguments": arguments}}]}, finish="tool_calls")


def final_usage():
    return frame(
        usage={
            "prompt_tokens": 17,
            "completion_tokens": 4,
            "total_tokens": 21,
            "prompt_tokens_details": {"cached_tokens": 3},
        }
    )


class ByteStream(httpx.AsyncByteStream):
    def __init__(self, parts, error=None):
        self.parts = parts
        self.error = error
        self.closed = False

    async def __aiter__(self):
        for part in self.parts:
            yield part
        if self.error:
            raise self.error

    async def aclose(self):
        self.closed = True


def client_with(responses, requests):
    async def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=responses.pop(0))

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def model_for(client):
    return V42StreamingChatOpenAI(
        api_key="sk-test",
        base_url="https://test.invalid/v1",
        model="demo",
        streaming=False,
        max_retries=0,
        timeout=120,
        http_async_client=client,
    )


@pytest.mark.asyncio
async def test_stream_preserves_raw_arguments_usage_finish_and_sends_no_reasoning():
    requests = []
    stream = ByteStream([tool_start(), tool_end(), final_usage(), b"data: [DONE]\n\n"])
    async with client_with([stream], requests) as client:
        model = model_for(client)
        previous = AIMessage(
            content="",
            tool_calls=[{"name": "old", "args": {}, "id": "before"}],
            additional_kwargs={"reasoning_content": "opaque private thinking"},
        )
        result = await model._agenerate([previous, ToolMessage(content="ok", tool_call_id="before")])
    message = result.generations[0].message
    assert message.tool_calls[0]["args"] == {"x": 1}
    assert message.additional_kwargs["reviewforge_raw_tool_calls"][0]["function"]["arguments"] == '{"x":1}'
    assert result.generations[0].generation_info["finish_reason"] == "tool_calls"
    assert result.llm_output["token_usage"]["prompt_tokens_details"]["cached_tokens"] == 3
    assert message.usage_metadata["input_tokens"] == 17
    assert requests[0]["stream"] is True and requests[0]["stream_options"] == {"include_usage": True}
    assert all("reasoning_content" not in item for item in requests[0]["messages"])
    assert stream.closed


@pytest.mark.asyncio
async def test_duplicate_argument_keys_are_not_lost_during_stream_assembly():
    requests = []
    stream = ByteStream([tool_start('{"x":1,'), tool_end('"x":2}'), final_usage(), b"data: [DONE]\n\n"])
    async with client_with([stream], requests) as client:
        result = await model_for(client)._agenerate([HumanMessage(content="review")])
    message = result.generations[0].message
    assert message.additional_kwargs["reviewforge_raw_tool_calls"][0]["function"]["arguments"] == '{"x":1,"x":2}'
    valid, errors = inspect_tool_calls(message)
    assert not valid and errors


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "parts",
    [
        [tool_start()],
        [tool_start(), tool_end(), final_usage()],
        [tool_start(), b"data: [DONE]\n\n"],
    ],
)
async def test_truncated_sse_never_returns_partial_tool_calls(parts):
    requests = []
    stream = ByteStream(parts)
    async with client_with([stream], requests) as client:
        with pytest.raises(openai.APIConnectionError):
            await model_for(client)._agenerate([HumanMessage(content="review")])
    assert stream.closed


@pytest.mark.asyncio
async def test_retry_records_interrupted_attempt_as_unknown_and_does_not_merge_it():
    requests, receipts = [], []
    first = ByteStream([tool_start('{"bad":')], httpx.RemoteProtocolError("cut"))
    second = ByteStream([tool_start(), tool_end(), final_usage(), b"data: [DONE]\n\n"])

    async def record(event):
        receipts.append(event)

    async with client_with([first, second], requests) as client:
        bound = RetryChatLLM(
            model_for(client), policy=RetryPolicy(max_attempts=2, base_delay_seconds=0), attempt_sink=record
        )
        answer = await bound.ainvoke([HumanMessage(content="review")])
    assert len(requests) == 2
    assert answer.tool_calls[0]["args"] == {"x": 1}
    finished = [event for event in receipts if event["phase"] == "finished"]
    assert finished[0]["usage_status"] == "unknown" and finished[0]["will_retry"]
    assert finished[1]["usage_status"] == "reported"
    assert first.closed and second.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("usage", [None, {"prompt_tokens": 8}, {"prompt_tokens": "8", "completion_tokens": 1}])
async def test_absent_or_incomplete_usage_is_not_normalized_to_zero(usage):
    frames = [frame(delta={"content": "hello"}, finish="stop")]
    if usage is not None:
        frames.append(frame(usage=usage))
    frames.append(b"data: [DONE]\n\n")
    async with client_with([ByteStream(frames)], []) as client:
        result = await model_for(client)._agenerate([HumanMessage(content="review")])
    assert result.llm_output["token_usage"] is None
    assert result.generations[0].message.usage_metadata is None
    if usage is not None:
        assert result.llm_output["provider_partial_usage"] == usage


@pytest.mark.asyncio
async def test_noncompleted_finish_cannot_submit_a_tool_even_with_usage():
    parts = [
        tool_start(),
        frame(delta={"tool_calls": [{"index": 0, "function": {"arguments": "1}"}}]}, finish="length"),
        final_usage(),
        b"data: [DONE]\n\n",
    ]
    async with client_with([ByteStream(parts)], []) as client:
        result = await model_for(client)._agenerate([HumanMessage(content="review")])
    message = result.generations[0].message
    assert not message.tool_calls and message.invalid_tool_calls
    assert "reviewforge_raw_tool_calls" not in message.additional_kwargs
    assert "tool_calls" not in message.additional_kwargs
    assert "reviewforge_incomplete_tool_calls" in message.additional_kwargs
    valid, errors = inspect_tool_calls(message)
    assert not valid and errors
    from langchain_core.messages import messages_from_dict, messages_to_dict

    restored = messages_from_dict(messages_to_dict([message]))[0]
    valid, errors = inspect_tool_calls(restored)
    assert not valid and errors
    assert result.llm_output["token_usage"]["completion_tokens"] == 4
    assert result.generations[0].generation_info["finish_reason"] == "length"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "part",
    [
        {"index": 0, "id": "other", "function": {"arguments": "1}"}},
        {"index": 1, "id": "call-a", "function": {"name": "other", "arguments": "{}"}},
    ],
)
async def test_tool_identity_conflicts_fail_closed(part):
    parts = [tool_start(), frame(delta={"tool_calls": [part]}, finish="tool_calls"), final_usage(), b"data: [DONE]\n\n"]
    async with client_with([ByteStream(parts)], []) as client:
        with pytest.raises(ValueError, match="tool-call"):
            await model_for(client)._agenerate([HumanMessage(content="review")])


def test_opt_in_is_v42_only_and_cache_isolated():
    config = LLMConfig(base_url="https://test.invalid/v1", api_key="test", model="model")
    router = ModelRouter(config)
    legacy = router.get_llm("investigator", max_retries=0)
    streaming = router.get_llm("v42_reviewer", max_retries=0, stream_transport=True)
    assert isinstance(legacy, ReasoningContentChatOpenAI) and not isinstance(legacy, V42StreamingChatOpenAI)
    assert isinstance(streaming, V42StreamingChatOpenAI) and streaming is not legacy
    assert router.get_llm("v42_reviewer", max_retries=0, stream_transport=True) is streaming
    with pytest.raises(ValueError, match="restricted"):
        router.get_llm("investigator", max_retries=0, stream_transport=True)


@pytest.mark.asyncio
async def test_sse_account_error_preserves_402_for_runtime_circuit_breaker():
    error = b'data: {"error":{"message":"account unavailable","code":"402"}}\n\n'
    async with client_with([ByteStream([tool_start(), error])], []) as client:
        with pytest.raises(openai.APIStatusError) as caught:
            await model_for(client)._agenerate([HumanMessage(content="review")])
    assert caught.value.status_code == 402


@pytest.mark.asyncio
async def test_cancel_during_partial_stream_closes_response_and_propagates():
    import asyncio

    entered = asyncio.Event()

    class WaitingStream(ByteStream):
        async def __aiter__(self):
            yield tool_start()
            entered.set()
            await asyncio.Event().wait()

    stream = WaitingStream([])
    async with client_with([stream], []) as client:
        task = asyncio.create_task(model_for(client)._agenerate([HumanMessage(content="review")]))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert stream.closed


def test_streaming_roles_are_separately_cached_even_if_global_profile_is_shared():
    config = LLMConfig(
        base_url="https://test.invalid/v1",
        api_key="test",
        model="model",
        temperature_reviewer=0.2,
        temperature_verifier=0.0,
    )
    router = ModelRouter(config)
    reviewer = router.get_llm("v42_reviewer", max_retries=0, stream_transport=True)
    verifier = router.get_llm("v42_verifier", max_retries=0, stream_transport=True)
    assert reviewer is not verifier
    assert reviewer.temperature == 0.2 and verifier.temperature == 0.0
    assert reviewer.v42_stream_transport is True


def test_anthropic_routing_refuses_openai_stream_transport():
    config = LLMConfig(base_url="https://api.minimaxi.com/v1", api_key="test", model="MiniMax-M3")
    router = ModelRouter(config)
    with pytest.raises(ValueError, match="Anthropic"):
        router.get_llm("v42_reviewer", max_retries=0, stream_transport=True)


@pytest.mark.asyncio
async def test_retry_wrapper_native_bind_tools_preserves_stream_and_raw_arguments():
    requests = []
    schema = {
        "type": "function",
        "function": {
            "name": "inspect_source",
            "description": "Inspect source",
            "parameters": {"type": "object", "properties": {"x": {"type": "integer"}}, "required": ["x"]},
        },
    }
    stream = ByteStream([tool_start(), tool_end(), final_usage(), b"data: [DONE]\n\n"])
    async with client_with([stream], requests) as client:
        bound = RetryChatLLM(model_for(client), policy=RetryPolicy(max_attempts=1)).bind_tools(
            [schema], tool_choice="auto"
        )
        response = await bound.ainvoke([HumanMessage(content="review")])
    assert requests[0]["tools"][0]["function"]["name"] == "inspect_source"
    assert requests[0]["tool_choice"] == "auto"
    assert response.additional_kwargs["reviewforge_raw_tool_calls"][0]["function"]["arguments"] == '{"x":1}'
    assert response.usage_metadata["output_tokens"] == 4


@pytest.mark.asyncio
async def test_named_sse_error_is_account_block_and_ping_is_ignored():
    error = b'event: error\ndata: {"status":402,"message":"account unavailable"}\n\n'
    async with client_with([ByteStream([b"event: ping\ndata: {}\n\n", error])], []) as client:
        with pytest.raises(openai.APIStatusError) as caught:
            await model_for(client)._agenerate([HumanMessage(content="review")])
    assert caught.value.status_code == 402


@pytest.mark.asyncio
async def test_duplicate_sse_envelope_fields_cannot_be_silently_overwritten():
    event = (
        b'data: {"id":"id-1","model":"model-1","choices":[{"index":0,"delta":'
        b'{"tool_calls":[{"index":0,"id":"c","function":'
        b'{"name":"f","arguments":"{}","arguments":"{}"}}]},'
        b'"finish_reason":"tool_calls"}]}\n\n'
    )
    async with client_with([ByteStream([event, b"data: [DONE]\n\n"])], []) as client:
        with pytest.raises(ValueError, match="duplicate SSE object key"):
            await model_for(client)._agenerate([HumanMessage(content="review")])


@pytest.mark.asyncio
async def test_truncated_by_output_limit_preserves_usage_without_complete_identity():
    partial = frame(delta={"tool_calls": [{"index": 0, "function": {"arguments": '{"x":'}}]}, finish="length")
    async with client_with([ByteStream([partial, final_usage(), b"data: [DONE]\n\n"])], []) as client:
        result = await model_for(client)._agenerate([HumanMessage(content="review")])
    valid, errors = inspect_tool_calls(result.generations[0].message)
    assert not valid and errors
    assert result.llm_output["token_usage"]["prompt_tokens"] == 17
    assert result.generations[0].generation_info["finish_reason"] == "length"


@pytest.mark.asyncio
async def test_metered_v42_wrapper_records_success_and_unknown_interrupted_usage(tmp_path):
    from reviewforge.review_v42.integration import MeteredModel, PhysicalMeter

    requests = []
    first = ByteStream([tool_start()], httpx.RemoteProtocolError("cut"))
    second = ByteStream([tool_start(), tool_end(), final_usage(), b"data: [DONE]\n\n"])
    meter = PhysicalMeter(tmp_path / "receipts.jsonl")

    async def sink(row):
        meter.append(row)

    async with client_with([first, second], requests) as client:
        wrapped = RetryChatLLM(
            MeteredModel(model_for(client), meter, None, "run", "v42_reviewer"),
            policy=RetryPolicy(max_attempts=2, base_delay_seconds=0),
            attempt_sink=sink,
        )
        answer = await wrapped.ainvoke([HumanMessage(content="review")])
    records = meter.requests()
    assert answer.tool_calls[0]["args"] == {"x": 1}
    assert len(records) == 2
    assert records[0]["usage_status"] == "unknown" and not records[0].get("usage")
    assert records[1]["usage"] == {"input_tokens": 17, "output_tokens": 4, "cache_read_tokens": 3}
