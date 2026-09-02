from reviewforge.core.config import ReviewForgeConfig
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
