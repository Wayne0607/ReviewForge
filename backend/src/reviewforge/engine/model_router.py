"""Model Router — maps agent model profiles to LLM instances.

Supports multi-model routing: different agents can use different models,
temperatures, and token limits. Falls back to default config when no
profile override is specified.
"""

from __future__ import annotations

import logging

from langchain_openai import ChatOpenAI

from reviewforge.core.config import LLMConfig

logger = logging.getLogger(__name__)

# Default profile assignments — agents not listed use "default"
DEFAULT_PROFILE_MAP = {
    "planner": "fast",
    "security_reviewer": "accurate",
    "performance_reviewer": "fast",
    "style_reviewer": "fast",
    "localization_reviewer": "fast",
    "testing_reviewer": "fast",
    "doc_reviewer": "fast",
    "dependency_reviewer": "fast",
    "accessibility_reviewer": "fast",
    "verifier": "accurate",
    "commenter": "fast",
}


class ModelRouter:
    """Routes agent names to ChatOpenAI instances based on config profiles.

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
    """

    def __init__(self, config: LLMConfig) -> None:
        self._config = config
        self._cache: dict[str, ChatOpenAI] = {}

    def get_llm(self, agent_name: str) -> ChatOpenAI:
        """Get or create an LLM instance for the given agent."""
        profile_name = DEFAULT_PROFILE_MAP.get(agent_name, "default")

        if profile_name in self._cache:
            return self._cache[profile_name]

        profile = self._config.profiles.get(profile_name)
        if profile:
            llm = ChatOpenAI(
                base_url=profile.base_url or self._config.base_url,
                api_key=profile.api_key or self._config.api_key,
                model=profile.model or self._config.model,
                temperature=profile.temperature,
                max_tokens=profile.max_tokens,
            )
            logger.info(f"LLM[{profile_name}]: model={profile.model or self._config.model}, temp={profile.temperature}")
        else:
            # Fallback to default config
            temp_map = {
                "planner": self._config.temperature_planner,
                "verifier": self._config.temperature_verifier,
            }
            temp = temp_map.get(agent_name, self._config.temperature_reviewer)
            llm = ChatOpenAI(
                base_url=self._config.base_url,
                api_key=self._config.api_key,
                model=self._config.model,
                temperature=temp,
            )
            logger.info(f"LLM[default]: model={self._config.model}, temp={temp}")

        self._cache[profile_name] = llm
        return llm
