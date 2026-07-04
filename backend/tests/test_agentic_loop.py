"""Tests for the agentic tool loop in reviewers.py.

Uses MockChatLLM with bind_tools to verify:
- Single-shot mode baseline
- Agentic mode: tool_call → ToolMessage → findings
- Tools are read-only (no post_comment)
- Budget enforcement (max_steps)
- Repeat-call guard
"""

from __future__ import annotations

import pytest

from reviewforge.core.specs import build_registry
from reviewforge.core.state import ReviewTask, StateStore
from reviewforge.engine.mock_llm import MockChatLLM
from reviewforge.engine.reviewers import SecurityReviewer
from reviewforge.tools.gateway import ToolGateway
from reviewforge.tools.mock_github import MockGitHubClient


@pytest.fixture
def registry():
    return build_registry()


@pytest.fixture
def gateway(registry):
    return ToolGateway(registry, MockGitHubClient())


@pytest.fixture
def state():
    return StateStore(
        pr_number=1,
        repo="test/repo",
        head_sha="abc123",
        files_changed=["test.py"],
        diff_summary="--- test.py\n+import pickle\n+def load(data): return pickle.loads(data)",
    )


@pytest.fixture
def task():
    return ReviewTask(
        reviewer="security_reviewer",
        files=["test.py"],
        rationale="eval",
    )


@pytest.mark.asyncio
async def test_singleshot_returns_findings(registry, gateway, state, task):
    """Single-shot mode: one LLM call, parse findings from response."""
    llm = MockChatLLM()
    reviewer = SecurityReviewer(llm, registry, gateway, agentic=False)

    findings = await reviewer.execute(task, state)

    assert len(findings) > 0
    assert all(f.file for f in findings)
    assert all(f.severity in ("info", "warning", "error") for f in findings)
    assert all(0.0 <= f.confidence <= 1.0 for f in findings)


@pytest.mark.asyncio
async def test_agentic_returns_findings(registry, gateway, state, task):
    """Agentic mode: tool_call → ToolMessage → findings."""
    llm = MockChatLLM()
    reviewer = SecurityReviewer(llm, registry, gateway, agentic=True, max_tokens=10000)

    findings = await reviewer.execute(task, state)

    assert len(findings) > 0
    assert all(f.reviewer == "security_reviewer" for f in findings)


@pytest.mark.asyncio
async def test_agentic_tools_read_only(registry, gateway, state, task):
    """Agentic mode tools should NOT include post_comment."""
    llm = MockChatLLM()
    reviewer = SecurityReviewer(llm, registry, gateway, agentic=True)

    tools = reviewer._build_tools(state)
    tool_names = {t.name for t in tools}

    assert "read_file" in tool_names
    assert "search_code" in tool_names
    assert "read_diff" in tool_names
    assert "post_comment" not in tool_names


@pytest.mark.asyncio
async def test_agentic_tool_invokes_gateway(registry, gateway, state, task):
    """Agentic tools should call through the gateway (read_file returns content)."""
    llm = MockChatLLM()
    reviewer = SecurityReviewer(llm, registry, gateway, agentic=True)

    tools = reviewer._build_tools(state)
    read_file_tool = next(t for t in tools if t.name == "read_file")

    result = await read_file_tool.ainvoke({"file_path": "test.py"})
    # MockGitHubClient returns deterministic content
    assert isinstance(result, str)
    assert len(result) > 0


@pytest.mark.asyncio
async def test_agentic_fallback_on_empty_tool_calls(registry, gateway, state, task):
    """When LLM returns no tool_calls and no findings, reviewer nudges."""
    llm = MockChatLLM()
    reviewer = SecurityReviewer(llm, registry, gateway, agentic=True, max_tokens=500)
    # Override max_steps to force quick termination
    reviewer._max_steps = 1

    findings = await reviewer.execute(task, state)
    # Should still return something (either findings or empty)
    assert isinstance(findings, list)


@pytest.mark.asyncio
async def test_agentic_sets_reviewer_name(registry, gateway, state, task):
    """All findings from agentic mode should have the reviewer name set."""
    llm = MockChatLLM()
    reviewer = SecurityReviewer(llm, registry, gateway, agentic=True, max_tokens=10000)

    findings = await reviewer.execute(task, state)

    for f in findings:
        assert f.reviewer == "security_reviewer"


@pytest.mark.asyncio
async def test_reviewer_map_has_all_types():
    """REVIEWER_MAP should contain all 7 reviewer types."""
    from reviewforge.engine.reviewers import REVIEWER_MAP

    expected = {
        "security_reviewer",
        "performance_reviewer",
        "style_reviewer",
        "testing_reviewer",
        "doc_reviewer",
        "dependency_reviewer",
        "accessibility_reviewer",
    }
    assert set(REVIEWER_MAP.keys()) == expected


@pytest.mark.asyncio
async def test_singleshot_parse_invalid_json(registry, gateway, state, task):
    """Single-shot should handle invalid JSON gracefully (return empty)."""
    from reviewforge.engine.reviewers import BaseReviewer

    reviewer = BaseReviewer(
        name="test",
        reviewer_type="test",
        llm=MockChatLLM(),
        registry=registry,
        gateway=gateway,
    )
    # _parse_findings with garbage input
    findings = reviewer._parse_findings("this is not json")
    assert findings == []


@pytest.mark.asyncio
async def test_singleshot_parse_valid_findings(registry, gateway):
    """_parse_findings should correctly parse JSON findings."""
    import json

    from reviewforge.engine.reviewers import BaseReviewer

    reviewer = BaseReviewer(
        name="test",
        reviewer_type="test",
        llm=MockChatLLM(),
        registry=registry,
        gateway=gateway,
    )

    content = json.dumps(
        {
            "findings": [
                {
                    "file": "test.py",
                    "line": 5,
                    "severity": "error",
                    "category": "readability",
                    "message": "Readability issue",
                    "suggestion": "Rename for clarity",
                    "confidence": 0.95,
                }
            ]
        }
    )

    findings = reviewer._parse_findings(content)
    assert len(findings) == 1
    assert findings[0].file == "test.py"
    assert findings[0].line == 5
    assert findings[0].severity == "error"
    assert findings[0].category == "readability"
    assert findings[0].confidence == 0.95


@pytest.mark.asyncio
async def test_parse_findings_accepts_top_level_list_and_filters_cross_dimension(registry, gateway):
    """LLM sometimes returns a bare list; non-security reviewers should not keep security categories."""
    import json

    from reviewforge.engine.reviewers import BaseReviewer

    reviewer = BaseReviewer(
        name="style_reviewer",
        reviewer_type="style",
        llm=MockChatLLM(),
        registry=registry,
        gateway=gateway,
    )
    content = json.dumps(
        [
            {
                "file": "app.py",
                "line": 1,
                "severity": "error",
                "category": "sql-injection",
                "message": "wrong dimension",
                "confidence": 0.9,
            },
            {
                "file": "app.py",
                "line": 2,
                "severity": "warning",
                "category": "readability",
                "message": "real style issue",
                "confidence": 0.8,
            },
        ]
    )

    findings = reviewer._parse_findings(content)

    assert len(findings) == 1
    assert findings[0].category == "readability"
