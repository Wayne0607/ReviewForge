"""Mock LLM for testing without real API calls.

W3: Supports bind_tools for agentic loop testing.
Pattern: first call returns tool_call, second call returns findings.
"""

from __future__ import annotations

import json
import re
from typing import Any

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult


class MockChatLLM(BaseChatModel):
    """Returns deterministic responses based on the prompt content.

    Supports agentic loop: if bound with tools, first call returns
    a tool_call, second call (after ToolMessage) returns findings.
    """

    model_name: str = "mock"
    _bound_tools: list = []

    def bind_tools(self, tools: list, **kwargs: Any) -> MockChatLLM:
        """W3: Store tools for agentic loop simulation."""
        # Return a copy with tools bound
        clone = MockChatLLM()
        clone._bound_tools = tools
        return clone

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        if not messages:
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content="{}"))])

        system = messages[0].content if messages else ""

        # W3: Agentic loop — check if tools are bound
        if self._bound_tools:
            return self._generate_agentic(messages, system)

        # Original single-shot behavior
        content = messages[-1].content if messages else ""
        if "对抗性验证器" in system or "adversarial verifier" in system.lower():
            response = self._mock_adversarial_calibration(content)
        elif "最终裁决者" in system or "final judge" in system.lower():
            response = self._mock_final_judgment(content)
        elif "Planner" in system or "planner" in system.lower():
            response = self._mock_planner(content)
        elif "Verifier" in system or "verifier" in system.lower():
            response = self._mock_verifier(content)
        else:
            response = self._mock_reviewer(content)

        return ChatResult(
            generations=[ChatGeneration(message=type(messages[-1])(content=response) if messages else None)],
        )

    def _generate_agentic(self, messages: list[BaseMessage], system: str) -> ChatResult:
        """W3: Agentic loop mock — first call: tool_call, second: findings."""
        # Check if there's already a ToolMessage in the conversation
        has_tool_response = any(isinstance(m, ToolMessage) for m in messages)

        if not has_tool_response:
            # First call: return a tool_call to read_file
            tool_name = self._bound_tools[0].name if self._bound_tools else "read_file"
            # Extract a file path from the diff if available
            file_path = "test.py"
            for m in messages:
                if hasattr(m, "content") and "--- " in str(m.content):
                    # Try to extract filename from diff header
                    import re

                    match = re.search(r"--- (\S+)", str(m.content))
                    if match:
                        file_path = match.group(1)
                        break

            tool_call = {
                "name": tool_name,
                "args": {"file_path": file_path} if "file" in tool_name else {"pattern": "import"},
                "id": "mock_call_001",
                "type": "tool_call",
            }
            msg = AIMessage(content="", tool_calls=[tool_call])
            return ChatResult(generations=[ChatGeneration(message=msg)])
        else:
            # Second call: return findings
            response = self._mock_reviewer("")
            msg = AIMessage(content=response)
            return ChatResult(generations=[ChatGeneration(message=msg)])

    def _mock_planner(self, content: str) -> str:
        return json.dumps(
            {
                "tasks": [
                    {"reviewer": "security_reviewer", "files": ["test.py"], "rationale": "涉及安全相关代码"},
                    {"reviewer": "style_reviewer", "files": ["test.py"], "rationale": "检查代码风格"},
                    {"reviewer": "testing_reviewer", "files": ["test.py"], "rationale": "检查测试覆盖"},
                    {"reviewer": "doc_reviewer", "files": ["test.py"], "rationale": "检查文档完整性"},
                ]
            }
        )

    def _mock_reviewer(self, content: str) -> str:
        return json.dumps(
            {
                "findings": [
                    {
                        "file": "test.py",
                        "line": 1,
                        "severity": "warning",
                        "category": "security",
                        "message": "发现硬编码的 import pickle，存在反序列化安全风险",
                        "suggestion": "避免使用 pickle.loads 处理不可信数据，考虑使用 json 替代",
                        "confidence": 0.85,
                    }
                ]
            }
        )

    def _mock_verifier(self, content: str) -> str:
        return json.dumps(
            {
                "verified": [
                    {
                        "file": "test.py",
                        "line": 1,
                        "verdict": "confirmed",
                        "reason": "pickle 反序列化确实是安全风险",
                    }
                ]
            }
        )

    @staticmethod
    def _finding_ids(content: str) -> list[str]:
        return list(dict.fromkeys(re.findall(r"^- \[([^]]+)]", content or "", re.MULTILINE)))

    def _mock_adversarial_calibration(self, content: str) -> str:
        return json.dumps(
            [
                {
                    "finding_id": finding_id,
                    "verdict": "confirmed",
                    "adjusted_confidence": 0.9,
                    "challenge": "Mock calibration found the issue actionable.",
                }
                for finding_id in self._finding_ids(content)
            ]
        )

    def _mock_final_judgment(self, content: str) -> str:
        return json.dumps(
            [
                {
                    "finding_id": finding_id,
                    "verdict": "confirmed",
                    "confidence": 0.9,
                    "reason": "Mock final judgment confirmed the issue.",
                }
                for finding_id in self._finding_ids(content)
            ]
        )

    @property
    def _llm_type(self) -> str:
        return "mock"

    @property
    def _identifying_params(self) -> dict[str, Any]:
        return {"model_name": self.model_name}
