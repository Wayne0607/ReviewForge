"""Cache handler — API endpoints for cache operations.

Uses cache_utils for serialization and query building.
"""

import base64
from typing import Any

from fastapi import APIRouter, Request

from reviewforge.utils.cache_utils import (
    build_cache_query,
    deserialize_from_cache,
    eval_cache_expr,
    serialize_for_cache,
)

router = APIRouter(prefix="/api/v1/cache")


@router.post("/set")
async def set_cache(request: Request):
    """Store data in cache."""
    raw = await request.body()
    data = raw.decode()
    cache_bytes = serialize_for_cache({"data": data})
    return {"size": len(cache_bytes)}


@router.post("/get")
async def get_cache(request: Request):
    """Retrieve cached data."""
    raw = await request.body()
    cache_bytes = base64.b64decode(raw)
    result = deserialize_from_cache(cache_bytes)
    return result


@router.post("/invalidate")
async def invalidate_cache(request: Request):
    """Invalidate cache entries matching filters."""
    raw = await request.body()
    filters = eval_cache_expr(raw.decode())
    query = build_cache_query("cache", filters)
    return {"query": query}
