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
class LLMConfig:
    base_url: str = "https://token-plan-cn.xiaomimimo.com/v1"
    api_key: str = ""
    model: str = "mimo-v2.5-pro"
    temperature_planner: float = 0.0
    temperature_reviewer: float = 0.1
    temperature_verifier: float = 0.0


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

    @classmethod
    def load(cls, config_path: str | Path | None = None) -> ReviewForgeConfig:
        """Load config from YAML file, with env var overrides."""
        cfg = cls()

        # 1. Load from YAML if exists
        if config_path:
            path = Path(config_path)
        else:
            path = Path("reviewforge.yaml")

        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            cfg._apply_dict(data)

        # 2. Environment variable overrides
        cfg._apply_env()

        # 3. Set defaults for reviewers if empty
        if not cfg.reviewers:
            cfg.reviewers = [
                ReviewerConfig(name="security_reviewer", type="security", max_steps=10),
                ReviewerConfig(name="performance_reviewer", type="performance", max_steps=8),
                ReviewerConfig(name="style_reviewer", type="style", max_steps=6),
            ]

        return cfg

    def _apply_dict(self, data: dict[str, Any]) -> None:
        """Apply values from a dict."""
        if "llm" in data:
            for k, v in data["llm"].items():
                if hasattr(self.llm, k):
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
