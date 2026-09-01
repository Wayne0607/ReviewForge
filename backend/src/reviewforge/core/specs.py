"""Spec Registry — declarative capability registration.

All agents, tools, and skills are declared here before any code.
The Planner prompt auto-generates from these specs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from reviewforge.core.reviewer_catalog import REVIEWER_CATALOG, ReviewerDefinition


@dataclass(frozen=True)
class ToolSpec:
    """Declares a tool's contract, risk level, and runtime requirements."""

    name: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] = field(default_factory=dict)
    risk_level: str = "low"  # low / medium / high


@dataclass(frozen=True)
class AgentSpec:
    """Declares an agent's role, capabilities, and constraints."""

    name: str
    role: str  # executor / validator / synthesizer
    description: str
    allowed_tools: list[str] = field(default_factory=list)
    model_profile: str = "default"  # maps to LLM config
    max_steps: int = 5
    output_contract: dict[str, Any] = field(default_factory=dict)


@dataclass
class SpecRegistry:
    """Central registry for all specs. New capabilities register here."""

    agents: dict[str, AgentSpec] = field(default_factory=dict)
    tools: dict[str, ToolSpec] = field(default_factory=dict)
    skills: set[str] = field(default_factory=set)

    def register_agent(self, spec: AgentSpec) -> None:
        self.agents[spec.name] = spec

    def register_tool(self, spec: ToolSpec) -> None:
        self.tools[spec.name] = spec

    def register_skill(self, name: str) -> None:
        self.skills.add(name)

    def unregister_agent(self, name: str) -> bool:
        return self.agents.pop(name, None) is not None

    def unregister_skill(self, name: str) -> bool:
        if name in self.skills:
            self.skills.discard(name)
            return True
        return False

    def validate(self) -> list[str]:
        """Validate cross-references. Returns list of errors (empty = OK)."""
        errors: list[str] = []
        for name, agent in self.agents.items():
            for tool_name in agent.allowed_tools:
                if tool_name not in self.tools:
                    errors.append(f"Agent '{name}' references unknown tool '{tool_name}'")
        return errors

    def get_agent(self, name: str) -> AgentSpec:
        if name not in self.agents:
            raise KeyError(f"Unknown agent: {name}")
        return self.agents[name]

    def get_tool(self, name: str) -> ToolSpec:
        if name not in self.tools:
            raise KeyError(f"Unknown tool: {name}")
        return self.tools[name]


def _reviewer_output_contract() -> dict[str, Any]:
    """Return a fresh finding contract shared by every built-in reviewer."""

    return {
        "type": "object",
        "properties": {
            "findings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "file": {"type": "string"},
                        "line": {"type": "integer"},
                        "severity": {"type": "string"},
                        "category": {"type": "string"},
                        "message": {"type": "string"},
                        "suggestion": {"type": "string"},
                        "confidence": {"type": "number"},
                    },
                    "required": ["file", "line", "severity", "message", "confidence"],
                },
            }
        },
        "required": ["findings"],
    }


def _reviewer_agent_spec(definition: ReviewerDefinition) -> AgentSpec:
    """Derive a registry spec from the canonical built-in catalog."""

    return AgentSpec(
        name=definition.name,
        role="executor",
        description=definition.description,
        allowed_tools=list(definition.allowed_tools),
        model_profile=definition.registry_model_profile,
        max_steps=definition.max_steps,
        output_contract=_reviewer_output_contract(),
    )


def build_registry() -> SpecRegistry:
    """Build the default spec registry with all built-in capabilities."""
    registry = SpecRegistry()

    # --- Tools ---
    registry.register_tool(
        ToolSpec(
            name="read_diff",
            description="Read the diff of a specific file in the PR",
            input_schema={
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "File path in the PR"},
                },
                "required": ["file_path"],
            },
            risk_level="low",
        )
    )

    registry.register_tool(
        ToolSpec(
            name="read_file",
            description="Read the full content of a file at the PR head commit",
            input_schema={
                "type": "object",
                "properties": {
                    "file_path": {"type": "string"},
                    "start_line": {
                        "type": "integer",
                        "description": "Optional 1-based first line to return",
                    },
                    "end_line": {
                        "type": "integer",
                        "description": "Optional inclusive last line to return",
                    },
                },
                "required": ["file_path"],
            },
            risk_level="low",
        )
    )

    registry.register_tool(
        ToolSpec(
            name="search_code",
            description="Search for a pattern in the repository",
            input_schema={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Search pattern (regex)"},
                    "file_glob": {"type": "string", "description": "File glob filter"},
                },
                "required": ["pattern"],
            },
            risk_level="low",
        )
    )

    registry.register_tool(
        ToolSpec(
            name="get_change_context",
            description=(
                "Read the precomputed Impact Manifest: changed symbols, calls, imports, "
                "repository references, likely tests, and historical graph edges"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "Optional changed-file filter"},
                    "symbol": {"type": "string", "description": "Optional symbol filter"},
                },
            },
            risk_level="low",
        )
    )

    registry.register_tool(
        ToolSpec(
            name="post_comment",
            description="Post a review comment on a specific line of the PR",
            input_schema={
                "type": "object",
                "properties": {
                    "file_path": {"type": "string"},
                    "line": {"type": "integer"},
                    "body": {"type": "string"},
                    "severity": {"type": "string", "enum": ["info", "warning", "error"]},
                },
                "required": ["file_path", "line", "body", "severity"],
            },
            risk_level="medium",
        )
    )

    registry.register_tool(
        ToolSpec(
            name="post_review",
            description="Create one GitHub review containing multiple inline comments",
            input_schema={
                "type": "object",
                "properties": {
                    "comments": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 40,
                        "items": {
                            "type": "object",
                            "properties": {
                                "file_path": {"type": "string"},
                                "line": {"type": "integer"},
                                "body": {"type": "string"},
                            },
                            "required": ["file_path", "line", "body"],
                        },
                    }
                },
                "required": ["comments"],
            },
            risk_level="medium",
        )
    )

    # --- Agents ---
    registry.register_agent(
        AgentSpec(
            name="planner",
            role="planner",
            description="Reads PR diff, decides which reviewers to dispatch",
            model_profile="planner",
            max_steps=1,
            output_contract={
                "type": "object",
                "properties": {
                    "tasks": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "reviewer": {"type": "string"},
                                "files": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                                "rationale": {"type": "string", "maxLength": 500},
                            },
                            "required": ["reviewer", "files"],
                        },
                        "maxItems": 6,
                    },
                },
                "required": ["tasks"],
            },
        )
    )

    # Built-in reviewer specs derive from the immutable catalog.
    for definition in REVIEWER_CATALOG.definitions:
        registry.register_agent(_reviewer_agent_spec(definition))

    registered_reviewers = {name for name, spec in registry.agents.items() if spec.role == "executor"}
    if registered_reviewers != REVIEWER_CATALOG.names:
        raise RuntimeError(
            "Reviewer registry/catalog mismatch: "
            f"registry={sorted(registered_reviewers)}, catalog={sorted(REVIEWER_CATALOG.names)}"
        )

    registry.register_agent(
        AgentSpec(
            name="verifier",
            role="validator",
            description="Reviews candidate findings, removes false positives",
            allowed_tools=[],
            model_profile="verifier",
            max_steps=1,
            output_contract={
                "type": "object",
                "properties": {
                    "verified": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "file": {"type": "string"},
                                "line": {"type": "integer"},
                                "verdict": {"type": "string", "enum": ["confirmed", "false_positive"]},
                                "reason": {"type": "string"},
                            },
                            "required": ["file", "line", "verdict"],
                        },
                    }
                },
                "required": ["verified"],
            },
        )
    )

    registry.register_agent(
        AgentSpec(
            name="commenter",
            role="synthesizer",
            description="Formats confirmed findings into GitHub review comments",
            allowed_tools=["post_comment", "post_review"],
            model_profile="commenter",
            max_steps=1,
        )
    )

    # --- Skills ---
    for skill_name in ["python_best_practices", "react_patterns", "security_rules"]:
        registry.register_skill(skill_name)

    return registry
