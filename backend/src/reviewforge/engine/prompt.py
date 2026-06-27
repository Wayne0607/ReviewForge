"""Prompt Builder — section-based composable prompt generation.

Each section is a callable that returns str | None.
None means "skip this section". Prompts auto-generate from specs.
"""

from __future__ import annotations

from typing import Any, Callable

from reviewforge.core.specs import SpecRegistry

PromptSection = Callable[[dict[str, Any]], str | None]


def _identity(ctx: dict[str, Any]) -> str:
    role = ctx.get("role", "reviewer")
    identities = {
        "planner": "You are the ReviewForge Planner. You analyze PR diffs and decide which specialized reviewers to dispatch.",
        "reviewer": f"You are the ReviewForge {ctx.get('reviewer_type', 'code')} reviewer. You examine code changes and report findings.",
        "verifier": "You are the ReviewForge Verifier. You review candidate findings and decide if they are real issues or false positives.",
        "commenter": "You are the ReviewForge Commenter. You format confirmed findings into clear, actionable GitHub review comments.",
    }
    return identities.get(role, identities["reviewer"])


def _available_tools(ctx: dict[str, Any]) -> str | None:
    registry: SpecRegistry = ctx["registry"]
    agent_name = ctx.get("agent_name", "")
    if not agent_name or agent_name not in registry.agents:
        return None
    agent = registry.agents[agent_name]
    if not agent.allowed_tools:
        return None
    lines = ["## Available Tools\n"]
    for tool_name in agent.allowed_tools:
        tool = registry.tools.get(tool_name)
        if tool:
            lines.append(f"- **{tool_name}**: {tool.description}")
    return "\n".join(lines)


def _output_contract(ctx: dict[str, Any]) -> str | None:
    registry: SpecRegistry = ctx["registry"]
    agent_name = ctx.get("agent_name", "")
    if not agent_name or agent_name not in registry.agents:
        return None
    contract = registry.agents[agent_name].output_contract
    if not contract:
        return None
    return f"## Output Contract\n\nYou MUST respond with valid JSON matching this schema:\n```json\n{contract}\n```"


def _planner_mission(ctx: dict[str, Any]) -> str:
    return """## Mission

Analyze the PR diff and decide which reviewers to dispatch.

Rules:
- Only dispatch reviewers whose expertise is needed for the changed files
- Security reviewer: if files involve auth, input handling, crypto, network, secrets, SQL
- Performance reviewer: if files involve loops, data processing, caching, database queries
- Style reviewer: always dispatch for readability check
- Each task should list specific files to review
- Max 4 tasks per round"""


def _reviewer_mission(ctx: dict[str, Any]) -> str:
    reviewer_type = ctx.get("reviewer_type", "general")
    missions = {
        "security": """## Mission

Review code for security vulnerabilities:
- SQL injection, XSS, CSRF, path traversal
- Hardcoded secrets, insecure defaults
- Missing input validation/sanitization
- Insecure crypto, weak auth patterns
- Dependency vulnerabilities""",
        "performance": """## Mission

Review code for performance issues:
- O(n²) or worse complexity in hot paths
- Missing caching opportunities
- N+1 query patterns
- Unnecessary memory allocations
- Blocking I/O in async contexts""",
        "style": """## Mission

Review code for readability and maintainability:
- Unclear naming, magic numbers
- Missing docstrings on public APIs
- Overly complex functions (>30 lines)
- Dead code, unused imports
- Inconsistent patterns with rest of codebase""",
    }
    return missions.get(reviewer_type, "## Mission\n\nReview the code changes and report findings.")


def _verifier_mission(ctx: dict[str, Any]) -> str:
    return """## Mission

For each candidate finding, decide:
- **confirmed**: the issue is real and actionable
- **false_positive**: the finding is a mistake, not applicable, or too noisy

Be strict. Only confirm findings you are confident about.
If confidence < 0.6, mark as false_positive."""


def _anti_patterns(ctx: dict[str, Any]) -> str:
    return """## Anti-Patterns (DO NOT DO)

- Do NOT invent findings that aren't grounded in the actual code
- Do NOT report issues on lines that weren't changed in the PR
- Do NOT duplicate the same finding across files
- Do NOT suggest refactors that aren't related to the PR's purpose
- Do NOT leave placeholder text in suggestions"""


def _findings_format(ctx: dict[str, Any]) -> str:
    return """## Findings Format

For each finding, provide:
- `file`: exact file path from the diff
- `line`: exact line number in the changed file
- `severity`: "info" | "warning" | "error"
- `category`: short label (e.g., "sql-injection", "n-plus-one", "naming")
- `message`: what the issue is (1-2 sentences)
- `suggestion`: how to fix it (concrete code suggestion if possible)
- `confidence`: 0.0-1.0 (how sure you are this is a real issue)"""


def build_planner_prompt(ctx: dict[str, Any]) -> list[dict[str, str]]:
    """Build system + user messages for the Planner agent."""
    sections = [_identity, _planner_mission, _available_tools, _output_contract, _anti_patterns]
    system_parts = [s({**ctx, "role": "planner", "agent_name": "planner"}) for s in sections]
    system = "\n\n".join(p for p in system_parts if p)

    user = f"""## PR Context

**Repository**: {ctx.get('repo', 'unknown')}
**PR #{ctx.get('pr_number', '?')}**: {ctx.get('pr_title', '')}
**Files changed**: {', '.join(ctx.get('files_changed', []))}

## Diff Summary

{ctx.get('diff_summary', 'No diff available.')}

## Instructions

Analyze the diff and dispatch reviewers. Output JSON."""

    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def build_reviewer_prompt(ctx: dict[str, Any]) -> list[dict[str, str]]:
    """Build system + user messages for a Reviewer agent."""
    reviewer_type = ctx.get("reviewer_type", "general")
    sections = [_identity, _reviewer_mission, _available_tools, _findings_format, _output_contract, _anti_patterns]
    system_parts = [s({**ctx, "role": "reviewer", "agent_name": f"{reviewer_type}_reviewer"}) for s in sections]
    system = "\n\n".join(p for p in system_parts if p)

    files_to_review = ctx.get("files_to_review", [])
    diffs = ctx.get("diffs", {})

    diff_text = ""
    for f in files_to_review:
        diff_text += f"### {f}\n```\n{diffs.get(f, 'No diff available.')}\n```\n\n"

    user = f"""## Files to Review

{diff_text}

## Instructions

Review the above changes and report findings as JSON."""

    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def build_verifier_prompt(ctx: dict[str, Any]) -> list[dict[str, str]]:
    """Build system + user messages for the Verifier agent."""
    sections = [_identity, _verifier_mission, _output_contract, _anti_patterns]
    system_parts = [s({**ctx, "role": "verifier", "agent_name": "verifier"}) for s in sections]
    system = "\n\n".join(p for p in system_parts if p)

    findings = ctx.get("candidate_findings", [])
    findings_text = "\n".join(
        f"- [{f['id']}] {f['file']}:{f['line']} ({f['severity']}) {f['message']}"
        for f in findings
    )

    user = f"""## Candidate Findings

{findings_text}

## Instructions

For each finding, determine if it is confirmed or false_positive. Output JSON."""

    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
