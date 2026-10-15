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
from dataclasses import dataclass
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatResult
from langchain_core.runnables.base import RunnableBindingBase


class RequestBudgetExceededError(ValueError):
    outcome = "budget-exhausted"


class RequestBudget:
    def __init__(
        self,
        limit: int | None,
        *,
        discovery_limit: int = 80_000,
        editor_reserve: int = 16_000,
        saved: dict[str, Any] | None = None,
        adaptive: bool = False,
    ) -> None:
        previous = saved or {}
        # Allocation is frozen with the run. Historical checkpoints keep their
        # original phase limits; restoring them must not grant more credit.
        self.metered = previous.get("allocation_policy") == "usage-only-v1" if previous else limit is None
        self.adaptive = (
            previous.get("allocation_policy") == "adaptive-v1" if previous else adaptive
        ) and not self.metered
        old_limit = previous.get("limit", limit)
        self.limit = None if self.metered else minimum_limit(limit, old_limit)
        if not self.metered and self.limit is None:
            raise ValueError("a capped budget requires its frozen limit")
        self.discovery_limit = (
            None if self.metered else min(discovery_limit, int(previous.get("discovery_limit", discovery_limit)))
        )
        self.editor_reserve = (
            0 if self.metered else min(max(0, int(previous.get("editor_reserve", editor_reserve))), self.limit)
        )
        self.investigation_reserve = max(0, int(previous.get("investigation_reserve", 0))) if self.adaptive else 0
        self.allocation: dict[str, Any] = copy.deepcopy(previous.get("allocation", {})) if self.adaptive else {}
        self.reported = max(0, int(previous.get("reported_tokens", 0)))
        self.unreported = max(0, int(previous.get("unreported_reserved_tokens", 0)))
        self.by_phase = dict(previous.get("charged_by_phase", {}))
        self.task_usage = copy.deepcopy(previous.get("charged_by_task", {}))
        self.pending: dict[str, dict[str, Any]] = {}
        self.denials = max(0, int(previous.get("denied_requests", 0)))
        self.serial = max(0, int(previous.get("request_count", 0)))
        self.unreported_requests = copy.deepcopy(previous.get("unreported_requests", {}))
        self.reconciliations = copy.deepcopy(previous.get("reconciliations", {}))
        self.external_binding = copy.deepcopy(previous.get("external_budget", {}))
        self.external = None
        # A process interrupted after dispatch has no reliable usage receipt.
        for identity, entry in previous.get("pending", {}).items():
            amount = max(0, int(entry["reserved_tokens"]))
            self.unreported += amount
            self.by_phase[entry["phase"]] = self.by_phase.get(entry["phase"], 0) + amount
            self.unreported_requests[identity] = copy.deepcopy(entry)
            self._charge_task(entry, amount, known=False)

    def _charge_task(self, entry: dict[str, Any], amount: int, *, known: bool) -> None:
        if not entry.get("task_id"):
            return
        usage = self.task_usage.setdefault(
            entry["task_id"], {"reported_tokens": 0, "unreported_reserved_tokens": 0, "steps": 0}
        )
        key = "reported_tokens" if known else "unreported_reserved_tokens"
        usage[key] += amount
        usage["steps"] = max(usage["steps"], entry.get("steps", 0))

    def allocate(self, *, verification: int, investigation: int, basis: dict[str, Any]) -> None:
        """Protect forecast work, without spending it or changing a hard cap."""
        if not self.adaptive:
            return
        self.editor_reserve = min(self.limit, max(0, verification))
        self.investigation_reserve = min(self.limit, max(0, investigation))
        self.allocation = copy.deepcopy(basis)

    def bind_external(self, scope) -> None:
        """Bind an authorization envelope before allocating any stage credit.

        The grant is a ceiling on the same currency, not another phase budget.
        A saved grant cannot disappear or be replaced on checkpoint recovery.
        """
        if scope is None:
            if self.external_binding:
                raise ValueError("saved external budget scope is required")
            return
        binding = {"id": scope.identity, "limit": scope.budget.limit}
        if scope.budget is self or scope.budget.external is not None:
            raise ValueError("external budget must be an independent root envelope")
        if self.external_binding and (
            self.external_binding["id"] != scope.identity or scope.budget.limit > self.external_binding["limit"]
        ):
            raise ValueError("external budget identity or frozen limit changed")
        if self.external is not None and self.external is not scope:
            raise ValueError("budget is already bound to another live scope")
        self.external_binding, self.external = binding, scope

    def bind_current_external(self) -> None:
        """Persist authorization identity even before the first model is built."""
        self.bind_external(_external_budget.get())

    def available(self, phase: str) -> int | None:
        """None means metering without a token ceiling, never infinite credit."""
        pending = sum(item["reserved_tokens"] for item in self.pending.values())
        remaining = None if self.metered else self.limit - self.reported - self.unreported - pending
        if self.external_binding and self.external is None:
            return 0  # A recovered run cannot spend before its grant is restored.
        if self.external is not None:
            if not self.external.active:
                return 0
            # Intersect raw balances FIRST. min(PR - reserve, grant) would let
            # discovery consume money needed for investigation and verification.
            remaining = minimum_limit(remaining, self.external.budget.available("editor"))
        if self.metered:
            return None if remaining is None else max(0, remaining)
        if phase != "editor":
            remaining -= self.editor_reserve
        if phase == "discovery":
            if self.adaptive:
                remaining -= self.investigation_reserve
            else:
                phase_pending = sum(item["reserved_tokens"] for item in self.pending.values() if item["phase"] == phase)
                remaining = min(remaining, self.discovery_limit - self.by_phase.get(phase, 0) - phase_pending)
        return max(0, remaining)

    def reserve(self, phase: str, tokens: int, *, task_id: str = "", steps: int = 0) -> str:
        if tokens <= 0:
            raise ValueError("request reservation must be positive")
        available = self.available(phase)
        if available is not None and tokens > available:
            self.denials += 1
            raise RequestBudgetExceededError(
                f"{phase} request needs {tokens} tokens; available {self.available(phase)}"
            )
        self.serial += 1
        identity = f"request_{self.serial}"
        self.pending[identity] = {"phase": phase, "reserved_tokens": tokens}
        if task_id:
            self.pending[identity].update(task_id=task_id, steps=steps)
        return identity

    def settle(self, identity: str, actual: int | None) -> None:
        reservation = self.pending.pop(identity)
        if actual is None:
            amount = reservation["reserved_tokens"]
            self.unreported += amount
            self.unreported_requests[identity] = copy.deepcopy(reservation)
        else:
            amount = max(0, actual)
            self.reported += amount
        phase = reservation["phase"]
        self.by_phase[phase] = self.by_phase.get(phase, 0) + amount
        self._charge_task(reservation, amount, known=actual is not None)

    def release(self, identity: str) -> None:
        """Release a request rejected by another budget before dispatch."""
        self.pending.pop(identity)

    def reconcile_unknown(self, identity: str, actual: int, *, receipt_ref: str) -> None:
        """Apply an externally verified receipt for this exact physical attempt.

        The caller must verify the provider's request identity and receipt. A
        timeout, local estimate, or later retry is not a receipt. Legacy aggregate
        unknown usage has no attempt identity and cannot be refunded here.
        """
        if type(actual) is not int or actual < 0 or not isinstance(receipt_ref, str) or not receipt_ref.strip():
            raise ValueError("reconciliation requires actual usage and a verified receipt reference")
        if identity not in self.unreported_requests or receipt_ref in {
            row["receipt_ref"] for row in self.reconciliations.values()
        }:
            raise ValueError("unknown attempt or reused receipt")
        reservation = self.unreported_requests.pop(identity)
        reserved = reservation["reserved_tokens"]
        self.unreported -= reserved
        self.reported += actual
        phase = reservation["phase"]
        self.by_phase[phase] = self.by_phase.get(phase, 0) + actual - reserved
        if reservation.get("task_id"):
            usage = self.task_usage[reservation["task_id"]]
            usage["unreported_reserved_tokens"] -= reserved
            usage["reported_tokens"] += actual
        self.reconciliations[identity] = {"actual_tokens": actual, "receipt_ref": receipt_ref, **reservation}

    @property
    def over_limit(self) -> bool:
        return (self.limit is not None and self.reported + self.unreported > self.limit) or (
            self.external is not None and self.external.budget.over_limit
        )

    def snapshot(self) -> dict[str, Any]:
        external_receipt = copy.deepcopy(self.external_binding)
        if self.external is not None:
            grant = self.external.budget
            external_receipt.update(
                reported_tokens=grant.reported,
                unreported_reserved_tokens=grant.unreported,
                pending_reserved_tokens=sum(row["reserved_tokens"] for row in grant.pending.values()),
            )
        return {
            "version": 3 if self.metered else 2 if self.adaptive else 1,
            "limit": self.limit,
            "discovery_limit": self.discovery_limit,
            "editor_reserve": self.editor_reserve,
            "reported_tokens": self.reported,
            "unreported_reserved_tokens": self.unreported,
            "charged_by_phase": dict(self.by_phase),
            "charged_by_task": copy.deepcopy(self.task_usage),
            "pending": copy.deepcopy(self.pending),
            "denied_requests": self.denials,
            "request_count": self.serial,
            "unreported_requests": copy.deepcopy(self.unreported_requests),
            "reconciliations": copy.deepcopy(self.reconciliations),
            **({"allocation_policy": "usage-only-v1"} if self.metered else {}),
            **({"external_budget": external_receipt} if external_receipt else {}),
            **(
                {
                    "allocation_policy": "adaptive-v1",
                    "investigation_reserve": self.investigation_reserve,
                    "allocation": copy.deepcopy(self.allocation),
                }
                if self.adaptive
                else {}
            ),
        }


def budget_snapshot_over_limit(snapshot: dict[str, Any]) -> bool:
    """Publication/completeness must honor the same limits as admission."""
    return any(
        row.get("limit") is not None
        and row.get("reported_tokens", 0) + row.get("unreported_reserved_tokens", 0) > row["limit"]
        for row in (snapshot, snapshot.get("external_budget", {}))
    )


def minimum_limit(*limits: int | None) -> int | None:
    """Intersect actual ceilings; absence of a ceiling is represented by None."""
    finite = [value for value in limits if value is not None]
    return min(finite) if finite else None


def snapshot_available(snapshot: dict[str, Any], *, fallback: int | None) -> int | None:
    limit = snapshot.get("limit", fallback)
    available = (
        None
        if limit is None
        else max(
            0,
            limit
            - snapshot.get("reported_tokens", 0)
            - snapshot.get("unreported_reserved_tokens", 0)
            - sum(row["reserved_tokens"] for row in snapshot.get("pending", {}).values()),
        )
    )
    grant = snapshot.get("external_budget")
    if grant:
        available = minimum_limit(
            available,
            max(
                0,
                grant["limit"]
                - grant.get("reported_tokens", 0)
                - grant.get("unreported_reserved_tokens", 0)
                - grant.get("pending_reserved_tokens", 0),
            ),
        )
    return available


class TaskRequestBudget(RequestBudget):
    def __init__(self, limit: int | None, *, task_id: str = ""):
        super().__init__(limit, discovery_limit=limit if limit is not None else 0, editor_reserve=0)
        self.task_id = task_id
        self.progress_steps = 0


@dataclass
class ExternalBudgetScope:
    budget: RequestBudget
    identity: str
    on_change: Callable[[dict[str, Any]], Awaitable[None]]
    active: bool = True


_external_budget: contextvars.ContextVar[ExternalBudgetScope | None] = contextvars.ContextVar(
    "v4_external_request_budget", default=None
)


@contextmanager
def external_request_budget_scope(budget: RequestBudget, *, identity: str, on_change):
    """Share one durable grant across sequential PRs and an optional judge.

    The caller restores the grant snapshot and persists every callback before
    dispatch. Provider attempts are charged to both ledgers exactly once; these
    are two views of the same cost and must not be added together.
    """
    if budget.limit is None or not identity.strip() or not callable(on_change) or _external_budget.get() is not None:
        raise ValueError("a named, persistent, non-nested external budget is required")
    scope = ExternalBudgetScope(budget, identity, on_change)
    token = _external_budget.set(scope)
    try:
        yield budget
    finally:
        scope.active = False
        _external_budget.reset(token)


_task_budget: contextvars.ContextVar[TaskRequestBudget | None] = contextvars.ContextVar(
    "v4_investigation_request_budget", default=None
)


@contextmanager
def investigation_request_scope(limit: int | None, *, task_id: str = ""):
    """Meter every retry/closure; historical tasks retain their finite cap."""
    budget = TaskRequestBudget(limit, task_id=task_id)
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


@dataclass(frozen=True)
class RequestBudgetView:
    input_tokens: int
    available_tokens: int | None


@dataclass(frozen=True)
class RequestPlan:
    input_tokens: int
    available_tokens: int | None
    max_output_tokens: int


def plan_request(
    llm: Any,
    messages: list[BaseMessage],
    *,
    max_output_tokens: int = 4_000,
    fallback_available: int | None,
    reserve_tokens: int = 0,
) -> RequestPlan:
    """Forecast once before retries, using the same input as physical admission.

    This is a read-only snapshot, not a reservation. Concurrent requests may
    consume capacity before dispatch; the physical budget remains authoritative.
    A logical retry never replans or changes the already chosen output limit.
    """
    if max_output_tokens <= 0 or reserve_tokens < 0:
        raise ValueError("planning requires a positive output cap and nonnegative reserve")
    fallback_available = None if fallback_available is None else max(0, fallback_available)
    options = {"max_tokens": max_output_tokens}
    while isinstance(llm, RunnableBindingBase):
        options = llm.kwargs | options
        llm = llm.bound
    planner = getattr(llm, "request_budget_view", None)
    view = planner(messages, options) if callable(planner) else None
    if view is None:
        view = RequestBudgetView(estimate_request(messages, options), fallback_available)
    available = minimum_limit(view.available_tokens, fallback_available)
    output = (
        max_output_tokens
        if available is None
        else max(0, min(max_output_tokens, available - view.input_tokens - reserve_tokens))
    )
    return RequestPlan(view.input_tokens, available, output)


class BudgetedChatLLM(BaseChatModel):
    _inner: BaseChatModel
    _budget: RequestBudget
    _phase: str
    _on_change: Callable[[dict[str, Any]], Awaitable[None]] | None

    def __init__(self, inner, budget, *, phase, on_change=None):
        super().__init__()
        budget.bind_current_external()
        self._inner, self._budget, self._phase, self._on_change = inner, budget, phase, on_change

    async def _changed(self):
        if self._budget.external is not None and self._budget.external.active:
            scope = self._budget.external
            await scope.on_change(scope.budget.snapshot())
        if self._on_change is not None:
            await self._on_change(self._budget.snapshot())

    def request_budget_view(self, messages, options) -> RequestBudgetView:
        available = self._budget.available(self._phase)
        task_budget = _task_budget.get()
        if task_budget is not None:
            available = minimum_limit(available, task_budget.available(self._phase))
        return RequestBudgetView(estimate_request(messages, options), available)

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        options = dict(kwargs)
        options.setdefault("max_tokens", 4_000)
        maximum = options["max_tokens"]
        if not isinstance(maximum, int) or maximum <= 0:
            raise ValueError("a finite positive output budget is required")
        estimate = estimate_request(messages, options) + maximum
        task_budget = _task_budget.get()
        external = self._budget.external
        identity = task_identity = external_identity = None
        try:
            identity = self._budget.reserve(
                self._phase,
                estimate,
                task_id=task_budget.task_id if task_budget is not None else "",
                steps=task_budget.progress_steps if task_budget is not None else 0,
            )
            if task_budget is not None:
                task_identity = task_budget.reserve(self._phase, estimate)
            if external is not None:
                external_identity = external.budget.reserve("editor", estimate)
                self._budget.pending[identity]["external_request_id"] = external_identity
                external.budget.pending[external_identity].update(review_request_id=identity, review_phase=self._phase)
        except RequestBudgetExceededError:
            if identity is not None:
                self._budget.release(identity)
            if task_identity is not None:
                task_budget.release(task_identity)
            await self._changed()
            raise
        try:
            await self._changed()
        except BaseException:
            # A failed pre-dispatch checkpoint is known not to have contacted
            # the provider. Only dispatched/interrupted attempts are unknown.
            self._budget.release(identity)
            if task_identity is not None:
                task_budget.release(task_identity)
            if external_identity is not None:
                external.budget.release(external_identity)
            raise
        try:
            result = await self._inner._agenerate(messages, stop, run_manager, **options)
        except BaseException:
            self._budget.settle(identity, None)
            if task_identity is not None:
                task_budget.settle(task_identity, None)
            if external_identity is not None:
                external.budget.settle(external_identity, None)
            await self._changed()
            raise
        actual = _actual_usage(result)
        self._budget.settle(identity, actual)
        if task_identity is not None:
            task_budget.settle(task_identity, actual)
        if external_identity is not None:
            external.budget.settle(external_identity, actual)
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
