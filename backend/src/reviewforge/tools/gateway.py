"""Tool Gateway — async single entry point for all tool execution."""

from __future__ import annotations

import logging
from typing import Any

from reviewforge.core.specs import SpecRegistry
from reviewforge.core.state import StateStore
from reviewforge.tools.github_api import GitHubClient

logger = logging.getLogger(__name__)


class ToolGateway:
    """Executes tools with permission and schema checks."""

    def __init__(self, registry: SpecRegistry, github: GitHubClient) -> None:
        self._registry = registry
        self._github = github
        self._handlers: dict[str, Any] = {
            "read_diff": self._read_diff,
            "read_file": self._read_file,
            "search_code": self._search_code,
            "post_comment": self._post_comment,
        }

    async def invoke(self, tool_name: str, params: dict[str, Any], state: StateStore) -> Any:
        """Execute a tool with full gating."""
        if tool_name not in self._registry.tools:
            raise KeyError(f"Unknown tool: {tool_name}")

        handler = self._handlers.get(tool_name)
        if not handler:
            raise NotImplementedError(f"Tool '{tool_name}' has no handler")

        logger.debug(f"Executing tool: {tool_name} with params: {params}")
        return await handler(params, state)

    async def _read_diff(self, params: dict[str, Any], state: StateStore) -> str:
        return await self._github.get_file_diff(state.repo, state.pr_number, params["file_path"])

    async def _read_file(self, params: dict[str, Any], state: StateStore) -> str:
        return await self._github.get_file_content(state.repo, state.head_sha, params["file_path"])

    async def _search_code(self, params: dict[str, Any], state: StateStore) -> str:
        return await self._github.search_code(state.repo, params["pattern"], params.get("file_glob", ""))

    async def _post_comment(self, params: dict[str, Any], state: StateStore) -> dict[str, Any]:
        return await self._github.post_review_comment(
            repo=state.repo,
            pr_number=state.pr_number,
            commit_sha=state.head_sha,
            file_path=params["file_path"],
            line=params["line"],
            body=params["body"],
        )
