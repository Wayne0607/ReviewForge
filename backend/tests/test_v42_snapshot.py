from types import SimpleNamespace

import pytest

from reviewforge.review_v42.snapshot import read_snapshot_bytes, snapshot_identity
from reviewforge.tools.workspace import WorkspaceUnavailable


class MemoryWorkspace:
    source = "tarball"
    truncated = False

    def __init__(self, files):
        self.files = files

    def list_paths(self):
        return sorted(self.files)

    def read_bytes(self, path):
        return self.files[path]


async def test_raw_bytes_and_drift_with_same_size_are_checked():
    ws = MemoryWorkspace({"a.py": b"abc\r\ndef\r\n", "b.py": b"one"})
    first = await snapshot_identity(ws)
    assert await read_snapshot_bytes(ws, "a.py") == b"abc\r\ndef\r\n"
    other = MemoryWorkspace({"a.py": b"xyz\r\ndef\r\n", "b.py": b"one"})
    assert await snapshot_identity(other) != first
    ws.files["a.py"] = b"xyz\r\ndef\r\n"
    with pytest.raises(WorkspaceUnavailable):
        await read_snapshot_bytes(ws, "a.py")
    with pytest.raises(WorkspaceUnavailable):
        await snapshot_identity(ws)


async def test_non_utf8_and_partial_source_fail_closed():
    ws = MemoryWorkspace({"x": b"\xff", "bin": b"x\x00y"})
    await snapshot_identity(ws)
    with pytest.raises(UnicodeDecodeError):
        await read_snapshot_bytes(ws, "x")
    with pytest.raises(WorkspaceUnavailable):
        await read_snapshot_bytes(ws, "bin")
    ws.truncated = True
    with pytest.raises(WorkspaceUnavailable):
        await snapshot_identity(ws)


async def test_materialized_workspace_preserves_bytes_and_rejects_symlinks(tmp_path):
    (tmp_path / "a").write_bytes(b"foo\r\n")
    ws = MemoryWorkspace({})
    ws.info = SimpleNamespace(root=tmp_path)
    ws._local_path = lambda path: tmp_path / path
    ws.list_paths = lambda: ["a"]
    await snapshot_identity(ws)
    assert await read_snapshot_bytes(ws, "a") == b"foo\r\n"
    (tmp_path / "a").write_bytes(b"bar\r\n")
    with pytest.raises(WorkspaceUnavailable):
        await read_snapshot_bytes(ws, "a")


async def test_must_not_fall_back_to_lossy_text_and_invalid_path_rejected():
    ws = SimpleNamespace(source="tarball", truncated=False, list_paths=lambda: ["a"], read_async=lambda p: "text")
    with pytest.raises(WorkspaceUnavailable):
        await snapshot_identity(ws)
    with pytest.raises(ValueError):
        await read_snapshot_bytes(ws, "../secrets")


@pytest.mark.parametrize("archive_view", [True, False])
async def test_real_workspace_uses_original_tarball_bytes(archive_view, monkeypatch):
    import io
    import tarfile

    from reviewforge.core.state import StateStore
    from reviewforge.tools import workspace as workspace_module

    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        payload = b"a = 1\r\nb = 2\r\n"
        info = tarfile.TarInfo("project/src/a.py")
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))

    class Github:
        async def get_repo_tarball(self, repo, sha):
            return output.getvalue()

    monkeypatch.setattr(workspace_module, "_use_archive_view", lambda: archive_view)
    state = StateStore(repo="a/b", head_sha="head", files_changed=["src/a.py"])
    ws = await workspace_module.PRHeadWorkspace.build(state, Github())
    try:
        await snapshot_identity(ws)
        assert await read_snapshot_bytes(ws, "src/a.py") == payload
        if not archive_view:
            (ws.info.root / "extra").write_bytes(b"unexpected")
            with pytest.raises(WorkspaceUnavailable):
                await snapshot_identity(ws)
    finally:
        ws.cleanup()
