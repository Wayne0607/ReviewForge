"""Probe whether a provider supports a complete tool-calling round trip.

Exit code ``0`` is reserved for a fully evidenced result: both model turns
report token usage, the first turn calls the bound read-only tool, and the
second turn returns the exact JSON envelope requested by the probe. Unknown
telemetry and incompatible behavior both return exit code ``1``.

Usage::

    python scripts/probe_tool_calling.py
    python scripts/probe_tool_calling.py --expect-model deepseek-v4-flash
    python scripts/probe_tool_calling.py --mock --expect-model probe-mock

The probe never prints response content, tool arguments, exception messages,
API keys, base URLs, challenge values, or provider reasoning content.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import secrets
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import StructuredTool

from reviewforge.core.config import ReviewForgeConfig
from reviewforge.core.json_output import extract_json_value, strip_reasoning_blocks
from reviewforge.core.llm_settings import (
    EncryptedLLMSettingsStore,
    LLMSettingsError,
    LLMSettingsOverride,
    apply_override,
)
from reviewforge.engine.model_router import ModelRouter

_PROBE_TOOL_NAME = "read_probe_file"
_PROBE_NAME = "reviewforge_tool_round_trip"
_MOCK_MODEL_ID = "probe-mock"
_SAFE_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/:@+\-]{0,127}")
_SECRET_MODEL_MARKERS = ("api_key", "apikey", "bearer", "token=", "sk-")
_PROBE_PROMPT = (
    "Call read_probe_file exactly once with file_path='probe.txt'. After receiving its JSON result, return ONLY "
    'one JSON object with exactly these keys and values: {"probe":"reviewforge_tool_round_trip",'
    '"status":"ok","tool_result":"<exact probe_token from the tool result>"}. Do not add keys, prose, '
    "Markdown, or another tool call."
)


class ProbeStatus(StrEnum):
    SUPPORTED = "SUPPORTED"
    NOT_COMPATIBLE = "NOT COMPATIBLE"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class ProbeResult:
    """Safe, serializable outcome of the two-turn capability probe."""

    status: ProbeStatus
    reason: str
    tool_calls_executed: int = 0

    @property
    def compatible(self) -> bool:
        return self.status is ProbeStatus.SUPPORTED

    @property
    def exit_code(self) -> int:
        return 0 if self.compatible else 1


@dataclass(frozen=True)
class ProbeSetup:
    """The same effective reviewer model selection used by the application."""

    llm: Any
    effective_model: str
    settings_source: str


def _result(status: ProbeStatus, reason: str, calls: int = 0) -> ProbeResult:
    return ProbeResult(status=status, reason=reason, tool_calls_executed=calls)


def _load_dotenv() -> None:
    """Load the repository ``.env`` without overwriting process variables."""

    env_path = Path(__file__).resolve().parents[2] / ".env"
    if not env_path.exists():
        return
    with env_path.open(encoding="utf-8") as env_file:
        for raw_line in env_file:
            line = raw_line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip())


def _load_console_override_read_only(store: EncryptedLLMSettingsStore) -> LLMSettingsOverride | None:
    """Load an existing console file without letting ``load`` create a key.

    ``EncryptedLLMSettingsStore._key`` intentionally creates ``master.key``
    for first-time writes. A diagnostic probe must not exercise that path: if
    encrypted settings already exist without either supported key source, the
    only safe result is a read-only, fail-closed error.
    """

    if not store.path.exists():
        return None
    configured_key = os.environ.get("REVIEWFORGE_SECRETS_KEY", "").strip()
    if not configured_key and not store.key_path.is_file():
        raise LLMSettingsError("加密模型配置存在，但没有可用的只读解密密钥")
    return store.load()


def setup_real_probe(config_path: str | Path | None = None) -> ProbeSetup:
    """Apply startup and console settings, then build the routed reviewer LLM."""

    config = ReviewForgeConfig.load(config_path)
    bootstrap_llm_config = apply_override(config.llm, None)
    runtime_dir = Path(config.events_dir).parent
    settings_store = EncryptedLLMSettingsStore(runtime_dir)
    stored_override = _load_console_override_read_only(settings_store)
    effective_llm_config = apply_override(bootstrap_llm_config, stored_override)

    router = ModelRouter(effective_llm_config)
    effective_model = router.effective("security_reviewer")["model"]
    return ProbeSetup(
        llm=router.get_llm("security_reviewer"),
        effective_model=effective_model,
        settings_source="console" if stored_override is not None else "startup",
    )


def build_probe_tool(challenge: str) -> StructuredTool:
    """Build the single side-effect-free virtual-file tool."""

    def read_probe_file(file_path: str) -> str:
        """Read a deterministic virtual file used by the capability probe."""

        return json.dumps({"file_path": file_path, "probe_token": challenge})

    return StructuredTool.from_function(
        func=read_probe_file,
        name=_PROBE_TOOL_NAME,
        description="Read a deterministic virtual file used only by a capability probe.",
    )


def _status_code(error: Exception) -> int | None:
    direct = getattr(error, "status_code", None)
    if isinstance(direct, int):
        return direct
    response = getattr(error, "response", None)
    response_status = getattr(response, "status_code", None)
    return response_status if isinstance(response_status, int) else None


def _safe_provider_failure(stage: str, error: Exception) -> ProbeResult:
    """Classify provider errors without echoing their potentially secret body."""

    error_text = str(error).lower()
    status = _status_code(error)
    if "reasoning_content" in error_text:
        if status == 400:
            reason = f"{stage}: HTTP 400; provider rejected missing reasoning_content"
        else:
            reason = f"{stage}: provider rejected missing reasoning_content"
    elif status == 400 or any(
        marker in error_text
        for marker in ("400 bad request", "error code: 400", "http 400", "status code 400", "status_code=400")
    ):
        reason = f"{stage}: HTTP 400 from provider"
    elif isinstance(error, NotImplementedError):
        reason = f"{stage}: bind_tools is not implemented"
    else:
        reason = f"{stage}: provider request failed ({type(error).__name__})"
    return _result(ProbeStatus.NOT_COMPATIBLE, reason)


def _safe_model_id(model_id: object) -> str:
    """Show ordinary model IDs while refusing secret-like or malformed values."""

    if not isinstance(model_id, str) or not _SAFE_MODEL_ID.fullmatch(model_id):
        return "<redacted-invalid-model-id>"
    lowered = model_id.lower()
    if any(marker in lowered for marker in _SECRET_MODEL_MARKERS):
        return "<redacted-invalid-model-id>"
    return model_id


def _tool_calls(message: Any) -> list[Any]:
    calls = getattr(message, "tool_calls", None)
    return list(calls) if isinstance(calls, (list, tuple)) else []


def _parse_tool_call(call: Any) -> tuple[str, dict[str, Any], str] | None:
    if not isinstance(call, dict):
        return None
    name = call.get("name")
    arguments = call.get("args")
    call_id = call.get("id")
    if not isinstance(name, str) or not isinstance(arguments, dict) or not isinstance(call_id, str):
        return None
    if not name or not call_id:
        return None
    return name, arguments, call_id


def _is_truncated(message: Any) -> bool:
    metadata = getattr(message, "response_metadata", None)
    if not isinstance(metadata, Mapping):
        return False
    finish_reason = metadata.get("finish_reason")
    return isinstance(finish_reason, str) and finish_reason.lower() == "length"


def _numeric_token_count(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0


def _usage_mapping_present(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    total = value.get("total_tokens")
    if _numeric_token_count(total) and total > 0:
        return True
    input_count = value.get("input_tokens", value.get("prompt_tokens"))
    output_count = value.get("output_tokens", value.get("completion_tokens"))
    return _numeric_token_count(input_count) and _numeric_token_count(output_count) and input_count + output_count > 0


def _has_usage(message: Any) -> bool:
    if _usage_mapping_present(getattr(message, "usage_metadata", None)):
        return True
    metadata = getattr(message, "response_metadata", None)
    if not isinstance(metadata, Mapping):
        return False
    return _usage_mapping_present(metadata.get("token_usage")) or _usage_mapping_present(metadata.get("usage"))


def _expected_final_envelope(challenge: str) -> dict[str, str]:
    return {"probe": _PROBE_NAME, "status": "ok", "tool_result": challenge}


def _has_exact_final_envelope(content: Any, challenge: str) -> bool:
    parsed = extract_json_value(content, required_key="probe", allow_list=False)
    expected = _expected_final_envelope(challenge)
    if not isinstance(parsed, dict) or parsed != expected:
        return False
    try:
        direct = json.loads(strip_reasoning_blocks(content))
    except (json.JSONDecodeError, TypeError):
        return False
    return direct == expected


async def probe_tool_calling(llm: Any, *, challenge: str | None = None) -> ProbeResult:
    """Run the provider-independent two-turn tool-calling contract probe."""

    challenge = challenge or secrets.token_hex(12)
    tool = build_probe_tool(challenge)
    try:
        bound_llm = llm.bind_tools([tool])
    except Exception as error:
        return _safe_provider_failure("bind", error)

    conversation: list[Any] = [HumanMessage(content=_PROBE_PROMPT)]
    try:
        first_response = await bound_llm.ainvoke(conversation)
    except Exception as error:
        return _safe_provider_failure("first round", error)
    if _is_truncated(first_response):
        return _result(ProbeStatus.NOT_COMPATIBLE, "first round: finish_reason=length")

    first_calls = _tool_calls(first_response)
    if not first_calls:
        invalid_calls = getattr(first_response, "invalid_tool_calls", None) or []
        reason = "first round: malformed tool call" if invalid_calls else "first round: no tool_calls"
        return _result(ProbeStatus.NOT_COMPATIBLE, reason)

    # Keep the exact provider message in the conversation. Reconstructing it
    # can drop provider-specific metadata such as ``reasoning_content`` and
    # cause an otherwise valid tool continuation to fail with HTTP 400.
    conversation.append(first_response)
    first_tool_names: set[str] = set()
    calls_executed = 0
    for raw_call in first_calls:
        parsed = _parse_tool_call(raw_call)
        if parsed is None:
            return _result(ProbeStatus.NOT_COMPATIBLE, "first round: malformed tool call", calls_executed)
        name, arguments, call_id = parsed
        if name != tool.name:
            return _result(ProbeStatus.NOT_COMPATIBLE, "first round: requested an unknown tool", calls_executed)
        try:
            tool_result = await tool.ainvoke(arguments)
        except Exception:
            return _result(
                ProbeStatus.NOT_COMPATIBLE,
                "first round: tool arguments failed local validation",
                calls_executed,
            )
        first_tool_names.add(name)
        calls_executed += 1
        conversation.append(ToolMessage(content=str(tool_result), tool_call_id=call_id))

    try:
        second_response = await bound_llm.ainvoke(conversation)
    except Exception as error:
        failure = _safe_provider_failure("second round", error)
        return ProbeResult(failure.status, failure.reason, calls_executed)
    if _is_truncated(second_response):
        return _result(ProbeStatus.NOT_COMPATIBLE, "second round: finish_reason=length", calls_executed)

    second_calls = _tool_calls(second_response)
    if second_calls:
        repeated = any(
            parsed is not None and parsed[0] in first_tool_names
            for parsed in (_parse_tool_call(raw_call) for raw_call in second_calls)
        )
        reason = "second round: repeated the same tool" if repeated else "second round: requested another tool"
        return _result(ProbeStatus.NOT_COMPATIBLE, reason, calls_executed)
    if getattr(second_response, "invalid_tool_calls", None):
        return _result(ProbeStatus.NOT_COMPATIBLE, "second round: malformed tool call", calls_executed)
    if not _has_exact_final_envelope(getattr(second_response, "content", None), challenge):
        return _result(ProbeStatus.NOT_COMPATIBLE, "second round: invalid final JSON envelope", calls_executed)

    missing_usage = [
        stage for stage, message in (("first", first_response), ("second", second_response)) if not _has_usage(message)
    ]
    if missing_usage:
        stages = " and ".join(missing_usage)
        return _result(ProbeStatus.UNKNOWN, f"{stages} round usage unavailable", calls_executed)

    return _result(ProbeStatus.SUPPORTED, "complete evidenced two-turn tool round trip", calls_executed)


def format_result(result: ProbeResult) -> str:
    """Format an outcome using only probe-owned, non-sensitive fields."""

    return f"RESULT: {result.status.value} - {result.reason}; tool_calls_executed={result.tool_calls_executed}"


class _ProbeMockLLM:
    """Minimal deterministic mock that implements the exact probe contract."""

    def __init__(self) -> None:
        self._tools: list[StructuredTool] = []

    def bind_tools(self, tools: list[StructuredTool]) -> _ProbeMockLLM:
        self._tools = tools
        return self

    async def ainvoke(self, messages: list[Any]) -> AIMessage:
        usage = {"input_tokens": 8, "output_tokens": 4, "total_tokens": 12}
        tool_messages = [message for message in messages if isinstance(message, ToolMessage)]
        if not tool_messages:
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": self._tools[0].name,
                        "args": {"file_path": "probe.txt"},
                        "id": "probe-mock-call",
                        "type": "tool_call",
                    }
                ],
                response_metadata={"finish_reason": "tool_calls"},
                usage_metadata=usage,
            )
        tool_payload = json.loads(str(tool_messages[-1].content))
        return AIMessage(
            content=json.dumps(_expected_final_envelope(tool_payload["probe_token"])),
            response_metadata={"finish_reason": "stop"},
            usage_metadata=usage,
        )


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mock", action="store_true", help="run against the probe's deterministic mock")
    parser.add_argument(
        "--expect-model",
        metavar="MODEL_ID",
        help="fail before probing unless the effective model ID matches exactly",
    )
    return parser.parse_args(argv)


async def async_main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint, separated from ``SystemExit`` for unit testing."""

    args = _parse_args(argv)
    _load_dotenv()
    try:
        if args.mock:
            llm = _ProbeMockLLM()
            effective_model = _MOCK_MODEL_ID
            mode = "mock"
            settings_source = "mock"
        else:
            setup = setup_real_probe()
            effective_model = setup.effective_model
            llm = setup.llm
            mode = "real"
            settings_source = setup.settings_source
    except Exception as error:
        result = _safe_provider_failure("setup", error)
        print(format_result(result))
        return result.exit_code

    print(f"[{mode}] effective_model={_safe_model_id(effective_model)} settings_source={settings_source}")
    if args.expect_model is not None and effective_model != args.expect_model:
        result = _result(ProbeStatus.NOT_COMPATIBLE, "effective model does not match --expect-model")
        print(format_result(result))
        return result.exit_code

    result = await probe_tool_calling(llm)
    print(format_result(result))
    return result.exit_code


def main(argv: Sequence[str] | None = None) -> int:
    return asyncio.run(async_main(argv))


if __name__ == "__main__":
    raise SystemExit(main())
