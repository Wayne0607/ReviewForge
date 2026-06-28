"""Planner Agent — single-shot LLM decision maker.

Reads PR diff summary, outputs task proposals for reviewers.
This is the Conductor: one LLM call per round, not an agentic loop.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from reviewforge.core.specs import SpecRegistry
from reviewforge.core.state import ReviewTask, StateStore
from reviewforge.engine.prompt import build_planner_prompt

logger = logging.getLogger(__name__)


class Planner:
    """Single-shot planner that decides which reviewers to dispatch."""

    def __init__(self, llm: ChatOpenAI, registry: SpecRegistry) -> None:
        self._llm = llm
        self._registry = registry

    async def plan(self, state: StateStore) -> list[ReviewTask]:
        """Analyze the PR and return task proposals."""
        ctx = {
            "registry": self._registry,
            "repo": state.repo,
            "pr_number": state.pr_number,
            "pr_title": "",  # could be enriched
            "files_changed": state.files_changed,
            "diff_summary": state.diff_summary,
        }
        messages = build_planner_prompt(ctx)

        response = await self._llm.ainvoke(
            [SystemMessage(content=messages[0]["content"]),
             HumanMessage(content=messages[1]["content"])]
        )

        return self._parse_response(response.content)

    def _parse_response(self, content: str) -> list[ReviewTask]:
        """Parse LLM JSON output into ReviewTask objects."""
        # Strip markdown code fences if present
        content = content.strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[1] if "\n" in content else content[3:]
        if content.endswith("```"):
            content = content[:-3]
        content = content.strip()

        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            logger.warning("Planner returned invalid JSON, falling back to style-only review")
            return [ReviewTask(reviewer="style_reviewer", files=[], rationale="fallback")]

        tasks = []
        for item in data.get("tasks", []):
            reviewer = item.get("reviewer", "")
            # Normalize: lowercase, replace spaces/hyphens with underscores
            reviewer = reviewer.lower().replace(" ", "_").replace("-", "_")
            # Map short names to full names
            reviewer_map = {
                "security": "security_reviewer",
                "security_reviewer": "security_reviewer",
                "performance": "performance_reviewer",
                "performance_reviewer": "performance_reviewer",
                "style": "style_reviewer",
                "style_reviewer": "style_reviewer",
                "architecture": "style_reviewer",
                "readability": "style_reviewer",
            }
            reviewer = reviewer_map.get(reviewer, reviewer)

            if reviewer not in self._registry.agents:
                logger.warning(f"Planner proposed unknown reviewer '{reviewer}', skipping")
                continue

            tasks.append(ReviewTask(
                reviewer=reviewer,
                files=item.get("files", []),
                rationale=item.get("rationale", ""),
            ))

        return tasks or [ReviewTask(reviewer="style_reviewer", files=[], rationale="fallback")]
