"""Configuration — YAML-based config with env var overrides.

Config priority: environment variables > reviewforge.yaml > defaults.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class ModelProfile:
    """A named model configuration for multi-model routing."""

    model: str = ""
    base_url: str = ""
    api_key: str = ""
    temperature: float = 0.1
    max_tokens: int = 4096


@dataclass
class LLMConfig:
    base_url: str = "https://token-plan-cn.xiaomimimo.com/v1"
    api_key: str = ""
    model: str = "mimo-v2.5-pro"
    temperature_planner: float = 0.0
    temperature_reviewer: float = 0.1
    temperature_verifier: float = 0.0
    profiles: dict[str, ModelProfile] = field(default_factory=dict)


@dataclass
class ReviewerConfig:
    name: str = ""
    type: str = ""  # security / performance / style
    enabled: bool = True
    max_steps: int = 8
    max_findings: int = 20
    confidence_threshold: float = 0.5


@dataclass
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8000


@dataclass
class GitHubConfig:
    token: str = ""
    webhook_secret: str = ""


@dataclass
class ReviewForgeConfig:
    """Top-level configuration."""

    llm: LLMConfig = field(default_factory=LLMConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    github: GitHubConfig = field(default_factory=GitHubConfig)
    reviewers: list[ReviewerConfig] = field(default_factory=list)
    skills_dir: str = "skills"
    events_dir: str = ".reviewforge/events"
    confidence_threshold: float = 0.5
    agentic_reviewers: list[str] = field(default_factory=list)
    agentic_default: bool = False  # default OFF — escalate-on-uncertainty replaces full agentic

    # Escalation: auto-verify uncertain findings with agentic tools
    escalation_enabled: bool = True
    escalation_confidence_min: float = 0.4
    escalation_confidence_max: float = 0.7
    escalation_max_steps: int = 3
    escalation_max_tokens: int = 5000

    @classmethod
    def load(cls, config_path: str | Path | None = None) -> ReviewForgeConfig:
        """Load config from YAML file, with env var overrides."""
        cfg = cls()

        # 1. Load from YAML if exists
        if config_path:
            path = Path(config_path)
        else:
            path = cls._find_default_config_path()
        config_base = path.parent.resolve() if path.exists() else Path.cwd().resolve()

        if path.exists():
            with open(path, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            cfg._apply_dict(data)

        # 2. Environment variable overrides
        cfg._apply_env()
        cfg._normalize_paths(config_base)

        # 3. Set defaults for reviewers if empty
        if not cfg.reviewers:
            cfg.reviewers = [
                ReviewerConfig(name="security_reviewer", type="security", max_steps=10),
                ReviewerConfig(name="performance_reviewer", type="performance", max_steps=8),
                ReviewerConfig(name="style_reviewer", type="style", max_steps=6),
                ReviewerConfig(name="testing_reviewer", type="testing", max_steps=6),
                ReviewerConfig(name="doc_reviewer", type="documentation", max_steps=5),
                ReviewerConfig(name="dependency_reviewer", type="dependency", max_steps=6),
                ReviewerConfig(name="accessibility_reviewer", type="accessibility", max_steps=6),
            ]

        return cfg

    @staticmethod
    def _find_default_config_path() -> Path:
        """Find reviewforge.yaml from cwd or its parents, falling back to cwd."""
        cwd = Path.cwd().resolve()
        for base in (cwd, *cwd.parents):
            candidate = base / "reviewforge.yaml"
            if candidate.exists():
                return candidate
        return cwd / "reviewforge.yaml"

    def _normalize_paths(self, config_base: Path) -> None:
        """Resolve relative runtime paths so commands work from repo root or backend/."""
        package_skills = Path(__file__).resolve().parent.parent / "skills"

        skills = Path(self.skills_dir)
        if not skills.is_absolute():
            candidates = [
                config_base / skills,
                Path.cwd().resolve() / skills,
                package_skills,
            ]
            self.skills_dir = str(next((p for p in candidates if p.exists()), candidates[0]))

        events = Path(self.events_dir)
        if not events.is_absolute():
            self.events_dir = str(config_base / events)

    def _apply_dict(self, data: dict[str, Any]) -> None:
        """Apply values from a dict."""
        if "llm" in data:
            for k, v in data["llm"].items():
                if k == "profiles" and isinstance(v, dict):
                    self.llm.profiles = {name: ModelProfile(**p) if isinstance(p, dict) else p for name, p in v.items()}
                elif hasattr(self.llm, k):
                    setattr(self.llm, k, v)
        if "server" in data:
            for k, v in data["server"].items():
                if hasattr(self.server, k):
                    setattr(self.server, k, v)
        if "github" in data:
            for k, v in data["github"].items():
                if hasattr(self.github, k):
                    setattr(self.github, k, v)
        if "reviewers" in data:
            self.reviewers = [ReviewerConfig(**r) for r in data["reviewers"]]
        if "skills_dir" in data:
            self.skills_dir = data["skills_dir"]
        if "events_dir" in data:
            self.events_dir = data["events_dir"]
        if "confidence_threshold" in data:
            self.confidence_threshold = data["confidence_threshold"]
        if "agentic_reviewers" in data and isinstance(data["agentic_reviewers"], list):
            self.agentic_reviewers = [str(name).strip() for name in data["agentic_reviewers"] if str(name).strip()]
        if "agentic_default" in data:
            value = data["agentic_default"]
            self.agentic_default = (
                value.strip().lower() not in ("0", "false", "no", "") if isinstance(value, str) else bool(value)
            )
        if "escalation" in data:
            esc = data["escalation"]
            _esc_types = {
                "enabled": bool,
                "confidence_min": float,
                "confidence_max": float,
                "max_steps": int,
                "max_tokens": int,
            }
            for k, v in esc.items():
                attr = f"escalation_{k}"
                if hasattr(self, attr):
                    expected = _esc_types.get(k)
                    if expected:
                        try:
                            v = expected(v)
                        except (ValueError, TypeError):
                            pass
                    setattr(self, attr, v)

    def _apply_env(self) -> None:
        """Environment variables override config file."""
        self.github.token = os.environ.get("GITHUB_TOKEN", self.github.token)
        self.github.webhook_secret = os.environ.get("GITHUB_WEBHOOK_SECRET", self.github.webhook_secret)
        self.llm.base_url = os.environ.get("LLM_BASE_URL", self.llm.base_url)
        self.llm.api_key = os.environ.get("LLM_API_KEY", self.llm.api_key)
        self.llm.model = os.environ.get("REVIEWFORGE_MODEL", self.llm.model)
        self.server.host = os.environ.get("REVIEWFORGE_HOST", self.server.host)
        port = os.environ.get("REVIEWFORGE_PORT")
        if port:
            self.server.port = int(port)
        # W1: agentic reviewers (comma-separated allowlist)
        agentic = os.environ.get("REVIEWFORGE_AGENTIC_REVIEWERS", "")
        if agentic:
            self.agentic_reviewers = [r.strip() for r in agentic.split(",") if r.strip()]
        # #1: agentic tool loop is the default for all reviewers (when no explicit allowlist)
        default_flag = os.environ.get("REVIEWFORGE_AGENTIC_DEFAULT")
        if default_flag is not None:
            self.agentic_default = default_flag.strip().lower() not in ("0", "false", "no", "")
        # Escalation env overrides
        esc_flag = os.environ.get("REVIEWFORGE_ESCALATION_ENABLED")
        if esc_flag is not None:
            self.escalation_enabled = esc_flag.strip().lower() not in ("0", "false", "no", "")
