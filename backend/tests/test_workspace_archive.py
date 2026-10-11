"""Archive source must remain available independently of host file handling."""

from __future__ import annotations

import hashlib
import io
import tarfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import reviewforge.tools.workspace as workspace_module
from reviewforge.core.state import StateStore
from reviewforge.tools.workspace import PRHeadWorkspace


def archive(files: dict[str, bytes], *, extra_members=()) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as source:
        for path, content in files.items():
            member = tarfile.TarInfo("repo-head/" + path)
            member.size = len(content)
            member.mtime = 1_700_000_000
            source.addfile(member, io.BytesIO(content))
        for member in extra_members:
            source.addfile(member)
    return output.getvalue()


class GitHub:
    def __init__(self, payload: bytes):
        self.payload = payload
        self.content_calls = []

    async def get_repo_tarball(self, repo, sha):
        assert (repo, sha) == ("fork/repo", "fixed-head")
        return self.payload

    async def get_file_content(self, *args):
        self.content_calls.append(args)
        raise AssertionError("archive reads must not use another network source")


def state():
    return StateStore(repo="owner/repo", head_repo="fork/repo", head_sha="fixed-head", files_changed=["src/service.py"])


@pytest.fixture
def use_archive(monkeypatch):
    # Exercise the Windows source view on Linux CI as well.
    monkeypatch.setattr(workspace_module, "_use_archive_view", lambda: True, raising=False)


@pytest.mark.asyncio
async def test_archive_search_never_materializes_or_reads_untrusted_source(use_archive, monkeypatch):
    files = {
        "config/initializers/logster.rb": b"logger = true\n",
        "src/service.py": b"def normalize(value):\n    return value\n\nnormalize(1)\n",
    }
    github = GitHub(archive(files))
    workspace = await PRHeadWorkspace.build(state(), github)
    original = Path.read_text

    def blocked(path, *args, **kwargs):
        if path.is_relative_to(workspace.root):
            raise OSError(22, "host blocked source read")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", blocked)
    try:
        assert list(workspace.root.rglob("*")) == []
        assert workspace.info.storage == "archive-memory"
        assert workspace.exists("config/initializers/logster.rb")
        assert workspace.read("src/service.py") == files["src/service.py"].decode()
        assert [(h.path, h.line) for h in workspace.grep("normalize", globs=None, max_hits=10)] == [
            ("src/service.py", 1),
            ("src/service.py", 4),
        ]
        assert [h.path for h in workspace.find_symbol_definitions("normalize", language="python")] == ["src/service.py"]
        assert [h.line for h in workspace.find_callers("normalize", language="python", max_hits=10)] == [4]
        assert github.content_calls == []
    finally:
        workspace.cleanup()


@pytest.mark.asyncio
async def test_archive_manifest_and_text_match_filesystem_source(monkeypatch):
    files = {"src/service.py": "value = '中文'\r\nother = 2\rfinal = 3\n".encode(), "src/empty.py": b""}
    payload = archive(files)
    monkeypatch.setattr(workspace_module, "_use_archive_view", lambda: False, raising=False)
    physical = await PRHeadWorkspace.build(state(), GitHub(payload))
    monkeypatch.setattr(workspace_module, "_use_archive_view", lambda: True, raising=False)
    virtual = await PRHeadWorkspace.build(state(), GitHub(payload))
    try:
        assert virtual.info.storage == "archive-memory"
        assert virtual.info.archive_sha256 == hashlib.sha256(payload).hexdigest()
        assert (virtual.info.file_count, virtual.info.byte_size, virtual.digest) == (
            physical.info.file_count,
            physical.info.byte_size,
            physical.digest,
        )
        for path in files:
            assert virtual.read(path) == physical.read(path)
            assert virtual.exists(path)
        assert virtual.read("src/service.py", 2, 2) == "2: other = 2"
        assert await virtual.read_async("src/empty.py") == ""
        assert not virtual.exists("missing.py")
        assert virtual.read("../outside.py") is None
    finally:
        physical.cleanup()
        virtual.cleanup()


@pytest.mark.asyncio
async def test_archive_view_keeps_case_distinct_paths_and_rejects_links(use_archive):
    symlink = tarfile.TarInfo("repo-head/src/link.py")
    symlink.type, symlink.linkname = tarfile.SYMTYPE, "../../outside.py"
    hardlink = tarfile.TarInfo("repo-head/src/hard.py")
    hardlink.type, hardlink.linkname = tarfile.LNKTYPE, "repo-head/src/Case.py"
    files = {"src/Case.py": b"upper = True\n", "src/case.py": b"lower = True\n", "../escape.py": b"unsafe\n"}
    workspace = await PRHeadWorkspace.build(state(), GitHub(archive(files, extra_members=[symlink, hardlink])))
    try:
        assert workspace.info.file_count == 2
        assert workspace.read("src/Case.py") == "upper = True\n"
        assert workspace.read("src/case.py") == "lower = True\n"
        assert not workspace.exists("src/link.py")
        assert not workspace.exists("src/hard.py")
        assert not workspace.exists("../escape.py")
        assert workspace.grep("True", globs=None, max_hits=10)[0].path == "src/Case.py"
    finally:
        workspace.cleanup()


@pytest.mark.asyncio
async def test_invalid_archive_body_does_not_become_a_complete_source(use_archive):
    member = tarfile.TarInfo("repo-head/src/service.py")
    member.size = 4096
    payload = member.tobuf() + b"short body"

    class FallbackGitHub(GitHub):
        async def get_file_content(self, repo, sha, path):
            self.content_calls.append((repo, sha, path))
            return "from_fixed_head = True\n"

    github = FallbackGitHub(payload)
    workspace = await PRHeadWorkspace.build(state(), github)
    try:
        assert workspace.source == "api-fallback"
        assert workspace._archive_view is None
        assert await workspace.read_async("src/service.py") == "from_fixed_head = True\n"
        assert workspace.grep("from_fixed_head", globs=None, max_hits=10) == []
        assert github.content_calls == [("fork/repo", "fixed-head", "src/service.py")]
    finally:
        workspace.cleanup()


@pytest.mark.asyncio
async def test_archive_view_uses_existing_truncation_policy_and_releases_storage(use_archive):
    files = {"src/service.py": b"x = 1\n", "src/other.py": b"y = 2\n", "docs/omitted.md": b"outside source roots\n"}
    workspace = await PRHeadWorkspace.build(state(), GitHub(archive(files)), max_bytes=10)
    try:
        assert workspace.truncated
        assert workspace.info.byte_size == len(files["src/service.py"])
        assert workspace.read("src/service.py") == "x = 1\n"
        assert workspace.read("src/other.py") is None
        assert workspace.read("docs/omitted.md") is None
    finally:
        workspace.cleanup()
    workspace.cleanup()
    assert workspace._archive_view is None
    assert workspace.read("src/service.py") is None
    assert workspace.grep("x", globs=None, max_hits=10) == []
    assert not workspace.root.exists()


@pytest.mark.asyncio
async def test_parallel_archive_reads_preserve_exact_file_binding(use_archive):
    files = {f"src/file_{i}.py": (f"v = {i}\n" * 300).encode() for i in range(12)}
    workspace = await PRHeadWorkspace.build(state(), GitHub(archive(files)))
    try:
        paths = list(files) * 4
        with ThreadPoolExecutor(max_workers=4) as threads:
            values = list(threads.map(workspace.read, paths))
        assert values == [files[path].decode() for path in paths]
        expected = "".join(f"{p}:{len(files[p])}:1700000000\n" for p in sorted(files))
        assert workspace.digest == hashlib.sha256(expected.encode()).hexdigest()
    finally:
        workspace.cleanup()
