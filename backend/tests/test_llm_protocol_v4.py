import pytest

from reviewforge.engine.llm_protocol_v4 import request_options


@pytest.mark.parametrize(
    "agent,closing",
    [("hypothesis_generator", False), ("editor", False), ("investigator", False), ("investigator", True)],
)
def test_qwen_flash_v4_uses_bounded_native_switch_without_forcing_json(agent, closing):
    assert request_options("qwen3.8-flash", agent, closing=closing, protocol_version=4) == {
        "extra_body": {"enable_thinking": False}
    }


def test_qwen_override_unknown_provider_and_saved_protocol_are_preserved():
    assert request_options("qwen3.8-flash", "editor", protocol_version=3) == {}
    assert request_options("unrecognized", "editor", protocol_version=4) == {}
    assert request_options("qwen3.8-flash", "editor", protocol_version=4, json_mode="json_object") == {
        "extra_body": {"enable_thinking": False},
        "response_format": {"type": "json_object"},
    }
