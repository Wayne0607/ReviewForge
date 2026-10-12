"""Provider metadata is an allowlist; retry hints never remove admission bounds."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import openai
import pytest
from test_martian_runner import runner as runner
from test_provider_retry import SequenceLLM, response

from reviewforge.core.config import ReviewForgeConfig
from reviewforge.engine.provider_retry import RetryChatLLM, RetryPolicy, safe_provider_diagnostics


@pytest.mark.asyncio
async def test_sdk_mock_trace_and_attempt_share_safe_diagnostics(runner, monkeypatch, tmp_path, caplog):
    from reviewforge.engine import model_router, provider_retry

    requests = []

    def respond(request):
        requests.append(request.content)
        if len(requests) == 1:
            return httpx.Response(
                429,
                headers={"Retry-After": "2", "x-api-key": "secret-provider-header"},
                json={
                    "error": {
                        "code": "rate_limit_exceeded",
                        "type": "tokens",
                        "message": "secret-provider-body",
                        "api_key": "secret-provider-key",
                    }
                },
            )
        return httpx.Response(
            200,
            json={
                "id": "mock",
                "object": "chat.completion",
                "model": "test-model",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 4, "completion_tokens": 1, "total_tokens": 5},
            },
        )

    async def sleep(_):
        pass

    http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    original = model_router.ChatOpenAI
    monkeypatch.setattr(model_router, "ChatOpenAI", lambda **kwargs: original(**kwargs, http_async_client=http))
    monkeypatch.setattr(provider_retry.asyncio, "sleep", sleep)
    monkeypatch.setenv("GITHUB_TOKEN", "placeholder")
    monkeypatch.setenv("LLM_API_KEY", "placeholder")
    monkeypatch.setenv("REVIEWFORGE_SETTINGS_DIR", str(tmp_path))
    caplog.set_level("INFO", logger="reviewforge.engine.provider_retry")
    orchestrator, db, github = await runner._build_runtime(
        tmp_path, "test-model", llm_min_interval=0, llm_max_attempts=2
    )
    try:
        await orchestrator._model_router.get_llm("hypothesis_generator").ainvoke("fixed", max_tokens=32)
        assert len(requests) == 2 and requests[0] == requests[1]
        errors = [json.loads(p.read_text()) for p in (tmp_path / "llm-traces").glob("*-error.json")]
        failed = [json.loads(p.read_text()) for p in (tmp_path / "provider-attempts").glob("*-finished.json")]
        first = next(event for event in failed if event["outcome"] == "provider-error")
        assert len(errors) == 1
        assert errors[0]["provider_diagnostics"] == first["provider_diagnostics"]
        assert first["delay_seconds"] == 2
        assert first["provider_diagnostics"]["limit_kind"] == "provider_reported_tokens"
        assert "secret-provider" not in json.dumps(errors + failed)
        assert "secret-provider" not in caplog.text
    finally:
        await db.close()
        await github.close()
        await http.aclose()


def error(hint=None, *, body=None):
    headers = {"Retry-After": hint} if hint is not None else {}
    headers["x-secret-api-key"] = "secret-header-marker"
    request = httpx.Request("POST", "https://relay.invalid/v1?api_key=secret-url-marker")
    reply = httpx.Response(429, headers=headers, request=request)
    return openai.RateLimitError("secret-message-marker", response=reply, body=body)


def test_only_allowlisted_error_tokens_are_retained():
    diagnostics = safe_provider_diagnostics(
        error(
            "10",
            body={
                "error": {
                    "code": "rate_limit_exceeded",
                    "type": "tokens",
                    "message": "secret-body-marker",
                    "api_key": "secret-key-marker",
                }
            },
        )
    )
    assert diagnostics == {
        "diagnostic_version": 1,
        "error_code": "rate_limit_exceeded",
        "error_type": "tokens",
        "limit_kind": "provider_reported_tokens",
        "retry_after_seconds": 10,
        "retry_after_format": "delay_seconds",
    }
    assert "secret" not in json.dumps(diagnostics)
    assert "TPM" not in json.dumps(diagnostics)


def test_unknown_values_and_headers_are_discarded():
    diagnostics = safe_provider_diagnostics(
        error(
            "secret-delay-marker",
            body={"code": "secret-code-marker", "type": "secret-type-marker", "message": "secret-body-marker"},
        )
    )
    assert diagnostics["error_code"] == diagnostics["error_type"] == "unrecognized"
    assert diagnostics["limit_kind"] == "unspecified"
    assert diagnostics["retry_after_format"] == "invalid"
    assert "secret" not in json.dumps(diagnostics)


@pytest.mark.parametrize(
    "hint", ["NaN", "inf", "Infinity", "-1", "1e30", "9" * 129, "Tue, 01 Jan 2030 00:00:00 GMT secret-header-marker"]
)
def test_malicious_or_nonfinite_delays_are_invalid(hint):
    diagnostic = safe_provider_diagnostics(error(hint))
    assert diagnostic["retry_after_format"] == "invalid"
    assert diagnostic["retry_after_seconds"] is None
    assert "secret" not in json.dumps(diagnostic)


def test_http_date_and_past_date_are_normalized():
    now = datetime(2030, 1, 1, tzinfo=UTC)
    future = safe_provider_diagnostics(error(format_datetime(now + timedelta(seconds=12), usegmt=True)), now=now)
    past = safe_provider_diagnostics(error(format_datetime(now - timedelta(seconds=12), usegmt=True)), now=now)
    assert (future["retry_after_seconds"], future["retry_after_format"]) == (12, "http_date")
    assert past["retry_after_seconds"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(("hint", "expected"), [("15", 15), ("0.5", 1), ("NaN", 1)])
async def test_bounded_server_hint_uses_maximum_of_backoff_and_hint(monkeypatch, hint, expected):
    from reviewforge.engine import provider_retry

    delays, events = [], []

    async def sleep(delay):
        delays.append(delay)

    async def sink(event):
        events.append(event)

    monkeypatch.setattr(provider_retry.asyncio, "sleep", sleep)
    inner = SequenceLLM([error(hint), response()])
    await RetryChatLLM(inner, attempt_sink=sink).ainvoke("fixed", max_tokens=32)
    assert delays == [expected] and len(inner._calls) == 2
    assert inner._calls[0][:3] == inner._calls[1][:3]
    assert events[1]["provider_diagnostics"]["retry_after_format"] in {"delay_seconds", "invalid"}
    assert "secret" not in json.dumps(events)


@pytest.mark.asyncio
@pytest.mark.parametrize("hint", ["61", "9999999999999999"])
async def test_delay_beyond_cap_ends_request_without_sending_early(monkeypatch, hint):
    from reviewforge.engine import provider_retry

    async def forbidden_sleep(_):
        pytest.fail("No sleep or early retry is permitted")

    events = []

    async def sink(event):
        events.append(event)

    monkeypatch.setattr(provider_retry.asyncio, "sleep", forbidden_sleep)
    failed = error(hint)
    inner = SequenceLLM([failed, response()])
    with pytest.raises(openai.RateLimitError) as caught:
        await RetryChatLLM(inner, attempt_sink=sink).ainvoke("fixed")
    assert caught.value is failed and len(inner._calls) == 1
    assert not events[-1]["will_retry"]
    assert events[-1]["retry_suppressed_reason"] == "retry-after-exceeds-policy-cap"


@pytest.mark.asyncio
async def test_server_wait_is_cancelled_without_another_attempt():
    ready = asyncio.Event()

    async def sink(event):
        if event["phase"] == "finished":
            ready.set()

    inner = SequenceLLM([error("60"), response()])
    task = asyncio.create_task(RetryChatLLM(inner, attempt_sink=sink).ainvoke("fixed"))
    await asyncio.wait_for(ready.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    assert len(inner._calls) == 1


@pytest.mark.asyncio
async def test_quota_diagnostics_do_not_release_unknown_reservation(monkeypatch):
    from reviewforge.engine import provider_retry
    from reviewforge.engine.request_budget import BudgetedChatLLM, RequestBudget

    async def sleep(_):
        pass

    monkeypatch.setattr(provider_retry.asyncio, "sleep", sleep)
    budget = RequestBudget(10000, discovery_limit=10000, editor_reserve=0)
    raw = SequenceLLM([error(body={"code": "insufficient_quota"}), response()])
    await RetryChatLLM(BudgetedChatLLM(raw, budget, phase="discovery")).ainvoke("fixed", max_tokens=32)
    assert budget.unreported > 0 and budget.reported == 10 and not budget.pending
    assert (
        safe_provider_diagnostics(error(body={"code": "insufficient_quota"}))["limit_kind"] == "provider_reported_quota"
    )


@pytest.mark.parametrize("value", [".nan", ".inf", "-1", "true", "bad"])
def test_invalid_retry_after_config_keeps_finite_default(tmp_path, value):
    path = tmp_path / "reviewforge.yaml"
    path.write_text(f"pipeline_v4:\n  provider_retry_after_max_seconds: {value}\n")
    assert ReviewForgeConfig.load(path).pipeline_v4.provider_retry_after_max_seconds == 60


def test_retry_after_config_round_trip_and_policy_bounds(tmp_path):
    path = tmp_path / "reviewforge.yaml"
    path.write_text("pipeline_v4:\n  provider_retry_after_max_seconds: 12.5\n")
    assert ReviewForgeConfig.load(path).pipeline_v4.provider_retry_after_max_seconds == 12.5
    assert RetryPolicy().to_dict()["retry_after_max_seconds"] == 60
    for invalid in (True, -1, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            RetryPolicy(retry_after_max_seconds=invalid)
