"""Tests for python_correctness skill: discovery, resolver precedence, and prompt injection."""

from pathlib import Path

import reviewforge
from reviewforge.core.specs import build_registry
from reviewforge.engine.prompt import build_reviewer_prompt
from reviewforge.skills.loader import SkillLoader

SKILLS_DIR = Path(reviewforge.__file__).parent / "skills"


def _discover():
    loader = SkillLoader(SKILLS_DIR)
    loader.discover()
    return loader


# --- discovery ---


def test_python_correctness_skill_is_discovered():
    loader = _discover()
    meta = loader.get_meta("python_correctness")
    assert meta is not None
    assert meta.category == "correctness"
    assert meta.reviewer_type == "correctness"
    assert meta.languages == ["python"]


def test_python_correctness_skill_loads_body():
    loader = _discover()
    content = loader.load("python_correctness")
    assert len(content.body) > 100
    assert "None vs falsy" in content.body
    assert "Django" in content.body


# --- resolver precedence ---


def test_python_correctness_takes_precedence_over_universal_for_python():
    """When both python_correctness and correctness_rules exist,
    _resolve_skill must pick python_correctness for language='python'."""
    loader = _discover()
    correctness_metas = [m for m in loader.list_all() if m.reviewer_type == "correctness"]
    names = {m.name for m in correctness_metas}
    assert "python_correctness" in names
    assert "correctness_rules" in names

    # Simulate resolver priority 2: (language, no framework)
    python_specific = [m for m in correctness_metas if "python" in m.languages and not m.frameworks]
    universal = [m for m in correctness_metas if not m.languages and not m.frameworks]

    assert len(python_specific) == 1
    assert python_specific[0].name == "python_correctness"
    assert len(universal) == 1
    assert universal[0].name == "correctness_rules"


def test_universal_correctness_still_falls_back_for_non_python():
    """For a non-Python language with no specific skill, the universal
    correctness_rules skill should still be available."""
    loader = _discover()
    correctness_metas = [m for m in loader.list_all() if m.reviewer_type == "correctness"]
    universal = [m for m in correctness_metas if not m.languages and not m.frameworks]
    assert len(universal) == 1
    assert universal[0].name == "correctness_rules"


# --- prompt injection ---


def test_prompt_injects_python_correctness_body():
    loader = _discover()
    content = loader.load("python_correctness")
    ctx = {
        "registry": build_registry(),
        "reviewer_type": "correctness",
        "agent_name": "correctness_reviewer",
        "files_to_review": ["app.py"],
        "diffs": {"app.py": "x = None or []"},
        "skill_body": content.body,
        "skill_refs": [],
    }
    system = build_reviewer_prompt(ctx)[0]["content"]
    assert "审查规则集" in system
    assert "None vs falsy" in system
