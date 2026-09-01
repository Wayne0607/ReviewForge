from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from reviewforge.core.config import RoleOverride
from reviewforge.core.llm_settings import EncryptedLLMSettingsStore, LLMSettingsError, LLMSettingsOverride
from scripts import probe_tool_calling as probe

_CHALLENGE = "test-challenge"
_USAGE = {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10}


def _write_probe_config(tmp_path: Path) -> tuple[Path, Path]:
    runtime_dir = tmp_path / ".reviewforge"
    config_path = tmp_path / "reviewforge.yaml"
    config_path.write_text(
        "\n".join(
            [
                "llm:",
                '  base_url: "https://startup.example/v1"',
                '  api_key: "startup-secret"',
                '  model: "startup-global"',
                "  profiles:",
                "    accurate:",
                '      model: "startup-accurate"',
                f'events_dir: "{(runtime_dir / "events").as_posix()}"',
            ]
        ),
        encoding="utf-8",
    )
    return config_path, runtime_dir


def _clear_llm_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("LLM_BASE_URL", "LLM_API_KEY", "REVIEWFORGE_MODEL", "REVIEWFORGE_SECRETS_KEY"):
        monkeypatch.delenv(name, raising=False)


def _tool_call(
    *,
    call_id: str = "call-1",
    name: str = "read_probe_file",
    args: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": call_id,
        "name": name,
        "args": args if args is not None else {"file_path": "probe.txt"},
        "type": "tool_call",
    }


def _ai(
    content: str = "",
    *,
    tool_calls: list[dict[str, Any]] | None = None,
    usage: bool = True,
    finish_reason: str = "stop",
    additional_kwargs: dict[str, Any] | None = None,
) -> AIMessage:
    kwargs: dict[str, Any] = {
        "content": content,
        "response_metadata": {"finish_reason": finish_reason},
    }
    if tool_calls is not None:
        kwargs["tool_calls"] = tool_calls
    if usage:
        kwargs["usage_metadata"] = _USAGE
    if additional_kwargs is not None:
        kwargs["additional_kwargs"] = additional_kwargs
    return AIMessage(**kwargs)


def _final_json(**overrides: str) -> str:
    envelope = {
        "probe": "reviewforge_tool_round_trip",
        "status": "ok",
        "tool_result": _CHALLENGE,
    }
    envelope.update(overrides)
    return json.dumps(envelope)


class _ScriptedLLM:
    def __init__(self, *steps: AIMessage | Exception) -> None:
        self.steps = list(steps)
        self.invocations: list[list[Any]] = []
        self.bound_tools: list[Any] = []

    def bind_tools(self, tools: list[Any]) -> _ScriptedLLM:
        self.bound_tools = tools
        return self

    async def ainvoke(self, messages: list[Any]) -> AIMessage:
        self.invocations.append(list(messages))
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


@pytest.mark.asyncio
async def test_probe_executes_a_complete_evidenced_two_turn_round_trip() -> None:
    private_reasoning = "private chain that must never be printed"
    first = _ai(
        tool_calls=[_tool_call()],
        finish_reason="tool_calls",
        additional_kwargs={"reasoning_content": private_reasoning},
    )
    llm = _ScriptedLLM(first, _ai(_final_json()))

    result = await probe.probe_tool_calling(llm, challenge=_CHALLENGE)

    assert result == probe.ProbeResult(probe.ProbeStatus.SUPPORTED, "complete evidenced two-turn tool round trip", 1)
    assert len(llm.invocations) == 2
    assert len(llm.bound_tools) == 1
    assert isinstance(llm.invocations[0][0], HumanMessage)
    second_turn = llm.invocations[1]
    assert second_turn[1] is first
    assert second_turn[1].additional_kwargs["reasoning_content"] == private_reasoning
    assert isinstance(second_turn[2], ToolMessage)
    assert second_turn[2].tool_call_id == "call-1"
    assert json.loads(str(second_turn[2].content))["probe_token"] == _CHALLENGE
    assert "private chain" not in probe.format_result(result)


@pytest.mark.asyncio
async def test_probe_rejects_a_first_response_without_tool_calls() -> None:
    llm = _ScriptedLLM(_ai("I can answer without a tool."))

    result = await probe.probe_tool_calling(llm, challenge=_CHALLENGE)

    assert result.status is probe.ProbeStatus.NOT_COMPATIBLE
    assert result.reason == "first round: no tool_calls"
    assert len(llm.invocations) == 1


@pytest.mark.asyncio
async def test_probe_rejects_malformed_or_unknown_first_tool_calls() -> None:
    malformed = _ScriptedLLM(_ai(tool_calls=[_tool_call(call_id="")]))
    unknown = _ScriptedLLM(_ai(tool_calls=[_tool_call(name="write_file")]))

    malformed_result = await probe.probe_tool_calling(malformed, challenge=_CHALLENGE)
    unknown_result = await probe.probe_tool_calling(unknown, challenge=_CHALLENGE)

    assert malformed_result.reason == "first round: malformed tool call"
    assert unknown_result.reason == "first round: requested an unknown tool"


@pytest.mark.asyncio
async def test_probe_rejects_invalid_local_tool_arguments() -> None:
    first = _ai(tool_calls=[_tool_call(args={"unexpected": "value"})])
    llm = _ScriptedLLM(first)

    result = await probe.probe_tool_calling(llm, challenge=_CHALLENGE)

    assert result.status is probe.ProbeStatus.NOT_COMPATIBLE
    assert result.reason == "first round: tool arguments failed local validation"
    assert result.tool_calls_executed == 0


@pytest.mark.asyncio
async def test_probe_rejects_a_repeated_tool_request_on_second_round() -> None:
    first = _ai(tool_calls=[_tool_call()])
    repeated = _ai(tool_calls=[_tool_call(call_id="call-2")])
    llm = _ScriptedLLM(first, repeated)

    result = await probe.probe_tool_calling(llm, challenge=_CHALLENGE)

    assert result.status is probe.ProbeStatus.NOT_COMPATIBLE
    assert result.reason == "second round: repeated the same tool"
    assert result.tool_calls_executed == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "final_content",
    [
        "The tool worked.",
        f"The result is {_final_json()}",
        f"```json\n{_final_json()}\n```",
        json.dumps({"probe": "reviewforge_tool_round_trip", "status": "ok"}),
        _final_json(extra="not allowed"),
        _final_json(tool_result="wrong-challenge"),
    ],
)
async def test_probe_requires_the_exact_final_json_envelope(final_content: str) -> None:
    first = _ai(tool_calls=[_tool_call()])
    llm = _ScriptedLLM(first, _ai(final_content))

    result = await probe.probe_tool_calling(llm, challenge=_CHALLENGE)

    assert result.status is probe.ProbeStatus.NOT_COMPATIBLE
    assert result.reason == "second round: invalid final JSON envelope"


@pytest.mark.asyncio
@pytest.mark.parametrize("truncated_stage", ["first", "second"])
async def test_probe_rejects_finish_reason_length(truncated_stage: str) -> None:
    first_reason = "length" if truncated_stage == "first" else "tool_calls"
    second_reason = "length" if truncated_stage == "second" else "stop"
    llm = _ScriptedLLM(
        _ai(tool_calls=[_tool_call()], finish_reason=first_reason),
        _ai(_final_json(), finish_reason=second_reason),
    )

    result = await probe.probe_tool_calling(llm, challenge=_CHALLENGE)

    assert result.status is probe.ProbeStatus.NOT_COMPATIBLE
    assert result.reason == f"{truncated_stage} round: finish_reason=length"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("first_usage", "second_usage", "expected_reason"),
    [
        (False, True, "first round usage unavailable"),
        (True, False, "second round usage unavailable"),
        (False, False, "first and second round usage unavailable"),
    ],
)
async def test_probe_reports_unknown_and_nonzero_when_usage_is_missing(
    first_usage: bool,
    second_usage: bool,
    expected_reason: str,
) -> None:
    llm = _ScriptedLLM(
        _ai(tool_calls=[_tool_call()], usage=first_usage),
        _ai(_final_json(), usage=second_usage),
    )

    result = await probe.probe_tool_calling(llm, challenge=_CHALLENGE)

    assert result.status is probe.ProbeStatus.UNKNOWN
    assert result.reason == expected_reason
    assert result.compatible is False
    assert result.exit_code == 1
    assert probe.format_result(result).startswith("RESULT: UNKNOWN")


class _Provider400Error(Exception):
    status_code = 400


@pytest.mark.asyncio
async def test_probe_marks_missing_reasoning_content_400_incompatible_without_leaking() -> None:
    first = _ai(tool_calls=[_tool_call()])
    secret = "sk-never-print-this"
    error = _Provider400Error(f"reasoning_content missing; key={secret}; value=private-reasoning")
    llm = _ScriptedLLM(first, error)

    result = await probe.probe_tool_calling(llm, challenge=_CHALLENGE)
    rendered = probe.format_result(result)

    assert result.status is probe.ProbeStatus.NOT_COMPATIBLE
    assert result.reason == "second round: HTTP 400; provider rejected missing reasoning_content"
    assert secret not in rendered
    assert "private-reasoning" not in rendered


@pytest.mark.asyncio
async def test_probe_marks_generic_http_400_incompatible_without_echoing_body() -> None:
    secret = "sk-hidden"
    llm = _ScriptedLLM(_Provider400Error(f"invalid request with api_key={secret}"))

    result = await probe.probe_tool_calling(llm, challenge=_CHALLENGE)

    assert result.status is probe.ProbeStatus.NOT_COMPATIBLE
    assert result.reason == "first round: HTTP 400 from provider"
    assert secret not in probe.format_result(result)


@pytest.mark.asyncio
async def test_probe_handles_bind_tools_not_implemented() -> None:
    class _NoToolsLLM:
        def bind_tools(self, _tools: list[Any]) -> Any:
            raise NotImplementedError("provider detail must not be printed")

    result = await probe.probe_tool_calling(_NoToolsLLM(), challenge=_CHALLENGE)

    assert result.status is probe.ProbeStatus.NOT_COMPATIBLE
    assert result.reason == "bind: bind_tools is not implemented"
    assert result.exit_code == 1


@pytest.mark.asyncio
async def test_mock_cli_path_is_self_consistent_and_displays_effective_model(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = await probe.async_main(["--mock", "--expect-model", "probe-mock"])

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "[mock] effective_model=probe-mock" in output
    assert "RESULT: SUPPORTED" in output
    assert "reasoning_content" not in output
    assert "api_key" not in output


@pytest.mark.asyncio
async def test_expect_model_uses_strict_comparison_and_fails_before_probe(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = await probe.async_main(["--mock", "--expect-model", "Probe-Mock"])

    output = capsys.readouterr().out
    assert exit_code == 1
    assert "effective_model=probe-mock" in output
    assert "does not match --expect-model" in output
    assert "Probe-Mock" not in output


def test_safe_model_display_redacts_secret_like_or_malformed_values() -> None:
    assert probe._safe_model_id("deepseek-v4-flash") == "deepseek-v4-flash"
    assert probe._safe_model_id("provider/deep.model:latest") == "provider/deep.model:latest"
    assert probe._safe_model_id("sk-secret-model") == "<redacted-invalid-model-id>"
    assert probe._safe_model_id("model?api_key=secret") == "<redacted-invalid-model-id>"


def test_real_setup_uses_startup_routing_only_when_console_file_is_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_llm_env(monkeypatch)
    config_path, runtime_dir = _write_probe_config(tmp_path)

    setup = probe.setup_real_probe(config_path)

    assert setup.settings_source == "startup"
    assert setup.effective_model == "startup-accurate"
    assert not (runtime_dir / "llm-settings.enc").exists()
    assert not (runtime_dir / "master.key").exists()


def test_real_setup_uses_console_role_override_for_effective_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_llm_env(monkeypatch)
    config_path, runtime_dir = _write_probe_config(tmp_path)
    monkeypatch.setenv("REVIEWFORGE_SECRETS_KEY", "console test deployment secret")
    store = EncryptedLLMSettingsStore(runtime_dir)
    store.save(
        LLMSettingsOverride(
            base_url="https://console.example/v1",
            api_key="console-api-secret",
            model="console-global",
            version=2,
            roles={"deep_review": RoleOverride(model="console-deep-review")},
        )
    )

    setup = probe.setup_real_probe(config_path)

    assert setup.settings_source == "console"
    assert setup.effective_model == "console-deep-review"
    assert not store.key_path.exists()


def test_real_setup_fails_closed_without_a_key_and_does_not_create_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_llm_env(monkeypatch)
    config_path, runtime_dir = _write_probe_config(tmp_path)
    store = EncryptedLLMSettingsStore(runtime_dir)
    runtime_dir.mkdir(parents=True)
    ciphertext = b"encrypted-settings-with-unavailable-key"
    store.path.write_bytes(ciphertext)

    with pytest.raises(LLMSettingsError, match="解密密钥"):
        probe.setup_real_probe(config_path)

    assert store.path.read_bytes() == ciphertext
    assert not store.key_path.exists()


def test_real_setup_fails_closed_on_key_mismatch_without_mutating_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_llm_env(monkeypatch)
    config_path, runtime_dir = _write_probe_config(tmp_path)
    monkeypatch.setenv("REVIEWFORGE_SECRETS_KEY", "first deployment secret")
    store = EncryptedLLMSettingsStore(runtime_dir)
    store.save(LLMSettingsOverride("https://console.example/v1", "console-secret", "console-model"))
    original_ciphertext = store.path.read_bytes()
    monkeypatch.setenv("REVIEWFORGE_SECRETS_KEY", "different deployment secret")

    with pytest.raises(LLMSettingsError, match="主密钥"):
        probe.setup_real_probe(config_path)

    assert store.path.read_bytes() == original_ciphertext
    assert not store.key_path.exists()


def test_real_setup_fails_closed_on_corrupt_ciphertext_without_mutating_local_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_llm_env(monkeypatch)
    config_path, runtime_dir = _write_probe_config(tmp_path)
    store = EncryptedLLMSettingsStore(runtime_dir)
    store.save(LLMSettingsOverride("https://console.example/v1", "console-secret", "console-model"))
    original_key = store.key_path.read_bytes()
    corrupt_ciphertext = b"corrupt-console-ciphertext"
    store.path.write_bytes(corrupt_ciphertext)

    with pytest.raises(LLMSettingsError, match="损坏"):
        probe.setup_real_probe(config_path)

    assert store.path.read_bytes() == corrupt_ciphertext
    assert store.key_path.read_bytes() == original_key


@pytest.mark.asyncio
async def test_setup_failure_output_does_not_echo_encrypted_settings_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret = "sk-console-secret-must-not-leak"
    monkeypatch.setattr(probe, "_load_dotenv", lambda: None)

    def fail_setup(_config_path: str | Path | None = None) -> probe.ProbeSetup:
        raise LLMSettingsError(f"damaged settings containing {secret}")

    monkeypatch.setattr(probe, "setup_real_probe", fail_setup)

    exit_code = await probe.async_main([])
    output = capsys.readouterr().out

    assert exit_code == 1
    assert "RESULT: NOT COMPATIBLE" in output
    assert "LLMSettingsError" in output
    assert secret not in output
