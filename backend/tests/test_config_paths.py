from pathlib import Path

import reviewforge
from reviewforge.core.config import ReviewForgeConfig


def test_default_config_search_and_skill_path_resolution(monkeypatch):
    package_root = Path(reviewforge.__file__).resolve().parents[2]
    repo_root = package_root.parent
    monkeypatch.chdir(package_root)

    cfg = ReviewForgeConfig.load()

    assert Path(cfg.events_dir).parent == repo_root / ".reviewforge"
    assert (Path(cfg.skills_dir) / "security_rules" / "SKILL.md").exists()


def test_publication_gate_config_is_loaded(tmp_path):
    config_path = tmp_path / "reviewforge.yaml"
    config_path.write_text(
        """
publication_gate:
  enabled: true
  max_steps: 5
  max_tokens: 7000
  concurrency: 6
""",
        encoding="utf-8",
    )

    cfg = ReviewForgeConfig.load(config_path)

    assert cfg.publication_gate_enabled is True
    assert cfg.publication_gate_max_steps == 5
    assert cfg.publication_gate_max_tokens == 7000
    assert cfg.publication_gate_concurrency == 6
