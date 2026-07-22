"""Tests for typescript_correctness skill discovery, resolver precedence, and prompt injection."""

from pathlib import Path

import reviewforge
from reviewforge.core.events import EventBus
from reviewforge.core.specs import build_registry
from reviewforge.engine.mock_llm import MockChatLLM
from reviewforge.engine.orchestrator import Orchestrator
from reviewforge.engine.prompt import build_reviewer_prompt
from reviewforge.skills.loader import SkillLoader
from reviewforge.tools.gateway import ToolGateway
from reviewforge.tools.mock_github import MockGitHubClient

SKILLS_DIR = Path(reviewforge.__file__).parent / "skills"


def _loader():
    loader = SkillLoader(SKILLS_DIR)
    loader.discover()
    return loader


def _orch():
    reg = build_registry()
    return Orchestrator(
        registry=reg,
        gateway=ToolGateway(reg, MockGitHubClient()),
        event_bus=EventBus(),
        planner_llm=MockChatLLM(),
        reviewer_llm=MockChatLLM(),
        calibrator_llm=MockChatLLM(),
    )


# ── Discovery ──────────────────────────────────────────────────


def test_typescript_correctness_is_discovered():
    loader = _loader()
    meta = loader.get_meta("typescript_correctness")
    assert meta is not None
    assert meta.name == "typescript_correctness"
    assert meta.category == "correctness"
    assert meta.reviewer_type == "correctness"
    assert meta.languages == ["typescript"]


def test_typescript_correctness_body_is_loaded():
    loader = _loader()
    content = loader.load("typescript_correctness")
    assert len(content.body) > 100
    assert "Promise" in content.body
    assert "nullish" in content.body.lower() or "null" in content.body


def test_typescript_correctness_is_under_5000_chars():
    loader = _loader()
    # Body excludes frontmatter; total file should be under 5000
    skill_path = loader.get_meta("typescript_correctness").path / "SKILL.md"
    total = skill_path.read_text(encoding="utf-8")
    assert len(total) < 5000, f"SKILL.md is {len(total)} chars, exceeds 5000 limit"


# ── Resolver precedence ────────────────────────────────────────


def test_resolve_skill_prefers_ts_correctness_over_universal_for_typescript():
    """When language is 'typescript' and reviewer_type is 'correctness',
    typescript_correctness should be chosen over the universal correctness_rules."""
    orch = _orch()
    metas = orch._skills_by_type.get("correctness", [])
    assert any(m.name == "typescript_correctness" for m in metas)
    assert any(m.name == "correctness_rules" for m in metas)

    resolved = orch._resolve_skill(metas, language="typescript")
    assert resolved.name == "typescript_correctness"


def test_resolve_skill_falls_back_to_universal_for_python():
    """For Python (no language-specific correctness skill), universal correctness_rules wins."""
    orch = _orch()
    metas = orch._skills_by_type.get("correctness", [])

    resolved = orch._resolve_skill(metas, language="python")
    assert resolved.name == "correctness_rules"


def test_resolve_skill_falls_back_to_universal_when_no_language():
    """When language is unknown, universal correctness_rules is the fallback."""
    orch = _orch()
    metas = orch._skills_by_type.get("correctness", [])

    resolved = orch._resolve_skill(metas, language=None)
    assert resolved.name == "correctness_rules"


# ── Skill attachment ───────────────────────────────────────────


def test_ts_correctness_skill_attached_to_reviewer_for_typescript():
    """The correctness reviewer should get typescript_correctness skill body for .ts files."""
    orch = _orch()
    r = orch._create_reviewer("correctness_reviewer")
    orch._attach_skill(r, target_language="typescript")
    assert r._skill_name == "typescript_correctness"
    assert len(r._skill_body) > 50
    assert "Promise" in r._skill_body or "async" in r._skill_body


def test_universal_correctness_skill_attached_for_python():
    """The correctness reviewer should get universal correctness_rules for .py files."""
    orch = _orch()
    r = orch._create_reviewer("correctness_reviewer")
    orch._attach_skill(r, target_language="python")
    assert r._skill_name == "correctness_rules"


# ── Prompt injection ───────────────────────────────────────────


def test_prompt_injects_ts_correctness_skill_body():
    loader = _loader()
    content = loader.load("typescript_correctness")
    ctx = {
        "registry": build_registry(),
        "reviewer_type": "correctness",
        "agent_name": "correctness_reviewer",
        "files_to_review": ["src/handler.ts"],
        "diffs": {"src/handler.ts": "+async function handle() {}"},
        "skill_body": content.body,
        "skill_refs": [],
    }
    system = build_reviewer_prompt(ctx)[0]["content"]
    assert "审查规则集" in system
    assert "Promise" in system or "async" in system


# ── Content quality ────────────────────────────────────────────


def test_skill_covers_required_topics():
    """Verify the skill covers the required TypeScript correctness topics."""
    loader = _loader()
    content = loader.load("typescript_correctness")
    body = content.body.lower()

    required_topics = [
        "promise",  # Promise/async lifecycle
        "nullish",  # nullish vs falsy
        "union",  # union narrowing
        "zod",  # Zod schema
        "prisma",  # Prisma/ORM
        "oauth",  # OAuth/token refresh
        "date",  # date/timezone
        "serializ",  # server/client serialization
        "spread",  # object spread/mutation
    ]
    for topic in required_topics:
        assert topic in body, f"Missing required topic: {topic}"


def test_skill_suppresses_style_and_naming():
    """Verify the skill explicitly suppresses style, naming, and speculative advice."""
    loader = _loader()
    content = loader.load("typescript_correctness")
    body = content.body.lower()

    assert "naming" in body
    assert "style" in body
    assert "speculative" in body or "speculat" in body
