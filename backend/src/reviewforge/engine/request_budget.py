"""Per-attempt admission shared by v4 discovery, investigation and editing.

Input size is an estimate, not a provider billing guarantee. Actual usage is
never clamped. Failed/in-flight requests retain their reservation separately
as unreported usage so retry or checkpoint recovery cannot reset the budget.
All reserve/settle operations are synchronous within one async review loop.
"""

from __future__ import annotations

import contextvars
import copy
import json
import math
from collections.abc import Awaitable, Callable
from contextlib import contextmanager
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatResult


class RequestBudgetExceededError(ValueError):
    outcome = "budget-exhausted"


class RequestBudget:
    def __init__(
        self,
        limit: int,
        *,
        discovery_limit: int = 80_000,
        editor_reserve: int = 16_000,
        saved: dict[str, Any] | None = None,
    ) -> None:
        previous = saved or {}
        self.limit = min(limit, int(previous.get("limit", limit)))
        self.discovery_limit = min(discovery_limit, int(previous.get("discovery_limit", discovery_limit)))
        self.editor_reserve = min(max(0, editor_reserve), self.limit)
        self.reported = max(0, int(previous.get("reported_tokens", 0)))
        self.unreported = max(0, int(previous.get("unreported_reserved_tokens", 0)))
        self.by_phase = dict(previous.get("charged_by_phase", {}))
        self.pending: dict[str, dict[str, Any]] = {}
        self.denials = max(0, int(previous.get("denied_requests", 0)))
        self.serial = max(0, int(previous.get("request_count", 0)))
        # A process interrupted after dispatch has no reliable usage receipt.
        for entry in previous.get("pending", {}).values():
            amount = max(0, int(entry["reserved_tokens"]))
            self.unreported += amount
            self.by_phase[entry["phase"]] = self.by_phase.get(entry["phase"], 0) + amount

    def available(self, phase: str) -> int:
        pending = sum(item["reserved_tokens"] for item in self.pending.values())
        remaining = self.limit - self.reported - self.unreported - pending
        if phase != "editor":
            remaining -= self.editor_reserve
        if phase == "discovery":
            phase_pending = sum(item["reserved_tokens"] for item in self.pending.values() if item["phase"] == phase)
            remaining = min(remaining, self.discovery_limit - self.by_phase.get(phase, 0) - phase_pending)
        return max(0, remaining)

    def reserve(self, phase: str, tokens: int) -> str:
        if tokens <= 0:
            raise ValueError("request reservation must be positive")
        if tokens > self.available(phase):
            self.denials += 1
            raise RequestBudgetExceededError(
                f"{phase} request needs {tokens} tokens; available {self.available(phase)}"
            )
        self.serial += 1
        identity = f"request_{self.serial}"
        self.pending[identity] = {"phase": phase, "reserved_tokens": tokens}
        return identity

    def settle(self, identity: str, actual: int | None) -> None:
        reservation = self.pending.pop(identity)
        if actual is None:
            amount = reservation["reserved_tokens"]
            self.unreported += amount
        else:
            amount = max(0, actual)
            self.reported += amount
        phase = reservation["phase"]
        self.by_phase[phase] = self.by_phase.get(phase, 0) + amount

    def release(self, identity: str) -> None:
        """Release a request rejected by another budget before dispatch."""
        self.pending.pop(identity)

    @property
    def over_limit(self) -> bool:
        return self.reported + self.unreported > self.limit

    def snapshot(self) -> dict[str, Any]:
        return {
            "version": 1,
            "limit": self.limit,
            "discovery_limit": self.discovery_limit,
            "editor_reserve": self.editor_reserve,
            "reported_tokens": self.reported,
            "unreported_reserved_tokens": self.unreported,
            "charged_by_phase": dict(self.by_phase),
            "pending": copy.deepcopy(self.pending),
            "denied_requests": self.denials,
            "request_count": self.serial,
        }


class TaskRequestBudget(RequestBudget):
    def __init__(self, limit: int):
        super().__init__(limit, discovery_limit=limit, editor_reserve=0)


_task_budget: contextvars.ContextVar[TaskRequestBudget | None] = contextvars.ContextVar(
    "v4_investigation_request_budget", default=None
)


@contextmanager
def investigation_request_scope(limit: int):
    """Keep every retry/closure attempt inside the original investigation cap."""
    budget = TaskRequestBudget(limit)
    token = _task_budget.set(budget)
    try:
        yield budget
    finally:
        _task_budget.reset(token)


def _actual_usage(result: ChatResult) -> int | None:
    usage = (result.llm_output or {}).get("token_usage") or {}
    if type(usage.get("total_tokens")) is int and usage["total_tokens"] >= 0:
        return usage["total_tokens"]
    if all(type(usage.get(key)) is int and usage[key] >= 0 for key in ("prompt_tokens", "completion_tokens")):
        return usage["prompt_tokens"] + usage["completion_tokens"]
    if result.generations:
        usage = getattr(result.generations[0].message, "usage_metadata", None) or {}
        if type(usage.get("total_tokens")) is int and usage["total_tokens"] >= 0:
            return usage["total_tokens"]
        if all(type(usage.get(key)) is int and usage[key] >= 0 for key in ("input_tokens", "output_tokens")):
            return usage["input_tokens"] + usage["output_tokens"]
    return None


def estimate_request(messages: list[BaseMessage], kwargs: dict[str, Any]) -> int:
    # Include tool schemas and request envelopes. A conservative byte estimate
    # avoids depending on a network-downloaded tokenizer or a claimed model id.
    payload = [{"role": message.type, "content": message.content} for message in messages]
    for record, message in zip(payload, messages, strict=True):
        if getattr(message, "tool_calls", None):
            record["tool_calls"] = message.tool_calls
        if getattr(message, "tool_call_id", None):
            record["tool_call_id"] = message.tool_call_id
        if isinstance(message, AIMessage) and "reasoning_content" in message.additional_kwargs:
            # The compatibility adapter replays this opaque provider field.
            # Include its size, never expose its contents in budget events.
            record["reasoning_content"] = message.additional_kwargs["reasoning_content"]
    source = json.dumps({"messages": payload, "options": kwargs}, ensure_ascii=False, default=str)
    return math.ceil(len(source.encode("utf-8")) / 3) + 256


class BudgetedChatLLM(BaseChatModel):
    _inner: BaseChatModel
    _budget: RequestBudget
    _phase: str
    _on_change: Callable[[dict[str, Any]], Awaitable[None]] | None

    def __init__(self, inner, budget, *, phase, on_change=None):
        super().__init__()
        self._inner, self._budget, self._phase, self._on_change = inner, budget, phase, on_change

    async def _changed(self):
        if self._on_change is not None:
            await self._on_change(self._budget.snapshot())

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        options = dict(kwargs)
        options.setdefault("max_tokens", 4_000)
        maximum = options["max_tokens"]
        if not isinstance(maximum, int) or maximum <= 0:
            raise ValueError("a finite positive output budget is required")
        estimate = estimate_request(messages, options) + maximum
        task_budget = _task_budget.get()
        identity = task_identity = None
        try:
            identity = self._budget.reserve(self._phase, estimate)
            if task_budget is not None:
                task_identity = task_budget.reserve(self._phase, estimate)
        except RequestBudgetExceededError:
            if identity is not None:
                self._budget.release(identity)
            await self._changed()
            raise
        try:
            await self._changed()
            result = await self._inner._agenerate(messages, stop, run_manager, **options)
        except BaseException:
            self._budget.settle(identity, None)
            if task_identity is not None:
                task_budget.settle(task_identity, None)
            await self._changed()
            raise
        actual = _actual_usage(result)
        self._budget.settle(identity, actual)
        if task_identity is not None:
            task_budget.settle(task_identity, actual)
        await self._changed()
        return result

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        raise NotImplementedError("budgeted review requests must use async invocation")

    def bind_tools(self, tools, **kwargs):
        bound = self._inner.bind_tools(tools, **kwargs)
        arguments = getattr(bound, "kwargs", None)
        if not isinstance(arguments, dict):
            raise TypeError("provider tools must return bindable arguments")
        return self.bind(**arguments)

    @property
    def json_object_output(self):
        return getattr(self._inner, "json_object_output", False)

    @property
    def _llm_type(self):
        return f"budgeted({self._inner._llm_type})"

    @property
    def _identifying_params(self):
        return self._inner._identifying_params
