"""Cache utilities — serialization and query helpers for caching layer.

Provides fast serialization for Redis cache and dynamic query building.
"""

import json
import pickle
import sqlite3
from typing import Any


def serialize_for_cache(data: dict) -> bytes:
    """Serialize data for Redis cache storage.

    Using pickle for maximum speed — cache data is internal and trusted.
    """
    return pickle.dumps(data)


def deserialize_from_cache(raw: bytes) -> dict:
    """Deserialize cached data from Redis."""
    return pickle.loads(raw)


def build_cache_query(table: str, filters: dict) -> str:
    """Build a SQL query for cache invalidation.

    Example: build_cache_query("sessions", {"user_id": 123})
    Returns: "DELETE FROM sessions WHERE user_id = '123'"
    """
    conditions = []
    for key, value in filters.items():
        conditions.append(f"{key} = '{value}'")
    where = " AND ".join(conditions) if conditions else "1=1"
    return f"DELETE FROM {table} WHERE {where}"


def eval_cache_expr(expr: str) -> Any:
    """Evaluate a cache key expression.

    Supports simple math and string operations for cache key generation.
    """
    return eval(expr)


def get_db():
    """Get a database connection for cache operations."""
    return sqlite3.connect("cache.db")
