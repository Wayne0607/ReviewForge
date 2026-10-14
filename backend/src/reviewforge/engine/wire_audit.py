"""Opt-in HTTP boundary receipts: hashes/schemas, never headers or prompt text."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import uuid
from pathlib import Path

import httpx


def _hash(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


class WireAudit:
    def __init__(self, directory: str):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def _write(self, identity: str, side: str, record: dict) -> None:
        try:
            (self.directory / f"{identity}-{side}.json").write_text(
                json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
        except OSError as exc:
            logging.getLogger(__name__).warning("wire audit write unavailable: %s", type(exc).__name__)

    def request(self, request: httpx.Request) -> None:
        identity = uuid.uuid4().hex
        body = request.content
        try:
            data = json.loads(body)
        except (ValueError, TypeError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        request.extensions["reviewforge_wire"] = (identity, data.get("stream", False))
        tools = data.get("tools", [])
        self._write(
            identity,
            "request",
            {
                "method": request.method,
                "host": request.url.host,
                "path": request.url.path,
                "body_sha256": _hash(body),
                "model": data.get("model"),
                "tools": tools,
                "tool_choice": data.get("tool_choice"),
                "message_count": len(data.get("messages", [])),
                "messages_sha256": _hash(json.dumps(data.get("messages", []), sort_keys=True).encode()),
            },
        )

    def response(self, response: httpx.Response) -> None:
        identity, streaming = response.request.extensions.get("reviewforge_wire", ("", True))
        if not identity or streaming:
            return
        self.save_response(identity, response.status_code, response.read())

    async def async_request(self, request: httpx.Request) -> None:
        self.request(request)

    async def async_response(self, response: httpx.Response) -> None:
        identity, streaming = response.request.extensions.get("reviewforge_wire", ("", True))
        if identity and not streaming:
            self.save_response(identity, response.status_code, await response.aread())

    def save_response(self, identity: str, status: int, body: bytes) -> None:
        try:
            data = json.loads(body)
        except (ValueError, TypeError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        choices = []
        raw_choices = data.get("choices") or []
        for choice in raw_choices if isinstance(raw_choices, list) else []:
            if not isinstance(choice, dict):
                continue
            message = choice.get("message") or {}
            if not isinstance(message, dict):
                continue
            choices.append(
                {
                    "finish_reason": choice.get("finish_reason"),
                    "tool_names": [call.get("function", {}).get("name") for call in message.get("tool_calls") or []],
                    "content_sha256": _hash(str(message.get("content") or "").encode()),
                }
            )
        usage = data.get("usage") or {}
        if not isinstance(usage, dict):
            usage = {}
        self._write(
            identity,
            "response",
            {
                "status": status,
                "body_sha256": _hash(body),
                "choices": choices,
                "usage": {
                    key: value
                    for key, value in usage.items()
                    if key in {"prompt_tokens", "completion_tokens", "total_tokens"} and type(value) is int
                },
            },
        )


def client_options() -> dict:
    directory = os.environ.get("REVIEWFORGE_WIRE_AUDIT_DIR", "")
    if not directory:
        return {}
    audit = WireAudit(directory)
    return {
        "http_client": httpx.Client(
            timeout=120, event_hooks={"request": [audit.request], "response": [audit.response]}
        ),
        "http_async_client": httpx.AsyncClient(
            timeout=120,
            event_hooks={
                "request": [audit.async_request],
                "response": [audit.async_response],
            },
        ),
    }
