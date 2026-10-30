from pathlib import Path

import pytest

from reviewforge.core.config import PipelineV4Config, ReviewForgeConfig, _pipeline_v4_from_dict
from reviewforge.engine.model_router import ROLE_MAP


def test_pipeline_v4_defaults_and_yaml(tmp_path) -> None:
    path = tmp_path / "reviewforge.yaml"
    path.write_text(
        "pipeline_v4:\n  mode: shadow\n  output_language: en\n  max_lenses: 2\n",
        encoding="utf-8",
    )
    cfg = ReviewForgeConfig.load(path)
    assert cfg.pipeline_v4.mode == "shadow"
    assert cfg.pipeline_v4.output_language == "en"
    assert cfg.pipeline_v4.max_lenses == 2
    assert cfg.pipeline_v4.generator_max_hypotheses == 12
    assert cfg.pipeline_v4.protocol_version == 12
    assert cfg.pipeline_v4.budget_policy == "metered"
    assert cfg.pipeline_v4.provider_max_attempts == 3
    assert cfg.pipeline_v4.provider_retry_404 is False


def test_v4_protocol_and_retry_policy_are_explicit_and_bounded(tmp_path):
    path = tmp_path / "reviewforge.yaml"
    path.write_text(
        "pipeline_v4:\n  protocol_version: 2\n  provider_max_attempts: 1\n"
        "  provider_retry_404: true\n  discovery_max_tokens: 65000\n",
        encoding="utf-8",
    )
    cfg = ReviewForgeConfig.load(path).pipeline_v4
    assert (cfg.protocol_version, cfg.provider_max_attempts, cfg.provider_retry_404) == (2, 1, True)
    assert cfg.discovery_max_tokens == 65_000
    path.write_text(
        "pipeline_v4:\n  protocol_version: 99\n  provider_max_attempts: 999\n  provider_retry_404: 'false'\n",
        encoding="utf-8",
    )
    cfg = ReviewForgeConfig.load(path).pipeline_v4
    assert (cfg.protocol_version, cfg.provider_max_attempts, cfg.provider_retry_404) == (12, 3, False)


@pytest.mark.parametrize("protocol", range(1, 13))
def test_explicit_protocols_are_preserved_without_automatic_upgrade(protocol):
    assert _pipeline_v4_from_dict({"protocol_version": protocol}).protocol_version == protocol


@pytest.mark.parametrize("invalid", [0, 13, 99, True, "12", None])
def test_invalid_protocols_use_the_new_run_default(invalid):
    assert _pipeline_v4_from_dict({"protocol_version": invalid}).protocol_version == 12
    assert PipelineV4Config().protocol_version == 12


def test_repository_yaml_matches_the_library_new_run_protocol_default():
    path = Path(__file__).resolve().parents[2] / "reviewforge.yaml"
    config = ReviewForgeConfig.load(path).pipeline_v4
    assert config.protocol_version == PipelineV4Config().protocol_version == 12
    assert config.mode == "legacy"


def test_pipeline_v4_environment_overrides_yaml(monkeypatch, tmp_path) -> None:
    path = tmp_path / "reviewforge.yaml"
    path.write_text("pipeline_v4:\n  mode: legacy\n", encoding="utf-8")
    monkeypatch.setenv("REVIEWFORGE_PIPELINE", "hypothesis")
    monkeypatch.setenv("REVIEWFORGE_OUTPUT_LANGUAGE", "zh-CN")
    cfg = ReviewForgeConfig.load(path)
    assert cfg.pipeline_v4.mode == "hypothesis"
    assert cfg.pipeline_v4.output_language == "zh-CN"


def test_pipeline_agents_have_fixed_roles() -> None:
    assert ROLE_MAP["hypothesis_generator"] == "deep_review"
    assert ROLE_MAP["investigator"] == "verifier"
    assert ROLE_MAP["editor"] == "publication_gate"
    assert {
        ROLE_MAP[f"lens_{name}"] for name in ("security", "localization", "accessibility", "concurrency", "dependency")
    } == {"fast_review"}
