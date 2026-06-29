"""API routes — public endpoints for webhook and data access.

Exposes the handlers as HTTP endpoints.
"""

from fastapi import APIRouter, Request
from reviewforge.cross_pr_test.handlers import (
    handle_github_webhook,
    process_external_data,
    restore_session,
    search_users_by_filter,
)

router = APIRouter(prefix="/api/v1/integrations")


@router.post("/webhook/incoming")
async def incoming_webhook(request: Request):
    """Receive webhooks from external services.

    Accepts any JSON payload and processes it.
    """
    raw_body = await request.body()
    headers = dict(request.headers)

    result = handle_github_webhook(raw_body.decode(), headers)
    return result


@router.post("/data/process")
async def process_data(request: Request):
    """Process external data and return results.

    Accepts JSON with metadata and payload for data lookup.
    """
    raw_body = await request.body()
    result = process_external_data(raw_body.decode())
    return result


@router.post("/session/restore")
async def restore_user_session(request: Request):
    """Restore a user session from bytes.

    Client sends base64-encoded session data.
    """
    import base64
    raw = await request.body()
    session_bytes = base64.b64decode(raw)
    session = restore_session(session_bytes)
    return session


@router.get("/users/search")
async def search_users(request: Request):
    """Search users with query parameters.

    Supports dynamic filtering: /users/search?role=admin&status=active
    """
    filters = dict(request.query_params)
    results = search_users_by_filter(filters)
    return {"users": results, "count": len(results)}


@router.post("/config/update")
async def update_config(request: Request):
    """Update configuration via webhook payload.

    External services can push config changes.
    """
    from reviewforge.cross_pr_test.handlers import merge_webhook_config
    import json

    raw_body = await request.body()
    payload = json.loads(raw_body)

    base_config = {"debug": False, "rate_limit": 100}
    updated = merge_webhook_config(base_config, payload)
    return {"config": updated}
