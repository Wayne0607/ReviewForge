import asyncio

import httpx
import pytest

from reviewforge.core.change_manifest import is_verified_rename, populate_file_changes
from reviewforge.core.specs import build_registry
from reviewforge.core.state import StateStore
from reviewforge.tools.gateway import ToolGateway
from reviewforge.tools.github_api import GitHubClient

BASE, HEAD, MERGE, BLOB = (char * 40 for char in "abcd")


class GitHub:
    def __init__(self, *, changed_blob=False, fail=False, mode="100644", stale=False):
        self.calls = []
        self.changed_blob, self.fail, self.mode, self.stale = changed_blob, fail, mode, stale

    async def get_pr_info(self, repo, number):
        self.calls.append(("info", repo, number))
        return {"base": {"sha": BASE}, "head": {"sha": "e" * 40 if self.stale else HEAD}}

    async def get_pr_files(self, repo, number):
        self.calls.append(("files", repo, number))
        return [{"filename": "new.txt", "previous_filename": "old.txt", "status": "renamed"}]

    async def get_comparison_base(self, repo, base_sha, head_repo, head_sha):
        self.calls.append(("compare", repo, base_sha, head_repo, head_sha))
        return MERGE

    async def get_git_entries(self, repo, ref, paths):
        self.calls.append(("entries", repo, ref, tuple(paths)))
        if self.fail:
            raise OSError("private HTTP body must not reach proof_error")
        await asyncio.sleep(0)
        return {
            path: {
                "sha": "e" * 40 if self.changed_blob and ref == HEAD else BLOB,
                "mode": self.mode if ref == HEAD else "100644",
                "type": "blob",
            }
            for path in paths
        }


def state():
    return StateStore(
        repo="owner/repo",
        head_repo="fork/repo",
        pr_number=1,
        base_sha=BASE,
        head_sha=HEAD,
        files_changed=["new.txt"],
        file_diffs={"new.txt": ""},
    )


@pytest.mark.asyncio
async def test_preloaded_diff_and_concurrent_readers_verify_merge_base_not_base_tip():
    github = GitHub()
    gateway = ToolGateway(build_registry(), github, pipeline_mode="hypothesis")
    value = state()
    await asyncio.gather(gateway.ensure_file_changes(value), gateway.ensure_file_changes(value))
    change = value.file_changes["new.txt"]
    assert is_verified_rename(change, HEAD)
    assert change.base_sha == BASE and change.comparison_base_sha == MERGE
    assert ("entries", "owner/repo", MERGE, ("old.txt",)) in github.calls
    assert ("entries", "fork/repo", HEAD, ("new.txt",)) in github.calls
    assert sum(call[0] == "compare" for call in github.calls) == 1
    previous = len(github.calls)
    await gateway.ensure_file_changes(value)
    assert len(github.calls) == previous


@pytest.mark.asyncio
async def test_structural_preparation_before_context_diff_loading_keeps_verified_record():
    github = GitHub()
    gateway = ToolGateway(build_registry(), github, pipeline_mode="hypothesis")
    value = state()
    value.file_diffs = None
    await gateway.ensure_file_changes(value)
    verified = value.file_changes["new.txt"]
    assert is_verified_rename(verified, HEAD)
    calls = len(github.calls)
    await gateway.ensure_file_diffs(value)
    assert value.file_changes["new.txt"] == verified
    assert len(github.calls) == calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs,error,patch_state",
    [
        ({"changed_blob": True}, "blob-mismatch", "absent-unverified"),
        ({"fail": True}, "metadata-unavailable", "absent-unverified"),
        ({"stale": True}, "stale-pr-head", "absent-unverified"),
        ({"mode": "100755"}, "unsupported-mode", "unsupported"),
    ],
)
async def test_failed_proof_is_explicit_and_not_retried(kwargs, error, patch_state):
    github = GitHub(**kwargs)
    gateway = ToolGateway(build_registry(), github)
    value = state()
    populate_file_changes(
        value,
        [{"filename": "new.txt", "previous_filename": "old.txt", "status": "renamed", "additions": 0, "deletions": 0}],
    )
    await gateway.ensure_file_changes(value)
    change = value.file_changes["new.txt"]
    assert not is_verified_rename(change, HEAD)
    assert change.proof_error == error and change.patch_state == patch_state
    assert change.previous_path == "old.txt" and change.status == "renamed"
    previous = len(github.calls)
    await gateway.ensure_file_changes(value)
    assert len(github.calls) == previous
    assert "private" not in change.proof_error


@pytest.mark.asyncio
async def test_legacy_diff_loading_retains_metadata_without_extra_requests():
    github = GitHub()
    gateway = ToolGateway(build_registry(), github, pipeline_mode="legacy")
    value = state()
    value.file_diffs = None
    await gateway.ensure_file_diffs(value)
    assert value.file_changes["new.txt"].previous_path == "old.txt"
    assert not is_verified_rename(value.file_changes["new.txt"], HEAD)
    assert [call[0] for call in github.calls] == ["files"]


@pytest.mark.asyncio
async def test_lightweight_client_and_malformed_head_do_not_fabricate_metadata():
    gateway = ToolGateway(build_registry(), object())
    value = state()
    value.head_sha = "abc"
    await gateway.ensure_file_changes(value)
    assert value.file_changes["new.txt"].proof_error
    assert not is_verified_rename(value.file_changes["new.txt"], "abc")


@pytest.mark.asyncio
async def test_stale_checkpoint_proof_is_rehydrated():
    github = GitHub()
    gateway = ToolGateway(build_registry(), github)
    value = state()
    await gateway.ensure_file_changes(value)
    value.head_sha = "f" * 40
    await gateway.ensure_file_changes(value)
    assert not is_verified_rename(value.file_changes["new.txt"], value.head_sha)
    assert value.file_changes["new.txt"].head_sha == value.head_sha


@pytest.mark.asyncio
async def test_git_entries_are_exact_commit_and_complete_tree_bound():
    requests = []
    tree_sha = "e" * 40

    def respond(request):
        requests.append(request.url.path)
        if "/git/commits/" in request.url.path:
            return httpx.Response(200, json={"sha": HEAD, "tree": {"sha": tree_sha}})
        return httpx.Response(
            200,
            json={
                "sha": tree_sha,
                "truncated": False,
                "tree": [
                    {"path": "new.txt", "sha": BLOB, "type": "blob", "mode": "100644"},
                    {"path": "prefix-new.txt", "sha": "f" * 40, "type": "blob", "mode": "100644"},
                ],
            },
        )

    client = GitHubClient("test-placeholder")
    await client._client.aclose()
    client._client = httpx.AsyncClient(base_url=client.BASE_URL, transport=httpx.MockTransport(respond))
    try:
        entries = await client.get_git_entries("fork/repo", HEAD, ["new.txt", "missing.txt"])
        assert entries == {"new.txt": {"sha": BLOB, "type": "blob", "mode": "100644"}}
        assert len(requests) == 2
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_git_entries_reject_wrong_commit_and_unbounded_paths():
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"sha": "e" * 40, "tree": {"sha": BLOB}})

    client = GitHubClient("test-placeholder")
    await client._client.aclose()
    client._client = httpx.AsyncClient(base_url=client.BASE_URL, transport=httpx.MockTransport(respond))
    try:
        with pytest.raises(ValueError):
            await client.get_git_entries("fork/repo", HEAD, ["new.txt"])
        assert len(requests) == 1
        with pytest.raises(ValueError):
            await client.get_git_entries("fork/repo", HEAD, ["../escape.txt"])
        assert len(requests) == 1
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("incomplete", [False, True])
async def test_truncated_recursive_tree_requires_complete_targeted_lookup(incomplete):
    tree_sha, subtree_sha = "e" * 40, "f" * 40
    requests = []

    def respond(request):
        requests.append(request)
        if "/git/commits/" in request.url.path:
            return httpx.Response(200, json={"sha": HEAD, "tree": {"sha": tree_sha}})
        sha = request.url.path.rsplit("/", 1)[-1]
        if request.url.params.get("recursive"):
            # This matching but incomplete result must not become evidence.
            return httpx.Response(
                200,
                json={
                    "sha": sha,
                    "truncated": True,
                    "tree": [{"path": "dir/new.txt", "sha": "a" * 40, "mode": "100644", "type": "blob"}],
                },
            )
        if sha == tree_sha:
            return httpx.Response(
                200,
                json={
                    "sha": sha,
                    "truncated": incomplete,
                    "tree": [{"path": "dir", "sha": subtree_sha, "mode": "040000", "type": "tree"}],
                },
            )
        return httpx.Response(
            200,
            json={
                "sha": sha,
                "truncated": False,
                "tree": [{"path": "new.txt", "sha": BLOB, "mode": "100644", "type": "blob"}],
            },
        )

    client = GitHubClient("test-placeholder")
    await client._client.aclose()
    client._client = httpx.AsyncClient(base_url=client.BASE_URL, transport=httpx.MockTransport(respond))
    try:
        if incomplete:
            with pytest.raises(ValueError, match="incomplete Git tree"):
                await client.get_git_entries("fork/repo", HEAD, ["dir/new.txt"])
        else:
            result = await client.get_git_entries("fork/repo", HEAD, ["dir/new.txt"])
            assert result["dir/new.txt"]["sha"] == BLOB
            assert len(requests) == 4
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_comparison_base_uses_pinned_fork_commit_and_checks_base():
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"base_commit": {"sha": BASE}, "merge_base_commit": {"sha": MERGE}})

    client = GitHubClient("test-placeholder")
    await client._client.aclose()
    client._client = httpx.AsyncClient(base_url=client.BASE_URL, transport=httpx.MockTransport(respond))
    try:
        assert await client.get_comparison_base("owner/repo", BASE, "fork/repo", HEAD) == MERGE
        assert requests[0].url.path.endswith(f"{BASE}...fork:{HEAD}")
        with pytest.raises(ValueError, match="identity mismatch"):
            await client.get_comparison_base("owner/repo", MERGE, "fork/repo", HEAD)
    finally:
        await client.close()
