"""Agentic 循环的 token 预算与输出截断常量。"""

from __future__ import annotations

from langchain_core.messages import BaseMessage

# 单次工具结果注回上下文的上限字符数（防止 read_file 撑爆上下文）
MAX_TOOL_OUTPUT_CHARS = 6000

# 同一 (tool, args) 重复调用上限（reviewer 级 loop 防护）
MAX_TOOL_CALLS_PER_FILE = 3


class TokenBudget:
    """累计 agentic 循环消耗的 token，超限即停。"""

    def __init__(self, max_tokens: int) -> None:
        self._max = max_tokens
        self._used = 0

    def add(self, message: BaseMessage) -> None:
        """从 message 的 usage_metadata 累加 token；无 usage 时用字符数粗估。"""
        usage = getattr(message, "usage_metadata", None)
        if usage and usage.get("total_tokens"):
            self._used += int(usage["total_tokens"])
        else:
            # provider 未回 usage：用字符数粗估（4 char ~ 1 token）兜底
            content = getattr(message, "content", "")
            self._used += len(str(content)) // 4

    @property
    def used(self) -> int:
        return self._used

    @property
    def remaining(self) -> int:
        return max(0, self._max - self._used)

    def exhausted(self) -> bool:
        return self._used >= self._max
