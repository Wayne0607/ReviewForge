"""Tests for model_router profile resolution and token budget assignment."""

from __future__ import annotations

import pytest

from reviewforge.core.config import LLMConfig, ModelProfile
from reviewforge.engine.model_router import DEFAULT_PROFILE_MAP, ModelRouter


class DummyLLM:
    """Lightweight stand-in for ChatOpenAI; records constructor kwargs."""

    instances: list[DummyLLM] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        DummyLLM.instances.append(self)


@pytest.fixture(autouse=True)
def _patch_chat_openai(monkeypatch):
    """Replace ChatOpenAI with DummyLLM for every test."""
    DummyLLM.instances.clear()
    monkeypatch.setattr(
        "reviewforge.engine.model_router.ChatOpenAI", DummyLLM
    )


def _make_config() -> LLMConfig:
    """Build an LLMConfig with distinct fast/accurate token budgets."""
    return LLMConfig(
        base_url="https://test.example.com/v1",
        api_key="sk-test",
        model="mimo-v2.5-pro",
        profiles={
            "fast": ModelProfile(model="mimo-v2.5-pro", temperature=0.1, max_tokens=4096),
            "accurate": ModelProfile(model="mimo-v2.5-pro", temperature=0.0, max_tokens=8192),
        },
    )


def test_correctness_reviewer_uses_accurate_profile():
    """correctness_reviewer must route to accurate (8192 tokens), not fast."""
    router = ModelRouter(_make_config())
    llm = router.get_llm("correctness_reviewer")

    assert DummyLLM.instances[-1].kwargs["max_tokens"] == 8192
    assert llm is DummyLLM.instances[-1]


def test_fast_reviewer_uses_fast_profile():
    """A normal fast reviewer (performance) must resolve to 4096 tokens."""
    router = ModelRouter(_make_config())
    router.get_llm("performance_reviewer")

    assert DummyLLM.instances[-1].kwargs["max_tokens"] == 4096


def test_profile_cache_returns_same_instance():
    """Repeated lookups for the same profile should reuse the cached LLM."""
    router = ModelRouter(_make_config())
    llm_a = router.get_llm("correctness_reviewer")
    llm_b = router.get_llm("security_reviewer")  # also accurate
    assert llm_a is llm_b

    llm_c = router.get_llm("performance_reviewer")
    llm_d = router.get_llm("style_reviewer")  # also fast
    assert llm_c is llm_d
    assert llm_c is not llm_a


def test_default_profile_map_routes_correctness_to_accurate():
    """The mapping constant itself should point correctness to accurate."""
    assert DEFAULT_PROFILE_MAP["correctness_reviewer"] == "accurate"
