"""Plugin Loader — dynamic reviewer discovery from plugins/ directory.

Users can drop custom reviewer .py files into the plugins/ directory.
Each file should contain a class that inherits from BaseReviewer
and has `plugin_name` and `plugin_type` class attributes.

Example plugin:

    from reviewforge.engine.reviewers import BaseReviewer

    class MyReviewer(BaseReviewer):
        plugin_name = "my_custom_reviewer"
        plugin_type = "custom"

        def __init__(self, llm, registry, gateway):
            super().__init__(
                name=self.plugin_name,
                reviewer_type=self.plugin_type,
                llm=llm, registry=registry, gateway=gateway,
                max_steps=6,
            )
"""

from __future__ import annotations

import importlib.util
import inspect
import logging
from pathlib import Path

from reviewforge.engine.reviewers import BaseReviewer

logger = logging.getLogger(__name__)


class PluginLoader:
    """Discover and load custom reviewer plugins from a directory."""

    def discover(self, plugins_dir: Path) -> dict[str, type[BaseReviewer]]:
        """Scan plugins/ directory for reviewer classes.

        Returns:
            Dict mapping plugin_name → reviewer class.
        """
        if not plugins_dir.exists():
            return {}

        plugins: dict[str, type[BaseReviewer]] = {}

        for py_file in sorted(plugins_dir.glob("*.py")):
            if py_file.name.startswith("_"):
                continue

            try:
                loaded = self._load_module(py_file)
                for name, cls in loaded:
                    plugins[name] = cls
                    logger.info(f"Plugin loaded: {name} from {py_file.name}")
            except Exception as e:
                logger.warning(f"Failed to load plugin {py_file.name}: {e}")

        return plugins

    def _load_module(self, py_file: Path) -> list[tuple[str, type[BaseReviewer]]]:
        """Import a .py file and extract reviewer classes."""
        module_name = f"reviewforge.plugins.{py_file.stem}"
        spec = importlib.util.spec_from_file_location(module_name, str(py_file))
        if not spec or not spec.loader:
            return []

        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        results = []
        for attr_name in dir(module):
            attr = getattr(module, attr_name)
            if (
                inspect.isclass(attr)
                and issubclass(attr, BaseReviewer)
                and attr is not BaseReviewer
                and hasattr(attr, "plugin_name")
            ):
                plugin_name = attr.plugin_name
                if not self.validate(attr):
                    logger.warning(f"Plugin {plugin_name} failed validation, skipping")
                    continue
                results.append((plugin_name, attr))

        return results

    @staticmethod
    def validate(cls: type[BaseReviewer]) -> bool:
        """校验插件类是否具备必需属性。返回 True 表示通过。"""
        return (
            bool(getattr(cls, "plugin_name", "")) and bool(getattr(cls, "plugin_type", "")) and hasattr(cls, "execute")
        )
