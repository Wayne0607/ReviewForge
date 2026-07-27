"""Model Router — maps agent names to role-based LLM instances.

Supports multi-model routing: the 5 fixed functional roles (planner,
fast_review, deep_review, verifier, publication_gate) may each have their
own base_url/api_key/model override, falling back to the global LLMConfig.
Custom agents (with an explicit ``model_profile``) and the legacy
fast/accurate profiles continue to work as before.
"""

from __future__ import annotations

import logging
from urllib.parse import urlsplit, urlunsplit

from langchain_anthropic import ChatAnthropic
from langchain_core.language_models import BaseChatModel
from langchain_openai import ChatOpenAI

from reviewforge.core.config import ROLE_NAMES, LLMConfig, RoleOverride

logger = logging.getLogger(__name__)


# Legacy / explicit-profile map.  Agents not listed use "default".
# Kept for backward compatibility with the v1 fast/accurate YAML config.
DEFAULT_PROFILE_MAP = {
    "planner": "fast",
    "security_reviewer": "accurate",
    "performance_reviewer": "fast",
    "style_reviewer": "fast",
    # correctness outputs 6 structured findings — needs accurate's 8192-token
    # ceiling to avoid JSON truncation and costly full-prompt retries
    "correctness_reviewer": "accurate",
    "coverage_gap_reviewer": "accurate",
    "localization_reviewer": "fast",
    "testing_reviewer": "fast",
    "doc_reviewer": "fast",
    "dependency_reviewer": "fast",
    "accessibility_reviewer": "fast",
    "verifier": "accurate",
    "commenter": "fast",
}


# 5-role functional map.  An agent may map to one of the fixed roles OR
# to a legacy profile (fast/accurate/default).  When an agent maps to a
# role that has a console override, the override is used; otherwise the
# global config wins.
ROLE_MAP: dict[str, str] = {
    "planner": "planner",
    # Fast reviewers
    "performance_reviewer": "fast_review",
    "style_reviewer": "fast_review",
    "localization_reviewer": "fast_review",
    "testing_reviewer": "fast_review",
    "doc_reviewer": "fast_review",
    "dependency_reviewer": "fast_review",
    "accessibility_reviewer": "fast_review",
    # Deep reviewers — security / correctness need a larger context
    "security_reviewer": "deep_review",
    "correctness_reviewer": "deep_review",
    "coverage_gap_reviewer": "deep_review",
    # Verifier / calibrator / cross-PR / evidence
    "verifier": "verifier",
    "calibrator": "verifier",
    "cross_pr_analyzer": "verifier",
    "evidence_prover": "verifier",
    "evidence_refuter": "verifier",
    "evidence_arbiter": "verifier",
    "escalation": "verifier",
    # Publication gate is its own role so it can run a stronger model.
    "publication_gate": "publication_gate",
    "commenter": "fast_review",
}


def _is_minimax(base_url: str, model: str) -> bool:
    return "minimax" in base_url.lower() and model.lower().startswith("minimax-")


def _minimax_anthropic_url(base_url: str) -> str:
    """Map either MiniMax OpenAI endpoint to its Anthropic-compatible root."""

    parsed = urlsplit(base_url)
    return urlunsplit((parsed.scheme, parsed.netloc, "/anthropic", "", ""))


def _build_llm(
    *,
    base_url: str,
    api_key: str,
    model: str,
    temperature: float,
    max_tokens: int | None = None,
) -> BaseChatModel:
    common = {
        "model": model,
        "temperature": temperature,
    }
    if max_tokens is not None:
        common["max_tokens"] = max_tokens
    if _is_minimax(base_url, model):
        # MiniMax recommends Anthropic compatibility for M-series models. It
        # preserves native tool calls and lets M3 return final text without
        # spending the response budget on OpenAI-style <think> content.
        return ChatAnthropic(
            base_url=_minimax_anthropic_url(base_url),
            anthropic_api_key=api_key,
            **common,
        )
    return ChatOpenAI(base_url=base_url, api_key=api_key, **common)


def _temperature_for(config: LLMConfig, agent_name: str) -> float:
    if agent_name == "planner":
        return config.temperature_planner
    verifier_agents = {
        "verifier",
        "calibrator",
        "cross_pr_analyzer",
        "evidence_prover",
        "evidence_refuter",
        "evidence_arbiter",
    }
    if agent_name in verifier_agents or agent_name == "publication_gate":
        return config.temperature_verifier
    return config.temperature_reviewer


def _max_tokens_for(config: LLMConfig, profile_name: str | None) -> int | None:
    """Preserve the legacy accurate 8192-token ceiling for deep reviewers."""
    if profile_name and profile_name in config.profiles:
        return config.profiles[profile_name].max_tokens
    return None


def _resolve_role_override(config: LLMConfig, role: str) -> dict[str, str] | None:
    """Return the (base_url, api_key, model) tuple when the role is overridden."""
    override = config.role_overrides.get(role) if hasattr(config, "role_overrides") else None
    if override is None:
        return None
    # Only treat it as an override when at least one field is non-empty.
    if not (override.base_url or override.api_key or override.model):
        return None
    return {
        "base_url": override.base_url or config.base_url,
        "api_key": override.api_key or config.api_key,
        "model": override.model or config.model,
    }


class ModelRouter:
    """Routes agent names to LLM instances using 5-role + legacy profile maps.

    Config example:
        llm:
          base_url: "https://api.example.com/v1"
          api_key: "sk-..."
          model: "default-model"
          profiles:
            fast:
              model: "small-model"
              temperature: 0.1
            accurate:
              model: "large-model"
              temperature: 0.0
          role_overrides:
            deep_review:
              base_url: "https://api.example.com/v1"
              model: "large-model"
              api_key: "sk-deep"
    """

    def __init__(self, config: LLMConfig) -> None:
        self._config = config
        self._cache: dict[str, BaseChatModel] = {}
        # Seed an empty role_overrides dict so legacy configs still index safely.
        if not getattr(config, "role_overrides", None):
            config.role_overrides = {name: RoleOverride() for name in ROLE_NAMES}
        else:
            for name in ROLE_NAMES:
                config.role_overrides.setdefault(name, RoleOverride())

    def _resolve(self, agent_name: str) -> tuple[str, dict[str, str], str | None, float, int | None]:
        """Pick the (cache-key, effective, profile_name, temperature, max_tokens)."""
        role = ROLE_MAP.get(agent_name)
        # 1) 5-role routing: a configured per-role override always wins for
        # the 5 fixed roles.
        if role in ROLE_NAMES:
            override_effective = _resolve_role_override(self._config, role)
            if override_effective is not None:
                return (
                    f"role:{role}",
                    override_effective,
                    None,
                    _temperature_for(self._config, agent_name),
                    None,
                )
            # No override — fall through to legacy profile resolution.
        # 2) Legacy profile-based routing (fast / accurate / default).
        profile_name = DEFAULT_PROFILE_MAP.get(agent_name, "default")
        profile = self._config.profiles.get(profile_name)
        if profile:
            base_url = profile.base_url or self._config.base_url
            model = profile.model or self._config.model
            effective = {
                "base_url": base_url,
                "api_key": profile.api_key or self._config.api_key,
                "model": model,
            }
            return (
                f"profile:{profile_name}",
                effective,
                profile_name,
                profile.temperature,
                profile.max_tokens,
            )
        # 3) Plain global config.
        return (
            "default",
            {
                "base_url": self._config.base_url,
                "api_key": self._config.api_key,
                "model": self._config.model,
            },
            None,
            _temperature_for(self._config, agent_name),
            None,
        )

    def get_llm(self, agent_name: str) -> BaseChatModel:
        """Get or create an LLM instance for the given agent."""
        key, effective, profile_name, temperature, max_tokens = self._resolve(agent_name)
        if key in self._cache:
            return self._cache[key]

        llm = _build_llm(
            base_url=effective["base_url"],
            api_key=effective["api_key"],
            model=effective["model"],
            temperature=temperature,
            max_tokens=max_tokens,
        )
        self._cache[key] = llm
        if key.startswith("role:"):
            logger.info(
                "LLM[%s → %s]: base_url=%s, model=%s, temp=%s",
                agent_name,
                key,
                effective["base_url"],
                effective["model"],
                temperature,
            )
        else:
            logger.info(
                "LLM[%s/%s]: model=%s, temp=%s",
                key,
                agent_name,
                effective["model"],
                temperature,
            )
        return llm

    # ── introspection helpers (used by tests + admin introspection) ──

    def role_for(self, agent_name: str) -> str | None:
        """Return the role this agent maps to, or None if it uses a legacy profile."""
        return ROLE_MAP.get(agent_name)

    def effective(self, agent_name: str) -> dict[str, str]:
        """Return the resolved (base_url, api_key, model) without building the LLM."""
        _, effective, _, _, _ = self._resolve(agent_name)
        return effective

    def invalidate_cache(self) -> None:
        """Drop cached LLM instances (used by admin hot-reload)."""
        self._cache.clear()
