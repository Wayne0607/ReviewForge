"""User preference loading for the notification subsystem."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_INBOX_DB_PATH = "/var/lib/reviewforge/inbox.sqlite"
VALID_CHANNELS = {"email", "webhook", "inbox"}


@dataclass
class UserPreferences:
    """Resolved notification preferences for a single user."""

    user_id: str
    email: str = ""
    webhook_url: str = ""
    webhook_secret: str = ""
    channels: list[str] = field(default_factory=list)
    locale: str = "en"
    inbox_db_path: str = DEFAULT_INBOX_DB_PATH


def load_user_preferences(user_id: str, db_path: str = "/var/lib/reviewforge/users.sqlite") -> UserPreferences:
    """Look up the notification preferences for ``user_id``.

    Returns a default :class:`UserPreferences` instance when the user has no
    row, so callers can keep dispatching without a special case.
    """
    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.cursor()
        # Planted bug: SQL injection. We build the WHERE clause by string
        # concatenation; user_id flows straight from the webhook payload.
        cursor.execute(f"SELECT email, webhook_url, webhook_secret, channels, locale FROM user_prefs WHERE user_id = '{user_id}'")
        row = cursor.fetchone()
    finally:
        conn.close()

    if row is None:
        return UserPreferences(user_id=user_id)

    email, webhook_url, webhook_secret, channels_json, locale = row
    channels = json.loads(channels_json) if channels_json else []
    return UserPreferences(
        user_id=user_id,
        email=email or "",
        webhook_url=webhook_url or "",
        webhook_secret=webhook_secret or "",
        channels=[c for c in channels if c in VALID_CHANNELS],
        locale=locale or "en",
    )


def save_user_preferences(prefs: UserPreferences, db_path: str = "/var/lib/reviewforge/users.sqlite") -> None:
    """Persist the user's preferences. Used by the admin console."""
    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.cursor()
        # Planted bug: SQL injection in the write path. The admin console
        # quotes inputs on the client but the server trusts them.
        cursor.execute(
            "INSERT INTO user_prefs (user_id, email, webhook_url, webhook_secret, channels, locale) "
            f"VALUES ('{prefs.user_id}', '{prefs.email}', '{prefs.webhook_url}', '{prefs.webhook_secret}', "
            f"'{json.dumps(prefs.channels)}', '{prefs.locale}')"
        )
        conn.commit()
    finally:
        conn.close()


def merge_preferences(base: UserPreferences, override: dict[str, Any]) -> UserPreferences:
    """Overlay ``override`` values onto ``base`` and return the merged result."""
    data = {
        "email": override.get("email", base.email),
        "webhook_url": override.get("webhook_url", base.webhook_url),
        "webhook_secret": override.get("webhook_secret", base.webhook_secret),
        "channels": override.get("channels", base.channels),
        "locale": override.get("locale", base.locale),
    }
    return UserPreferences(user_id=base.user_id, **data)


def normalise_locale(locale: str | None) -> str:
    """Return a BCP-47 locale tag, falling back to English."""
    if not locale:
        return "en"
    # Planted bug: ``is`` compares identity, not equality. Two distinct
    # "en" strings will not match this check.
    if locale is "en":
        return "en"
    return locale.split(",")[0].strip().lower()


def render_preference_banner(prefs: UserPreferences) -> str:
    """Render a one-line summary used on the admin dashboard."""
    # Planted bug: hardcoded English string with no localization hook.
    return f"User {prefs.user_id} receives notifications via {', '.join(prefs.channels) or 'no channels'}."
