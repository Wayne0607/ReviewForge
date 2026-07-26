"""Secure persistence and validation for console-managed LLM settings."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import ipaddress
import json
import os
import socket
import time
from copy import deepcopy
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from cryptography.fernet import Fernet, InvalidToken

from reviewforge.core.config import LLMConfig, ModelProfile


class LLMSettingsError(ValueError):
    """A safe, user-facing LLM settings error."""


@dataclass(frozen=True)
class LLMSettingsOverride:
    """The small set of LLM fields managed by the single-admin console."""

    base_url: str
    api_key: str
    model: str
    fast_model: str = ""
    accurate_model: str = ""
    version: int = 1


def _write_private(path: Path, data: bytes) -> None:
    """Atomically write a file and restrict it to the service account."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


class EncryptedLLMSettingsStore:
    """Fernet-encrypted, restart-safe LLM settings stored outside the Git tree."""

    def __init__(self, runtime_dir: str | Path) -> None:
        root = Path(runtime_dir)
        self.path = root / "llm-settings.enc"
        self.key_path = root / "master.key"

    def _key(self) -> bytes:
        configured = os.environ.get("REVIEWFORGE_SECRETS_KEY", "").strip()
        if configured:
            raw = configured.encode("utf-8")
            try:
                Fernet(raw)
                return raw
            except (ValueError, TypeError):
                # Accept a strong passphrase too, while never storing it.
                return base64.urlsafe_b64encode(hashlib.sha256(raw).digest())

        if self.key_path.exists():
            raw = self.key_path.read_bytes().strip()
            try:
                Fernet(raw)
            except (ValueError, TypeError) as exc:
                raise LLMSettingsError("本地主密钥无效，无法读取模型配置") from exc
            return raw

        raw = Fernet.generate_key()
        _write_private(self.key_path, raw + b"\n")
        return raw

    def load(self) -> LLMSettingsOverride | None:
        if not self.path.exists():
            return None
        try:
            clear = Fernet(self._key()).decrypt(self.path.read_bytes())
            data = json.loads(clear.decode("utf-8"))
            return LLMSettingsOverride(
                base_url=str(data["base_url"]),
                api_key=str(data["api_key"]),
                model=str(data["model"]),
                fast_model=str(data.get("fast_model", "")),
                accurate_model=str(data.get("accurate_model", "")),
                version=int(data.get("version", 1)),
            )
        except (InvalidToken, KeyError, TypeError, ValueError, json.JSONDecodeError, OSError) as exc:
            raise LLMSettingsError("加密模型配置损坏或主密钥不匹配") from exc

    def save(self, settings: LLMSettingsOverride) -> None:
        payload = {
            **asdict(settings),
            "updated_at": int(time.time()),
        }
        encrypted = Fernet(self._key()).encrypt(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )
        _write_private(self.path, encrypted)

    def delete(self) -> None:
        self.path.unlink(missing_ok=True)


def apply_override(base: LLMConfig, override: LLMSettingsOverride | None) -> LLMConfig:
    """Return a detached effective config without mutating the startup config."""
    cfg = deepcopy(base)
    if override is None:
        return cfg
    cfg.base_url = override.base_url
    cfg.api_key = override.api_key
    cfg.model = override.model
    for profile_name, model in (("fast", override.fast_model), ("accurate", override.accurate_model)):
        profile = cfg.profiles.get(profile_name)
        if profile is None:
            profile = ModelProfile()
            cfg.profiles[profile_name] = profile
        if model:
            profile.model = model
        # Console base URL/API key are deliberately global. Clear legacy profile
        # secrets so a hidden YAML value cannot silently win.
        profile.base_url = ""
        profile.api_key = ""
    return cfg


def make_override(
    current: LLMConfig,
    *,
    base_url: str,
    model: str,
    api_key: str | None,
    fast_model: str = "",
    accurate_model: str = "",
) -> LLMSettingsOverride:
    """Normalize a console payload, keeping the active key when left blank."""
    normalized_url = base_url.strip().rstrip("/")
    normalized_model = model.strip()
    key = (api_key or "").strip() or current.api_key
    if not normalized_url or len(normalized_url) > 2048:
        raise LLMSettingsError("Base URL 不能为空或过长")
    if not normalized_model or len(normalized_model) > 200:
        raise LLMSettingsError("默认模型不能为空或过长")
    if not key or len(key) > 4096:
        raise LLMSettingsError("API Key 不能为空或过长")
    for value, label in ((fast_model, "快速模型"), (accurate_model, "高精度模型")):
        if len(value.strip()) > 200:
            raise LLMSettingsError(f"{label}名称过长")
    return LLMSettingsOverride(
        base_url=normalized_url,
        api_key=key,
        model=normalized_model,
        fast_model=fast_model.strip(),
        accurate_model=accurate_model.strip(),
    )


def safe_settings(config: LLMConfig, source: str) -> dict[str, Any]:
    """Serialize effective settings without ever returning the API key."""
    key = config.api_key or ""
    return {
        "base_url": config.base_url,
        "model": config.model,
        "fast_model": config.profiles.get("fast", ModelProfile()).model or config.model,
        "accurate_model": config.profiles.get("accurate", ModelProfile()).model or config.model,
        "api_key_configured": bool(key),
        "api_key_last4": key[-4:] if len(key) >= 8 else ("****" if key else ""),
        "source": source,
    }


def validate_endpoint_security(base_url: str) -> None:
    """Reject malformed, credential-bearing, metadata and private endpoints."""
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise LLMSettingsError("Base URL 必须是有效的 http(s) 地址")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise LLMSettingsError("Base URL 不能包含账号、密码、查询参数或片段")

    hostname = parsed.hostname.lower().rstrip(".")
    try:
        literal_address = ipaddress.ip_address(hostname)
    except ValueError:
        literal_address = None
    local_target = hostname == "localhost" or bool(literal_address and literal_address.is_loopback)
    allow_private = os.environ.get("REVIEWFORGE_ALLOW_PRIVATE_LLM_ENDPOINTS") == "1"
    if parsed.scheme != "https" and not local_target and not allow_private:
        raise LLMSettingsError("公网模型地址必须使用 HTTPS")

    try:
        addresses = {
            ipaddress.ip_address(item[4][0])
            for item in socket.getaddrinfo(hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
        }
    except (OSError, ValueError) as exc:
        raise LLMSettingsError("无法解析模型服务地址") from exc

    if not addresses:
        raise LLMSettingsError("无法解析模型服务地址")
    for address in addresses:
        if address.is_loopback and local_target:
            continue
        if address.is_link_local or address.is_multicast or address.is_unspecified:
            raise LLMSettingsError("禁止访问链路本地、组播或未指定地址")
        if not address.is_global and not allow_private:
            raise LLMSettingsError("默认禁止访问内网模型地址；如确有需要请在服务端显式开启")


def _safe_connection_error(status: int) -> str:
    if status in {401, 403}:
        return "认证失败，请检查 API Key"
    if status == 404:
        return "接口或模型不存在，请检查 Base URL 与模型名称"
    if status == 429:
        return "模型服务当前限流或额度不足"
    if 400 <= status < 500:
        return f"模型服务拒绝了测试请求（HTTP {status}）"
    return f"模型服务暂时不可用（HTTP {status}）"


async def test_llm_connection(config: LLMConfig, timeout: float = 15.0) -> dict[str, Any]:
    """Perform a minimal OpenAI-compatible request without leaking credentials."""
    await asyncio.to_thread(validate_endpoint_security, config.base_url)
    started = time.perf_counter()
    endpoint = f"{config.base_url.rstrip('/')}/chat/completions"
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(timeout),
            follow_redirects=False,
            trust_env=False,
        ) as client:
            response = await client.post(
                endpoint,
                headers={"Authorization": f"Bearer {config.api_key}", "Content-Type": "application/json"},
                json={
                    "model": config.model,
                    "messages": [{"role": "user", "content": "Reply with OK."}],
                    "temperature": 0,
                    "max_tokens": 8,
                },
            )
    except httpx.TimeoutException as exc:
        raise LLMSettingsError("连接模型服务超时") from exc
    except httpx.HTTPError as exc:
        raise LLMSettingsError("无法连接模型服务") from exc
    if response.status_code >= 400:
        raise LLMSettingsError(_safe_connection_error(response.status_code))
    try:
        body = response.json()
    except ValueError as exc:
        raise LLMSettingsError("模型服务返回了无效响应") from exc
    if not isinstance(body, dict) or not isinstance(body.get("choices"), list):
        raise LLMSettingsError("模型服务响应不兼容 OpenAI Chat Completions")
    return {
        "ok": True,
        "latency_ms": round((time.perf_counter() - started) * 1000),
        "model": config.model,
    }


async def validate_llm_profiles(config: LLMConfig) -> dict[str, Any]:
    """Validate every distinct model that the production router will use."""
    models = [
        config.model,
        config.profiles.get("fast", ModelProfile()).model or config.model,
        config.profiles.get("accurate", ModelProfile()).model or config.model,
    ]
    results = []
    for model in dict.fromkeys(models):
        candidate = deepcopy(config)
        candidate.model = model
        results.append(await test_llm_connection(candidate))
    return {
        "ok": True,
        "latency_ms": sum(item["latency_ms"] for item in results),
        "model": config.model,
        "tested_models": [item["model"] for item in results],
    }
