"""Prompt templates for the hypothesis pipeline.

Templates live as Markdown files next to this package and use ``{{placeholder}}``
tokens that code fills before an LLM call.  Keeping the prose out of Python keeps
the T8 prompt-tuning surface a plain-text diff instead of a code change.
"""

from __future__ import annotations

from pathlib import Path

_TEMPLATE_DIR = Path(__file__).resolve().parent


def load_prompt(name: str, **values: object) -> str:
    """Load a prompt template and substitute ``{{placeholder}}`` tokens.

    Unknown placeholders are left untouched so a missing value stays visible in
    the rendered prompt instead of silently disappearing.
    """

    path = _TEMPLATE_DIR / f"{name}.md"
    text = path.read_text(encoding="utf-8")
    for key, value in values.items():
        text = text.replace("{{" + key + "}}", str(value))
    return text


__all__ = ["load_prompt"]
