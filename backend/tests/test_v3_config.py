"""Tests for V3 config plumbing: defaults, YAML loading, env overrides, normalization."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from reviewforge.core.config import ReviewForgeConfig, V3Config, _v3_from_dict

# ── V3Config defaults ────────────────────────────────────────────────────────


class TestV3Defaults:
    def test_default_values(self):
        v3 = V3Config()
        assert v3.enabled is False
        assert v3.coverage_min_risk_score == pytest.approx(0.15)
        assert v3.coverage_max_cells_per_round == 24
        assert v3.coverage_max_attempts == 2
        assert v3.evidence_mode == "shadow"
        assert v3.evidence_max_candidates == 20

    def test_top_level_config_has_v3(self):
        cfg = ReviewForgeConfig()
        assert isinstance(cfg.v3, V3Config)
        assert cfg.v3.enabled is False


# ── YAML loading ─────────────────────────────────────────────────────────────


class TestV3YAMLLoading:
    def test_production_yaml_uses_bounded_closure_without_shadow_cost(self):
        project_root = Path(__file__).resolve().parents[2]
        cfg = ReviewForgeConfig.load(project_root / "reviewforge.yaml")

        assert cfg.v3.coverage_max_cells_per_round == 24
        assert cfg.v3.evidence_mode == "off"

    def test_yaml_v3_section(self, tmp_path: Path):
        cfg_file = tmp_path / "reviewforge.yaml"
        cfg_file.write_text(
            textwrap.dedent("""\
                v3:
                  enabled: true
                  coverage_min_risk_score: 0.25
                  coverage_max_cells_per_round: 10
                  coverage_max_attempts: 5
                  evidence_mode: enforce
                  evidence_max_candidates: 30
            """),
            encoding="utf-8",
        )
        cfg = ReviewForgeConfig.load(cfg_file)
        assert cfg.v3.enabled is True
        assert cfg.v3.coverage_min_risk_score == pytest.approx(0.25)
        assert cfg.v3.coverage_max_cells_per_round == 10
        assert cfg.v3.coverage_max_attempts == 5
        assert cfg.v3.evidence_mode == "enforce"
        assert cfg.v3.evidence_max_candidates == 30

    def test_yaml_v3_disabled(self, tmp_path: Path):
        cfg_file = tmp_path / "reviewforge.yaml"
        cfg_file.write_text("v3:\n  enabled: false\n", encoding="utf-8")
        cfg = ReviewForgeConfig.load(cfg_file)
        assert cfg.v3.enabled is False

    def test_yaml_v3_partial(self, tmp_path: Path):
        cfg_file = tmp_path / "reviewforge.yaml"
        cfg_file.write_text(
            textwrap.dedent("""\
                v3:
                  enabled: true
                  evidence_mode: "off"
            """),
            encoding="utf-8",
        )
        cfg = ReviewForgeConfig.load(cfg_file)
        assert cfg.v3.enabled is True
        assert cfg.v3.evidence_mode == "off"
        # defaults preserved
        assert cfg.v3.coverage_min_risk_score == pytest.approx(0.15)
        assert cfg.v3.coverage_max_cells_per_round == 24

    def test_yaml_no_v3_section(self, tmp_path: Path):
        cfg_file = tmp_path / "reviewforge.yaml"
        cfg_file.write_text("server:\n  port: 9000\n", encoding="utf-8")
        cfg = ReviewForgeConfig.load(cfg_file)
        assert cfg.v3.enabled is False

    def test_yaml_v3_not_a_dict(self, tmp_path: Path):
        """If v3 is not a mapping (e.g. a string), defaults apply."""
        cfg_file = tmp_path / "reviewforge.yaml"
        cfg_file.write_text("v3: nonsense\n", encoding="utf-8")
        cfg = ReviewForgeConfig.load(cfg_file)
        assert cfg.v3.enabled is False


# ── String bool parsing ──────────────────────────────────────────────────────


class TestV3StringBool:
    @pytest.mark.parametrize(
        "value, expected",
        [
            ("true", True),
            ("True", True),
            ("TRUE", True),
            ("1", True),
            ("yes", True),
            ("false", False),
            ("False", False),
            ("0", False),
            ("no", False),
            ("", False),
        ],
    )
    def test_string_bool_values(self, value: str, expected: bool):
        result = _v3_from_dict({"enabled": value})
        assert result.enabled is expected

    def test_bool_passthrough(self):
        assert _v3_from_dict({"enabled": True}).enabled is True
        assert _v3_from_dict({"enabled": False}).enabled is False

    def test_int_truthy(self):
        assert _v3_from_dict({"enabled": 1}).enabled is True
        assert _v3_from_dict({"enabled": 0}).enabled is False


# ── Env var overrides ────────────────────────────────────────────────────────


class TestV3EnvOverrides:
    def test_env_v3_enabled(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        cfg_file = tmp_path / "reviewforge.yaml"
        cfg_file.write_text("v3:\n  enabled: false\n", encoding="utf-8")
        monkeypatch.setenv("REVIEWFORGE_V3_ENABLED", "true")
        cfg = ReviewForgeConfig.load(cfg_file)
        assert cfg.v3.enabled is True

    def test_env_v3_enabled_false(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        cfg_file = tmp_path / "reviewforge.yaml"
        cfg_file.write_text("v3:\n  enabled: true\n", encoding="utf-8")
        monkeypatch.setenv("REVIEWFORGE_V3_ENABLED", "false")
        cfg = ReviewForgeConfig.load(cfg_file)
        assert cfg.v3.enabled is False

    def test_env_v3_evidence_mode(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        cfg_file = tmp_path / "reviewforge.yaml"
        cfg_file.write_text('v3:\n  evidence_mode: "off"\n', encoding="utf-8")
        monkeypatch.setenv("REVIEWFORGE_V3_EVIDENCE_MODE", "enforce")
        cfg = ReviewForgeConfig.load(cfg_file)
        assert cfg.v3.evidence_mode == "enforce"

    def test_env_overrides_yaml(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        cfg_file = tmp_path / "reviewforge.yaml"
        cfg_file.write_text(
            textwrap.dedent("""\
                v3:
                  enabled: true
                  evidence_mode: "off"
            """),
            encoding="utf-8",
        )
        monkeypatch.setenv("REVIEWFORGE_V3_ENABLED", "0")
        monkeypatch.setenv("REVIEWFORGE_V3_EVIDENCE_MODE", "enforce")
        cfg = ReviewForgeConfig.load(cfg_file)
        assert cfg.v3.enabled is False
        assert cfg.v3.evidence_mode == "enforce"


# ── Invalid / malformed values ───────────────────────────────────────────────


class TestV3InvalidValues:
    def test_invalid_evidence_mode_falls_back_to_shadow(self):
        result = _v3_from_dict({"evidence_mode": "bogus"})
        assert result.evidence_mode == "shadow"

    def test_non_string_evidence_mode_falls_back_to_shadow(self):
        result = _v3_from_dict({"evidence_mode": 42})
        assert result.evidence_mode == "shadow"

    def test_malformed_int_clamps_to_default(self):
        result = _v3_from_dict({"coverage_max_cells_per_round": "not_a_number"})
        assert result.coverage_max_cells_per_round == 24

    def test_negative_int_clamps_to_minimum(self):
        result = _v3_from_dict({"coverage_max_cells_per_round": -5})
        assert result.coverage_max_cells_per_round == 1

    def test_zero_int_clamps_to_minimum(self):
        result = _v3_from_dict({"coverage_max_attempts": 0})
        assert result.coverage_max_attempts == 1

    def test_malformed_float_clamps_to_default(self):
        result = _v3_from_dict({"coverage_min_risk_score": "abc"})
        assert result.coverage_min_risk_score == pytest.approx(0.15)

    def test_negative_float_clamps_to_minimum(self):
        result = _v3_from_dict({"coverage_min_risk_score": -1.0})
        assert result.coverage_min_risk_score == pytest.approx(0.0)

    def test_malformed_yaml_does_not_crash(self, tmp_path: Path):
        cfg_file = tmp_path / "reviewforge.yaml"
        cfg_file.write_text(
            textwrap.dedent("""\
                v3:
                  enabled: "no"
                  coverage_min_risk_score: [bad]
                  coverage_max_cells_per_round: {also: bad}
                  evidence_mode: 123
            """),
            encoding="utf-8",
        )
        cfg = ReviewForgeConfig.load(cfg_file)
        # should not raise, values fall back to defaults
        assert cfg.v3.enabled is False
        assert cfg.v3.coverage_min_risk_score == pytest.approx(0.15)
        assert cfg.v3.coverage_max_cells_per_round == 24
        assert cfg.v3.evidence_mode == "shadow"

    def test_none_v3_dict_uses_defaults(self):
        """When v3 is present in YAML but evaluates to None."""
        result = _v3_from_dict({})
        assert result.enabled is False
        assert result.evidence_mode == "shadow"
