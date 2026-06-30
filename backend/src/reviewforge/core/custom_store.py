"""Persistence for console-created Skills and config-type Agents.

Two stores, both restart-safe (untracked files survive `git reset --hard` redeploys):
  - SkillStore       → writes skills/<name>/SKILL.md (auto-discovered by SkillLoader)
  - CustomAgentStore → writes a single custom_agents.json next to the DB

Validation helpers reject unsafe slugs, collisions with built-ins, and unknown tools.
No arbitrary code is ever stored or executed.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import yaml

# Slug: lowercase, starts with a letter, 2-40 chars of [a-z0-9_].
_SLUG = re.compile(r"^[a-z][a-z0-9_]{1,39}$")

# Built-in reviewer types a console agent must not shadow.
BUILTIN_TYPES = {
    "security",
    "performance",
    "style",
    "documentation",
    "testing",
    "dependency",
    "accessibility",
}

DEFAULT_TOOLS = ["read_diff", "read_file", "search_code", "post_comment"]


class ValidationError(ValueError):
    """Raised for an invalid skill/agent payload (mapped to HTTP 400 by the API)."""


def _require_slug(value: str, field: str) -> str:
    value = (value or "").strip()
    if not _SLUG.match(value):
        raise ValidationError(f"{field} 必须是小写字母开头、2-40 位的 [a-z0-9_]（收到: {value!r}）")
    return value


# ── Skills ───────────────────────────────────────────────────


class SkillStore:
    """File CRUD over the skills directory the SkillLoader scans."""

    def __init__(self, skills_dir: str | Path) -> None:
        self._dir = Path(skills_dir)

    def _skill_dir(self, name: str) -> Path:
        name = _require_slug(name, "skill name")
        d = (self._dir / name).resolve()
        if not str(d).startswith(str(self._dir.resolve())):
            raise ValidationError("非法路径（path traversal）")
        return d

    def read(self, name: str) -> str | None:
        f = self._skill_dir(name) / "SKILL.md"
        return f.read_text(encoding="utf-8") if f.exists() else None

    def write(
        self,
        *,
        name: str,
        description: str,
        reviewer_type: str,
        body: str,
        category: str = "",
        references: list[str] | None = None,
    ) -> Path:
        name = _require_slug(name, "skill name")
        if reviewer_type:
            _require_slug(reviewer_type, "reviewer_type")
        if not body.strip():
            raise ValidationError("skill body 不能为空")
        frontmatter: dict[str, Any] = {"name": name, "description": description.strip()}
        if category:
            frontmatter["category"] = category
        if reviewer_type:
            frontmatter["reviewer_type"] = reviewer_type
        if references:
            frontmatter["references"] = references
        fm = yaml.safe_dump(frontmatter, allow_unicode=True, sort_keys=False).strip()
        content = f"---\n{fm}\n---\n\n{body.strip()}\n"
        d = self._skill_dir(name)
        d.mkdir(parents=True, exist_ok=True)
        path = d / "SKILL.md"
        path.write_text(content, encoding="utf-8")
        return path

    def delete(self, name: str) -> bool:
        d = self._skill_dir(name)
        f = d / "SKILL.md"
        if not f.exists():
            return False
        f.unlink()
        # Drop the (now-empty) skill dir, but never anything with leftover content.
        try:
            d.rmdir()
        except OSError:
            pass
        return True


# ── Config-type Agents ───────────────────────────────────────


def normalize_agent(payload: dict[str, Any], known_tools: set[str]) -> dict[str, Any]:
    """Validate + normalize a console agent payload into a stored spec dict."""
    reviewer_type = _require_slug(payload.get("reviewer_type", ""), "reviewer_type")
    if reviewer_type in BUILTIN_TYPES:
        raise ValidationError(f"reviewer_type '{reviewer_type}' 与内置类型冲突，请换一个名字")

    description = (payload.get("description") or "").strip()
    if not description:
        raise ValidationError("description 不能为空")

    tools = payload.get("allowed_tools") or DEFAULT_TOOLS
    if not isinstance(tools, list) or not all(isinstance(t, str) for t in tools):
        raise ValidationError("allowed_tools 必须是字符串数组")
    unknown = [t for t in tools if t not in known_tools]
    if unknown:
        raise ValidationError(f"未知工具: {unknown}（可用: {sorted(known_tools)}）")

    max_steps = payload.get("max_steps", 6)
    if not isinstance(max_steps, int) or not (1 <= max_steps <= 20):
        raise ValidationError("max_steps 必须是 1-20 的整数")

    return {
        "reviewer_type": reviewer_type,
        "name": f"{reviewer_type}_reviewer",
        "description": description,
        "allowed_tools": tools,
        "model_profile": (payload.get("model_profile") or "default").strip() or "default",
        "max_steps": max_steps,
        "instructions": (payload.get("instructions") or "").strip(),
        "enabled": bool(payload.get("enabled", True)),
    }


class CustomAgentStore:
    """JSON-file persistence for config-type agents, keyed by reviewer_type."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._agents: dict[str, dict[str, Any]] = {}
        self.load()

    def load(self) -> None:
        self._agents.clear()
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            for a in data:
                if isinstance(a, dict) and a.get("reviewer_type"):
                    self._agents[a["reviewer_type"]] = a
        except (json.JSONDecodeError, OSError):
            pass

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(list(self._agents.values()), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def list(self) -> list[dict[str, Any]]:
        return list(self._agents.values())

    def get(self, reviewer_type: str) -> dict[str, Any] | None:
        return self._agents.get(reviewer_type)

    def upsert(self, spec: dict[str, Any]) -> None:
        self._agents[spec["reviewer_type"]] = spec
        self._save()

    def delete(self, reviewer_type: str) -> bool:
        if reviewer_type in self._agents:
            del self._agents[reviewer_type]
            self._save()
            return True
        return False
