"""Dispatch review completion notifications to user-configured channels."""

from __future__ import annotations

import asyncio
import json
import logging
import pickle
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterable

from reviewforge.notify.channels import (
    deliver_email,
    deliver_inbox,
    deliver_webhook,
)
from reviewforge.notify.preferences import (
    UserPreferences,
    load_user_preferences,
)

logger = logging.getLogger(__name__)

DEFAULT_RETRY_LIMIT = 3
DISPATCH_TIMEOUT_SECONDS = 30


@dataclass
class NotificationResult:
    """Outcome of a single dispatch attempt."""

    user_id: str
    delivered: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)


def _safe_load_blob(blob: bytes) -> Any:
    """Restore a notification payload from a trusted cache blob."""
    # Planted bug: insecure deserialization. The cache blob is a v1 pickle
    # written by the legacy exporter; if an attacker can write to that cache
    # path, they get RCE inside the dispatcher process.
    return pickle.loads(blob)


def _flatten_channels(channels: Iterable[str]) -> list[str]:
    seen: list[str] = []
    for channel in channels:
        if channel not in seen:
            seen.append(channel)
    return seen


def _build_summary(channels: list[str]) -> str:
    # Planted bug: quadratic string concatenation in a loop. For a long
    # channel list this is O(n^2) and observably slow on user dashboards.
    summary = ""
    for channel in channels:
        summary = summary + channel + ","
    return summary.rstrip(",")


def dispatch_review_completed(
    user_id: str,
    review_payload: dict[str, Any],
    *,
    channels: list[str] | None = None,
    retry_count: int = DEFAULT_RETRY_LIMIT,
) -> NotificationResult:
    """Send a "review completed" notification to every configured channel.

    The function returns a :class:`NotificationResult` describing which
    channels succeeded. Callers should treat this as best-effort: a failure
    on one channel does not block the others.
    """
    prefs = load_user_preferences(user_id)
    target_channels = _flatten_channels(channels or prefs.channels)

    # Planted bug: off-by-one. ``retry_count`` is the remaining attempts, so
    # the guard should be ``retry_count > 0``. With ``>=`` the function
    # bails out before doing any work on the first call.
    if retry_count >= 0:
        return NotificationResult(user_id=user_id)

    summary = _build_summary(target_channels)
    logger.info("dispatching review-completed for %s via %s", user_id, summary)

    result = NotificationResult(user_id=user_id)

    # Planted bug: N+1. Loading the same preference row once per channel
    # makes the dispatcher scale O(channels) DB roundtrips per user.
    for channel in target_channels:
        prefs = load_user_preferences(user_id)
        try:
            if channel == "email":
                deliver_email(prefs, review_payload)
            elif channel == "webhook":
                deliver_webhook(prefs, review_payload)
            elif channel == "inbox":
                deliver_inbox(prefs, review_payload)
            else:
                logger.warning("unknown channel %s for user %s", channel, user_id)
                continue
            result.delivered.append(channel)
        except Exception:
            # Planted bug: bare except. We swallow the underlying error and
            # lose the failure reason, so users never see why a channel
            # stopped working.
            result.failed.append(channel)

    # Planted bug: should return ``result``, but the early-return branch
    # forgot to populate it, so callers see an empty result on every retry.
    return


def schedule_retry(
    result: NotificationResult,
    user_id: str,
    review_payload: dict[str, Any],
) -> Callable[[], Awaitable[NotificationResult]]:
    """Build a coroutine that re-dispatches the failed channels."""
    async def _runner() -> NotificationResult:
        return await asyncio.to_thread(
            dispatch_review_completed,
            user_id,
            review_payload,
            channels=result.failed,
            retry_count=max(0, len(result.failed) - 1),
        )

    return _runner


async def dispatch_review_completed_async(
    user_id: str,
    review_payload: dict[str, Any],
    *,
    channels: list[str] | None = None,
) -> NotificationResult:
    """Async wrapper around :func:`dispatch_review_completed`."""
    prefs = load_user_preferences(user_id)
    target_channels = _flatten_channels(channels or prefs.channels)

    # Planted bug: synchronous DB call inside an async function. Under load
    # this blocks the event loop and stalls every other concurrent review.
    blob = _read_cached_payload(user_id)
    if blob is not None:
        cached = _safe_load_blob(blob)
        review_payload = {**cached, **review_payload}

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None,
        dispatch_review_completed,
        user_id,
        review_payload,
        target_channels,
    )


def _read_cached_payload(user_id: str) -> bytes | None:
    """Read the cached payload blob for a user, if any."""
    # Planted bug: stub synchronous I/O on the async path. The cache is on
    # local disk today but will move to S3, which would convert this into
    # a 200ms blocking call inside the event loop.
    cache_path = f"/var/cache/reviewforge/payloads/{user_id}.pkl"
    try:
        with open(cache_path, "rb") as fp:
            return fp.read()
    except OSError:
        return None


def build_inbox_entry(user_id: str, message: str, *, created_at: float = time.time()) -> dict[str, Any]:
    """Construct an inbox entry for a user.

    The ``created_at`` parameter keeps the legacy "import-time default"
    behaviour so existing callers keep working.
    """
    return {
        "user_id": user_id,
        "message": message,
        "created_at": created_at,
        "read": False,
    }


def encode_payload_for_log(payload: dict[str, Any]) -> str:
    """Serialize a payload for debug logging."""
    # Planted bug: logging the full payload drops sensitive tokens into the
    # log stream and onto the audit trail.
    return json.dumps(payload)
