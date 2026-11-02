"""The v4.2 transport must not inherit legacy profile output ceilings."""

from __future__ import annotations

import math

import httpx
import pytest
from langchain_core.messages import HumanMessage
from test_v42_stream_transport import ByteStream, final_usage, tool_end, tool_start

from reviewforge.core.config import LLMConfig, ModelProfile, RoleOverride
from reviewforge.core.llm_settings import LLMSettingsOverride, apply_override
from reviewforge.engine.model_router import ModelRouter
from reviewforge.engine.openai_compat import ReasoningContentChatOpenAI, V42StreamingChatOpenAI


def console_config(*, roles=None):
    return apply_override(
        LLMConfig(),
        LLMSettingsOverride(
            base_url="https://test.invalid/v1",
            api_key="test-key",
            model="deepseek-ai/deepseek-v4.1-flash",
            roles=roles or {},
        ),
    )


def test_console_default_profile_does_not_limit_v42_output():
    config = console_config()
    assert config.profiles["default"].max_tokens == 4096
    router = ModelRouter(config)
    for agent in ("v42_reviewer", "v42_verifier"):
        model = router.get_llm(agent, max_retries=0, stream_transport=True)
        assert isinstance(model, V42StreamingChatOpenAI)
        assert model.max_tokens is None
        assert model.temperature == config.profiles["default"].temperature
        assert model.model_name == config.model
        assert "max_tokens" not in model._get_request_payload([HumanMessage(content="review")])
        assert "max_completion_tokens" not in model._get_request_payload([HumanMessage(content="review")])


@pytest.mark.parametrize("agent,role", [("v42_reviewer", "deep_review"), ("v42_verifier", "publication_gate")])
def test_explicit_role_override_still_selects_model_and_temperature(agent, role):
    role_override = RoleOverride(base_url="https://role.invalid/v1", api_key="role-key", model="role-model")
    config = console_config(roles={role: role_override})
    model = ModelRouter(config).get_llm(agent, max_retries=0, stream_transport=True)
    assert model.model_name == "role-model"
    assert str(model.openai_api_base) == "https://role.invalid/v1"
    assert model.temperature == (
        config.temperature_reviewer if agent == "v42_reviewer" else config.temperature_verifier
    )
    assert model.max_tokens is None
    assert "max_completion_tokens" not in model._get_request_payload([HumanMessage(content="review")])


@pytest.mark.parametrize("first", ["legacy", "v42"])
def test_legacy_profile_limit_and_v42_cache_are_isolated(first):
    config = console_config()
    router = ModelRouter(config)
    names = [first, "v42" if first == "legacy" else "legacy"]
    agents = {"legacy": "custom_legacy_agent", "v42": "v42_reviewer"}
    result = {name: router.get_llm(agents[name], max_retries=0) for name in names}
    assert result["legacy"] is not result["v42"]
    assert isinstance(result["legacy"], ReasoningContentChatOpenAI)
    assert result["legacy"].max_tokens == 4096
    assert result["v42"].max_tokens is None
    assert result["legacy"].temperature == result["v42"].temperature
    assert router.get_llm("custom_legacy_agent", max_retries=0) is result["legacy"]
    assert router.get_llm("v42_reviewer", max_retries=0) is result["v42"]
    assert result["legacy"]._get_request_payload([HumanMessage(content="review")])["max_completion_tokens"] == 4096


def test_explicit_legacy_profile_output_ceiling_does_not_become_v42_ceiling():
    config = LLMConfig(api_key="test", profiles={"default": ModelProfile(max_tokens=8192, temperature=0.37)})
    router = ModelRouter(config)
    assert router.get_llm("v42_reviewer").max_tokens is None
    assert router.get_llm("v42_reviewer").temperature == 0.37
    assert router.get_llm("legacy_custom").max_tokens == 8192


@pytest.mark.parametrize("stream", [False, True])
def test_explicit_timeout_is_v42_only_and_part_of_cache(stream):
    router = ModelRouter(console_config())
    base = router.get_llm("v42_reviewer", max_retries=0, stream_transport=stream)
    longer = router.get_llm("v42_reviewer", max_retries=0, stream_transport=stream, request_timeout=600)
    other = router.get_llm("v42_reviewer", max_retries=0, stream_transport=stream, request_timeout=300)
    assert base.request_timeout == 120
    assert longer.request_timeout == 600 and other.request_timeout == 300
    assert len({id(base), id(longer), id(other)}) == 3
    assert router.get_llm("v42_reviewer", max_retries=0, stream_transport=stream, request_timeout=600) is longer
    with pytest.raises(ValueError, match="restricted"):
        router.get_llm("legacy_custom", request_timeout=600)
    router.invalidate_cache()
    assert router.get_llm("v42_reviewer", max_retries=0, stream_transport=stream, request_timeout=600) is not longer


@pytest.mark.parametrize("bad", [0, -1, True, False, math.inf, -math.inf, math.nan, "120", 10**1000])
def test_invalid_explicit_timeouts_rejected(bad):
    with pytest.raises(ValueError, match="finite positive"):
        ModelRouter(console_config()).get_llm("v42_verifier", request_timeout=bad)


@pytest.mark.asyncio
async def test_console_configuration_streams_native_wire_without_legacy_cap(monkeypatch):
    requests = []

    async def handler(request):
        requests.append(request)
        stream = ByteStream([tool_start(), tool_end(), final_usage(), b"data: [DONE]\n\n"])
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        monkeypatch.setattr("reviewforge.engine.model_router.client_options", lambda: {"http_async_client": client})
        model = ModelRouter(console_config()).get_llm(
            "v42_reviewer", max_retries=0, stream_transport=True, request_timeout=600
        )
        result = await model._agenerate([HumanMessage(content="review")])
    import json

    payload = json.loads(requests[0].content)
    assert payload["model"] == "deepseek-ai/deepseek-v4.1-flash"
    assert payload["temperature"] == 0.1
    assert payload["stream"] is True
    assert "max_tokens" not in payload and "max_completion_tokens" not in payload
    assert requests[0].extensions["timeout"]["read"] == 600
    assert result.generations[0].message.tool_calls[0]["args"] == {"x": 1}


def test_anthropic_explicit_timeout_does_not_change_legacy_timeout():
    config = LLMConfig(base_url="https://api.minimaxi.com/v1", api_key="test", model="MiniMax-M3")
    router = ModelRouter(config)
    assert router.get_llm("legacy_custom", max_retries=0).default_request_timeout is None
    assert router.get_llm("v42_reviewer", max_retries=0).default_request_timeout is None
    assert router.get_llm("v42_reviewer", max_retries=0, request_timeout=600).default_request_timeout == 600


def test_v42_default_profile_changes_only_legacy_output_limit():
    router = ModelRouter(console_config())
    legacy = router.get_llm("legacy_custom", max_retries=0)
    reviewer = router.get_llm("v42_reviewer", max_retries=0)
    message = [HumanMessage(content="same input")]
    old_payload = legacy._get_request_payload(message)
    new_payload = reviewer._get_request_payload(message)
    assert old_payload.pop("max_completion_tokens") == 4096
    assert new_payload == old_payload
    assert "thinking" not in new_payload
