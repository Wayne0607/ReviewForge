"""Mock LLM for testing without real API calls."""

from __future__ import annotations

import json
from typing import Any

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import BaseMessage
from langchain_core.outputs import ChatResult, ChatGeneration


class MockChatLLM(BaseChatModel):
    """Returns deterministic responses based on the prompt content."""

    model_name: str = "mock"

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        content = messages[-1].content if messages else ""

        # Detect which agent is asking based on system prompt
        system = messages[0].content if messages else ""

        if "Planner" in system or "planner" in system.lower():
            response = self._mock_planner(content)
        elif "Verifier" in system or "verifier" in system.lower():
            response = self._mock_verifier(content)
        else:
            response = self._mock_reviewer(content)

        return ChatResult(
            generations=[ChatGeneration(message=type(messages[-1])(content=response) if messages else None)],
        )

    def _mock_planner(self, content: str) -> str:
        return json.dumps({
            "tasks": [
                {"reviewer": "security_reviewer", "files": ["test.py"], "rationale": "涉及安全相关代码"},
                {"reviewer": "style_reviewer", "files": ["test.py"], "rationale": "检查代码风格"},
                {"reviewer": "testing_reviewer", "files": ["test.py"], "rationale": "检查测试覆盖"},
                {"reviewer": "doc_reviewer", "files": ["test.py"], "rationale": "检查文档完整性"},
            ]
        })

    def _mock_reviewer(self, content: str) -> str:
        return json.dumps({
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
        })

    def _mock_verifier(self, content: str) -> str:
        return json.dumps({
            "verified": [
                {
                    "file": "test.py",
                    "line": 1,
                    "verdict": "confirmed",
                    "reason": "pickle 反序列化确实是安全风险",
                }
            ]
        })

    @property
    def _llm_type(self) -> str:
        return "mock"

    @property
    def _identifying_params(self) -> dict[str, Any]:
        return {"model_name": self.model_name}
