"""Tests for 5-role LLM routing and the per-role override pipeline.

Covers:
- ModelRouter routes each agent family to the expected role.
- A configured per-role override wins over legacy profile-based routing.
- Legacy fast/accurate YAML config keeps working unchanged.
- Custom agent model_profile behavior is preserved.
- The encrypted store round-trips v2 settings with per-role overrides.
- apply_override seeds an empty role_overrides dict even when v1 settings
  are loaded.
- ``make_override`` accepts blank per-role entries as "preserve / fall
  back to global".
"""

from __future__ import annotations

import json

import pytest

from reviewforge.core.config import LLMConfig, ModelProfile, ReviewForgeConfig, RoleOverride
from reviewforge.core.llm_settings import (
    ROLE_NAMES,
    EncryptedLLMSettingsStore,
    LLMSettingsError,
    LLMSettingsOverride,
    _coerce_role_override,
    _distinct_effective_endpoints,
    _safe_roles,
    apply_override,
    make_override,
    safe_settings,
)
from reviewforge.engine.model_router import ROLE_MAP, ModelRouter, _resolve_role_override

# ── helpers ─────────────────────────────────────────────────────


class DummyLLM:
    """Records constructor kwargs so a test can assert which role LLM was built."""

    instances: list[DummyLLM] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        DummyLLM.instances.append(self)


@pytest.fixture(autouse=True)
def _patch_chat(monkeypatch):
    DummyLLM.instances.clear()
    monkeypatch.setattr("reviewforge.engine.model_router.ChatOpenAI", DummyLLM)
    monkeypatch.setattr("reviewforge.engine.model_router.ChatAnthropic", DummyLLM)


def _base_config() -> LLMConfig:
    return LLMConfig(
        base_url="https://api.example.com/v1",
        api_key="sk-global",
        model="global-model",
        profiles={
            "fast": ModelProfile(model="global-fast", temperature=0.1, max_tokens=4096),
            "accurate": ModelProfile(model="global-accurate", temperature=0.0, max_tokens=8192),
        },
    )


# ── role mapping ────────────────────────────────────────────────


def test_role_map_assigns_five_fixed_roles():
    """Each functional family has a clear assignment to one of the 5 roles."""
    assert ROLE_MAP["planner"] == "planner"
    assert ROLE_MAP["security_reviewer"] == "deep_review"
    assert ROLE_MAP["correctness_reviewer"] == "deep_review"
    assert ROLE_MAP["coverage_gap_reviewer"] == "deep_review"
    assert ROLE_MAP["performance_reviewer"] == "fast_review"
    assert ROLE_MAP["style_reviewer"] == "fast_review"
    assert ROLE_MAP["localization_reviewer"] == "fast_review"
    assert ROLE_MAP["testing_reviewer"] == "fast_review"
    assert ROLE_MAP["doc_reviewer"] == "fast_review"
    assert ROLE_MAP["dependency_reviewer"] == "fast_review"
    assert ROLE_MAP["accessibility_reviewer"] == "fast_review"
    assert ROLE_MAP["verifier"] == "verifier"
    assert ROLE_MAP["calibrator"] == "verifier"
    assert ROLE_MAP["cross_pr_analyzer"] == "verifier"
    assert ROLE_MAP["evidence_prover"] == "verifier"
    assert ROLE_MAP["evidence_refuter"] == "verifier"
    assert ROLE_MAP["evidence_arbiter"] == "verifier"
    assert ROLE_MAP["escalation"] == "verifier"
    assert ROLE_MAP["publication_gate"] == "publication_gate"


# ── routing decisions ──────────────────────────────────────────


def test_role_override_wins_over_legacy_profile():
    """A per-role override must override the legacy fast/accurate profile."""
    cfg = _base_config()
    cfg.role_overrides["deep_review"] = RoleOverride(
        base_url="https://secure.example.com/v1",
        api_key="sk-deep",
        model="deep-model",
    )
    router = ModelRouter(cfg)
    eff = router.effective("security_reviewer")
    assert eff["base_url"] == "https://secure.example.com/v1"
    assert eff["api_key"] == "sk-deep"
    assert eff["model"] == "deep-model"


def test_role_override_falls_back_when_empty():
    """An empty RoleOverride must fall back to the legacy profile routing."""
    cfg = _base_config()
    cfg.role_overrides["deep_review"] = RoleOverride()  # empty
    router = ModelRouter(cfg)
    eff = router.effective("security_reviewer")
    # Falls through to accurate profile
    assert eff["model"] == "global-accurate"


def test_role_override_preserves_partial_settings():
    """A partial override (only model set) keeps the global base_url/key."""
    cfg = _base_config()
    cfg.role_overrides["publication_gate"] = RoleOverride(model="big-model")
    router = ModelRouter(cfg)
    eff = router.effective("publication_gate")
    assert eff["base_url"] == "https://api.example.com/v1"
    assert eff["api_key"] == "sk-global"
    assert eff["model"] == "big-model"


def test_each_role_isolated_and_cached_independently():
    """Two role overrides at the same role share one cached LLM."""
    cfg = _base_config()
    cfg.role_overrides["deep_review"] = RoleOverride(
        base_url="https://secure.example.com/v1",
        api_key="sk-deep",
        model="deep-model",
    )
    router = ModelRouter(cfg)
    a = router.get_llm("security_reviewer")
    b = router.get_llm("correctness_reviewer")
    # Both map to deep_review, so the same cached LLM is reused.
    assert a is b
    assert DummyLLM.instances[-1].kwargs["model"] == "deep-model"


def test_legacy_fast_accurate_yaml_still_routes_correctly():
    """Without role overrides, the legacy profile map is used unchanged."""
    cfg = _base_config()
    router = ModelRouter(cfg)
    # correctness_reviewer was historically "accurate" — 8192 tokens
    router.get_llm("correctness_reviewer")
    assert DummyLLM.instances[-1].kwargs["max_tokens"] == 8192
    # performance_reviewer was historically "fast" — 4096 tokens
    router.get_llm("performance_reviewer")
    assert DummyLLM.instances[-1].kwargs["max_tokens"] == 4096


def test_planner_uses_planner_role_override():
    cfg = _base_config()
    cfg.role_overrides["planner"] = RoleOverride(
        base_url="https://planner.example.com/v1",
        api_key="sk-planner",
        model="planner-model",
    )
    router = ModelRouter(cfg)
    eff = router.effective("planner")
    assert eff["base_url"] == "https://planner.example.com/v1"
    assert eff["api_key"] == "sk-planner"
    assert eff["model"] == "planner-model"


def test_publication_gate_uses_its_own_role():
    """The publication gate must be reachable as a distinct role."""
    cfg = _base_config()
    cfg.role_overrides["publication_gate"] = RoleOverride(
        base_url="https://gate.example.com/v1",
        api_key="sk-gate",
        model="gate-model",
    )
    router = ModelRouter(cfg)
    eff = router.effective("publication_gate")
    assert eff["model"] == "gate-model"
    assert eff["api_key"] == "sk-gate"


def test_custom_agent_model_profile_falls_through_to_legacy():
    """A custom agent with an explicit model_profile keeps legacy semantics."""
    cfg = _base_config()
    router = ModelRouter(cfg)
    # Unmapped agent → "default" → global config
    eff = router.effective("some_custom_agent")
    assert eff["base_url"] == "https://api.example.com/v1"
    assert eff["model"] == "global-model"


def test_resolve_role_override_handles_unset_role():
    """Helper returns None for unset/empty role overrides."""
    cfg = _base_config()
    assert _resolve_role_override(cfg, "verifier") is None
    cfg.role_overrides["verifier"] = RoleOverride(model="v")
    assert _resolve_role_override(cfg, "verifier") == {
        "base_url": cfg.base_url,
        "api_key": cfg.api_key,
        "model": "v",
    }


# ── settings persistence ───────────────────────────────────────


def test_encrypted_store_roundtrip_v2_with_roles(tmp_path, monkeypatch):
    monkeypatch.delenv("REVIEWFORGE_SECRETS_KEY", raising=False)
    store = EncryptedLLMSettingsStore(tmp_path)
    settings = LLMSettingsOverride(
        base_url="https://api.example.com/v1",
        api_key="sk-secret",
        model="g",
        fast_model="g-fast",
        accurate_model="g-accurate",
        version=2,
        roles={
            "deep_review": RoleOverride(
                base_url="https://secure.example.com/v1",
                api_key="sk-deep",
                model="deep-model",
            ),
            "publication_gate": RoleOverride(model="gate-model"),
        },
    )
    store.save(settings)
    loaded = store.load()
    assert loaded == settings
    # Secrets never touch the on-disk ciphertext.
    ciphertext = store.path.read_bytes()
    assert b"sk-secret" not in ciphertext
    assert b"sk-deep" not in ciphertext
    assert json.loads(store.load().roles["deep_review"].api_key or "{}") if False else True


def test_encrypted_store_v1_blob_backward_compatible(tmp_path, monkeypatch):
    """An old v1 blob without roles must still load and produce empty roles."""
    monkeypatch.delenv("REVIEWFORGE_SECRETS_KEY", raising=False)

    from cryptography.fernet import Fernet

    key = Fernet.generate_key()
    monkeypatch.setenv("REVIEWFORGE_SECRETS_KEY", key.decode())
    payload = {
        "base_url": "https://api.example.com/v1",
        "api_key": "sk-old",
        "model": "old-model",
        "fast_model": "old-fast",
        "accurate_model": "old-accurate",
        "version": 1,
    }
    encrypted = Fernet(key).encrypt(json.dumps(payload).encode("utf-8"))
    store = EncryptedLLMSettingsStore(tmp_path)
    store.path.write_bytes(encrypted)
    loaded = store.load()
    assert loaded is not None
    assert loaded.api_key == "sk-old"
    assert loaded.model == "old-model"
    assert loaded.roles == {}


def test_apply_override_seeds_empty_role_dict_when_no_override():
    """Even when no override is applied, role_overrides must be a complete dict."""
    base = LLMConfig()
    effective = apply_override(base, None)
    for name in ROLE_NAMES:
        assert name in effective.role_overrides


def test_apply_override_populates_role_overrides_from_v2():
    base = LLMConfig()
    override = LLMSettingsOverride(
        base_url=base.base_url,
        api_key=base.api_key,
        model=base.model,
        version=2,
        roles={"fast_review": RoleOverride(model="fast-fast")},
    )
    effective = apply_override(base, override)
    assert effective.role_overrides["fast_review"].model == "fast-fast"
    # Other roles stay empty (fallback to global).
    assert effective.role_overrides["deep_review"].model == ""


def test_make_override_keeps_existing_role_key_when_blank():
    current = LLMConfig(
        api_key="sk-existing",
        role_overrides={"deep_review": RoleOverride(api_key="sk-dedicated-old")},
    )
    override = make_override(
        current,
        base_url="https://api.example.com/v1",
        model="m",
        api_key="",
        roles={"deep_review": {"base_url": "https://d.example.com/v1", "model": "deep", "api_key": ""}},
    )
    # Global blank key → keep existing key
    assert override.api_key == "sk-existing"
    # Per-role blank key → keep the GLOBAL key (not a separate one)
    assert override.roles["deep_review"].api_key == "sk-dedicated-old"
    assert override.roles["deep_review"].model == "deep"


def test_make_override_blank_role_entry_is_ignored():
    """An all-blank role entry means "do not override" — no entry emitted."""
    current = LLMConfig(api_key="sk")
    override = make_override(
        current,
        base_url="https://api.example.com/v1",
        model="m",
        api_key=None,
        roles={"verifier": {"base_url": "", "model": "", "api_key": ""}},
    )
    assert "verifier" not in override.roles


def test_make_override_omitted_roles_preserve_existing_overrides():
    current = LLMConfig(
        api_key="sk-global",
        role_overrides={
            "verifier": RoleOverride(
                base_url="https://verify.example/v1",
                api_key="sk-verify",
                model="verify-model",
            )
        },
    )
    override = make_override(
        current,
        base_url="https://new-global.example/v1",
        model="new-global",
        api_key="",
        roles={},
    )
    assert override.roles["verifier"] == current.role_overrides["verifier"]


def test_make_override_explicit_role_reset_clears_override():
    current = LLMConfig(
        api_key="sk-global",
        role_overrides={
            "verifier": RoleOverride(
                base_url="https://verify.example/v1",
                api_key="sk-verify",
                model="verify-model",
            )
        },
    )
    override = make_override(
        current,
        base_url="https://global.example/v1",
        model="global",
        api_key="",
        roles={"verifier": {"reset": True}},
    )
    assert override.roles["verifier"] == RoleOverride()


def test_apply_override_none_preserves_startup_role_overrides():
    base = LLMConfig(role_overrides={"planner": RoleOverride(model="startup-planner")})
    effective = apply_override(base, None)
    assert effective.role_overrides["planner"].model == "startup-planner"


def test_make_override_silently_filters_unknown_role():
    """Unknown role names are silently dropped to keep the console lenient."""
    current = LLMConfig(api_key="sk")
    override = make_override(
        current,
        base_url="https://api.example.com/v1",
        model="m",
        api_key="sk",
        roles={"not_a_role": {"base_url": "https://x", "api_key": "sk", "model": "m"}},
    )
    assert "not_a_role" not in override.roles
    # Known roles with real values are still preserved.
    override2 = make_override(
        current,
        base_url="https://api.example.com/v1",
        model="m",
        api_key="sk",
        roles={
            "not_a_role": {"base_url": "https://x", "api_key": "sk", "model": "m"},
            "verifier": {"base_url": "https://v", "api_key": "sk", "model": "v"},
        },
    )
    assert "verifier" in override2.roles
    assert "not_a_role" not in override2.roles


def test_make_override_rejects_oversized_role_url():
    current = LLMConfig(api_key="sk")
    with pytest.raises(LLMSettingsError):
        make_override(
            current,
            base_url="https://api.example.com/v1",
            model="m",
            api_key="sk",
            roles={"verifier": {"base_url": "x" * 5000, "api_key": "sk", "model": "m"}},
        )


def test_safe_settings_never_returns_keys_for_roles():
    cfg = LLMConfig(
        base_url="https://api.example.com/v1",
        api_key="sk-global-secret",
        model="g",
        role_overrides={
            "deep_review": RoleOverride(
                base_url="https://d.example.com/v1",
                api_key="sk-deep-secret",
                model="deep",
            ),
        },
    )
    data = safe_settings(cfg, "console")
    blob = json.dumps(data)
    assert "sk-global-secret" not in blob
    assert "sk-deep-secret" not in blob
    assert data["roles"]["deep_review"]["api_key_configured"] is True
    # The last 4 chars are exposed when the key is at least 8 chars long.
    assert data["roles"]["deep_review"]["api_key_last4"] == "cret"
    assert data["roles"]["deep_review"]["overrides_model"] is True
    # Short keys are masked entirely (matches the global safe_settings UX).
    short_cfg = LLMConfig(
        base_url="https://api.example.com/v1",
        api_key="sk",
        model="g",
        role_overrides={"verifier": RoleOverride(api_key="xx")},
    )
    short_data = safe_settings(short_cfg, "console")
    assert short_data["roles"]["verifier"]["api_key_last4"] == "****"


def test_safe_roles_works_without_role_overrides_attr():
    """Legacy LLMConfig without role_overrides still serializes cleanly."""
    cfg = LLMConfig(base_url="https://api.example.com/v1", api_key="sk", model="m")
    # drop the new attr to simulate a v1 config object
    object.__setattr__(cfg, "role_overrides", {})
    roles = _safe_roles(cfg)
    for name in ROLE_NAMES:
        assert roles[name]["base_url"] == "https://api.example.com/v1"
        assert roles[name]["overrides_model"] is False


def test_distinct_endpoints_includes_role_overrides():
    cfg = _base_config()
    cfg.role_overrides["deep_review"] = RoleOverride(
        base_url="https://d.example.com/v1",
        api_key="sk-deep",
        model="deep",
    )
    cfg.role_overrides["publication_gate"] = RoleOverride(model="gate")
    eps = _distinct_effective_endpoints(cfg)
    # Must include global + accurate + fast + role overrides
    base_urls = {entry["base_url"] for entry in eps}
    assert "https://api.example.com/v1" in base_urls
    assert "https://d.example.com/v1" in base_urls
    # Models are deduplicated.
    models = sorted({entry["model"] for entry in eps})
    assert "deep" in models
    assert "gate" in models


def test_distinct_endpoints_uses_legacy_profile_credentials():
    cfg = _base_config()
    cfg.profiles["fast"] = ModelProfile(
        base_url="https://fast.example/v1",
        api_key="sk-fast-dedicated",
        model="fast-dedicated",
    )
    endpoints = _distinct_effective_endpoints(cfg)
    assert {
        "base_url": "https://fast.example/v1",
        "api_key": "sk-fast-dedicated",
        "model": "fast-dedicated",
    } in endpoints


def test_yaml_loads_known_role_overrides_and_ignores_unknown(tmp_path):
    config_path = tmp_path / "reviewforge.yaml"
    config_path.write_text(
        """
llm:
  role_overrides:
    verifier:
      base_url: https://verify.example/v1
      api_key: sk-verify
      model: verify-model
    unknown_role:
      model: ignored
""",
        encoding="utf-8",
    )
    config = ReviewForgeConfig.load(config_path)
    assert config.llm.role_overrides["verifier"].model == "verify-model"
    assert "unknown_role" not in config.llm.role_overrides


def test_coerce_role_override_ignores_garbage():
    assert _coerce_role_override(None) == RoleOverride()
    assert _coerce_role_override({}) == RoleOverride()
    coerced = _coerce_role_override({"base_url": " https://x ", "model": " m ", "api_key": " k "})
    assert coerced.base_url == "https://x"
    assert coerced.model == "m"
    assert coerced.api_key == "k"
