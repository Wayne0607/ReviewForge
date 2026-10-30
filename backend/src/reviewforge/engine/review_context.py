"""Fixed-source requirements and reproducible, isolated existing-test execution.

Knowledge is raw source, never agent recollection. Execution is an optional
evidence action: missing Docker/dependencies remains unavailable, not a clean
review or proof of a bug. No source is executed on the review host.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
from copy import deepcopy
from pathlib import PurePosixPath
from typing import Literal
from uuid import uuid4

from pydantic import Field

from reviewforge.engine.session_tools import result_tool
from reviewforge.tools.workspace import WorkspaceUnavailable, _normalise_repo_path

_TOOLS = {"query_review_knowledge", "read_review_knowledge", "run_repository_test"}
_DOC_EXTENSIONS = {".md", ".rst", ".txt", ".adoc"}
_IMAGES = {"python-unittest": "python:3.12-slim", "node-test": "node:24-bookworm-slim"}
_TEST_PATH = re.compile(r"(?:^|/)(?:tests?|__tests__)(?:/|$)|(?:^|/)test_|(?:_test|\.test|\.spec)\.")


class OutputResourceError(RuntimeError):
    """The host capture guard fired; partial output is not a full receipt."""


async def _command(argv: list[str], *, data: bytes | None = None, timeout: float = 60) -> tuple[int, str, str]:
    """Capture complete normal output; stop explicitly at the host memory guard."""
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE if data is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    size = 0

    async def read(stream):
        nonlocal size
        pieces = []
        while block := await stream.read(64 * 1024):
            size += len(block)
            if size > 32 * 1024 * 1024:
                raise OutputResourceError("execution capture exceeded 32 MiB host memory guard")
            pieces.append(block)
        return b"".join(pieces).decode("utf-8", errors="replace")

    async def write():
        if process.stdin is not None:
            try:
                process.stdin.write(data)
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                process.stdin.close()

    tasks = [
        asyncio.create_task(read(process.stdout)),
        asyncio.create_task(read(process.stderr)),
        asyncio.create_task(write()),
        asyncio.create_task(process.wait()),
    ]
    try:
        stdout, stderr, _, code = await asyncio.wait_for(asyncio.gather(*tasks), timeout)
        return code, stdout, stderr
    finally:
        if process.returncode is None:
            process.kill()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        # Windows wait() also waits for pipe transports. Drain after killing;
        # a failed bounded reader must not leave a paused pipe deadlocking exit.
        await asyncio.gather(process.stdout.read(), process.stderr.read(), process.wait())


class ReviewContext:
    def __init__(self, workspace, state, protocol_version=11):
        self.workspace, self.state, self.protocol_version = workspace, state, protocol_version
        # API ingestion owns these snapshots. Never parse issue IDs from prose.
        self._issues = {}
        prefix = f"https://github.com/{state.repo}/issues/"
        for item in deepcopy(getattr(state, "linked_issues", []) or []):
            url = item.get("html_url", item.get("url", ""))
            revision = item.get("updated_at", "")
            if (
                isinstance(url, str)
                and url.casefold().startswith(prefix.casefold())
                and url[len(prefix) :].isdigit()
                and revision
            ):
                content = json.dumps(
                    {"title": item.get("title", ""), "body": item.get("body", "")}, ensure_ascii=False, sort_keys=True
                )
                self._issues["issue:" + url] = {"url": url, "revision": revision, "content": content}

    def owns(self, name):
        return self.protocol_version >= 11 and name in _TOOLS

    def tool_definitions(self):
        if self.protocol_version < 11:
            return []
        return [
            result_tool(
                "query_review_knowledge",
                "Find fixed repository docs and authoritative linked issue snapshots. "
                "Directory entries are navigation, not contracts. Empty query lists all available documents.",
                {"query": (str, ""), "paths": (list[str], Field(default_factory=list))},
            ),
            result_tool(
                "read_review_knowledge",
                "Read raw documentation or issue snapshot from this run's knowledge "
                "directory. Decide whether its precise text establishes the required behavior; never treat "
                "agent notes or an issue's proposed fix as established execution.",
                {"path": (str, ...)},
            ),
            result_tool(
                "run_repository_test",
                "Run one existing repository test in a disposable, network-disabled "
                "container at the pinned head. No installation, arbitrary commands or host execution. "
                "Missing runtime/dependencies is an explicit limitation, not proof of a bug.",
                {"runtime": (Literal["python-unittest", "node-test"], ...), "path": (str, ...)},
            ),
        ]

    def _envelope(self, status, **fields):
        return {
            "status": status,
            "head_sha": self.workspace.head_sha,
            "workspace_digest": self.workspace.digest,
            **fields,
        }

    def _documents(self):
        return [
            path
            for path in self.workspace.list_paths()
            if PurePosixPath(path).suffix.lower() in _DOC_EXTENSIONS
            or PurePosixPath(path).name.upper() in {"README", "AGENTS", "CONTRIBUTING"}
        ]

    async def execute(self, name, args):
        if not self.owns(name):
            raise ValueError("review context tool unavailable in this protocol")
        schema = next(tool.args_schema for tool in self.tool_definitions() if tool.name == name)
        args = schema.model_validate(args).model_dump()
        try:
            if name == "query_review_knowledge":
                query = args["query"].casefold().split()
                paths = [path.replace("\\", "/") for path in args["paths"]]
                entries = []
                for path in self._documents():
                    if paths and not any(path.startswith(p.rstrip("/") + "/") or path == p for p in paths):
                        continue
                    if query and not any(term in path.casefold() for term in query):
                        content = await self.workspace.read_async(path)
                        if not content or not any(term in content.casefold() for term in query):
                            continue
                    entries.append({"path": path, "kind": "documentation", "revision": self.workspace.head_sha})
                for path, item in self._issues.items():
                    if not query or any(term in item["content"].casefold() for term in query):
                        entries.append(
                            {"path": path, "kind": "issue", "url": item["url"], "revision": item["revision"]}
                        )
                result = self._envelope(
                    "navigation",
                    entries=entries,
                    issue_snapshot_count=len(self._issues),
                    issue_context_status=getattr(self.state, "linked_issue_context", {}).get("status", "not-captured"),
                    issue_context_coverage=getattr(self.state, "linked_issue_context", {}).get("coverage", "unknown"),
                    complete_workspace=not self.workspace.truncated and self.workspace.source == "tarball",
                )
            elif name == "read_review_knowledge":
                path = args["path"]
                if path in self._issues:
                    result = self._envelope("success", kind="issue", path=path, **self._issues[path])
                elif _normalise_repo_path(path) == path and path in self._documents():
                    content = await self.workspace.read_async(path)
                    result = (
                        self._envelope(
                            "success",
                            kind="documentation",
                            path=path,
                            revision=self.workspace.head_sha,
                            content=content,
                        )
                        if content is not None
                        else (self._envelope("unavailable", reason="document-source-unavailable"))
                    )
                else:
                    result = self._envelope("unavailable", reason="path-not-in-fixed-knowledge-directory")
                if result["status"] == "success":
                    result["content_sha256"] = hashlib.sha256(result["content"].encode()).hexdigest()
            else:
                result = await self._run_test(args["runtime"], args["path"])
        except (OSError, WorkspaceUnavailable) as exc:
            result = self._envelope("unavailable", reason=str(exc))
        return json.dumps(result, ensure_ascii=False, sort_keys=True)

    async def _run_test(self, runtime, path):
        if _normalise_repo_path(path) != path or not _TEST_PATH.search(path) or not self.workspace.exists(path):
            return self._envelope("unavailable", reason="target-must-be-an-existing-repository-test")
        target = PurePosixPath(path)
        if runtime == "python-unittest" and target.suffix == ".py":
            entrypoint, argv = (
                "python",
                ["-B", "-m", "unittest", "discover", "-s", str(target.parent), "-p", target.name],
            )
        elif runtime == "node-test" and target.suffix in {".js", ".mjs", ".cjs"}:
            entrypoint, argv = "node", ["--test", path]
        else:
            return self._envelope("unavailable", reason="test-framework-or-extension-not-supported")
        docker = shutil.which("docker")
        if not docker:
            return self._envelope("unavailable", reason="docker-not-installed")
        image = os.environ.get("REVIEWFORGE_TEST_IMAGE_" + runtime.replace("-", "_").upper(), _IMAGES[runtime])
        container = "reviewforge-test-" + uuid4().hex
        created = False
        preparation = container + "-source"
        prepared_tag = "reviewforge-test:" + container.removeprefix("reviewforge-test-")
        identity = {"runtime": runtime, "path": path, "argv": [entrypoint, *argv]}
        try:
            code, output, error = await _command([docker, "image", "inspect", "--format", "{{.Id}}", image])
            image_id = output.strip()
            if code or not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
                return self._envelope("unavailable", **identity, reason="local-runtime-image-unavailable", detail=error)
            source = await asyncio.to_thread(self.workspace.export_source_tar)
            identity.update(image_id=image_id, source_sha256=hashlib.sha256(source).hexdigest())
            create = [
                docker,
                "create",
                "--name",
                container,
                "--network",
                "none",
                "--read-only",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--user",
                "65534:65534",
                "--pids-limit",
                "64",
                "--cpus",
                "1",
                "--memory",
                "256m",
                "--memory-swap",
                "256m",
                "--tmpfs",
                "/tmp:rw,nosuid,noexec,size=64m",
                "--workdir",
                "/workspace",
                "--entrypoint",
                entrypoint,
                image_id,
                *argv,
            ]
            # The preparation container is never started. Docker cannot copy into
            # a read-only rootfs, so commit the fixed source first and start only
            # a separate read-only container. Nothing is extracted on the host.
            created = True
            prepare = create.copy()
            prepare[prepare.index(container)] = preparation
            prepare.remove("--read-only")
            code, output, error = await _command(prepare)
            if code:
                return self._envelope("unavailable", **identity, reason="container-create-failed", detail=error)
            code, output, error = await _command([docker, "cp", "-", preparation + ":/"], data=source)
            if code:
                return self._envelope("unavailable", **identity, reason="source-transfer-failed", detail=error)
            code, output, error = await _command([docker, "commit", preparation, prepared_tag])
            prepared_id = output.strip()
            if code or not re.fullmatch(r"sha256:[0-9a-f]{64}", prepared_id):
                return self._envelope("unavailable", **identity, reason="source-image-failed", detail=error)
            identity["execution_image_id"] = prepared_id
            create[create.index(image_id)] = prepared_id
            code, output, error = await _command(create)
            if code:
                return self._envelope("unavailable", **identity, reason="execution-container-failed", detail=error)
            code, output, error = await _command([docker, "start", "--attach", container])
            # Docker start's CLI failure must not masquerade as an executed test.
            inspected, state, inspection_error = await _command(
                [docker, "inspect", "--format", "{{json .State}}", container]
            )
            execution = json.loads(state) if inspected == 0 else {}
            if execution.get("Status") != "exited" or not execution.get("StartedAt", "").startswith("20"):
                return self._envelope(
                    "unavailable", **identity, reason="test-did-not-execute", detail=error or inspection_error
                )
            if execution.get("OOMKilled"):
                return self._envelope(
                    "resource-limit", **identity, reason="container-memory-guard", stdout=output, stderr=error
                )
            return self._envelope(
                "success",
                **identity,
                exit_code=execution["ExitCode"],
                stdout=output,
                stderr=error,
                started_at=execution["StartedAt"],
                finished_at=execution["FinishedAt"],
            )
        except TimeoutError:
            return self._envelope("timeout", **identity, reason="execution-host-wall-time-guard")
        except OutputResourceError as exc:
            return self._envelope("resource-limit", **identity, reason=str(exc))
        finally:
            if created:

                async def remove_resources():
                    await _command([docker, "rm", "--force", container, preparation])
                    await _command([docker, "image", "rm", "--force", prepared_tag])

                cleanup = asyncio.create_task(remove_resources())
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    await cleanup
                    raise
