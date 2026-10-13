"""References navigate literal bindings without inventing a resolved caller graph."""

import io
import tarfile
from collections import Counter
from types import SimpleNamespace

import pytest

import reviewforge.tools.workspace as workspace_module
from reviewforge.core.state import StateStore
from reviewforge.tools.workspace import PRHeadWorkspace


def archive(files):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as tar:
        for path, text in files.items():
            body = text.encode() if isinstance(text, str) else text
            member = tarfile.TarInfo("fixture/" + path)
            member.size = len(body)
            tar.addfile(member, io.BytesIO(body))
    return output.getvalue()


async def build(files):
    async def get_repo_tarball(repo, ref):
        assert (repo, ref) == ("example/repo", "fixed-head")
        return archive(files)

    return await PRHeadWorkspace.build(
        StateStore(repo="example/repo", head_sha="fixed-head", files_changed=[next(iter(files))]),
        SimpleNamespace(get_repo_tarball=get_repo_tarball),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("archive_view", [False, True])
async def test_literal_callback_property_and_jsx_references_keep_exact_source(monkeypatch, archive_view):
    monkeypatch.setattr(workspace_module, "_use_archive_view", lambda: archive_view)
    files = {
        "handler.ts": "export const saveHandler = async () => {};\n",
        "router.ts": 'cache.save = await import("./handler").then((mod) => mod.saveHandler);\n',
        "dialog.tsx": "  const mutation = rpc.viewer.save.useMutation();\n",
        "list.tsx": "  return <SaveDialog itemId={item.id} />;\n",
    }
    workspace = await build(files)
    try:
        assert workspace.find_callers("saveHandler", language="", max_hits=10) == []
        result = workspace.find_references("saveHandler", path="handler.ts")
        assert [h.path for h in result.hits] == ["router.ts", "handler.ts"]
        assert not result.truncated and result.scanned_files == 4
        assert workspace.find_references("viewer.save").hits[0].path == "dialog.tsx"
        assert workspace.find_references("SaveDialog").hits[0].path == "list.tsx"
        for symbol in ("saveHandler", "viewer.save", "SaveDialog"):
            for hit in workspace.find_references(symbol).hits:
                assert hit.text == files[hit.path].splitlines()[hit.line - 1]
        assert workspace.head_sha == "fixed-head"
    finally:
        workspace.cleanup()


@pytest.mark.asyncio
async def test_reference_limits_prioritize_other_files_and_diversity():
    files = {"a-origin.ts": "target\n" * 80}
    files.update({f"b-{i:02}.ts": "target\n" * 3 for i in range(25)})
    workspace = await build(files)
    try:
        result = workspace.find_references("target", path="a-origin.ts", max_hits=999)
        assert len(result.hits) == result.max_hits == 20
        assert len({h.path for h in result.hits}) == 20
        assert all(h.path != "a-origin.ts" and h.line == 1 for h in result.hits)
        assert result.truncated
        smaller = workspace.find_references("target", path="a-origin.ts", globs=["*-0[01].ts", "a-origin.ts"])
        assert [h.path for h in smaller.hits] == [
            "b-00.ts",
            "b-01.ts",
            "b-00.ts",
            "b-01.ts",
            "a-origin.ts",
            "a-origin.ts",
        ]
        assert max(Counter(h.path for h in smaller.hits).values()) == smaller.per_file_limit == 2
        assert smaller.truncated
    finally:
        workspace.cleanup()


@pytest.mark.asyncio
async def test_identifier_boundaries_are_literal_and_glob_is_actual_filter():
    workspace = await build(
        {
            "src/a.ts": "foo\nfoobar\n$foo\nfoo$\nviewer.foo\nviewerXfoo\nviewer.foobar\n$task\n",
            "docs/api.md": "viewer.foo is a textual reference, not a resolved call.\n",
            "config/settings.yml": "operation: viewer.foo\n",
        }
    )
    try:
        assert [h.line for h in workspace.find_references("foo", globs=["src/**"]).hits] == [1, 5]
        assert [h.line for h in workspace.find_references("$task").hits] == [8]
        dotted = workspace.find_references("viewer.foo", path="docs/api.md", globs=["**/*.md", "**/*.yml"])
        assert [h.path for h in dotted.hits] == ["config/settings.yml", "docs/api.md"]
        assert not dotted.truncated
    finally:
        workspace.cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize("symbol", ["", "target.*", "target()", "x y", "a..b", "1invalid", "x\ny", "x" * 257])
async def test_invalid_reference_queries_do_not_scan(monkeypatch, symbol):
    workspace = await build({"a.ts": "target\n"})
    try:
        monkeypatch.setattr(workspace, "_iter_files", lambda: pytest.fail("must reject before scan"))
        with pytest.raises(ValueError, match="identifier"):
            workspace.find_references(symbol)
    finally:
        workspace.cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize("archive_view", [False, True])
async def test_assets_and_large_text_are_not_decoded_or_cached(monkeypatch, archive_view):
    monkeypatch.setattr(workspace_module, "_use_archive_view", lambda: archive_view)
    workspace = await build(
        {
            "src/a.ts": "target\n",
            "README": "target\n",
            "settings.properties": "target=value\n",
            "assets/demo.mp4": b"target\x00",
            "unknown.blob": b"target\x00",
            "huge.json": b"target" + b" " * (2 * 1024 * 1024),
        }
    )
    try:
        original = workspace._read_local

        def guarded(relative, candidate):
            assert relative not in {"assets/demo.mp4", "unknown.blob", "huge.json"}
            return original(relative, candidate)

        monkeypatch.setattr(workspace, "_read_local", guarded)
        result = workspace.find_references("target")
        assert {h.path for h in result.hits} == {"src/a.ts", "README", "settings.properties"}
        assert result.scanned_files == 3 and result.skipped_files == 3 and result.skipped_large_files == 1
        assert not result.truncated  # hit truncation is distinct from lookup coverage
        assert not ({"assets/demo.mp4", "unknown.blob", "huge.json"} & workspace._content_cache.keys())
        assert workspace.info.file_count == 6
    finally:
        workspace.cleanup()


@pytest.mark.asyncio
async def test_invalid_origin_path_is_rejected_and_closed_lookup_reports_unavailable():
    workspace = await build({"a.ts": "target\n"})
    try:
        with pytest.raises(ValueError, match="path"):
            workspace.find_references("target", path="../elsewhere.ts")
        assert workspace.find_references("target", path="unseen.ts").hits
    finally:
        workspace.cleanup()
    result = workspace.find_references("target")
    assert not result.hits and result.unavailable and result.scanned_files == 0
