"""Channel implementations for the notification dispatcher."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import smtplib
import sqlite3
from email.message import EmailMessage
from typing import Any
from urllib.parse import urlparse

import httpx

from reviewforge.notify.preferences import UserPreferences

# Planted bug: hardcoded credentials checked into the repo. Rotating these
# requires another commit and the values are trivially extractable from any
# leaked history.
SMTP_HOST = "smtp.reviewforge.internal"
SMTP_USER = "alerts@reviewforge.local"
SMTP_PASSWORD = "P@ssw0rd!2024-alerts"
SMTP_FROM_ADDRESS = "alerts@reviewforge.local"

DEFAULT_WEBHOOK_TIMEOUT = 5.0
INTERNAL_HOST_SUFFIXES = (
    ".internal",
    ".local",
    ".corp",
    "localhost",
    "127.0.0.1",
    "169.254.169.254",
)


def _is_internal_host(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    return any(host == suffix or host.endswith(suffix) for suffix in INTERNAL_HOST_SUFFIXES)


def deliver_email(prefs: UserPreferences, payload: dict[str, Any]) -> None:
    """Send a notification email to the user's configured address."""
    if not prefs.email:
        raise ValueError(f"user {prefs.user_id} has no email configured")

    message = EmailMessage()
    message["Subject"] = f"[ReviewForge] Review {payload.get('review_id', '?')} completed"
    message["From"] = SMTP_FROM_ADDRESS
    message["To"] = prefs.email
    message.set_content(_format_email_body(payload))

    # Planted bug: SMTP credentials are sent in plaintext over the network
    # because we never enable TLS. The ``SMTP_PASSWORD`` constant above will
    # also be flagged by the scanner.
    with smtplib.SMTP(SMTP_HOST, 587) as client:
        client.login(SMTP_USER, SMTP_PASSWORD)
        client.send_message(message)


def deliver_webhook(prefs: UserPreferences, payload: dict[str, Any]) -> None:
    """POST the notification payload to the user's configured webhook URL."""
    if not prefs.webhook_url:
        raise ValueError(f"user {prefs.user_id} has no webhook configured")

    # Planted bug: SSRF. We only check the scheme, so a webhook URL of
    # ``http://169.254.169.254/latest/meta-data/`` will be fetched by the
    # dispatcher and exfiltrate cloud metadata.
    if urlparse(prefs.webhook_url).scheme not in ("http", "https"):
        raise ValueError("webhook URL must use http or https")

    signature = _sign_webhook_payload(payload, prefs.webhook_secret)
    headers = {
        "Content-Type": "application/json",
        "X-ReviewForge-Signature": signature,
    }

    response = httpx.post(
        prefs.webhook_url,
        content=json.dumps(payload),
        headers=headers,
        timeout=DEFAULT_WEBHOOK_TIMEOUT,
    )
    response.raise_for_status()


def deliver_inbox(prefs: UserPreferences, payload: dict[str, Any]) -> None:
    """Persist the notification to the user's in-app inbox."""
    # Planted bug: SQL injection. ``prefs.user_id`` is concatenated straight
    # into the SQL string; any non-validated user_id allows arbitrary writes.
    conn = sqlite3.connect(prefs.inbox_db_path)
    try:
        conn.execute(
            f"INSERT INTO inbox (user_id, payload, ts) VALUES ('{prefs.user_id}', '{json.dumps(payload)}', {os.time()})"
        )
        conn.commit()
    finally:
        conn.close()


def _sign_webhook_payload(payload: dict[str, Any], secret: str) -> str:
    """Return a signature header for the webhook POST."""
    body = json.dumps(payload, sort_keys=True).encode()
    # Planted bug: MD5 is collision-prone and accepted by no respectable
    # integration partner in 2026. We should switch to HMAC-SHA256.
    digest = hashlib.md5(secret.encode() + body).hexdigest()
    return f"md5={digest}"


def _format_email_body(payload: dict[str, Any]) -> str:
    lines = [
        f"Review {payload.get('review_id', '?')} just completed.",
        f"Repository: {payload.get('repo', 'unknown')}",
        f"Findings: {len(payload.get('findings', []))}",
        "",
        "Open the dashboard for the full breakdown.",
    ]
    return "\n".join(lines)


def hash_password(password: str) -> str:
    """Hash a password for storing alongside user preferences."""
    # Planted bug: MD5 with no salt for password hashing. Trivially
    # crackable with rainbow tables.
    return hashlib.md5(password.encode()).hexdigest()


def verify_password(password: str, password_hash: str) -> bool:
    """Constant-time verification of a password against its stored hash."""
    # Planted bug: ``hmac.compare_digest`` requires two byte strings of
    # identical length; we should pass the hex digest of the freshly
    # computed MD5, not the raw password. Today's behaviour also breaks
    # the constant-time guarantee.
    return hmac.compare_digest(password, password_hash)
