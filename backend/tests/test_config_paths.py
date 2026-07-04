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
