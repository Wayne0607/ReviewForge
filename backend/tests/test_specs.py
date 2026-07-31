"""Tests for spec registry."""

from reviewforge.core.specs import build_registry


def test_registry_builds():
    registry = build_registry()
    assert len(registry.agents) > 0
    assert len(registry.tools) > 0


def test_registry_validates():
    registry = build_registry()
    errors = registry.validate()
    assert errors == [], f"Spec validation errors: {errors}"


def test_planner_spec():
    registry = build_registry()
    planner = registry.get_agent("planner")
    assert planner.role == "planner"
    assert planner.max_steps == 1


def test_security_reviewer_spec():
    registry = build_registry()
    reviewer = registry.get_agent("security_reviewer")
    assert reviewer.role == "executor"
    assert "read_diff" in reviewer.allowed_tools
    assert "post_comment" not in reviewer.allowed_tools  # reviewers don't post directly


def test_verifier_spec():
    registry = build_registry()
    verifier = registry.get_agent("verifier")
    assert verifier.role == "validator"
    assert verifier.allowed_tools == []  # pure reasoning


def test_tool_exists():
    registry = build_registry()
    assert "read_diff" in registry.tools
    assert "post_comment" in registry.tools


def test_unknown_agent_raises():
    registry = build_registry()
    try:
        registry.get_agent("nonexistent")
        assert False, "Should have raised"
    except KeyError:
        pass
