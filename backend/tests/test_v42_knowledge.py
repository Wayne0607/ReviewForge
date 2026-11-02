from types import SimpleNamespace

import pytest

from reviewforge.review_v42.knowledge import capture_knowledge, validate_knowledge
from reviewforge.tools.github_api import _issue_context_digest


def state(body=""):
    return SimpleNamespace(repo="owner/repo", pr_number=99, head_sha="head", pr_body=body, linked_issue_context={})


def issue(number):
    return {
        "number": number,
        "url": f"https://api.github.com/repos/owner/repo/issues/{number}",
        "html_url": f"https://github.com/owner/repo/issues/{number}",
        "title": "Issue",
        "body": "original source",
        "updated_at": "2026-01-01T00:00:00Z",
    }


def timeline():
    result = {
        "schema": "github-pr-issue-context-v1",
        "repo": "owner/repo",
        "pr_number": 99,
        "head_sha": "head",
        "captured_at": "2026-01-02T00:00:00Z",
        "source_url": "https://api.github.com/repos/owner/repo/issues/99/timeline",
        "relation": "cross-referenced",
        "coverage": "partial",
        "status": "no-cross-references",
        "timeline_events": [],
        "issues": [],
        "failures": [],
    }
    result["snapshot_sha256"] = _issue_context_digest(result)
    return result


class Response:
    def __init__(self, value):
        self.value = value

    def raise_for_status(self):
        pass

    def json(self):
        return self.value


class GitHub:
    def __init__(self):
        self._client = self
        self.requests = []

    async def get_pr_issue_context(self, repo, number, head):
        self.requests.append("timeline")
        return timeline()

    async def get(self, path):
        self.requests.append(path)
        return Response(issue(int(path.rsplit("/", 1)[1])))


@pytest.mark.asyncio
async def test_explicit_links_fetch_only_same_repo_and_freeze():
    st = state(
        "Fixes #1, Closes #2. See https://github.com/owner/repo/issues/3 "
        "and https://github.com/other/repo/issues/4; Resolves other/repo#5"
    )
    gh = GitHub()
    snap = await capture_knowledge(st, gh)
    assert snap["declared_refs"] == [1, 2, 3]
    assert [i["number"] for i in snap["issues"]] == [1, 2, 3]
    assert gh.requests == [
        "timeline",
        "/repos/owner/repo/issues/1",
        "/repos/owner/repo/issues/2",
        "/repos/owner/repo/issues/3",
    ]
    assert snap["coverage"] == "partial" and snap["historical_head_time"] is False
    assert validate_knowledge(snap, st) == snap
    st.pr_body += " changed"
    with pytest.raises(ValueError):
        validate_knowledge(snap, st)


@pytest.mark.asyncio
async def test_wrong_issue_or_pull_request_is_partial():
    class Wrong(GitHub):
        async def get(self, path):
            result = issue(int(path.rsplit("/", 1)[1]))
            result["pull_request"] = {"url": "irrelevant"}
            return Response(result)

    st = state("Fixes #1")
    snap = await capture_knowledge(st, Wrong())
    assert snap["status"] == "unavailable" and snap["issues"] == []
    assert snap["failures"][0]["issue_number"] == 1
    assert validate_knowledge(snap, st) == snap


@pytest.mark.asyncio
async def test_historical_exclusion_never_requests_anything():
    st = state("Fixes #1")
    st.linked_issue_context = {"status": "excluded-historical-leakage"}
    gh = GitHub()
    snap = await capture_knowledge(st, gh)
    assert not gh.requests
    assert snap["status"] == "excluded-historical-leakage" and not snap["declared_refs"]
    assert validate_knowledge(snap, st) == snap


@pytest.mark.asyncio
async def test_digest_rebind_and_missing_outcome_are_rejected():
    from reviewforge.review_v42.knowledge import _digest

    st = state("Fixes #1")
    snap = await capture_knowledge(st, GitHub())
    snap["issues"] = []
    snap["snapshot_sha256"] = _digest({k: v for k, v in snap.items() if k != "snapshot_sha256"})
    with pytest.raises(ValueError):
        validate_knowledge(snap, st)


@pytest.mark.asyncio
async def test_prepared_snapshots_are_reused_without_recapturing_timeline():
    st = state("Fixes #1")
    st.linked_issue_context = timeline()
    gh = GitHub()
    snap = await capture_knowledge(st, gh)
    assert gh.requests == ["/repos/owner/repo/issues/1"]
    assert snap["issue_captures"]["1"] == snap["captured_at"]
    st.linked_issue_context = snap
    gh.requests.clear()
    assert await capture_knowledge(st, gh) == snap
    assert not gh.requests
