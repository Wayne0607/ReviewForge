"""Fixed knowledge and isolated execution must never fabricate a source receipt."""

import asyncio
import io
import json
import tarfile

import pytest

from reviewforge.core.state import StateStore
from reviewforge.engine import review_context
from reviewforge.engine.review_context import ReviewContext
from reviewforge.tools.workspace import PRHeadWorkspace


@pytest.fixture
async def context(monkeypatch):
    monkeypatch.setattr("reviewforge.tools.workspace._use_archive_view", lambda: True)
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for path, value in {
            "README.md": b"Callers must receive 404 for missing resources.",
            "tests/test_sample.py": b"import unittest\n",
            "tests/sample.test.mjs": b"import test from 'node:test';\n",
            "src/asset.bin": b"\x00\xff\r\n",
        }.items():
            entry = tarfile.TarInfo("root/" + path)
            entry.size = len(value)
            archive.addfile(entry, io.BytesIO(value))

    class GitHub:
        async def get_repo_tarball(self, *_):
            return output.getvalue()

    state = StateStore(
        repo="owner/repo",
        head_sha="fixed",
        files_changed=["README.md"],
        linked_issues=[
            {
                "html_url": "https://github.com/owner/repo/issues/3",
                "updated_at": "2026-01-01T00:00:00Z",
                "title": "Missing resources",
                "body": "The specified response is 404.",
            },
            {"html_url": "https://github.com/elsewhere/repo/issues/3", "updated_at": "now", "body": "not linked"},
        ],
    )
    workspace = await PRHeadWorkspace.build(state, GitHub())
    try:
        yield ReviewContext(workspace, state, 11)
    finally:
        workspace.cleanup()


async def test_fixed_raw_knowledge_and_issue_snapshot(context):
    from reviewforge.engine.knowledge_receipt import validate_knowledge_receipt

    index = json.loads(await context.execute("query_review_knowledge", {"query": "404"}))
    assert index["status"] == "navigation"
    assert len(index["entries"]) == 2
    context.state.linked_issues[0]["body"] = "later edited issue must not change the snapshot"
    for entry in index["entries"]:
        receipt = json.loads(await context.execute("read_review_knowledge", {"path": entry["path"]}))
        validate_knowledge_receipt(receipt, head_sha="fixed", workspace_digest=context.workspace.digest)
        assert "404" in receipt["content"]
        assert "later edited" not in receipt["content"]
    assert (
        json.loads(await context.execute("read_review_knowledge", {"path": "../README.md"}))["status"] == "unavailable"
    )
    assert not ReviewContext(context.workspace, context.state, 10).tool_definitions()


async def test_export_is_exact_deterministic_and_never_materialized(context):
    first = context.workspace.export_source_tar()
    assert context.workspace.export_source_tar() == first
    assert not list(context.workspace.root.rglob("*"))
    with tarfile.open(fileobj=io.BytesIO(first)) as archive:
        assert archive.extractfile("workspace/src/asset.bin").read() == b"\x00\xff\r\n"
        assert all(member.isfile() or member.isdir() for member in archive)
    from dataclasses import replace

    context.workspace.info = replace(context.workspace.info, truncated=True)
    with pytest.raises(Exception, match="complete fixed source"):
        context.workspace.export_source_tar()


def simulated_docker(monkeypatch, *, cancel=False, started=True):
    monkeypatch.setattr(review_context.shutil, "which", lambda _: "docker")
    calls = []

    async def execute(argv, **kwargs):
        calls.append((argv, kwargs))
        command = argv[1]
        if command == "image" and argv[2] == "inspect":
            return 0, "sha256:" + "a" * 64, ""
        if command == "commit":
            return 0, "sha256:" + "b" * 64, ""
        if command == "start":
            if cancel:
                raise asyncio.CancelledError()
            return 1, "test output", "assertion failed"
        if command == "inspect":
            return (
                0,
                json.dumps(
                    {
                        "Status": "exited" if started else "created",
                        "StartedAt": "2026-01-01",
                        "FinishedAt": "2026-01-01",
                        "ExitCode": 1,
                        "OOMKilled": False,
                    }
                ),
                "",
            )
        return 0, "", ""

    monkeypatch.setattr(review_context, "_command", execute)
    return calls


async def test_execution_is_bound_readonly_unprivileged_and_cleans_up(context, monkeypatch):
    calls = simulated_docker(monkeypatch)
    receipt = json.loads(
        await context.execute("run_repository_test", {"runtime": "python-unittest", "path": "tests/test_sample.py"})
    )
    assert receipt["status"] == "success" and receipt["exit_code"] == 1
    assert receipt["stdout"] == "test output"
    assert receipt["image_id"] == "sha256:" + "a" * 64
    assert receipt["execution_image_id"] == "sha256:" + "b" * 64
    creates = [argv for argv, _ in calls if argv[1] == "create"]
    assert len(creates) == 2 and "--read-only" not in creates[0] and "--read-only" in creates[1]
    run = creates[1]
    assert run[run.index("--network") + 1] == "none"
    assert run[run.index("--user") + 1] == "65534:65534"
    assert "--mount" not in run and "--volume" not in run and "--env" not in run
    assert not any(argv[1] == "start" and argv[-1].endswith("-source") for argv, _ in calls)
    assert calls[-2][0][1:3] == ["rm", "--force"]
    assert calls[-1][0][1:4] == ["image", "rm", "--force"]


async def test_cancel_cleans_container_and_source_image(context, monkeypatch):
    calls = simulated_docker(monkeypatch, cancel=True)
    with pytest.raises(asyncio.CancelledError):
        await context.execute("run_repository_test", {"runtime": "node-test", "path": "tests/sample.test.mjs"})
    assert calls[-2][0][1:3] == ["rm", "--force"]
    assert calls[-1][0][1:4] == ["image", "rm", "--force"]


async def test_unstarted_docker_is_not_execution_evidence(context, monkeypatch):
    simulated_docker(monkeypatch, started=False)
    result = json.loads(
        await context.execute("run_repository_test", {"runtime": "node-test", "path": "tests/sample.test.mjs"})
    )
    assert result["status"] == "unavailable" and result["reason"] == "test-did-not-execute"


async def test_arbitrary_paths_and_missing_docker_never_run(context, monkeypatch):
    calls = simulated_docker(monkeypatch)
    for path in ("../tests/test_sample.py", "README.md", "tests/missing.py"):
        receipt = json.loads(await context.execute("run_repository_test", {"runtime": "python-unittest", "path": path}))
        assert receipt["status"] == "unavailable"
    assert not calls
    monkeypatch.setattr(review_context.shutil, "which", lambda _: None)
    receipt = json.loads(
        await context.execute("run_repository_test", {"runtime": "python-unittest", "path": "tests/test_sample.py"})
    )
    assert receipt["status"] == "unavailable" and not calls


async def test_capture_machine_guard_and_cancel_are_explicit(monkeypatch):
    import sys

    assert await review_context._command([sys.executable, "-c", "print('receipt')"]) == (
        0,
        "receipt\r\n" if sys.platform == "win32" else "receipt\n",
        "",
    )
    with pytest.raises(TimeoutError):
        await review_context._command([sys.executable, "-c", "import time; time.sleep(10)"], timeout=0.05)
    with pytest.raises(review_context.OutputResourceError):
        await review_context._command([sys.executable, "-c", "import sys; sys.stdout.write('a' * (33*1024*1024))"])
