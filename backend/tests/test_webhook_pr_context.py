"""Webhook ingestion tests for authoritative PR and head identity context."""

import asyncio
import hashlib
import hmac
import json
import time
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from reviewforge.api.webhook import router as webhook_router
from reviewforge.core.state import StateStore


class _GitHub:
    async def get_pr_files(self, _repo: str, _pr_number: int) -> list[dict[str, Any]]:
        return [{"filename": "src/app.py", "additions": 1, "deletions": 0, "patch": "+print('ok')"}]


class _CaptureOrchestrator:
    def __init__(self) -> None:
        self.states: list[StateStore] = []

    async def run(self, state: StateStore) -> dict[str, str]:
        self.states.append(state)
        return {"status": "completed"}


def _deliver(payload: dict[str, Any]) -> StateStore:
    secret = "signed-payload-secret"
    orchestrator = _CaptureOrchestrator()
    app = FastAPI()
    app.include_router(webhook_router)
    app.state.webhook_secret = secret
    app.state.review_semaphore = asyncio.Semaphore(1)
    app.state.review_tasks = set()
    app.state.github_client = _GitHub()
    app.state.orchestrator = orchestrator

    body = json.dumps(payload).encode()
    signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

    with TestClient(app) as client:
        response = client.post(
            "/webhook/github",
            content=body,
            headers={"X-Hub-Signature-256": signature, "X-GitHub-Event": "pull_request"},
        )
        deadline = time.monotonic() + 2
        while app.state.review_tasks and time.monotonic() < deadline:
            time.sleep(0.01)

    assert response.status_code == 200
    assert response.json() == {"status": "review_triggered", "pr": "17"}
    assert len(orchestrator.states) == 1
    return orchestrator.states[0]


def _payload(*, body: str | None = "Fixes #432 without inferring issue metadata") -> dict[str, Any]:
    return {
        "action": "opened",
        "pull_request": {
            "number": 17,
            "title": "Preserve PR intent",
            "body": body,
            "head": {
                "sha": "authoritative-head-sha",
                "ref": "feature/pr-intent",
                "repo": {"full_name": "contributor/reviewforge-fork"},
            },
            "base": {"sha": "base-sha"},
        },
        "repository": {"full_name": "upstream/reviewforge"},
    }


def test_signed_payload_populates_pr_and_head_context():
    state = _deliver(_payload())

    assert state.repo == "upstream/reviewforge"
    assert state.head_sha == "authoritative-head-sha"
    assert state.pr_title == "Preserve PR intent"
    assert state.pr_body == "Fixes #432 without inferring issue metadata"
    assert state.head_repo == "contributor/reviewforge-fork"
    assert state.head_ref == "feature/pr-intent"
    assert state.linked_issues == []


def test_null_pr_body_is_normalized_to_empty_string():
    state = _deliver(_payload(body=None))

    assert state.pr_body == ""


@pytest.mark.parametrize("missing_head_repo", [None, pytest.param("omitted", id="omitted")])
def test_missing_fork_head_repo_stays_observably_empty(missing_head_repo: str | None):
    payload = _payload()
    if missing_head_repo == "omitted":
        payload["pull_request"]["head"].pop("repo")
    else:
        payload["pull_request"]["head"]["repo"] = None

    state = _deliver(payload)

    assert state.repo == "upstream/reviewforge"
    assert state.head_repo == ""
    assert state.head_ref == "feature/pr-intent"
