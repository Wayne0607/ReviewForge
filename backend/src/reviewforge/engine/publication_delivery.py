"""Durable v4 review delivery with conservative recovery of ambiguous writes."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from reviewforge.tools.github_api import GitHubAPIError


@dataclass(frozen=True)
class DeliveryOutcome:
    delivered: int = 0
    error: str = ""
    retryable: bool = False


async def deliver_saved_publication(
    database: Any, gateway: Any, state: Any, run_id: str, *, batch_id: str = ""
) -> DeliveryOutcome:
    record = await database.get_v4_publication(run_id, batch_id=batch_id)
    if record is None:
        raise ValueError("publication must be persisted before delivery")
    if record["head_sha"] != state.head_sha:
        raise ValueError("publication does not match PR head")
    payload = record["payload"]
    count = len(payload["comments"])
    if record["status"] == "delivered":
        return DeliveryOutcome(delivered=count)
    claimed = await database.claim_v4_publication(run_id, batch_id=batch_id)
    identity = f"{run_id}:{state.head_sha}" + (f":{batch_id}" if batch_id else "")
    key = hashlib.sha256(identity.encode()).hexdigest()[:24]
    # Coverage metadata belongs to the local outbox, never the tool/API payload.
    params = {
        "comments": payload["comments"],
        "body": payload["body"],
        "delivery_key": key,
        "reconcile_only": not claimed,
    }
    try:
        result = await gateway.invoke("post_review", params, state, agent_name="orchestrator")
    except GitHubAPIError as exc:
        # A 4xx response (except timeout) proves the request was rejected. A
        # lost response / server error must remain 'sending' until reconciled.
        if 400 <= exc.status_code < 500 and exc.status_code != 408:
            await database.reset_v4_publication(run_id, batch_id=batch_id)
        return DeliveryOutcome(error=f"GitHub delivery: {exc.kind}", retryable=exc.retryable)
    except Exception as exc:
        return DeliveryOutcome(error=f"delivery interrupted: {type(exc).__name__}", retryable=True)
    if (
        not isinstance(result, dict)
        or result.get("compatibility") is not False
        or result.get("delivered_indexes") != list(range(count))
        or not isinstance(result.get("review"), dict)
        or type(result["review"].get("id")) is not int
        or result["review"]["id"] <= 0
    ):
        return DeliveryOutcome(error="delivery receipt invalid or incomplete", retryable=True)
    # If this commit fails, the next attempt reconciles the submitted review.
    await database.finish_v4_publication(run_id, result["review"], batch_id=batch_id)
    return DeliveryOutcome(delivered=count)
