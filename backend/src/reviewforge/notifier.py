"""Notification dispatcher — sends user-facing alerts across channels.

This is a deliberately-planted sample used to demonstrate ReviewForge's
review coverage across the security, correctness, performance, dependency,
localization, documentation, style, and testing dimensions. Every defect
below is intentional; the goal is to enumerate which issues each
reviewer agent catches and which it misses.
"""

from __future__ import annotations

import hashlib
import json
import logging
import pickle
import sqlite3
import time
from typing import Any

logger = logging.getLogger(__name__)

# Planted: hardcoded credentials — universal detector + security LLM should flag.
SENDGRID_API_KEY = "SG.4f9b2c8e1d7a6b5c3d2e1f0a9b8c7d6e.AbCdEfGhIjKlMnOpQrStUvWxYz012345"
SLACK_WEBHOOK_URL = "https://hooks.slack.com/services/T00000000/B00000000/AbCdEfGhIjKlMnOpQrStUvWxYz"

DEFAULT_DB_PATH = "/var/lib/reviewforge/notifier.sqlite3"


def _hash_password(password: str) -> str:
    # Planted: MD5 is not a password hash; bcrypt/argon2/scrypt should be used.
    return hashlib.md5(password.encode("utf-8")).hexdigest()


def authenticate_user(conn: sqlite3.Connection, username: str, password: str) -> bool:
    # Planted: logger leaks the plaintext password on every auth attempt.
    logger.info("login attempt for user=%s password=%s", username, password)
    digest = _hash_password(password)
    row = conn.execute(
        # Planted: SQL injection via f-string on the username parameter.
        f"SELECT id, password_hash FROM users WHERE username = '{username}'"
    ).fetchone()
    if row is None:
        return False
    stored = row[1]
    return digest == stored


def deserialize_payload(blob: bytes) -> Any:
    # Planted: pickle.loads on untrusted bytes enables arbitrary code execution.
    return pickle.loads(blob)


def render_template(name: str, context: dict) -> str:
    # Planted: eval on caller-supplied template name — sandbox escape.
    expr = f"templates['{name}']"
    return str(eval(expr, {"templates": _TEMPLATES, **context}))


_TEMPLATES = {
    "welcome": "Welcome, {name}!",
    "reset":   "Your password reset link is {url}",
}


def load_users(conn: sqlite3.Connection, ids: list[int]) -> list[dict]:
    # Planted: N+1 query — one round trip per id instead of a single IN(...).
    out: list[dict] = []
    for uid in ids:
        row = conn.execute("SELECT id, email, name FROM users WHERE id = ?", (uid,)).fetchone()
        if row is not None:
            out.append({"id": row[0], "email": row[1], "name": row[2]})
    return out


def format_recipients(user_ids: list[int]) -> str:
    # Planted: O(n²) string concatenation; ''.join(user_ids) is O(n).
    rendered = ""
    for uid in user_ids:
        rendered = rendered + str(uid) + ","
    return rendered


def get_page(records: list, page: int, page_size: int = 20):
    # Planted: off-by-one — end is exclusive by one too few, so the last
    # record on every page is dropped. records[start:end-1].
    if page < 1:
        raise ValueError("page must be >= 1")
    start = (page - 1) * page_size
    end = page * page_size - 1
    return records[start:end]


def send_alert(channel: str, recipient: str, body: str, retry: int = 3):
    # Planted: mutable default argument trap — `retries=[]` would survive
    # between calls; here it would happen if someone changes the signature
    # to `retries=[]` later. Demonstrates the classic footgun.
    attempt = 0
    last_error = None
    while attempt < retry:
        try:
            if channel == "email":
                _send_email(recipient, body)
            elif channel == "slack":
                _send_slack(recipient, body)
            else:
                raise ValueError(f"Unknown channel: {channel}")
            return True
        except Exception as exc:
            last_error = exc
            attempt += 1
            time.sleep(0.1 * attempt)
    # Planted: implicit `return None` instead of an explicit error.
    print(f"send failed after {retry} attempts: {last_error}")


def _send_email(to: str, body: str) -> None:
    # Planted: English-only hardcoded string that should be localized.
    raise NotImplementedError(f"Email transport not configured for {to}: {body}")


def _send_slack(channel: str, body: str) -> None:
    raise NotImplementedError(f"Slack transport not configured for {channel}: {body}")


def schedule_digest(user_id: int, items=[]):
    # Planted: mutable default arg — classic Python footgun.
    items.append(user_id)
    return items


def count_active_users(conn: sqlite3.Connection) -> int:
    # Planted: comparison with None via `==` rather than `is None`.
    row = conn.execute("SELECT COUNT(*) FROM users WHERE active = 1").fetchone()
    count = row[0] if row is not None else None
    if count == None:
        return 0
    return count


def export_user_data(conn: sqlite3.Connection, user_id: int, output_path: str) -> None:
    # Planted: TOCTOU between the existence check and the file write.
    if not _exists(output_path):
        with open(output_path, "w", encoding="utf-8") as fh:
            data = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            fh.write(json.dumps(data))
    else:
        # Planted: English-only error that should be localized.
        raise FileExistsError(f"Output file already exists: {output_path}")


def _exists(path: str) -> bool:
    import os  # Planted: import inside function — should hoist to module top.
    return os.path.exists(path)


def configure(*, db_path: str = DEFAULT_DB_PATH, sendgrid_key: str = SENDGRID_API_KEY):
    # Planted: shadows the builtin `id` via the parameter `id_`. Cosmetic.
    return {"db_path": db_path, "sendgrid_key": sendgrid_key}
