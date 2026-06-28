"""GitHub Webhook handler — receives PR events and triggers reviews."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhook", tags=["webhook"])


def verify_signature(payload: bytes, signature: str, secret: str) -> bool:
    """Verify GitHub webhook signature."""
    expected = "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


@router.post("/github")
async def handle_github_webhook(request: Request) -> dict[str, str]:
    """Handle incoming GitHub webhook events."""
    body = await request.body()

    # Verify signature
    signature = request.headers.get("X-Hub-Signature-256", "")
    secret = request.app.state.webhook_secret
    if secret and not verify_signature(body, signature, secret):
        raise HTTPException(status_code=401, detail="Invalid signature")

    event = request.headers.get("X-GitHub-Event", "")
    payload = json.loads(body)

    if event == "pull_request":
        action = payload.get("action", "")
        if action in ("opened", "synchronize", "reopened"):
            pr = payload["pull_request"]
            repo = payload["repository"]["full_name"]
            pr_number = pr["number"]
            head_sha = pr["head"]["sha"]

            logger.info(f"PR #{pr_number} {action} on {repo}, triggering review")

            # Trigger async review
            orchestrator = request.app.state.orchestrator
            github = request.app.state.github_client

            # Fetch PR details
            files_data = await github.get_pr_files(repo, pr_number)
            file_paths = [f["filename"] for f in files_data]

            # Build diff summary for planner
            diff_summary = "\n".join(
                f"--- {f['filename']} (+{f.get('additions', 0)} -{f.get('deletions', 0)})"
                for f in files_data
            )

            from reviewforge.core.state import StateStore
            state = StateStore(
                pr_number=pr_number,
                repo=repo,
                head_sha=head_sha,
                base_sha=pr["base"]["sha"],
                files_changed=file_paths,
                diff_summary=diff_summary,
            )

            import asyncio

            async def _run_review():
                try:
                    logger.info(f"Starting review for PR #{pr_number} on {repo}")
                    summary = await orchestrator.run(state)
                    logger.info(f"Review completed for PR #{pr_number}: {summary}")
                except Exception as e:
                    logger.error(f"Review failed for PR #{pr_number}: {e}", exc_info=True)

            asyncio.create_task(_run_review())

            return {"status": "review_triggered", "pr": str(pr_number)}

    return {"status": "ignored", "event": event}
