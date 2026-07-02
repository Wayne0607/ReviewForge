"""Skill Loader — progressive disclosure for review rules.

Three levels:
1. Registration: parse frontmatter only (name + description, ~50 tokens/skill)
2. Load: return full SKILL.md content on demand
3. Read: return individual reference files from references/ subdirectory
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)


@dataclass
class SkillMeta:
    """Lightweight skill metadata (loaded at registration time)."""

    name: str
    description: str = ""
    category: str = ""  # security / performance / style / methodology
    reviewer_type: str = ""  # security / performance / style
    languages: list[str] = field(default_factory=list)  # ["python"], ["go"], [] = universal
    frameworks: list[str] = field(default_factory=list)  # ["react", "next"], ["vue"], [] = any
    references: list[str] = field(default_factory=list)
    path: Path = field(default_factory=Path)


@dataclass
class SkillContent:
    """Full skill content (loaded on demand)."""

    meta: SkillMeta
    body: str = ""
    reference_files: dict[str, str] = field(default_factory=dict)


class SkillLoader:
    """Discovers and loads skills with progressive disclosure.

    Usage:
        loader = SkillLoader(Path("skills"))
        loader.discover()                           # Level 1: parse frontmatter
        content = loader.load("security_rules")     # Level 2: full SKILL.md
        ref = loader.read_ref("security_rules", "patterns.md")  # Level 3: reference
    """

    def __init__(self, skills_dir: str | Path) -> None:
        self._skills_dir = Path(skills_dir)
        self._registry: dict[str, SkillMeta] = {}

    def discover(self) -> list[SkillMeta]:
        """Level 1: Scan skills directory, parse frontmatter only."""
        if not self._skills_dir.exists():
            logger.warning(f"Skills directory not found: {self._skills_dir}")
            return []

        self._registry.clear()
        for skill_dir in sorted(self._skills_dir.iterdir()):
            if not skill_dir.is_dir():
                continue
            skill_md = skill_dir / "SKILL.md"
            if not skill_md.exists():
                continue

            meta = self._parse_frontmatter(skill_md)
            if meta:
                self._registry[meta.name] = meta
                logger.debug(f"Discovered skill: {meta.name}")

        return list(self._registry.values())

    def list_all(self) -> list[SkillMeta]:
        """Return all discovered skill metadata."""
        return list(self._registry.values())

    def get_meta(self, name: str) -> SkillMeta | None:
        return self._registry.get(name)

    def load(self, name: str) -> SkillContent:
        """Level 2: Load full SKILL.md content."""
        meta = self._registry.get(name)
        if not meta:
            raise KeyError(f"Skill not found: {name}")

        skill_md = meta.path / "SKILL.md"
        body = skill_md.read_text(encoding="utf-8")

        # Strip frontmatter
        if body.startswith("---"):
            parts = body.split("---", 2)
            if len(parts) >= 3:
                body = parts[2].strip()

        return SkillContent(meta=meta, body=body)

    def read_ref(self, name: str, ref_path: str) -> str:
        """Level 3: Read a single reference file."""
        meta = self._registry.get(name)
        if not meta:
            raise KeyError(f"Skill not found: {name}")

        full_path = meta.path / "references" / ref_path
        if not full_path.exists():
            raise FileNotFoundError(f"Reference not found: {full_path}")

        # Security: prevent path traversal
        if not full_path.resolve().is_relative_to((meta.path / "references").resolve()):
            raise ValueError(f"Path traversal detected: {ref_path}")

        return full_path.read_text(encoding="utf-8")

    def list_refs(self, name: str) -> list[str]:
        """List available reference files for a skill."""
        meta = self._registry.get(name)
        if not meta:
            return []

        refs_dir = meta.path / "references"
        if not refs_dir.exists():
            return []

        return [f.name for f in refs_dir.iterdir() if f.is_file()]

    def _parse_frontmatter(self, skill_md: Path) -> SkillMeta | None:
        """Parse YAML frontmatter from SKILL.md."""
        try:
            content = skill_md.read_text(encoding="utf-8")
            fallback = SkillMeta(name=skill_md.parent.name, path=skill_md.parent)
            if not content.startswith("---"):
                return fallback

            parts = content.split("---", 2)
            if len(parts) < 3:
                return fallback

            frontmatter = yaml.safe_load(parts[1]) or {}
            return SkillMeta(
                name=frontmatter.get("name", skill_md.parent.name),
                description=frontmatter.get("description", ""),
                category=frontmatter.get("category", ""),
                reviewer_type=frontmatter.get("reviewer_type", ""),
                languages=frontmatter.get("languages", []),
                frameworks=frontmatter.get("frameworks", []),
                references=frontmatter.get("references", []),
                path=skill_md.parent,
            )
        except Exception as e:
            logger.error(f"Failed to parse {skill_md}: {e}")
            return None
