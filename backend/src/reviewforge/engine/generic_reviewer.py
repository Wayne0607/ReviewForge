"""Generic, config-driven reviewer.

A "config-type" agent is a reviewer that needs NO custom Python: its whole behaviour
is its declarative AgentSpec (name / tools / model / max_steps) plus its review rules.
The rules are injected as the reviewer's skill body, so a console-created agent can
carry its instructions inline without authoring a separate SKILL.md.

This deliberately reuses the plugin machinery (``plugin_name`` / ``plugin_type`` +
the ``(llm, registry, gateway)`` constructor), so the orchestrator's existing
``_create_reviewer`` path instantiates it unchanged — and crucially, NO arbitrary
code is executed (unlike file-based plugins).
"""

from __future__ import annotations

from typing import Any

from reviewforge.engine.reviewers import BaseReviewer


def make_config_reviewer(
    *,
    name: str,
    reviewer_type: str,
    instructions: str = "",
    max_steps: int = 6,
) -> type[BaseReviewer]:
    """Build a BaseReviewer subclass entirely from declarative config.

    `instructions` are stored as the reviewer's skill body and injected into the
    prompt by ``build_reviewer_prompt`` — same channel a SKILL.md uses.
    """
    _name, _type, _instr, _steps = name, reviewer_type, instructions, max_steps

    class ConfigReviewer(BaseReviewer):
        plugin_name = _name
        plugin_type = _type
        is_config_agent = True  # marker so callers can distinguish from code plugins

        def __init__(self, llm: Any, registry: Any, gateway: Any) -> None:
            super().__init__(
                name=_name,
                reviewer_type=_type,
                llm=llm,
                registry=registry,
                gateway=gateway,
                max_steps=_steps,
            )
            if _instr:
                # Inline rules behave like a Level-2 skill body.
                self._skill_body = _instr
                self._skill_name = _name

    ConfigReviewer.__name__ = f"ConfigReviewer_{reviewer_type}"
    ConfigReviewer.__qualname__ = ConfigReviewer.__name__
    return ConfigReviewer
