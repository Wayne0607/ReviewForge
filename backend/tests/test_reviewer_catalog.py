"""Cross-consumer invariants for the immutable built-in reviewer catalog."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from reviewforge.core.reviewer_catalog import REVIEWER_CATALOG
from reviewforge.core.scheduler import DEFAULT_PRIORITY, Scheduler
from reviewforge.core.specs import build_registry
from reviewforge.engine.model_router import DEFAULT_PROFILE_MAP, ROLE_MAP
from reviewforge.engine.orchestrator import Orchestrator
from reviewforge.engine.prompt import build_planner_prompt, build_reviewer_prompt
from reviewforge.engine.reviewers import _MAX_FINDINGS_BY_TYPE, REVIEWER_MAP


def _prompt_text(messages: list[dict[str, str]]) -> str:
    return "\n".join(message["content"] for message in messages)


def test_catalog_is_immutable_and_has_unique_canonical_names() -> None:
    security = REVIEWER_CATALOG["security_reviewer"]

    with pytest.raises(FrozenInstanceError):
        security.priority = 1  # type: ignore[misc]
    with pytest.raises(TypeError):
        REVIEWER_CATALOG.priorities["security_reviewer"] = 1  # type: ignore[index]

    assert len(REVIEWER_CATALOG.names) == len(REVIEWER_CATALOG.definitions)
    assert all(name.endswith("_reviewer") for name in REVIEWER_CATALOG.names)


def test_registry_factory_scheduler_and_model_router_match_catalog() -> None:
    registry = build_registry()
    registered = {name for name, spec in registry.agents.items() if spec.role == "executor"}

    assert registered == REVIEWER_CATALOG.names
    assert set(REVIEWER_MAP) == REVIEWER_CATALOG.names
    assert DEFAULT_PRIORITY == dict(REVIEWER_CATALOG.priorities)
    assert {name: ROLE_MAP[name] for name in REVIEWER_CATALOG} == dict(REVIEWER_CATALOG.model_roles)
    assert {name: DEFAULT_PROFILE_MAP[name] for name in REVIEWER_CATALOG} == dict(REVIEWER_CATALOG.model_profiles)
    assert _MAX_FINDINGS_BY_TYPE == {
        definition.reviewer_type: definition.max_findings for definition in REVIEWER_CATALOG.definitions
    }

    for definition in REVIEWER_CATALOG.definitions:
        spec = registry.get_agent(definition.name)
        assert spec.description == definition.description
        assert spec.allowed_tools == list(definition.allowed_tools)
        assert spec.max_steps == definition.max_steps


def test_factory_catalog_equivalence_fails_fast_in_both_directions() -> None:
    missing = set(REVIEWER_MAP) - {"doc_reviewer"}
    unexpected = {*REVIEWER_MAP, "invented_reviewer"}

    with pytest.raises(RuntimeError, match=r"missing=\['doc_reviewer'\]"):
        REVIEWER_CATALOG.assert_factory_keys(missing)
    with pytest.raises(RuntimeError, match=r"unexpected=\['invented_reviewer'\]"):
        REVIEWER_CATALOG.assert_factory_keys(unexpected)


def test_style_stays_constructible_but_disabled_for_planner() -> None:
    style = REVIEWER_CATALOG["style_reviewer"]

    assert style.planner_enabled is False
    assert "style_reviewer" in REVIEWER_MAP
    assert REVIEWER_CATALOG.resolve_planner_name("style_reviewer") == "correctness_reviewer"
    assert REVIEWER_CATALOG.resolve_planner_name("style") == "correctness_reviewer"


def test_planner_prompt_comes_from_enabled_catalog_and_includes_localization() -> None:
    registry = build_registry()
    prompt = _prompt_text(
        build_planner_prompt(
            {
                "registry": registry,
                "repo": "owner/repo",
                "pr_number": 1,
                "files_changed": ["locales/zh-CN.json"],
                "diff_summary": '+{"save": "保存"}',
            }
        )
    )

    assert "Localization Reviewer" in prompt
    assert "Style Reviewer" not in prompt
    assert DEFAULT_PRIORITY["localization_reviewer"] == 45

    class Task:
        def __init__(self, reviewer: str) -> None:
            self.reviewer = reviewer

    ordered = Scheduler().order([Task("testing_reviewer"), Task("localization_reviewer")])
    assert [task.reviewer for task in ordered] == ["localization_reviewer", "testing_reviewer"]


def test_doc_reviewer_prompt_uses_canonical_agent_for_tools_and_contract() -> None:
    registry = build_registry()
    prompt = _prompt_text(
        build_reviewer_prompt(
            {
                "registry": registry,
                "reviewer_type": "documentation",
                "agent_name": "doc_reviewer",
                "tools_enabled": True,
                "files_to_review": ["docs/api.md"],
                "diffs": {"docs/api.md": "+Returns HTTP 202."},
            }
        )
    )

    assert "## Available Tools" in prompt
    assert "## Output Contract" in prompt
    assert "read_diff" in prompt


def test_catalog_preserves_broad_and_closure_dimension_routing() -> None:
    assert REVIEWER_CATALOG["security_reviewer"].broad_dimensions == ("security",)
    assert REVIEWER_CATALOG["doc_reviewer"].broad_dimensions == ()
    assert REVIEWER_CATALOG["style_reviewer"].broad_dimensions == ()
    assert Orchestrator._reviewer_dimensions("doc_reviewer") == []
    assert Orchestrator._reviewer_dimensions("invented_reviewer") == []
    assert Orchestrator._reviewer_dimensions("security_reviewer") == ["security"]
    assert REVIEWER_CATALOG.reviewer_for_closure_dimension("security") == "security_reviewer"
    assert REVIEWER_CATALOG.reviewer_for_closure_dimension("contract") == "correctness_reviewer"
    assert REVIEWER_CATALOG.reviewer_for_closure_dimension("cross-PR") == "correctness_reviewer"
    assert Orchestrator._dimension_reviewer("localization") == "localization_reviewer"
    with pytest.raises(KeyError):
        Orchestrator._dimension_reviewer("invented-dimension")
