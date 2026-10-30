"""Durable protocol-11 discovery/investigation interleaving.

Discovery receipts yield to investigation before the next discovery request.
There is one active model phase at a time; this is deliberately not background
concurrency. The original investigator owns task transactions and verdicts.
"""

from __future__ import annotations

import asyncio
import copy
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from reviewforge.engine.hypothesis import Hypothesis, HypothesisLedger, HypothesisStatus
from reviewforge.engine.review_session import fingerprint

_PROTOCOL = "interleaved-review-v1"
_STATES = {"queued", "running", "completed", "interrupted"}


def unfinished_discovery_units(ledger: HypothesisLedger) -> set[str]:
    """A cancelled receipt must not turn unvisited persisted units into clean coverage."""
    if ledger.protocol_version < 11:
        return set()
    return {
        unit_id
        for block in ledger.discovery_blocks.values()
        for unit_id in set(block.get("unit_ids", ())) | set(block.get("units", {}))
        if not block.get("units", {}).get(unit_id, {}).get("finished")
    }


def candidate_digest(item: Hypothesis) -> str:
    """Bind the original case, excluding investigator-owned mutable results.

    Repeated discovery may add another source site for the same literal case.
    Its source/publication fingerprint still changes and must be proved again;
    that enrichment must not silently start a second semantic investigation.
    """
    return fingerprint(
        {
            key: getattr(item, key)
            for key in ("identity", "unit_id", "claim", "trigger", "impact", "open_question", "refutation", "mechanism")
        }
        | {"change_anchor": item.change_anchor.to_dict() if item.change_anchor is not None else None}
    )


def _binding(ledger: HypothesisLedger) -> dict[str, str]:
    return {key: getattr(ledger, key) for key in ("run_id", "head_sha", "workspace_digest")}


def validate_streaming_review(ledger: HypothesisLedger) -> None:
    state = ledger.streaming_review
    if not state:
        return
    if ledger.protocol_version < 11 or (
        not isinstance(state, dict)
        or set(state) != {"protocol", "binding", "candidates"}
        or state["protocol"] != _PROTOCOL
        or state["binding"] != _binding(ledger)
        or not isinstance(state["candidates"], dict)
    ):
        raise ValueError("invalid-streaming-review-binding")
    for identity, row in state["candidates"].items():
        item = ledger.items.get(identity)
        if (
            item is None
            or not isinstance(row, dict)
            or set(row) != {"candidate_digest", "state", "task_id", "error"}
            or row.get("candidate_digest") != candidate_digest(item)
            or row.get("state") not in _STATES
            or not isinstance(row.get("task_id"), str)
            or not isinstance(row.get("error"), str)
        ):
            raise ValueError("invalid-streaming-review-candidate")


class StreamingReviewScheduler:
    """Claim each case once per execution; restore unfinished original tasks."""

    def __init__(
        self,
        ledger: HypothesisLedger,
        investigate: Callable[[set[str]], Awaitable[Any]],
        checkpoint: Callable[[HypothesisLedger], Awaitable[None]],
        *,
        emit: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        if ledger.protocol_version < 11:
            raise ValueError("streaming review requires protocol 11")
        self.ledger, self._investigate, self._checkpoint = ledger, investigate, checkpoint
        self._emit = emit
        self._attempted: set[str] = set()
        self._lock = asyncio.Lock()
        with ledger._lock:
            validate_streaming_review(ledger)
            if not ledger.streaming_review:
                ledger.streaming_review = {"protocol": _PROTOCOL, "binding": _binding(ledger), "candidates": {}}
            for identity, row in ledger.streaming_review["candidates"].items():
                item = ledger.items[identity]
                if item.status == HypothesisStatus.UNKNOWN and item.retryable:
                    # Only a new scheduler execution retries a known operational
                    # interruption, preserving the investigator's saved task.
                    item.status = HypothesisStatus.OPEN
                row["state"] = "queued" if item.status == HypothesisStatus.OPEN else "completed"

    async def _save(self, identity: str) -> None:
        row = self.ledger.streaming_review["candidates"][identity]
        row["task_id"] = self.ledger.items[identity].investigation_task_id
        await self._checkpoint(self.ledger)
        if self._emit is not None:
            self._emit("streaming_review.transition", {"identity": identity, **copy.deepcopy(row)})

    async def on_candidates(self, current: HypothesisLedger, identities: Iterable[str]) -> None:
        if current is not self.ledger:
            raise ValueError("streaming review callback ledger mismatch")
        identities = tuple(dict.fromkeys(identities))
        if any(identity not in current.items for identity in identities):
            raise ValueError("unknown streaming review candidate")
        async with self._lock:
            validate_streaming_review(current)
            for identity in identities:
                item = current.items[identity]
                row = current.streaming_review["candidates"].setdefault(
                    identity,
                    {
                        "candidate_digest": candidate_digest(item),
                        "state": "queued",
                        "task_id": "",
                        "error": "",
                    },
                )
                if item.status != HypothesisStatus.OPEN:
                    row["state"] = "completed"
                    await self._save(identity)
                    continue
                if identity in self._attempted:
                    continue
                # Persist the queue and claim before issuing model requests.
                await self._save(identity)
                self._attempted.add(identity)
                row.update(state="running", error="")
                await self._save(identity)
                try:
                    await self._investigate({identity})
                except asyncio.CancelledError:
                    row.update(state="interrupted", error="cancelled")
                    await self._save(identity)
                    raise
                except Exception as exc:
                    # One failed task does not prevent later discovery groups
                    # or other accepted cases from running. OPEN remains partial.
                    row.update(state="interrupted", error=f"{type(exc).__name__}: {exc}")
                    current.items[identity].retryable = True
                else:
                    row.update(
                        state="completed"
                        if item.status != HypothesisStatus.OPEN and not item.retryable
                        else "interrupted"
                    )
                    if item.status == HypothesisStatus.OPEN:
                        row["error"] = "investigation returned without a verdict"
                        item.retryable = True
                await self._save(identity)

    async def drain(self) -> None:
        await self.on_candidates(self.ledger, [item.identity for item in self.ledger.open()])
