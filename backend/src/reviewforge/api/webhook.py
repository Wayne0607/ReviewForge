"""GitHub Webhook handler — receives PR events and triggers reviews."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import re

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

    # S1: fail-closed — secret 缺失直接拒
    signature = request.headers.get("X-Hub-Signature-256", "")
    secret = request.app.state.webhook_secret
    if not secret:
        logger.error("Webhook secret 未配置，拒绝请求")
        raise HTTPException(status_code=503, detail="Webhook secret not configured")
    if not signature:
        raise HTTPException(status_code=401, detail="Missing signature header")
    if not verify_signature(body, signature, secret):
        raise HTTPException(status_code=401, detail="Invalid signature")

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    event = request.headers.get("X-GitHub-Event", "")

    if event == "pull_request":
        action = payload.get("action", "")
        if action in ("opened", "synchronize", "reopened"):
            pr = payload["pull_request"]
            repo = payload["repository"]["full_name"]
            pr_number = pr["number"]
            head_sha = pr["head"]["sha"]

            # S6: 校验 repo 格式
            if not re.fullmatch(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+", repo):
                raise HTTPException(status_code=400, detail="Invalid repository name")

            # Dedup: GitHub redelivers webhooks and fires opened+synchronize for the
            # same head. If this exact commit was already reviewed (or a review is
            # in-flight), skip instead of spawning a duplicate run + wasting tokens.
            db = getattr(request.app.state, "db", None)
            if db is not None and await db.has_active_run_for_head(repo, int(pr_number), head_sha):
                logger.info(f"PR #{pr_number} @ {head_sha[:8]} already reviewed/in-flight, skipping duplicate")
                return {"status": "duplicate_skipped", "pr": str(pr_number)}

            logger.info(f"PR #{pr_number} {action} on {repo}, triggering review")

            # S7: 持引用 + 并发上限，IO 移入后台
            sem = request.app.state.review_semaphore
            tasks = request.app.state.review_tasks

            async def _run_review():
                async with sem:
                    try:
                        orchestrator = request.app.state.orchestrator
                        github = request.app.state.github_client

                        files_data = await github.get_pr_files(repo, pr_number)
                        file_paths = [f["filename"] for f in files_data]

                        diff_summary = "\n".join(
                            f"--- {f['filename']} (+{f.get('additions', 0)} -{f.get('deletions', 0)})\n{f.get('patch', '')}"  # noqa: E501
                            for f in files_data
                        )

                        from reviewforge.core.state import StateStore

                        state = StateStore(
                            pr_number=int(pr_number),
                            repo=repo,
                            head_sha=head_sha,
                            base_sha=pr["base"]["sha"],
                            files_changed=file_paths,
                            diff_summary=diff_summary,
                        )

                        logger.info(f"Starting review for PR #{pr_number} on {repo}")
                        summary = await orchestrator.run(state)
                        logger.info(f"Review completed for PR #{pr_number}: {summary}")
                    except Exception as e:
                        logger.error(f"Review failed for PR #{pr_number}: {e}", exc_info=True)

            t = asyncio.create_task(_run_review())
            tasks.add(t)
            t.add_done_callback(tasks.discard)

            return {"status": "review_triggered", "pr": str(pr_number)}

    return {"status": "ignored", "event": event}
