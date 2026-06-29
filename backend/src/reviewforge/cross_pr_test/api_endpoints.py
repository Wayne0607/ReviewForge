"""Public API endpoints — exposes data processing and session management.

This PR imports from previously reviewed modules (handlers.py, data_utils.py).
The cross-PR analyzer should detect the dangerous call chains.
"""

import json
from typing import Any

from fastapi import APIRouter, Request

from reviewforge.cross_pr_test.handlers import (
    process_external_data,
    restore_session,
    search_users_by_filter,
)

router = APIRouter(prefix="/api/v2/data")


@router.post("/process")
async def process_data(request: Request):
    """Process external webhook data and return results."""
    raw = await request.body()
    result = process_external_data(raw.decode())
    return result


@router.post("/session")
async def create_session(request: Request):
    """Restore a user session from stored bytes."""
    import base64
    raw = await request.body()
    session_bytes = base64.b64decode(raw)
    session = restore_session(session_bytes)
    return {"session": session}


@router.get("/users")
async def list_users(request: Request):
    """Search users with dynamic filters from query params."""
    filters = dict(request.query_params)
    results = search_users_by_filter(filters)
    return {"users": results}
