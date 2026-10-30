"""Read-only, explicitly partial issue history with frozen benchmark inputs."""

from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import httpx
import pytest
from test_martian_runner import runner  # noqa: F401

from reviewforge.tools.github_api import GitHubClient, validate_pr_issue_context

REPO = "owner/repo"
HEAD = "a" * 40


def issue(number=7, repo=REPO):
    return {
        "number": number,
        "url": f"https://api.github.com/repos/{repo}/issues/{number}",
        "html_url": f"https://github.com/{repo}/issues/{number}",
        "title": "Required stored-value behavior",
        "body": "The supported consumer expects the original stored value.\n" * 100,
        "updated_at": "2026-10-30T08:00:00Z",
    }


def referenced(value):
    return {"event": "cross-referenced", "source": {"type": "issue", "issue": value}}


async def client_with(handler):
    client = GitHubClient("test-placeholder")
    await client._client.aclose()
    client._client = httpx.AsyncClient(base_url=client.BASE_URL, transport=httpx.MockTransport(handler))
    return client


@pytest.mark.asyncio
async def test_only_authoritative_same_repo_source_issues_are_fetched_by_get_and_snapshotted():
    requests = []
    raw = issue()
    events = [
        referenced(raw),
        referenced(raw),
        referenced(issue(8, "other/repo")),
        referenced(issue(9) | {"pull_request": {"url": "https://api.github.com/repos/owner/repo/pulls/9"}}),
        {"event": "commented", "body": "Fixes #999"},
        {"event": "connected", "id": 123},
    ]

    def respond(request):
        requests.append(request)
        assert request.method == "GET"
        if request.url.path.endswith("/timeline"):
            return httpx.Response(200, json=events)
        assert request.url.path == "/repos/owner/repo/issues/7"
        return httpx.Response(200, json=raw)

    client = await client_with(respond)
    try:
        result = await client.get_pr_issue_context(REPO, 17, HEAD)
    finally:
        await client.close()
    assert len(requests) == 2 and result["issues"] == [raw] and result["timeline_events"] == events
    assert result["status"] == "available" and result["coverage"] == "partial"
    assert result["relation"] == "cross-referenced" and "closing/manual" in result["limitation"]
    assert validate_pr_issue_context(result, repo=REPO, pr_number=17, head_sha=HEAD) == result
    assert result["issues"][0]["body"] == raw["body"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status,expected", [(200, "no-cross-references"), (403, "unavailable"), (404, "unavailable")])
async def test_no_reference_is_distinct_from_permission_or_api_failure(status, expected):
    client = await client_with(lambda _request: httpx.Response(status, json=[]))
    try:
        result = await client.get_pr_issue_context(REPO, 17, HEAD)
    finally:
        await client.close()
    assert result["status"] == expected and result["issues"] == []
    assert bool(result["failures"]) is (status != 200)


@pytest.mark.asyncio
async def test_all_timeline_pages_are_read_and_one_bad_issue_does_not_erase_valid_source():
    events = [{"event": "commented", "id": index} for index in range(99)] + [referenced(issue(7))]
    visited = []

    def respond(request):
        visited.append(str(request.url))
        if request.url.path.endswith("/timeline"):
            return httpx.Response(200, json=events if request.url.params["page"] == "1" else [referenced(issue(8))])
        return httpx.Response(200, json=issue(7)) if request.url.path.endswith("/7") else httpx.Response(404, json={})

    client = await client_with(respond)
    try:
        result = await client.get_pr_issue_context(REPO, 17, HEAD)
    finally:
        await client.close()
    assert len(result["timeline_events"]) == 101 and len(visited) == 4
    assert result["status"] == "partial" and result["issues"] == [issue(7)]
    assert result["failures"][0]["issue_number"] == 8


@pytest.mark.asyncio
async def test_snapshot_rebinding_or_edit_is_rejected_without_refresh():
    client = await client_with(lambda _request: httpx.Response(200, json=[]))
    try:
        result = await client.get_pr_issue_context(REPO, 17, HEAD)
    finally:
        await client.close()
    with pytest.raises(ValueError, match="prepared"):
        validate_pr_issue_context(result, repo=REPO, pr_number=17, head_sha="b" * 40)
    changed = copy.deepcopy(result)
    changed["issues"] = [issue()]
    with pytest.raises(ValueError, match="prepared"):
        validate_pr_issue_context(changed, repo=REPO, pr_number=17, head_sha=HEAD)


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", [10, 11])
async def test_benchmark_captures_once_and_reuses_prepared_snapshot(runner, tmp_path, protocol):  # noqa: F811
    client = await client_with(lambda _request: httpx.Response(200, json=[]))
    try:
        snapshot = await client.get_pr_issue_context(REPO, 17, HEAD)
    finally:
        await client.close()
    states, captures = [], []

    class GitHub:
        async def get_pr_info(self, *_):
            return {"head": {"sha": HEAD}, "base": {"sha": "b" * 40}, "body": "Fixes #999"}

        async def get_pr_files(self, *_):
            return []

        async def get_pr_issue_context(self, *_):
            captures.append(True)
            return snapshot

    class DB:
        async def get_runs(self, **_kwargs):
            return []

    async def review(state):
        states.append(state)
        return {"status": "completed"}

    orchestrator = SimpleNamespace(
        _pipeline_v4_config=SimpleNamespace(protocol_version=protocol, mode="hypothesis"),
        run=review,
        _gateway=SimpleNamespace(_github=SimpleNamespace(comments={}, bodies={})),
    )
    item = {"repo": REPO, "pr_number": 17, "golden_url": "case"}
    directory = tmp_path / "issue-input"
    for _ in range(2):
        await runner._run_one(item, orchestrator, DB(), GitHub(), knowledge_snapshot_dir=directory)
    if protocol == 10:
        assert captures == [] and not directory.exists() and all(not state.linked_issue_context for state in states)
    else:
        assert len(captures) == 1 and len(list(directory.glob("*.json"))) == 1
        assert all(state.linked_issue_context == snapshot for state in states)
        path = next(directory.glob("*.json"))
        changed = json.loads(path.read_text())
        changed["head_sha"] = "b" * 40
        path.write_text(json.dumps(changed))
        with pytest.raises(ValueError, match="prepared"):
            await runner._run_one(item, orchestrator, DB(), GitHub(), knowledge_snapshot_dir=directory)
        assert len(captures) == 1


def test_webhook_eleven_fetches_authoritative_context_and_passes_it_to_review(monkeypatch):
    import test_webhook_pr_context as tests

    original = tests._CaptureOrchestrator.__init__

    def configured(self):
        original(self)
        self._pipeline_v4_config = SimpleNamespace(protocol_version=11, mode="hypothesis")

    async def capture(self, repo, number, head):
        assert (repo, number, head) == ("upstream/reviewforge", 17, "authoritative-head-sha")
        return {"status": "no-cross-references", "issues": [], "coverage": "partial"}

    monkeypatch.setattr(tests._CaptureOrchestrator, "__init__", configured)
    monkeypatch.setattr(tests._GitHub, "get_pr_issue_context", capture, raising=False)
    state = tests._deliver(tests._payload())
    assert state.linked_issue_context["status"] == "no-cross-references"
    assert not state.linked_issues
    snapshot = state.snapshot()
    snapshot["linked_issue_context"]["status"] = "edited"
    assert state.linked_issue_context["status"] == "no-cross-references"
