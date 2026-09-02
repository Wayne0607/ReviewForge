from __future__ import annotations

import io
import tarfile
from pathlib import Path

import pytest

from reviewforge.core.specs import build_registry
from reviewforge.core.state import StateStore
from reviewforge.tools.gateway import ToolGateway
from reviewforge.tools.workspace import PRHeadWorkspace, WorkspaceUnavailable

FIXTURE_TARBALL = Path(__file__).parent / "fixtures" / "workspace_repo.tar.gz"


class _TarballGitHub:
    def __init__(self, payload: bytes | None = None) -> None:
        self.payload = payload if payload is not None else FIXTURE_TARBALL.read_bytes()
        self.tarball_calls: list[tuple[str, str]] = []
        self.content_calls: list[tuple[str, str, str]] = []

    async def get_repo_tarball(self, repo: str, ref: str) -> bytes:
        self.tarball_calls.append((repo, ref))
        return self.payload

    async def get_file_content(self, repo: str, ref: str, path: str) -> str:
        self.content_calls.append((repo, ref, path))
        return "fallback line 1\nfallback line 2\n"


class _FallbackGitHub(_TarballGitHub):
    async def get_repo_tarball(self, repo: str, ref: str) -> bytes:
        self.tarball_calls.append((repo, ref))
        raise OSError("tarball unavailable")


class _UnavailableFallbackGitHub(_FallbackGitHub):
    async def get_file_content(self, repo: str, ref: str, path: str) -> str:
        error = FileNotFoundError(path)
        error.status_code = 404  # type: ignore[attr-defined]
        raise error


class _SequenceFallbackGitHub(_FallbackGitHub):
    async def get_file_content(self, repo: str, ref: str, path: str) -> str:
        self.content_calls.append((repo, ref, path))
        if path == "a.py":
            error = FileNotFoundError(path)
            error.status_code = 404  # type: ignore[attr-defined]
            raise error
        if path == "b.py":
            return "head fallback content\n"
        raise AssertionError(f"unexpected fallback probe: {path}")


def _state(**kwargs: object) -> StateStore:
    values: dict[str, object] = {
        "repo": "owner/repo",
        "head_repo": "contributor/repo",
        "head_sha": "head-sha",
        "pr_number": 42,
        "files_changed": ["python/service.py"],
    }
    values.update(kwargs)
    return StateStore(**values)


def _malicious_tarball() -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        wrapper = tarfile.TarInfo("workspace-repo-test")
        wrapper.type = tarfile.DIRTYPE
        archive.addfile(wrapper)

        safe = b"def safe() -> str:\n    return 'ok'\n"
        safe_info = tarfile.TarInfo("workspace-repo-test/src/safe.py")
        safe_info.size = len(safe)
        archive.addfile(safe_info, io.BytesIO(safe))

        escape = b"should never be extracted\n"
        escape_info = tarfile.TarInfo("workspace-repo-test/../escape.txt")
        escape_info.size = len(escape)
        archive.addfile(escape_info, io.BytesIO(escape))

        link = tarfile.TarInfo("workspace-repo-test/src/link.py")
        link.type = tarfile.SYMTYPE
        link.linkname = "../../outside.txt"
        archive.addfile(link)
    return output.getvalue()


@pytest.mark.asyncio
async def test_workspace_reads_pinned_tarball_and_manifest_identity() -> None:
    github = _TarballGitHub()
    workspace = await PRHeadWorkspace.build(_state(), github, max_bytes=200_000_000)
    try:
        assert workspace.info.repo == "owner/repo"
        assert workspace.info.head_repo == "contributor/repo"
        assert workspace.info.head_sha == "head-sha"
        assert workspace.source == "tarball"
        assert workspace.info.file_count == 5
        assert workspace.info.byte_size > 0
        assert len(workspace.digest) == 64
        assert "return normalize" in (workspace.read("python/service.py") or "")
        assert (
            workspace.read("python/service.py", 5, 5)
            == '5:     """Normalize a value before it is handed to the service."""'
        )
        assert workspace.exists("python/service.py")
        assert not workspace.exists("missing.py")
        assert github.tarball_calls == [("contributor/repo", "head-sha")]
    finally:
        root = workspace.root
        workspace.cleanup()
        assert not root.exists()


@pytest.mark.asyncio
async def test_workspace_finds_definitions_in_three_languages() -> None:
    workspace = await PRHeadWorkspace.build(_state(), _TarballGitHub())
    try:
        python_hits = workspace.find_symbol_definitions("normalize", language="python")
        typescript_hits = workspace.find_symbol_definitions("normalize", language="typescript")
        go_hits = workspace.find_symbol_definitions("Normalize", language="go")
        assert [hit.path for hit in python_hits] == ["python/service.py"]
        assert [hit.path for hit in typescript_hits] == ["typescript/service.ts"]
        assert [hit.path for hit in go_hits] == ["go/service.go"]
        assert all("normalize" in hit.excerpt.lower() for hit in python_hits + typescript_hits)
        assert go_hits[0].name == "Normalize"
        assert go_hits[0].file_path == go_hits[0].path
        # Definitions are cached by path/language; the second lookup does not
        # change the deterministic result.
        assert workspace.find_symbol_definitions("normalize", language="python") == python_hits
    finally:
        workspace.cleanup()


@pytest.mark.asyncio
async def test_workspace_finds_callers_and_excludes_definition_lines() -> None:
    workspace = await PRHeadWorkspace.build(_state(), _TarballGitHub())
    try:
        python_hits = workspace.find_callers("normalize", language="python", max_hits=10)
        typescript_hits = workspace.find_callers("normalize", language="typescript", max_hits=10)
        assert [(hit.path, hit.line) for hit in python_hits] == [
            ("python/service.py", 11),
            ("tests/service_spec.py", 5),
        ]
        assert [(hit.path, hit.line) for hit in typescript_hits] == [("typescript/service.ts", 6)]
        assert all("def normalize" not in hit.text for hit in python_hits)
        assert all("function normalize" not in hit.text for hit in typescript_hits)
    finally:
        workspace.cleanup()


@pytest.mark.asyncio
async def test_workspace_caps_extraction_and_marks_truncated() -> None:
    github = _TarballGitHub()
    workspace = await PRHeadWorkspace.build(_state(), github, max_bytes=40)
    try:
        assert workspace.source == "tarball"
        assert workspace.truncated is True
        assert workspace.info.byte_size <= 40
        assert workspace.info.file_count <= 5
        # The fixture's wrapper directory must not leak into the repository
        # paths exposed by read/grep.
        assert not workspace.exists("workspace-repo-test/python/service.py")
    finally:
        workspace.cleanup()


@pytest.mark.asyncio
async def test_workspace_fallback_reads_head_sha_and_search_is_empty() -> None:
    github = _FallbackGitHub()
    workspace = await PRHeadWorkspace.build(_state(), github, max_bytes=200_000_000)
    try:
        assert workspace.source == "api-fallback"
        # Build probes and warms the changed head file before returning.
        assert github.content_calls == [("contributor/repo", "head-sha", "python/service.py")]
        assert workspace.grep("normalize", globs=None, max_hits=10) == []
        assert await workspace.read_async("python/service.py") == "fallback line 1\nfallback line 2\n"
        assert await workspace.read_async("python/service.py", 2, 2) == "2: fallback line 2"
        assert github.content_calls == [("contributor/repo", "head-sha", "python/service.py")]
    finally:
        workspace.cleanup()


@pytest.mark.asyncio
async def test_workspace_fallback_stops_after_first_successful_probe() -> None:
    github = _SequenceFallbackGitHub()
    workspace = await PRHeadWorkspace.build(
        _state(files_changed=["c.py", "b.py", "a.py"]),
        github,
        max_bytes=200_000_000,
    )
    try:
        assert github.content_calls == [
            ("contributor/repo", "head-sha", "a.py"),
            ("contributor/repo", "head-sha", "b.py"),
        ]
        assert await workspace.read_async("b.py") == "head fallback content\n"
        assert github.content_calls == [
            ("contributor/repo", "head-sha", "a.py"),
            ("contributor/repo", "head-sha", "b.py"),
        ]
    finally:
        workspace.cleanup()


@pytest.mark.asyncio
async def test_workspace_fallback_fails_closed_for_deletion_only_pr() -> None:
    github = _UnavailableFallbackGitHub()
    with pytest.raises(WorkspaceUnavailable) as error:
        await PRHeadWorkspace.build(_state(files_changed=["deleted.py"]), github, max_bytes=200_000_000)
    assert error.value.reason == "workspace-unavailable"
    assert str(error.value) == "workspace-unavailable"


@pytest.mark.asyncio
async def test_workspace_rejects_traversal_and_symlink_members() -> None:
    workspace = await PRHeadWorkspace.build(
        _state(files_changed=["src/safe.py"]),
        _TarballGitHub(payload=_malicious_tarball()),
        max_bytes=200_000_000,
    )
    try:
        assert workspace.exists("src/safe.py")
        assert not workspace.exists("src/link.py")
        assert not (workspace.root.parent / "escape.txt").exists()
        assert not (workspace.root.parent.parent / "outside.txt").exists()
    finally:
        workspace.cleanup()


@pytest.mark.asyncio
async def test_gateway_uses_workspace_only_when_pipeline_is_nonlegacy(monkeypatch: pytest.MonkeyPatch) -> None:
    github = _TarballGitHub()
    gateway = ToolGateway(build_registry(), github)
    state = _state()

    monkeypatch.delenv("REVIEWFORGE_PIPELINE", raising=False)
    legacy = await gateway.invoke(
        "read_file", {"file_path": "python/service.py"}, state, agent_name="security_reviewer"
    )
    assert legacy == "fallback line 1\nfallback line 2\n"
    assert github.tarball_calls == []
    assert github.content_calls == [("owner/repo", "head-sha", "python/service.py")]

    monkeypatch.setenv("REVIEWFORGE_PIPELINE", "shadow")
    local = await gateway.invoke("read_file", {"file_path": "python/service.py"}, state, agent_name="security_reviewer")
    assert "return normalize" in (local or "")
    assert github.tarball_calls == [("contributor/repo", "head-sha")]

    state_two = _state(head_sha="other-head-sha")
    workspace_one = await gateway._workspace_for(state)
    workspace_two = await gateway._workspace_for(state_two)
    root_one, root_two = workspace_one.root, workspace_two.root
    assert root_one.exists() and root_two.exists()
    await gateway.cleanup_workspace(state)
    assert not root_one.exists()
    assert root_two.exists()
    await gateway.cleanup_workspace(state_two)
    assert not root_two.exists()


@pytest.mark.asyncio
async def test_gateway_cleanup_workspace_waits_for_inflight_build(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    github = _TarballGitHub()
    gateway = ToolGateway(build_registry(), github)
    state = _state()
    started = asyncio.Event()
    release = asyncio.Event()
    original_build = PRHeadWorkspace.build

    async def slow_build(state_arg: StateStore, github_arg: object, *, max_bytes: int):
        started.set()
        await release.wait()
        return await original_build(state_arg, github_arg, max_bytes=max_bytes)

    monkeypatch.setenv("REVIEWFORGE_PIPELINE", "shadow")
    monkeypatch.setattr(PRHeadWorkspace, "build", slow_build)
    loading = asyncio.create_task(gateway._workspace_for(state))
    await started.wait()
    cleaning = asyncio.create_task(gateway.cleanup_workspace(state))
    await asyncio.sleep(0)
    assert not cleaning.done()
    with pytest.raises(WorkspaceUnavailable, match="cleanup in progress"):
        await gateway._workspace_for(state)
    release.set()
    with pytest.raises(WorkspaceUnavailable, match="cleaned before use"):
        await loading
    await cleaning
    assert not gateway._workspaces
    assert not gateway._workspace_loads


def test_fixture_tarball_contains_a_single_safe_wrapper() -> None:
    with tarfile.open(fileobj=io.BytesIO(FIXTURE_TARBALL.read_bytes()), mode="r:gz") as archive:
        names = archive.getnames()
    assert names
    assert all(name == "workspace-repo-test" or name.startswith("workspace-repo-test/") for name in names)
