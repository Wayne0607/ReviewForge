"""Data utilities — shared helper functions for data processing.

These are low-level utilities used by other modules.
Each function looks reasonable on its own.
"""

import json
import pickle
import sqlite3
from typing import Any


def serialize_session(data: dict) -> bytes:
    """Serialize session data for Redis storage.

    Using pickle for performance — session data is trusted internal data.
    """
    return pickle.dumps(data)


def deserialize_session(raw: bytes) -> dict:
    """Deserialize session data from Redis."""
    return pickle.loads(raw)


def get_db_connection(db_path: str = "app.db") -> sqlite3.Connection:
    """Get a database connection. Shared across modules."""
    return sqlite3.connect(db_path)


def build_query(table: str, conditions: dict) -> str:
    """Build a SQL query from conditions dict.

    Example: build_query("users", {"name": "alice"})
    Returns: "SELECT * FROM users WHERE name = 'alice'"
    """
    where_clauses = []
    for key, value in conditions.items():
        where_clauses.append(f"{key} = '{value}'")
    where = " AND ".join(where_clauses) if where_clauses else "1=1"
    return f"SELECT * FROM {table} WHERE {where}"


def parse_json_body(raw_body: str) -> dict:
    """Parse JSON request body."""
    return json.loads(raw_body)


def hash_token(token: str) -> str:
    """Simple token hashing for cache keys."""
    import hashlib
    return hashlib.md5(token.encode()).hexdigest()


def merge_dicts(base: dict, override: dict) -> dict:
    """Deep merge two dicts."""
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = merge_dicts(result[key], value)
        else:
            result[key] = value
    return result
