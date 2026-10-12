from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

import reviewforge.tools.workspace as module


@pytest.mark.asyncio
async def test_incomplete_network_body_can_retry_only_the_same_pinned_head(monkeypatch):
    calls = []

    async def download(repo, sha):
        calls.append((repo, sha))
        if len(calls) == 1:
            raise httpx.RemoteProtocolError("incomplete chunked read")
        return b"complete archive"

    async def sleep(delay):
        assert delay == 1

    monkeypatch.setattr(module.asyncio, "sleep", sleep)
    assert (
        await module._download_tarball(SimpleNamespace(get_repo_tarball=download), "fork/repo", "head")
        == b"complete archive"
    )
    assert calls == [("fork/repo", "head"), ("fork/repo", "head")]


@pytest.mark.asyncio
async def test_repeated_transport_failure_is_bounded_and_propagates(monkeypatch):
    calls = []

    async def download(repo, sha):
        calls.append((repo, sha))
        raise httpx.ReadError("connection lost")

    async def sleep(delay):
        pass

    monkeypatch.setattr(module.asyncio, "sleep", sleep)
    with pytest.raises(httpx.ReadError):
        await module._download_tarball(SimpleNamespace(get_repo_tarball=download), "fork/repo", "head")
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_authentication_rejection_is_not_treated_as_transient():
    calls = []

    async def download(repo, sha):
        calls.append((repo, sha))
        raise httpx.HTTPStatusError(
            "unauthorized", request=httpx.Request("GET", "https://test.example"), response=httpx.Response(401)
        )

    with pytest.raises(httpx.HTTPStatusError):
        await module._download_tarball(SimpleNamespace(get_repo_tarball=download), "fork/repo", "head")
    assert len(calls) == 1
