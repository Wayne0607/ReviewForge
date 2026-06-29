"""Tests for progressive Skill loading wired into reviewer prompts (architecture feature #6)."""

from pathlib import Path

import reviewforge
from reviewforge.core.specs import build_registry
from reviewforge.engine.prompt import build_reviewer_prompt
from reviewforge.skills.loader import SkillLoader

SKILLS_DIR = Path(reviewforge.__file__).parent / "skills"


def _by_type():
    loader = SkillLoader(SKILLS_DIR)
    mapping = {}
    for m in loader.discover():
        mapping.setdefault(m.reviewer_type, m)
    return loader, mapping


def test_skill_discovery_maps_reviewer_type():
    _, mapping = _by_type()
    assert "security" in mapping
    assert "style" in mapping
    assert mapping["security"].name == "security_rules"


def test_prompt_injects_skill_body_when_present():
    loader, mapping = _by_type()
    content = loader.load(mapping["security"].name)
    assert len(content.body) > 50
    ctx = {
        "registry": build_registry(),
        "reviewer_type": "security",
        "agent_name": "security_reviewer",
        "files_to_review": ["a.py"],
        "diffs": {"a.py": "x"},
        "skill_body": content.body,
        "skill_refs": mapping["security"].references,
    }
    system = build_reviewer_prompt(ctx)[0]["content"]
    assert "审查规则集" in system
    assert content.body[:40] in system
    # security skill declares references → read_reference hint should appear
    assert "read_reference" in system


def test_prompt_omits_skill_section_when_absent():
    ctx = {
        "registry": build_registry(),
        "reviewer_type": "performance",
        "agent_name": "performance_reviewer",
        "files_to_review": ["a.py"],
        "diffs": {"a.py": "x"},
        "skill_body": "",
        "skill_refs": [],
    }
    system = build_reviewer_prompt(ctx)[0]["content"]
    assert "审查规则集" not in system
