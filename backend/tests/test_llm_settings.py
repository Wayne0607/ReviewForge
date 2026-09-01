"""Secure console LLM settings: encryption, validation and hot swapping."""

from __future__ import annotations

import ipaddress
import json
import os

import pytest

import reviewforge.core.llm_settings as llm_settings_module
from reviewforge.core.config import LLMConfig, ModelProfile, RoleOverride
from reviewforge.core.llm_settings import (
    EncryptedLLMSettingsStore,
    LLMSettingsError,
    LLMSettingsOverride,
    apply_override,
    make_override,
    safe_settings,
    validate_endpoint_security,
    validate_llm_profiles,
)
from reviewforge.core.llm_settings import (
    test_llm_connection as check_llm_connection,
)


def test_encrypted_store_roundtrip_and_no_plaintext_secret(tmp_path, monkeypatch):
    monkeypatch.delenv("REVIEWFORGE_SECRETS_KEY", raising=False)
    store = EncryptedLLMSettingsStore(tmp_path)
    settings = LLMSettingsOverride(
        base_url="https://api.example.com/v1",
        api_key="sk-super-secret-value",
        model="model-a",
        fast_model="model-fast",
        accurate_model="model-accurate",
    )
    store.save(settings)

    assert store.load() == settings
    assert b"sk-super-secret-value" not in store.path.read_bytes()
    assert b"sk-super-secret-value" not in store.key_path.read_bytes()
    assert store.key_path.read_bytes() == EncryptedLLMSettingsStore(tmp_path).key_path.read_bytes()
    if os.name != "nt":
        assert store.path.stat().st_mode & 0o777 == 0o600
        assert store.key_path.stat().st_mode & 0o777 == 0o600


def test_encrypted_store_fails_closed_with_wrong_key(tmp_path, monkeypatch):
    monkeypatch.setenv("REVIEWFORGE_SECRETS_KEY", "first strong deployment secret")
    store = EncryptedLLMSettingsStore(tmp_path)
    store.save(LLMSettingsOverride("https://api.example.com/v1", "secret", "model"))
    monkeypatch.setenv("REVIEWFORGE_SECRETS_KEY", "different deployment secret")
    with pytest.raises(LLMSettingsError, match="主密钥"):
        store.load()


def test_apply_override_is_detached_and_clears_profile_credentials():
    base = LLMConfig(
        base_url="https://old.example/v1",
        api_key="old",
        model="old-model",
        profiles={
            "fast": ModelProfile(model="old-fast", base_url="https://hidden", api_key="hidden"),
            "accurate": ModelProfile(model="old-accurate"),
        },
    )
    override = LLMSettingsOverride(
        base_url="https://new.example/v1",
        api_key="new-key",
        model="new-model",
        fast_model="new-fast",
        accurate_model="new-accurate",
    )
    effective = apply_override(base, override)

    assert effective is not base
    assert effective.base_url == "https://new.example/v1"
    assert effective.profiles["fast"].model == "new-fast"
    assert effective.profiles["fast"].base_url == ""
    assert effective.profiles["fast"].api_key == ""
    assert base.api_key == "old"
    assert base.profiles["fast"].api_key == "hidden"


def test_blank_console_profile_models_clear_startup_fallbacks_to_global():
    base = LLMConfig(
        model="startup-global",
        profiles={
            "default": ModelProfile(model="startup-default"),
            "fast": ModelProfile(model="startup-fast"),
            "accurate": ModelProfile(model="startup-accurate"),
        },
    )
    override = LLMSettingsOverride(
        base_url="https://console.example/v1",
        api_key="console-key",
        model="console-global",
        fast_model="",
        accurate_model="",
    )

    effective = apply_override(base, override)

    assert effective.profiles["default"].model == ""
    assert effective.profiles["fast"].model == ""
    assert effective.profiles["accurate"].model == ""
    roles = safe_settings(effective, "console")["roles"]
    assert roles["planner"]["model"] == "console-global"
    assert roles["verifier"]["model"] == "console-global"
    assert roles["publication_gate"]["model"] == "console-global"


def test_make_override_keeps_existing_key_and_safe_output_never_returns_it():
    current = LLMConfig(api_key="sk-existing", profiles={"fast": ModelProfile(model="fast")})
    override = make_override(
        current,
        base_url=" https://api.example.com/v1/ ",
        model=" model ",
        api_key="",
        fast_model="fast",
    )
    assert override.api_key == "sk-existing"
    data = safe_settings(apply_override(current, override), "console")
    assert data["api_key_last4"] == "ting"
    assert data["api_key_configured"] is True
    assert "sk-existing" not in json.dumps(data)
    assert "api_key" not in data
    assert safe_settings(LLMConfig(api_key="short"), "startup")["api_key_last4"] == "****"


def test_safe_role_settings_match_legacy_profile_fallback_and_explicit_override():
    config = LLMConfig(
        base_url="https://global.example/v1",
        api_key="global-secret",
        model="global-model",
        profiles={
            "fast": ModelProfile(
                base_url="https://fast.example/v1",
                api_key="fast-secret",
                model="fast-model",
            ),
            "accurate": ModelProfile(model="accurate-model"),
        },
        role_overrides={
            "planner": RoleOverride(),
            "deep_review": RoleOverride(model="deep-override"),
        },
    )

    roles = safe_settings(config, "startup")["roles"]

    assert roles["planner"]["model"] == "fast-model"
    assert roles["planner"]["base_url"] == "https://fast.example/v1"
    assert roles["fast_review"]["model"] == "fast-model"
    assert roles["verifier"]["model"] == "accurate-model"
    assert roles["verifier"]["base_url"] == "https://global.example/v1"
    assert roles["deep_review"]["model"] == "deep-override"
    assert roles["deep_review"]["base_url"] == "https://global.example/v1"
    assert roles["publication_gate"]["model"] == "global-model"
    assert "fast-secret" not in json.dumps(roles)


def test_publication_gate_safe_settings_match_default_profile_fallback():
    config = LLMConfig(
        base_url="https://global.example/v1",
        api_key="global-secret",
        model="global-model",
        profiles={
            "default": ModelProfile(
                base_url="https://default.example/v1",
                api_key="default-secret",
                model="default-model",
            )
        },
    )

    role = safe_settings(config, "startup")["roles"]["publication_gate"]

    assert role["model"] == "default-model"
    assert role["base_url"] == "https://default.example/v1"
    assert role["api_key_last4"] == "cret"


def test_endpoint_security_rejects_metadata_and_private_dns(monkeypatch):
    def fake_getaddrinfo(host, port):
        address = "169.254.169.254" if host == "metadata.example" else "10.0.0.8"
        return [(2, 1, 6, "", (address, port))]

    monkeypatch.setattr("socket.getaddrinfo", fake_getaddrinfo)
    monkeypatch.delenv("REVIEWFORGE_ALLOW_PRIVATE_LLM_ENDPOINTS", raising=False)
    with pytest.raises(LLMSettingsError):
        validate_endpoint_security("https://metadata.example/v1")
    with pytest.raises(LLMSettingsError):
        validate_endpoint_security("https://private.example/v1")


def test_endpoint_security_allows_public_https_and_explicit_localhost(monkeypatch):
    def fake_getaddrinfo(host, port):
        address = "127.0.0.1" if host == "localhost" else "1.1.1.1"
        return [(2, 1, 6, "", (address, port))]

    monkeypatch.setattr("socket.getaddrinfo", fake_getaddrinfo)
    validate_endpoint_security("https://public.example/v1")
    validate_endpoint_security("http://localhost:11434/v1")
    validate_endpoint_security("http://127.0.0.1:11434/v1")
    assert ipaddress.ip_address("1.1.1.1").is_global


async def test_profile_test_checks_each_distinct_routed_model(monkeypatch):
    checked = []

    async def fake_test(config, timeout=15.0):
        checked.append(config.model)
        return {"ok": True, "latency_ms": 7, "model": config.model}

    monkeypatch.setattr("reviewforge.core.llm_settings.test_llm_connection", fake_test)
    config = LLMConfig(
        model="default",
        profiles={
            "fast": ModelProfile(model="fast"),
            "accurate": ModelProfile(model="default"),
        },
    )
    result = await validate_llm_profiles(config)
    assert checked == ["default", "fast"]
    assert result["latency_ms"] == 14
    assert result["tested_models"] == ["default", "fast"]


async def test_profile_test_includes_default_profile_used_by_publication_gate(monkeypatch):
    checked = []

    async def fake_test(config, timeout=15.0):
        checked.append(config.model)
        return {"ok": True, "latency_ms": 7, "model": config.model}

    monkeypatch.setattr("reviewforge.core.llm_settings.test_llm_connection", fake_test)
    config = LLMConfig(
        model="global-model",
        profiles={"default": ModelProfile(model="gate-default")},
    )

    result = await validate_llm_profiles(config)

    assert checked == ["global-model", "gate-default"]
    assert result["tested_models"] == ["global-model", "gate-default"]


class _ConnectionResponse:
    status_code = 200

    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body


class _ConnectionClient:
    def __init__(self, response, captured):
        self._response = response
        self._captured = captured

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def post(self, endpoint, *, headers, json):
        self._captured.update(endpoint=endpoint, headers=headers, json=json)
        return self._response


async def test_connection_requires_final_text_and_reports_actual_model(monkeypatch):
    captured = {}
    response = _ConnectionResponse(
        {
            "model": "provider-resolved-model",
            "choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}],
        }
    )
    monkeypatch.setattr(llm_settings_module, "validate_endpoint_security", lambda _url: None)
    monkeypatch.setattr(
        llm_settings_module.httpx,
        "AsyncClient",
        lambda **_kwargs: _ConnectionClient(response, captured),
    )

    result = await check_llm_connection(
        LLMConfig(base_url="https://provider.example/v1", api_key="secret", model="requested-alias")
    )

    assert captured["json"]["max_tokens"] == 128
    assert result["model"] == "provider-resolved-model"
    assert result["requested_model"] == "requested-alias"


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (
            {"model": "resolved", "choices": [{"message": {"content": ""}, "finish_reason": "stop"}]},
            "最终文本",
        ),
        (
            {"model": "resolved", "choices": [{"message": {"content": "OK"}, "finish_reason": "length"}]},
            "截断",
        ),
        (
            {"model": "", "choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}]},
            "模型标识",
        ),
    ],
)
async def test_connection_rejects_incomplete_success_responses(monkeypatch, body, message):
    monkeypatch.setattr(llm_settings_module, "validate_endpoint_security", lambda _url: None)
    monkeypatch.setattr(
        llm_settings_module.httpx,
        "AsyncClient",
        lambda **_kwargs: _ConnectionClient(_ConnectionResponse(body), {}),
    )

    with pytest.raises(LLMSettingsError, match=message):
        await check_llm_connection(
            LLMConfig(base_url="https://provider.example/v1", api_key="secret", model="requested-alias")
        )


def test_admin_llm_settings_hot_swap_and_reset(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    import reviewforge.api.admin as admin_api
    from reviewforge.app import create_app

    runtime_dir = tmp_path / ".reviewforge"
    config_path = tmp_path / "reviewforge.yaml"
    config_path.write_text(
        "\n".join(
            [
                "llm:",
                '  base_url: "https://startup.example/v1"',
                '  api_key: "startup-secret"',
                '  model: "startup-model"',
                "  profiles:",
                "    fast:",
                '      model: "startup-fast"',
                "    accurate:",
                '      model: "startup-accurate"',
                f'events_dir: "{(runtime_dir / "events").as_posix()}"',
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("REVIEWFORGE_MOCK", "1")
    monkeypatch.setenv("REVIEWFORGE_API_TOKEN", "test-admin-token")
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.delenv("REVIEWFORGE_MODEL", raising=False)

    app = create_app(str(config_path))
    with TestClient(app, headers={"Authorization": "Bearer test-admin-token"}) as client:
        initial = client.get("/api/v1/admin/llm-settings").json()
        assert initial["source"] == "startup"
        assert initial["api_key_last4"] == "cret"
        old_orchestrator = app.state.orchestrator

        payload = {
            "base_url": "http://localhost:11434/v1",
            "api_key": "new-super-secret",
            "model": "new-model",
            "fast_model": "new-fast",
            "accurate_model": "new-accurate",
        }

        original_test = admin_api._test_candidate

        async def fail_connection(candidate, request):
            raise LLMSettingsError("认证失败，请检查 API Key")

        monkeypatch.setattr(admin_api, "_test_candidate", fail_connection)
        failed = client.post("/api/v1/admin/llm-settings", json=payload)
        assert failed.status_code == 400
        assert app.state.orchestrator is old_orchestrator
        assert not runtime_dir.joinpath("llm-settings.enc").exists()
        monkeypatch.setattr(admin_api, "_test_candidate", original_test)

        tested = client.post("/api/v1/admin/llm-settings/test", json=payload)
        assert tested.status_code == 200
        saved = client.post("/api/v1/admin/llm-settings", json=payload)
        assert saved.status_code == 200
        assert saved.json()["settings"]["source"] == "console"
        assert app.state.orchestrator is not old_orchestrator
        assert app.state.config.llm.api_key == "new-super-secret"

        ciphertext = runtime_dir.joinpath("llm-settings.enc").read_bytes()
        assert b"new-super-secret" not in ciphertext
        assert "new-super-secret" not in client.get("/api/v1/admin/llm-settings").text

        reset = client.post("/api/v1/admin/llm-settings/reset", json={})
        assert reset.status_code == 200
        assert reset.json()["settings"]["source"] == "startup"
        assert app.state.config.llm.model == "startup-model"
        assert not runtime_dir.joinpath("llm-settings.enc").exists()
