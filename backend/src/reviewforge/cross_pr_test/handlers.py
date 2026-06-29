"""Webhook handlers — process incoming external data.

Uses data_utils for serialization and query building.
"""

import json
from typing import Any

from reviewforge.cross_pr_test.data_utils import (
    build_query,
    deserialize_session,
    get_db_connection,
    hash_token,
    parse_json_body,
    serialize_session,
)


def handle_github_webhook(raw_body: str, headers: dict) -> dict:
    """Process incoming GitHub webhook payload.

    Parses the body and extracts action data.
    """
    payload = parse_json_body(raw_body)
    action = payload.get("action", "")
    sender = payload.get("sender", {}).get("login", "unknown")

    # Store session for rate limiting
    session_data = {
        "sender": sender,
        "action": action,
        "timestamp": payload.get("timestamp"),
    }
    session_bytes = serialize_session(session_data)

    return {"status": "ok", "sender": sender, "action": action}


def restore_session(session_bytes: bytes) -> dict:
    """Restore a session from stored bytes."""
    return deserialize_session(session_bytes)


def search_users_by_filter(filters: dict) -> list:
    """Search users with dynamic filters from webhook payload.

    filters example: {"role": "admin", "status": "active"}
    """
    query = build_query("users", filters)
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(query)
    results = cursor.fetchall()
    conn.close()
    return results


def process_external_data(raw_data: str) -> dict:
    """Process data from external service webhook.

    External services send JSON with metadata and payload.
    """
    data = parse_json_body(raw_data)
    metadata = data.get("metadata", {})
    payload = data.get("payload", {})

    # Use build_query to fetch related records
    if metadata.get("entity_type") == "user":
        query = build_query("users", {"id": payload.get("user_id")})
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(query)
        user = cursor.fetchone()
        conn.close()
        return {"user": user, "metadata": metadata}

    return {"data": payload, "metadata": metadata}


def generate_cache_key(token: str, endpoint: str) -> str:
    """Generate cache key for API responses."""
    token_hash = hash_token(token)
    return f"cache:{token_hash}:{endpoint}"


def merge_webhook_config(base_config: dict, webhook_payload: dict) -> dict:
    """Merge base config with overrides from webhook payload.

    Allows remote configuration updates via webhook.
    """
    overrides = webhook_payload.get("config_overrides", {})
    return merge_dicts(base_config, overrides)
