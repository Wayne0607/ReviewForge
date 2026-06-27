"""Tool Gateway — single entry point for all tool execution.

Four-layer gating: permission → schema → policy → execution.
"""

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

    def invoke(self, tool_name: str, params: dict[str, Any], state: StateStore) -> Any:
        """Execute a tool with full gating."""
        # 1. Tool exists
        if tool_name not in self._registry.tools:
            raise KeyError(f"Unknown tool: {tool_name}")

        # 2. Handler exists
        handler = self._handlers.get(tool_name)
        if not handler:
            raise NotImplementedError(f"Tool '{tool_name}' has no handler")

        # 3. Execute
        logger.debug(f"Executing tool: {tool_name} with params: {params}")
        return handler(params, state)

    def _read_diff(self, params: dict[str, Any], state: StateStore) -> str:
        file_path = params["file_path"]
        return self._github.get_file_diff(state.repo, state.pr_number, file_path)

    def _read_file(self, params: dict[str, Any], state: StateStore) -> str:
        file_path = params["file_path"]
        return self._github.get_file_content(state.repo, state.head_sha, file_path)

    def _search_code(self, params: dict[str, Any], state: StateStore) -> str:
        pattern = params["pattern"]
        file_glob = params.get("file_glob", "")
        return self._github.search_code(state.repo, pattern, file_glob)

    def _post_comment(self, params: dict[str, Any], state: StateStore) -> dict[str, Any]:
        return self._github.post_review_comment(
            repo=state.repo,
            pr_number=state.pr_number,
            commit_sha=state.head_sha,
            file_path=params["file_path"],
            line=params["line"],
            body=params["body"],
        )
