"""Verifier Agent — filters false positives from candidate findings.

Pure LLM reasoning, no tools. Reviews each candidate finding
and decides: confirmed or false_positive.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from reviewforge.core.specs import SpecRegistry
from reviewforge.core.state import Finding, StateStore
from reviewforge.engine.prompt import build_verifier_prompt

logger = logging.getLogger(__name__)


class Verifier:
    """Reviews candidate findings and removes false positives."""

    def __init__(self, llm: ChatOpenAI, registry: SpecRegistry) -> None:
        self._llm = llm
        self._registry = registry

    async def verify(self, state: StateStore) -> list[Finding]:
        """Verify all candidate findings. Returns only confirmed ones."""
        candidates = state.list_findings(status="candidate")
        if not candidates:
            return []

        ctx = {
            "registry": self._registry,
            "agent_name": "verifier",
            "candidate_findings": [f.to_dict() for f in candidates],
        }
        messages = build_verifier_prompt(ctx)

        response = await self._llm.ainvoke(
            [SystemMessage(content=messages[0]["content"]),
             HumanMessage(content=messages[1]["content"])]
        )

        return self._parse_response(response.content, candidates)

    def _parse_response(self, content: str, candidates: list[Finding]) -> list[Finding]:
        """Parse verifier output and update finding statuses."""
        content = content.strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[1] if "\n" in content else content[3:]
        if content.endswith("```"):
            content = content[:-3]
        content = content.strip()

        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            logger.warning("Verifier returned invalid JSON, marking all as confirmed")
            for f in candidates:
                f.status = "confirmed"
            return candidates

        verified_map: dict[str, dict] = {}
        for item in data.get("verified", []):
            verified_map[item.get("file", "") + ":" + str(item.get("line", 0))] = item

        confirmed = []
        for finding in candidates:
            key = f"{finding.file}:{finding.line}"
            verdict = verified_map.get(key, {})
            if verdict.get("verdict") == "false_positive":
                finding.status = "false_positive"
                finding.verified_by = "verifier"
                finding.verify_reason = verdict.get("reason", "")
            else:
                finding.status = "confirmed"
                finding.verified_by = "verifier"
                finding.verify_reason = verdict.get("reason", "")
                confirmed.append(finding)

        return confirmed
