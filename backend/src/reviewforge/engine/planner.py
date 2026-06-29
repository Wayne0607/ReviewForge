"""Planner Agent — single-shot LLM decision maker with deterministic security detection.

Reads PR diff summary, outputs task proposals for reviewers.
Security patterns are detected deterministically before LLM call.
"""

from __future__ import annotations

import json
import logging
import re

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from reviewforge.core.specs import SpecRegistry
from reviewforge.core.state import ReviewTask, StateStore
from reviewforge.engine.prompt import build_planner_prompt

logger = logging.getLogger(__name__)

# Deterministic security patterns — if any match, security_reviewer is forced
SECURITY_PATTERNS = [
    (r"os\.system\s*\(", "command-injection"),
    (r"subprocess\.\w+\(.*shell\s*=\s*True", "command-injection"),
    (r"os\.popen\s*\(", "command-injection"),
    (r"eval\s*\(", "code-injection"),
    (r"exec\s*\(", "code-injection"),
    (r"pickle\.loads?\s*\(", "insecure-deserialization"),
    (r"yaml\.load\s*\([^)]*\)", "insecure-deserialization"),  # missing Loader=SafeLoader
    (r"(?:SELECT|INSERT|UPDATE|DELETE).*\+\s*(?:str\(|f['\"])", "sql-injection"),
    (r"f['\"].*(?:SELECT|INSERT|UPDATE|DELETE).*\{", "sql-injection"),
    (r"(?:API_KEY|SECRET_KEY|PASSWORD|TOKEN)\s*=\s*['\"][^'\"]{8,}['\"]", "hardcoded-secrets"),
    (r"open\s*\([^)]*\+.*['\"]r['\"]", "path-traversal"),
    (r"(?:innerHTML|dangerouslySetInnerHTML|v-html)", "xss"),
    (r"subprocess\.(?:call|run|Popen)\s*\(", "command-injection"),
]

# Performance patterns
PERFORMANCE_PATTERNS = [
    (r"for\s+\w+\s+in\s+range.*\n.*for\s+\w+\s+in\s+range", "nested-loop"),
    (r"(?:urllib\.request\.urlopen|requests\.get)\s*\(.*\n.*for\s+", "blocking-io-in-loop"),
    (r"sqlite3\.connect\s*\(.*\n.*for\s+", "db-in-loop"),
]

# Testing patterns — trigger testing_reviewer
TESTING_PATTERNS = [
    (r"def\s+\w+\([^)]*\)\s*->", "has-type-hints"),  # new function with type hints
    (r"class\s+\w+:", "new-class"),  # new class definition
]

# Dependency patterns — trigger dependency_reviewer
DEPENDENCY_PATTERNS = [
    (r"(?:pip install|add\(|dependencies|pyproject)", "dep-change"),
    (r"(?:requirements.*\.txt|Pipfile|poetry\.lock|package\.json)", "dep-file-change"),
]

# Accessibility patterns — trigger accessibility_reviewer
ACCESSIBILITY_PATTERNS = [
    (r"(?:<img|<Image|<picture)", "image-element"),
    (r"(?:<input|<select|<textarea|<button)", "form-element"),
    (r"(?:onClick|onChange|onSubmit)", "interactive-handler"),
    (r"(?:aria-|role=)", "aria-usage"),
]


class Planner:
    """Single-shot planner with deterministic pattern detection."""

    def __init__(self, llm: ChatOpenAI, registry: SpecRegistry) -> None:
        self._llm = llm
        self._registry = registry

    async def plan(self, state: StateStore, notes: list | None = None) -> list[ReviewTask]:
        """Analyze the PR and return task proposals (re-planning aware).

        Reviewers already dispatched this run are excluded, so repeat rounds
        converge to empty (and the loop detector catches genuine repeats). Notes
        from prior rounds (e.g. loop-detector rescue hints) are fed to the LLM.
        """
        done_reviewers = {t.reviewer for t in state.list_tasks() if t.status in ("completed", "claimed", "failed")}
        first_round = not done_reviewers

        # Step 1: Deterministic pattern detection (skip already-dispatched reviewers)
        forced_reviewers = self._detect_patterns(state.files_changed, state.diff_summary) - done_reviewers

        # Step 2: LLM decision for additional reviewers
        ctx = {
            "registry": self._registry,
            "repo": state.repo,
            "pr_number": state.pr_number,
            "pr_title": "",
            "files_changed": state.files_changed,
            "diff_summary": state.diff_summary,
            "done_reviewers": sorted(done_reviewers),
            "notes": [{"from": n.from_agent, "type": n.type, "content": n.content} for n in (notes or [])],
        }
        messages = build_planner_prompt(ctx)

        response = await self._llm.ainvoke(
            [SystemMessage(content=messages[0]["content"]), HumanMessage(content=messages[1]["content"])]
        )

        llm_tasks = [t for t in self._parse_response(response.content) if t.reviewer not in done_reviewers]

        # Step 3: Merge — include forced reviewers; default style only on the first round
        return self._merge_tasks(forced_reviewers, llm_tasks, state.files_changed, first_round)

    def _detect_patterns(self, files: list[str], diff: str) -> set[str]:
        """Deterministically detect patterns and force relevant reviewers."""
        forced = set()
        file_set = set(files)
        is_frontend = any(f.endswith((".tsx", ".jsx", ".vue", ".html", ".svelte")) for f in file_set)

        for pattern, label in SECURITY_PATTERNS:
            if re.search(pattern, diff, re.IGNORECASE | re.MULTILINE):
                forced.add("security_reviewer")
                logger.info(f"Security pattern detected: {label}")
                break

        for pattern, label in PERFORMANCE_PATTERNS:
            if re.search(pattern, diff, re.IGNORECASE | re.MULTILINE):
                forced.add("performance_reviewer")
                logger.info(f"Performance pattern detected: {label}")
                break

        for pattern, label in TESTING_PATTERNS:
            if re.search(pattern, diff, re.IGNORECASE | re.MULTILINE):
                forced.add("testing_reviewer")
                logger.info(f"Testing pattern detected: {label}")
                break

        for pattern, label in DEPENDENCY_PATTERNS:
            if re.search(pattern, diff, re.IGNORECASE | re.MULTILINE):
                forced.add("dependency_reviewer")
                logger.info(f"Dependency pattern detected: {label}")
                break

        # Only force accessibility reviewer for frontend files
        if is_frontend:
            for pattern, label in ACCESSIBILITY_PATTERNS:
                if re.search(pattern, diff, re.IGNORECASE | re.MULTILINE):
                    forced.add("accessibility_reviewer")
                    logger.info(f"Accessibility pattern detected: {label}")
                    break

        return forced

    def _merge_tasks(
        self, forced: set[str], llm_tasks: list[ReviewTask], files: list[str], first_round: bool = True
    ) -> list[ReviewTask]:
        """Merge forced reviewers with LLM decisions.

        On the first round, style_reviewer is always added as a default and a
        fallback guarantees at least one task. On re-planning rounds an empty
        result is valid (it signals convergence — nothing more to dispatch).
        """
        llm_reviewers = {t.reviewer for t in llm_tasks}
        merged = list(llm_tasks)

        for reviewer in forced:
            if reviewer not in llm_reviewers:
                merged.append(
                    ReviewTask(
                        reviewer=reviewer,
                        files=files,
                        rationale="自动检测到安全/性能模式",
                    )
                )
                logger.info(f"Forced reviewer added: {reviewer}")

        if first_round and "style_reviewer" not in {t.reviewer for t in merged}:
            merged.append(
                ReviewTask(
                    reviewer="style_reviewer",
                    files=files,
                    rationale="默认风格审查",
                )
            )

        if first_round:
            return merged or [ReviewTask(reviewer="style_reviewer", files=files, rationale="fallback")]
        return merged

    def _parse_response(self, content: str) -> list[ReviewTask]:
        """Parse LLM JSON output into ReviewTask objects."""
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
            return []

        tasks = []
        for item in data.get("tasks", []):
            reviewer = item.get("reviewer", "")
            reviewer = reviewer.lower().replace(" ", "_").replace("-", "_")
            reviewer_map = {
                "security": "security_reviewer",
                "security_reviewer": "security_reviewer",
                "performance": "performance_reviewer",
                "performance_reviewer": "performance_reviewer",
                "style": "style_reviewer",
                "style_reviewer": "style_reviewer",
                "architecture": "style_reviewer",
                "readability": "style_reviewer",
                "testing": "testing_reviewer",
                "testing_reviewer": "testing_reviewer",
                "test": "testing_reviewer",
                "documentation": "doc_reviewer",
                "documentation_reviewer": "doc_reviewer",
                "doc": "doc_reviewer",
                "doc_reviewer": "doc_reviewer",
                "dependency": "dependency_reviewer",
                "dependency_reviewer": "dependency_reviewer",
                "deps": "dependency_reviewer",
                "accessibility": "accessibility_reviewer",
                "accessibility_reviewer": "accessibility_reviewer",
                "a11y": "accessibility_reviewer",
            }
            reviewer = reviewer_map.get(reviewer, reviewer)

            if reviewer not in self._registry.agents:
                logger.warning(f"Planner proposed unknown reviewer '{reviewer}', skipping")
                continue

            tasks.append(
                ReviewTask(
                    reviewer=reviewer,
                    files=item.get("files", []),
                    rationale=item.get("rationale", ""),
                )
            )

        return tasks
