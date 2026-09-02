"""Tool Gateway — async single entry point for all tool execution.

D3: 真正做权限门控 — 按 agent 的 allowed_tools 校验，
写工具 post_comment 仅限 commenter/orchestrator 身份。
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from reviewforge.core.specs import SpecRegistry
from reviewforge.core.state import StateStore
from reviewforge.tools.github_api import MAX_REVIEW_COMMENTS_PER_REQUEST, GitHubClient
from reviewforge.tools.workspace import DEFAULT_MAX_BYTES, PRHeadWorkspace, WorkspaceUnavailable

logger = logging.getLogger(__name__)


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

    def __init__(self, registry: SpecRegistry, github: GitHubClient) -> None:
        self._registry = registry
        self._github = github
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
        return {
            str(item["filename"]): str(item.get("patch", "") or "")
            for item in files
            if isinstance(item, dict) and item.get("filename")
        }

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

    @staticmethod
    def _pipeline_requires_workspace() -> bool:
        """Use the pinned workspace only for non-legacy pipeline modes.

        T4 will add the typed PipelineV4Config.  Reading the kill switch here
        keeps T2's gateway wiring independent of that later module while the
        default remains byte-for-byte compatible with the legacy path.
        """

        return os.environ.get("REVIEWFORGE_PIPELINE", "legacy").strip().lower() != "legacy"

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
                    max_bytes=_workspace_max_bytes(),
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

    async def cleanup_workspace(self, state: StateStore) -> None:
        """Release exactly the workspace associated with one review state.

        If a build is in flight, wait for it to finish so the just-created
        temporary directory is cleaned as well.  A concurrent waiter observes
        the generation change and cannot reinsert the closed workspace.
        """

        state_key = id(state)
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
        comments = params["comments"]
        if not comments or len(comments) > MAX_REVIEW_COMMENTS_PER_REQUEST:
            raise ValueError(f"post_review requires between 1 and {MAX_REVIEW_COMMENTS_PER_REQUEST} comments")
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
        if callable(batch_method):
            review = await batch_method(
                repo=state.repo,
                pr_number=state.pr_number,
                commit_sha=state.head_sha,
                comments=comments,
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
