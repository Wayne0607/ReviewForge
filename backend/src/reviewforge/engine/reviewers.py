"""Reviewer Agents — stateless per-task executors.

Each reviewer focuses on one dimension (security/performance/style).
They run a tool loop, collect findings, and write back to StateStore.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from reviewforge.core.specs import SpecRegistry
from reviewforge.core.state import Finding, ReviewTask, StateStore
from reviewforge.engine.prompt import build_reviewer_prompt
from reviewforge.tools.gateway import ToolGateway

logger = logging.getLogger(__name__)


class BaseReviewer:
    """Base class for all reviewers. Implements the tool loop."""

    def __init__(
        self,
        name: str,
        reviewer_type: str,
        llm: ChatOpenAI,
        registry: SpecRegistry,
        gateway: ToolGateway,
        max_steps: int = 8,
    ) -> None:
        self.name = name
        self.reviewer_type = reviewer_type
        self._llm = llm
        self._registry = registry
        self._gateway = gateway
        self._max_steps = max_steps

    async def execute(self, task: ReviewTask, state: StateStore) -> list[Finding]:
        """Run the review loop for the assigned files."""
        files = task.files or state.files_changed
        diffs = {}
        for f in files:
            diffs[f] = self._gateway.invoke("read_diff", {"file_path": f}, state) or ""

        ctx = {
            "registry": self._registry,
            "reviewer_type": self.reviewer_type,
            "agent_name": self.name,
            "files_to_review": files,
            "diffs": diffs,
        }
        messages = build_reviewer_prompt(ctx)

        # Tool loop: LLM calls tools, we execute them
        chat_messages = [
            SystemMessage(content=messages[0]["content"]),
            HumanMessage(content=messages[1]["content"]),
        ]

        for step in range(self._max_steps):
            response = await self._llm.ainvoke(chat_messages)
            content = response.content

            # Try to parse as findings output
            findings = self._parse_findings(content)
            if findings:
                for f in findings:
                    f.reviewer = self.name
                return findings

            # If LLM wants to use a tool (future: bind_tools)
            chat_messages.append(response)
            break  # For now, single-shot

        return []

    def _parse_findings(self, content: str) -> list[Finding]:
        """Parse LLM JSON output into Finding objects."""
        content = content.strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[1] if "\n" in content else content[3:]
        if content.endswith("```"):
            content = content[:-3]
        content = content.strip()

        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            logger.warning(f"{self.name}: invalid JSON output")
            return []

        findings = []
        for item in data.get("findings", []):
            findings.append(Finding(
                file=item.get("file", ""),
                line=item.get("line", 0),
                severity=item.get("severity", "info"),
                category=item.get("category", ""),
                message=item.get("message", ""),
                suggestion=item.get("suggestion", ""),
                confidence=item.get("confidence", 0.5),
            ))
        return findings


class SecurityReviewer(BaseReviewer):
    def __init__(self, llm: ChatOpenAI, registry: SpecRegistry, gateway: ToolGateway) -> None:
        super().__init__(
            name="security_reviewer",
            reviewer_type="security",
            llm=llm,
            registry=registry,
            gateway=gateway,
            max_steps=registry.get_agent("security_reviewer").max_steps,
        )


class PerformanceReviewer(BaseReviewer):
    def __init__(self, llm: ChatOpenAI, registry: SpecRegistry, gateway: ToolGateway) -> None:
        super().__init__(
            name="performance_reviewer",
            reviewer_type="performance",
            llm=llm,
            registry=registry,
            gateway=gateway,
            max_steps=registry.get_agent("performance_reviewer").max_steps,
        )


class StyleReviewer(BaseReviewer):
    def __init__(self, llm: ChatOpenAI, registry: SpecRegistry, gateway: ToolGateway) -> None:
        super().__init__(
            name="style_reviewer",
            reviewer_type="style",
            llm=llm,
            registry=registry,
            gateway=gateway,
            max_steps=registry.get_agent("style_reviewer").max_steps,
        )


REVIEWER_MAP: dict[str, type[BaseReviewer]] = {
    "security_reviewer": SecurityReviewer,
    "performance_reviewer": PerformanceReviewer,
    "style_reviewer": StyleReviewer,
}
