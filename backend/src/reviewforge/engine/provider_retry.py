"""Finite retries of one unchanged asynchronous provider request.

Place this outside per-attempt tracking and budget admission. SDK retries must
already be disabled; retries never replay business tools or change the prompt.
Failed attempts may have incurred unreported usage.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import math
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import anthropic
import httpx
import openai
from langchain_core.language_models import BaseChatModel
from pydantic import PrivateAttr

logger = logging.getLogger(__name__)
_attempt_var: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar("provider_attempt", default=None)


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    base_delay_seconds: float = 1.0
    max_delay_seconds: float = 4.0
    retry_404: bool = False

    def __post_init__(self) -> None:
        if type(self.max_attempts) is not int or not 1 <= self.max_attempts <= 3:
            raise ValueError("Provider requests require one to three attempts")
        if type(self.retry_404) is not bool:
            raise ValueError("404 recovery requires an explicit boolean policy")
        for delay in (self.base_delay_seconds, self.max_delay_seconds):
            if type(delay) not in {int, float} or not math.isfinite(delay) or delay < 0:
                raise ValueError("Provider retry delays must be finite and nonnegative")

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_attempts": self.max_attempts,
            "base_delay_seconds": self.base_delay_seconds,
            "max_delay_seconds": self.max_delay_seconds,
            "retry_404": self.retry_404,
        }


def current_retry_attempt() -> dict[str, Any] | None:
    """Trace linkage for the currently executing physical provider attempt."""
    current = _attempt_var.get()
    return dict(current) if current is not None else None


def _failure(exc: Exception, policy: RetryPolicy) -> tuple[bool, bool, int | None]:
    """Return provider-error/retryable/status without inspecting error bodies."""
    status = None
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
    elif isinstance(exc, (openai.APIStatusError, anthropic.APIStatusError)):
        status = exc.status_code
    if status is not None:
        return True, status == 429 or 500 <= status <= 599 or (status == 404 and policy.retry_404), status
    if isinstance(
        exc,
        (
            TimeoutError,
            ConnectionError,
            httpx.TimeoutException,
            httpx.NetworkError,
            httpx.RemoteProtocolError,
            openai.APIConnectionError,
            anthropic.APIConnectionError,
        ),
    ):
        return True, True, None
    return False, False, None


def _usage_status(result: Any) -> str:
    output = result.llm_output or {}
    if output.get("token_usage"):
        return "reported"
    if result.generations and getattr(result.generations[0].message, "usage_metadata", None):
        return "reported"
    return "unknown"


class RetryChatLLM(BaseChatModel):
    """Retry only classified transport failures; cancellation always propagates."""

    _inner: BaseChatModel = PrivateAttr()
    _policy: RetryPolicy = PrivateAttr()
    _attempt_sink: Callable[[dict[str, Any]], Awaitable[None]] | None = PrivateAttr()

    def __init__(
        self,
        inner: BaseChatModel,
        policy: RetryPolicy | None = None,
        attempt_sink: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> None:
        super().__init__()
        # Check wrappers without mutating SDK clients or the legacy router cache.
        node, visited = inner, set()
        while node is not None and id(node) not in visited:
            visited.add(id(node))
            if isinstance(node, RetryChatLLM):
                raise ValueError("Provider retry wrappers cannot be stacked")
            if getattr(node, "max_retries", 0):
                raise ValueError("Disable SDK retries before wrapping provider requests")
            for name in ("root_client", "root_async_client", "_client", "_async_client"):
                if getattr(getattr(node, name, None), "max_retries", 0):
                    raise ValueError("Disable SDK retries before wrapping provider requests")
            node = getattr(node, "_inner", None)
        self._inner = inner
        self._policy = policy or RetryPolicy()
        self._attempt_sink = attempt_sink

    async def _record(self, event: dict[str, Any]) -> None:
        # No exception text, request kwargs, endpoint keys or content.
        logger.info("Provider attempt: %s", event)
        if self._attempt_sink is not None:
            await self._attempt_sink(event)

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        request_id = uuid.uuid4().hex
        for attempt in range(1, self._policy.max_attempts + 1):
            identity = {"logical_request_id": request_id, "attempt": attempt, "max_attempts": self._policy.max_attempts}
            await self._record({**identity, "phase": "started", "usage_status": "unknown"})
            started = time.monotonic()
            token = _attempt_var.set(identity)
            try:
                result = await self._inner._agenerate(messages, stop, run_manager, **kwargs)
            except asyncio.CancelledError:
                await self._record(
                    {
                        **identity,
                        "phase": "finished",
                        "outcome": "cancelled",
                        "usage_status": "unknown",
                        "will_retry": False,
                        "elapsed_seconds": time.monotonic() - started,
                    }
                )
                raise
            except Exception as exc:
                provider, retryable, status = _failure(exc, self._policy)
                retry = retryable and attempt < self._policy.max_attempts
                delay = min(self._policy.max_delay_seconds, self._policy.base_delay_seconds * 2 ** (attempt - 1))
                await self._record(
                    {
                        **identity,
                        "phase": "finished",
                        "outcome": "provider-error" if provider else "error",
                        "error_type": type(exc).__name__,
                        "status_code": status,
                        "retryable": retryable,
                        "will_retry": retry,
                        "delay_seconds": delay if retry else 0,
                        "usage_status": "unknown",
                        "elapsed_seconds": time.monotonic() - started,
                    }
                )
                if not retry:
                    raise
            else:
                await self._record(
                    {
                        **identity,
                        "phase": "finished",
                        "outcome": "success",
                        "usage_status": _usage_status(result),
                        "will_retry": False,
                        "elapsed_seconds": time.monotonic() - started,
                    }
                )
                return result
            finally:
                _attempt_var.reset(token)
            # No thread or SDK retry survives cancellation during backoff.
            await asyncio.sleep(delay)
        raise AssertionError("Provider retry loop exhausted without an outcome")

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        raise NotImplementedError("Provider recovery requires asynchronous invocation")

    def bind_tools(self, tools, **kwargs):
        bound = self._inner.bind_tools(tools, **kwargs)
        provider_kwargs = getattr(bound, "kwargs", None)
        if not isinstance(provider_kwargs, dict):
            raise TypeError("Provider bind_tools did not return bindable request arguments")
        return self.bind(**provider_kwargs)

    @property
    def json_object_output(self) -> bool:
        return bool(getattr(self._inner, "json_object_output", False))

    @property
    def _llm_type(self):
        return f"retry({self._inner._llm_type})"

    @property
    def _identifying_params(self):
        return {"provider_retry": self._policy.to_dict()}
