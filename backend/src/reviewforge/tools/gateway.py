"""Tool Gateway — async single entry point for all tool execution.

D3: 真正做权限门控 — 按 agent 的 allowed_tools 校验，
写工具 post_comment 仅限 commenter/orchestrator 身份。
"""

from __future__ import annotations

import logging
from typing import Any

from reviewforge.core.specs import SpecRegistry
from reviewforge.core.state import StateStore
from reviewforge.tools.github_api import GitHubClient

logger = logging.getLogger(__name__)

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
            "post_comment": self._post_comment,
        }

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
        if tool_name == "post_comment" and agent_name not in ("commenter", "orchestrator", ""):
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
