import httpx
import pytest

from reviewforge.tools.github_api import GitHubAPIError, GitHubClient


async def make_client(handler):
    client = GitHubClient("test")
    await client._client.aclose()
    client._client = httpx.AsyncClient(base_url=client.BASE_URL, transport=httpx.MockTransport(handler))
    return client


async def test_issue_comments_paginate_and_reject_repeated_pages():
    pages = []

    def handler(request):
        page = int(request.url.params["page"])
        pages.append(page)
        return httpx.Response(200, json=[{"id": page * 100 + i} for i in range(100 if page == 1 else 1)])

    client = await make_client(handler)
    try:
        assert len(await client.list_delivery_issue_comments("owner/repo", 4)) == 101
        assert pages == [1, 2]
    finally:
        await client.close()

    client = await make_client(lambda request: httpx.Response(200, json=[{"id": i} for i in range(100)]))
    try:
        with pytest.raises(GitHubAPIError, match="Repeated"):
            await client.list_delivery_issue_comments("owner/repo", 4)
    finally:
        await client.close()


async def test_issue_comment_write_has_no_automatic_retries_and_requires_receipt():
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path))
        if len(calls) == 1:
            raise httpx.ReadTimeout("lost", request=request)
        return httpx.Response(200, json={"id": 0})

    client = await make_client(handler)
    try:
        with pytest.raises(GitHubAPIError, match="response lost"):
            await client.write_delivery_issue_comment("owner/repo", 4, "text")
        with pytest.raises(GitHubAPIError, match="receipt"):
            await client.write_delivery_issue_comment("owner/repo", 4, "text", remote_id=42)
        assert calls == [
            ("POST", "/repos/owner/repo/issues/4/comments"),
            ("PATCH", "/repos/owner/repo/issues/comments/42"),
        ]
    finally:
        await client.close()


async def test_authenticated_actor_requires_positive_integer_id():
    client = await make_client(lambda request: httpx.Response(200, json={"id": 234}))
    try:
        assert await client.get_authenticated_user_id() == 234
    finally:
        await client.close()
    client = GitHubClient("test", bot_user_id=987)
    try:
        assert await client.get_authenticated_user_id() == 987
    finally:
        await client.close()
    client = await make_client(lambda request: httpx.Response(200, json={"id": True}))
    try:
        with pytest.raises(GitHubAPIError, match="identity"):
            await client.get_authenticated_user_id()
    finally:
        await client.close()
