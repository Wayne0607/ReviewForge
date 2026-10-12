"""Tool Gateway — async single entry point for all tool execution.

D3: 真正做权限门控 — 按 agent 的 allowed_tools 校验，
写工具 post_comment 仅限 commenter/orchestrator 身份。
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import replace
from typing import Any

import httpx

from reviewforge.core.change_manifest import (
    FileChange,
    file_changes_from_pr_files,
    is_verified_rename,
    populate_file_changes,
    valid_path,
    valid_repo,
    valid_sha,
)
from reviewforge.core.specs import SpecRegistry
from reviewforge.core.state import StateStore
from reviewforge.tools.github_api import MAX_REVIEW_COMMENTS_PER_REQUEST, GitHubClient
from reviewforge.tools.workspace import DEFAULT_MAX_BYTES, PRHeadWorkspace, WorkspaceUnavailable

logger = logging.getLogger(__name__)

_METADATA_ERROR_TYPES = frozenset(
    {
        "HTTPStatusError",
        "ConnectError",
        "ReadError",
        "RemoteProtocolError",
        "DecodingError",
        "ConnectTimeout",
        "ReadTimeout",
        "WriteTimeout",
        "PoolTimeout",
        "TimeoutException",
        "OSError",
        "ValueError",
        "TypeError",
        "KeyError",
        "AttributeError",
        "RuntimeError",
    }
)


def _metadata_failure(stage: str, error: Exception) -> str:
    """Fixed diagnostic labels only; provider bodies and exception text stay out."""
    kind = type(error).__name__
    if isinstance(error, TimeoutError):
        kind = "timeout"
    elif kind not in _METADATA_ERROR_TYPES:
        kind = "other-exception"
    elif isinstance(error, httpx.TimeoutException):
        kind = "timeout-" + kind
    return stage + ":" + kind


def _workspace_max_bytes() -> int:
    """Read the temporary T2 limit override without changing legacy config."""

    raw = os.environ.get("REVIEWFORGE_WORKSPACE_MAX_BYTES")
    if raw is None:
        return DEFAULT_MAX_BYTES
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return DEFAULT_MAX_BYTES


# JSON Schema type → Python type(s), for lightweight param validation (no jsonschema dep).
_JSON_PY_TYPES: dict[str, Any] = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "object": dict,
    "array": list,
}


class ToolGateway:
    """Executes tools with permission and schema checks."""

    def __init__(
        self,
        registry: SpecRegistry,
        github: GitHubClient,
        *,
        pipeline_mode: str | None = None,
        workspace_max_bytes: int | None = None,
    ) -> None:
        self._registry = registry
        self._github = github
        self._pipeline_mode = pipeline_mode
        self._workspace_max_bytes = workspace_max_bytes
        self._handlers: dict[str, Any] = {
            "read_diff": self._read_diff,
            "read_file": self._read_file,
            "search_code": self._search_code,
            "get_change_context": self._get_change_context,
            "post_comment": self._post_comment,
            "post_review": self._post_review,
        }
        # Concurrent Phase-0 reads for one StateStore share a single PR-files
        # request. Entries live only while the bulk request is in flight; the
        # resulting patches are owned by the per-run StateStore.
        self._diff_loads: dict[int, asyncio.Task[dict[str, str]]] = {}
        self._change_loads: dict[int, asyncio.Task[dict[str, FileChange]]] = {}
        # Hypothesis/shadow tool reads share one pinned workspace per run.  The
        # map is intentionally keyed by StateStore identity because StateStore
        # is mutable and therefore not hashable.
        self._workspaces: dict[int, PRHeadWorkspace] = {}
        self._workspace_loads: dict[int, asyncio.Task[PRHeadWorkspace]] = {}
        # Keep the StateStore object alongside id(state): retaining the object
        # prevents Python id reuse from aliasing a later PR to an old snapshot.
        self._workspace_states: dict[int, StateStore] = {}
        # A generation makes cleanup safe when it races a build that is still
        # being awaited by another tool invocation.
        self._workspace_generations: dict[int, int] = {}
        self._workspace_cleaning: set[int] = set()

    async def invoke(self, tool_name: str, params: dict[str, Any], state: StateStore, agent_name: str = "") -> Any:
        """Execute a tool with full gating.

        Args:
            tool_name: 工具名
            params: 工具参数
            state: 共享状态
            agent_name: 调用方身份（用于权限校验）
        """
        if tool_name not in self._registry.tools:
            raise KeyError(f"Unknown tool: {tool_name}")

        # D3: 权限校验 — agent 的 allowed_tools 是否包含该工具
        if agent_name:
            spec = self._registry.agents.get(agent_name)
            if spec is not None and tool_name not in spec.allowed_tools:
                raise PermissionError(f"{agent_name} 无权调用 {tool_name}")

        # D3: 策略 — 写工具仅限特定身份
        if tool_name in {"post_comment", "post_review"} and agent_name not in ("commenter", "orchestrator", ""):
            raise PermissionError(f"{agent_name} 不允许直接发评论")

        # 四层门控第 2 层：Schema 校验 — 必填参数齐全且类型匹配 ToolSpec.input_schema
        self._validate_params(tool_name, params)

        handler = self._handlers.get(tool_name)
        if not handler:
            raise NotImplementedError(f"Tool '{tool_name}' has no handler")

        logger.debug(f"Executing tool: {tool_name} (agent={agent_name})")
        return await handler(params, state)

    def _validate_params(self, tool_name: str, params: dict[str, Any]) -> None:
        """Schema 校验（四层门控第 2 层）：必填键齐全 + 基础类型匹配 ToolSpec.input_schema。"""
        schema = self._registry.tools[tool_name].input_schema or {}
        for key in schema.get("required", []):
            if key not in params:
                raise ValueError(f"Tool '{tool_name}' missing required param: {key}")
        props = schema.get("properties", {})
        for key, value in params.items():
            expected = props.get(key, {}).get("type")
            if not expected:
                continue
            if expected in ("integer", "number") and isinstance(value, bool):
                raise ValueError(f"Tool '{tool_name}' param '{key}' must be {expected}, not bool")
            py = _JSON_PY_TYPES.get(expected)
            if py and not isinstance(value, py):
                raise ValueError(f"Tool '{tool_name}' param '{key}' must be {expected}")

    async def _read_diff(self, params: dict[str, Any], state: StateStore) -> str:
        await self.ensure_file_diffs(state)
        file_path = params["file_path"]
        if state.file_diffs is not None and file_path in state.file_diffs:
            return state.file_diffs[file_path]
        # Compatibility fallback for lightweight clients and a file missing
        # from an otherwise valid bulk response. Real PR files normally hit the
        # per-run cache above and never re-fetch the paginated list.
        return await self._github.get_file_diff(state.repo, state.pr_number, file_path)

    async def ensure_file_diffs(self, state: StateStore) -> None:
        """Populate the per-run diff cache with one bulk PR-files request."""

        if state.file_diffs is not None:
            return

        state_key = id(state)
        load = self._diff_loads.get(state_key)
        if load is None:
            load = asyncio.create_task(self._load_file_diffs(state))
            self._diff_loads[state_key] = load

        try:
            loaded = await load
            if state.file_diffs is None:
                state.file_diffs = loaded
        finally:
            if self._diff_loads.get(state_key) is load:
                self._diff_loads.pop(state_key, None)

    async def _load_file_diffs(self, state: StateStore) -> dict[str, str]:
        get_pr_files = getattr(self._github, "get_pr_files", None)
        if not callable(get_pr_files):
            # Compatibility for plugin/test clients implementing only the older
            # per-file contract. Production GitHubClient always supports bulk.
            return {}
        files = await get_pr_files(state.repo, state.pr_number)
        populate_file_changes(state, files)
        return {
            str(item["filename"]): str(item.get("patch", "") or "")
            for item in files
            if isinstance(item, dict) and item.get("filename")
        }

    async def ensure_file_changes(self, state: StateStore) -> None:
        """Explicit v3-only preparation; legacy diff loading never invokes it.

        Missing evidence stays missing. Concurrent callers share one attempt;
        a stable failure is retained in this state and is not silently retried.
        """
        # ContextEngine subsequently calls ensure_file_diffs. Load the raw
        # metadata first so that later lazy diff initialization cannot replace
        # a verified record with the unhydrated PR-file record.
        if state.file_diffs is None:
            await self.ensure_file_diffs(state)
        identity = (state.repo, state.head_repo or state.repo, state.base_sha, state.head_sha)
        records = state.file_changes
        if all(
            path in records
            and (records[path].repo, records[path].head_repo, records[path].base_sha, records[path].head_sha)
            == identity
            and (
                records[path].patch_state in {"present", "unsupported"}
                or records[path].proof_error
                or is_verified_rename(records[path], state.head_sha)
            )
            for path in state.files_changed
        ):
            return
        key = id(state)
        load = self._change_loads.get(key)
        if load is None:
            load = asyncio.create_task(self._load_file_changes(state))
            self._change_loads[key] = load
        try:
            loaded = await asyncio.shield(load)
            if (state.repo, state.head_repo or state.repo, state.base_sha, state.head_sha) != identity:
                raise ValueError("PR identity changed during structural verification")
            state.file_changes = loaded
        finally:
            if load.done() and self._change_loads.get(key) is load:
                self._change_loads.pop(key, None)

    async def _load_file_changes(self, state: StateStore) -> dict[str, FileChange]:
        repo, head_repo, base_sha, head_sha = state.repo, state.head_repo or state.repo, state.base_sha, state.head_sha
        paths = tuple(state.files_changed)
        # Rebinding every record prevents checkpoint proof from another head
        # being reused. Fresh PR-file metadata is read only for this v3 path.
        records = file_changes_from_pr_files(
            repo=repo,
            head_repo=head_repo,
            base_sha=base_sha,
            head_sha=head_sha,
            files=[{"filename": path, "patch": (state.file_diffs or {}).get(path, "")} for path in paths],
        )
        for path, change in state.file_changes.items():
            if path in records and (change.repo, change.head_repo, change.base_sha, change.head_sha) == (
                repo,
                head_repo,
                base_sha,
                head_sha,
            ):
                # Preserve captured rename metadata even if the first proof
                # lookup fails; proof fields must be supplied by this attempt.
                records[path] = replace(
                    change,
                    comparison_base_sha="",
                    base_blob_sha="",
                    head_blob_sha="",
                    base_mode="",
                    head_mode="",
                    base_type="",
                    head_type="",
                    patch_state=records[path].patch_state,
                    source="github-pr-files",
                    proof_error="",
                )

        def failed(error: str) -> dict[str, FileChange]:
            return {
                path: replace(change, proof_error=error) if change.patch_state != "present" else change
                for path, change in records.items()
            }

        if not valid_repo(repo) or not valid_repo(head_repo) or not valid_sha(base_sha) or not valid_sha(head_sha):
            return failed("invalid-pr-identity")
        api_names = ("get_pr_info", "get_pr_files", "get_comparison_base", "get_git_entries")
        if any(not callable(getattr(self._github, name, None)) for name in api_names):
            return failed("capability:unavailable")

        def matches(info: dict[str, Any]) -> bool:
            actual_repo = (info.get("head", {}).get("repo") or {}).get("full_name")
            return (
                info.get("head", {}).get("sha") == head_sha
                and info.get("base", {}).get("sha") == base_sha
                and (not actual_repo or actual_repo == head_repo)
            )

        stage = "pr-identity"
        try:
            async with asyncio.timeout(60):
                if not matches(await self._github.get_pr_info(repo, state.pr_number)):
                    return failed("stale-pr-head")
                stage = "pr-files"
                files = await self._github.get_pr_files(repo, state.pr_number)
                fresh = file_changes_from_pr_files(
                    repo=repo, head_repo=head_repo, base_sha=base_sha, head_sha=head_sha, files=files
                )
                records.update({path: fresh[path] for path in paths if path in fresh})
                candidates = [
                    change
                    for change in records.values()
                    if change.patch_state == "absent-unverified" and change.status == "renamed"
                ]
                for path, change in list(records.items()):
                    if change.patch_state == "absent-unverified" and change.status != "renamed":
                        records[path] = replace(change, proof_error="patch-unavailable")
                if not candidates:
                    return records
                stage = "comparison-base"
                comparison_base = await self._github.get_comparison_base(repo, base_sha, head_repo, head_sha)
                if not valid_sha(comparison_base):
                    return failed("comparison-base-unavailable")
                selected = []
                for index, change in enumerate(candidates):
                    if index >= 16:
                        records[change.path] = replace(change, proof_error="metadata-limit")
                    elif not valid_path(change.previous_path) or change.previous_path == change.path:
                        records[change.path] = replace(change, proof_error="invalid-rename-path")
                    else:
                        selected.append(change)
                if not selected:
                    return records
                stage = "before-tree"
                before = await self._github.get_git_entries(
                    repo, comparison_base, [change.previous_path for change in selected]
                )
                stage = "head-tree"
                after = await self._github.get_git_entries(head_repo, head_sha, [change.path for change in selected])
                stage = "pr-recheck"
                if not matches(await self._github.get_pr_info(repo, state.pr_number)):
                    return failed("stale-pr-head")
                stage = "proof-bind"
                for change in selected:
                    old, new = before.get(change.previous_path), after.get(change.path)
                    if not isinstance(old, dict) or not isinstance(new, dict):
                        records[change.path] = replace(change, proof_error="git-entry-unavailable")
                        continue
                    hydrated = replace(
                        change,
                        comparison_base_sha=comparison_base,
                        base_blob_sha=old.get("sha", ""),
                        head_blob_sha=new.get("sha", ""),
                        base_mode=old.get("mode", ""),
                        head_mode=new.get("mode", ""),
                        base_type=old.get("type", ""),
                        head_type=new.get("type", ""),
                        source="github-pr-files+git-tree",
                    )
                    if not valid_sha(hydrated.base_blob_sha) or not valid_sha(hydrated.head_blob_sha):
                        hydrated = replace(hydrated, proof_error="git-entry-unavailable")
                    elif hydrated.base_type != "blob" or hydrated.head_type != "blob":
                        hydrated = replace(hydrated, patch_state="unsupported", proof_error="unsupported-type")
                    elif hydrated.base_mode != hydrated.head_mode or hydrated.base_mode not in {"100644", "100755"}:
                        hydrated = replace(hydrated, patch_state="unsupported", proof_error="unsupported-mode")
                    elif hydrated.base_blob_sha != hydrated.head_blob_sha:
                        hydrated = replace(hydrated, proof_error="blob-mismatch")
                    else:
                        hydrated = replace(hydrated, patch_state="verified-no-text-change")
                    records[change.path] = hydrated
                return records
        except Exception as error:
            # Metadata failures are coverage gaps, never proof of unchanged
            # content. HTTP/error bodies must not enter facts or model inputs.
            return failed(_metadata_failure(stage, error))

    async def _read_file(self, params: dict[str, Any], state: StateStore) -> str | None:
        if self._pipeline_requires_workspace():
            workspace = await self._workspace_for(state)
            if workspace.source == "api-fallback":
                return await workspace.read_async(
                    params["file_path"],
                    start=params.get("start_line"),
                    end=params.get("end_line"),
                )
            return workspace.read(
                params["file_path"],
                start=params.get("start_line"),
                end=params.get("end_line"),
            )

        content = await self._github.get_file_content(
            state.repo,
            state.head_sha,
            params["file_path"],
        )
        start_line = params.get("start_line")
        end_line = params.get("end_line")
        if start_line is None and end_line is None:
            return content

        lines = content.splitlines()
        start = max(1, int(start_line or 1))
        end = min(len(lines), int(end_line or len(lines)))
        if end < start:
            raise ValueError("read_file end_line must be >= start_line")
        return "\n".join(f"{line_no}: {lines[line_no - 1]}" for line_no in range(start, end + 1))

    async def _search_code(self, params: dict[str, Any], state: StateStore) -> str:
        if self._pipeline_requires_workspace():
            workspace = await self._workspace_for(state)
            file_glob = params.get("file_glob", "")
            hits = workspace.grep(
                params["pattern"],
                globs=[file_glob] if file_glob else None,
                max_hits=10,
            )
            return "\n".join(f"- {hit.path}:{hit.line}: {hit.text}" for hit in hits) or "No results"
        return await self._github.search_code(state.repo, params["pattern"], params.get("file_glob", ""))

    def _pipeline_requires_workspace(self) -> bool:
        """Use the pinned workspace only for non-legacy pipeline modes.

        T4 will add the typed PipelineV4Config.  Reading the kill switch here
        keeps T2's gateway wiring independent of that later module while the
        default remains byte-for-byte compatible with the legacy path.
        """

        mode = self._pipeline_mode
        if mode is None:
            mode = os.environ.get("REVIEWFORGE_PIPELINE", "legacy")
        return str(mode).strip().lower() != "legacy"

    async def _workspace_for(self, state: StateStore) -> PRHeadWorkspace:
        state_key = id(state)
        if state_key in self._workspace_cleaning:
            raise WorkspaceUnavailable("workspace cleanup in progress", reason="workspace-cleaned")
        known_state = self._workspace_states.get(state_key)
        if known_state is not None and known_state is not state:
            raise WorkspaceUnavailable("workspace state identity collision")
        self._workspace_states.setdefault(state_key, state)
        generation = self._workspace_generations.get(state_key, 0)
        workspace = self._workspaces.get(state_key)
        if workspace is not None:
            return workspace

        load = self._workspace_loads.get(state_key)
        if load is None:
            load = asyncio.create_task(
                PRHeadWorkspace.build(
                    state,
                    self._github,
                    max_bytes=(
                        self._workspace_max_bytes if self._workspace_max_bytes is not None else _workspace_max_bytes()
                    ),
                )
            )
            self._workspace_loads[state_key] = load
        try:
            workspace = await load
            if self._workspace_generations.get(state_key, 0) != generation:
                workspace.cleanup()
                raise WorkspaceUnavailable("workspace cleaned before use", reason="workspace-cleaned")
            if self._workspace_states.get(state_key) is not state:
                workspace.cleanup()
                raise WorkspaceUnavailable("workspace state no longer active", reason="workspace-cleaned")
            self._workspaces[state_key] = workspace
            return workspace
        finally:
            if self._workspace_loads.get(state_key) is load:
                self._workspace_loads.pop(state_key, None)

    async def workspace_for(self, state: StateStore) -> PRHeadWorkspace:
        """Return the run-scoped immutable workspace used by pipeline stages."""

        return await self._workspace_for(state)

    async def cleanup_workspace(self, state: StateStore) -> None:
        """Release exactly the workspace associated with one review state.

        If a build is in flight, wait for it to finish so the just-created
        temporary directory is cleaned as well.  A concurrent waiter observes
        the generation change and cannot reinsert the closed workspace.
        """

        state_key = id(state)
        change_load = self._change_loads.pop(state_key, None)
        if change_load is not None:
            change_load.cancel()
            try:
                await change_load
            except asyncio.CancelledError:
                pass
        known_state = self._workspace_states.get(state_key)
        if known_state is not None and known_state is not state:
            return
        self._workspace_generations[state_key] = self._workspace_generations.get(state_key, 0) + 1
        self._workspace_cleaning.add(state_key)
        try:
            workspace = self._workspaces.pop(state_key, None)
            load = self._workspace_loads.pop(state_key, None)
            if load is not None:
                try:
                    built = await load
                except asyncio.CancelledError:
                    built = None
                except Exception:
                    built = None
                if built is not None:
                    workspace = built if workspace is None else workspace
            if workspace is not None:
                workspace.cleanup()
            if self._workspace_states.get(state_key) is state:
                self._workspace_states.pop(state_key, None)
        finally:
            self._workspace_cleaning.discard(state_key)

    def cleanup_workspaces(self) -> None:
        """Release temporary snapshots owned by this gateway instance."""

        for workspace in self._workspaces.values():
            workspace.cleanup()
        self._workspaces.clear()
        self._workspace_states.clear()
        for load in self._change_loads.values():
            load.cancel()
        self._change_loads.clear()

    async def _get_change_context(self, params: dict[str, Any], state: StateStore) -> str:
        """Return the precomputed Impact Manifest, optionally narrowed."""
        # Local import keeps the gateway independent from engine initialization.
        from reviewforge.engine.context_engine import render_impact_manifest

        files = [params["file_path"]] if params.get("file_path") else None
        return render_impact_manifest(state.impact_manifest, files=files, symbol=params.get("symbol", ""))

    async def _post_comment(self, params: dict[str, Any], state: StateStore) -> dict[str, Any]:
        return await self._github.post_review_comment(
            repo=state.repo,
            pr_number=state.pr_number,
            commit_sha=state.head_sha,
            file_path=params["file_path"],
            line=params["line"],
            body=params["body"],
        )

    async def _post_review(self, params: dict[str, Any], state: StateStore) -> dict[str, Any]:
        comments = params.get("comments", [])
        body = params.get("body", "")
        if not isinstance(body, str):
            raise ValueError("post_review body must be a string")
        if not comments and not body:
            raise ValueError(f"post_review requires between 1 and {MAX_REVIEW_COMMENTS_PER_REQUEST} comments")
        if len(comments) > MAX_REVIEW_COMMENTS_PER_REQUEST:
            raise ValueError(
                f"post_review requires between 1 and {MAX_REVIEW_COMMENTS_PER_REQUEST} comments and a body"
            )
        for index, item in enumerate(comments):
            if not isinstance(item, dict):
                raise ValueError(f"post_review comment {index} must be an object")
            if not isinstance(item.get("file_path"), str) or not item["file_path"]:
                raise ValueError(f"post_review comment {index} has invalid file_path")
            if isinstance(item.get("line"), bool) or not isinstance(item.get("line"), int):
                raise ValueError(f"post_review comment {index} has invalid line")
            if not isinstance(item.get("body"), str) or not item["body"]:
                raise ValueError(f"post_review comment {index} has invalid body")

        batch_method = getattr(self._github, "post_review_comments", None)
        delivery_options: dict[str, Any] = {}
        if params.get("delivery_key"):
            if not callable(batch_method):
                raise ValueError("v4 durable delivery requires the batch review API")
            delivery_options = {
                "delivery_key": params["delivery_key"],
                "reconcile_only": bool(params.get("reconcile_only")),
            }
        if callable(batch_method):
            review = await batch_method(
                repo=state.repo,
                pr_number=state.pr_number,
                commit_sha=state.head_sha,
                comments=comments,
                body=body,
                **delivery_options,
            )
            return {"delivered_indexes": list(range(len(comments))), "review": review, "compatibility": False}

        # Compatibility for plugins/test clients that only implement the legacy
        # single-comment method. Preserve per-item success so a later failure
        # cannot cause already-delivered comments to be reposted.
        delivered: list[int] = []
        failures: list[dict[str, Any]] = []
        for index, item in enumerate(comments):
            try:
                await self._github.post_review_comment(
                    repo=state.repo,
                    pr_number=state.pr_number,
                    commit_sha=state.head_sha,
                    file_path=item["file_path"],
                    line=item["line"],
                    body=item["body"],
                )
                delivered.append(index)
            except Exception as exc:
                error_text = str(exc)
                lowered = error_text.lower()
                kind = str(getattr(exc, "kind", "unknown"))
                status_code = int(getattr(exc, "status_code", 0) or 0)
                retryable = bool(getattr(exc, "retryable", False))
                if status_code == 0:
                    for candidate in (422, 429, 500, 502, 503, 504):
                        if str(candidate) in lowered:
                            status_code = candidate
                            break
                if kind == "unknown":
                    if "spam" in lowered or "secondary rate limit" in lowered:
                        kind = "spam"
                    elif "rate limit" in lowered or status_code == 429:
                        kind = "rate_limit"
                    elif status_code == 422:
                        kind = "validation"
                    elif status_code >= 500:
                        kind = "server"
                    elif any(marker in lowered for marker in ("network", "timeout", "connection")):
                        kind = "network"
                if kind in {"spam", "rate_limit", "network", "server"}:
                    retryable = True
                failures.append(
                    {
                        "index": index,
                        "error": error_text,
                        "kind": kind,
                        "status_code": status_code,
                        "retryable": retryable,
                        "response_body": str(getattr(exc, "response_body", "")),
                    }
                )
        return {
            "delivered_indexes": delivered,
            "failures": failures,
            "compatibility": True,
        }
