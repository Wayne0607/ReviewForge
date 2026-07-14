"""Tests for batched inline comments through GitHub's Create Review API."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from reviewforge.tools.github_api import GitHubAPIError, GitHubClient


async def _client_with_transport(
    handler: Callable[[httpx.Request], httpx.Response],
) -> GitHubClient:
    client = GitHubClient("test-token")
    await client._client.aclose()
    client._client = httpx.AsyncClient(
        base_url=client.BASE_URL,
        transport=httpx.MockTransport(handler),
    )
    return client


@pytest.mark.asyncio
async def test_post_review_comments_sends_create_review_payload() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["payload"] = json.loads(request.content)
        return httpx.Response(201, json={"id": 321})

    client = await _client_with_transport(handler)
    try:
        result = await client.post_review_comments(
            "acme/widget",
            17,
            "deadbeef",
            [
                {"file_path": "src/a.py", "line": 7, "body": "First issue"},
                {"path": "src/b.ts", "line": 9, "body": "Second issue"},
            ],
        )
    finally:
        await client.close()

    assert result == {"id": 321}
    assert captured == {
        "method": "POST",
        "path": "/repos/acme/widget/pulls/17/reviews",
        "payload": {
            "commit_id": "deadbeef",
            "event": "COMMENT",
            "comments": [
                {
                    "path": "src/a.py",
                    "line": 7,
                    "side": "RIGHT",
                    "body": "First issue",
                },
                {
                    "path": "src/b.ts",
                    "line": 9,
                    "side": "RIGHT",
                    "body": "Second issue",
                },
            ],
        },
    }


@pytest.mark.asyncio
async def test_plain_422_is_validation_error_without_retry() -> None:
    requests = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(
            422,
            json={
                "message": "Validation Failed, or you have spammed the endpoint.",
                "errors": [{"field": "line", "code": "invalid"}],
            },
        )

    async def no_wait(delay: float) -> None:
        sleeps.append(delay)

    client = await _client_with_transport(handler)
    client._sleep = no_wait
    try:
        with pytest.raises(GitHubAPIError) as exc_info:
            await client.post_review_comments(
                "acme/widget",
                17,
                "deadbeef",
                [{"path": "src/a.py", "line": 100, "body": "Invalid line"}],
            )
    finally:
        await client.close()

    error = exc_info.value
    assert error.status_code == 422
    assert error.kind == "validation"
    assert error.retryable is False
    assert "Validation Failed" in error.response_body
    assert requests == 1
    assert sleeps == []


@pytest.mark.parametrize(
    ("failures", "expected_delays"),
    [
        (
            [
                (
                    422,
                    {},
                    {"message": "Validation Failed, or you have spammed the endpoint."},
                )
            ],
            [1.0],
        ),
        (
            [
                (429, {"Retry-After": "0"}, {"message": "API rate limit exceeded"}),
                (429, {"Retry-After": "0"}, {"message": "API rate limit exceeded"}),
            ],
            [0.0, 0.0],
        ),
    ],
    ids=["spam-422", "rate-limit-429"],
)
@pytest.mark.asyncio
async def test_retryable_write_failures_retry_with_bound_before_success(
    failures: list[tuple[int, dict[str, str], dict[str, Any]]],
    expected_delays: list[float],
) -> None:
    responses = list(failures)
    requests = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        if responses:
            status, headers, body = responses.pop(0)
            return httpx.Response(status, headers=headers, json=body)
        return httpx.Response(201, json={"id": 654})

    async def no_wait(delay: float) -> None:
        sleeps.append(delay)

    client = await _client_with_transport(handler)
    client._sleep = no_wait
    try:
        result = await client.post_review_comments(
            "acme/widget",
            17,
            "deadbeef",
            [{"path": "src/a.py", "line": 7, "body": "Issue"}],
        )
    finally:
        await client.close()

    assert result == {"id": 654}
    assert requests == len(failures) + 1
    assert requests <= 3
    assert sleeps == expected_delays


@pytest.mark.asyncio
async def test_post_review_comments_accepts_at_most_40_comments() -> None:
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(201, json={"id": 987})

    def comments(count: int) -> list[dict[str, Any]]:
        return [{"path": "src/a.py", "line": index + 1, "body": f"Issue {index}"} for index in range(count)]

    client = await _client_with_transport(handler)
    try:
        assert await client.post_review_comments("acme/widget", 17, "deadbeef", comments(40)) == {"id": 987}

        with pytest.raises(ValueError, match="exceeds 40"):
            await client.post_review_comments("acme/widget", 17, "deadbeef", comments(41))
    finally:
        await client.close()

    assert requests == 1


@pytest.mark.asyncio
async def test_review_writes_are_serialized_across_client_instances() -> None:
    active = 0
    max_active = 0

    async def fake_post(_url: str, _payload: dict[str, Any], *, operation: str) -> dict[str, Any]:
        nonlocal active, max_active
        assert operation == "create pull request review"
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.01)
        active -= 1
        return {"id": 1}

    first = GitHubClient("first-token")
    second = GitHubClient("second-token")
    first._post_json_with_retry = fake_post  # type: ignore[method-assign]
    second._post_json_with_retry = fake_post  # type: ignore[method-assign]
    comment = [{"path": "src/a.py", "line": 1, "body": "Issue"}]
    try:
        await asyncio.gather(
            first.post_review_comments("acme/widget", 1, "sha", comment),
            second.post_review_comments("acme/widget", 2, "sha", comment),
        )
    finally:
        await first.close()
        await second.close()

    assert max_active == 1
