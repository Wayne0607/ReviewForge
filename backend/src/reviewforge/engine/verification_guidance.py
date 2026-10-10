"""Shared v4 contract knowledge; selection never decides a verdict.

Keep localization's existing path trigger identical across generation, lenses
and investigation so a specialist's contract facts survive the stage boundary.
The legacy skill is intentionally independent of this v4 overlay.
"""

from __future__ import annotations

import re

from reviewforge.engine.prompts_v4 import load_prompt

_LOCALIZATION_PATH = re.compile(r"\.(properties|po)$|messages_[^/]+\.json$|/locale/", re.IGNORECASE)


def is_localization_path(path: str) -> bool:
    return bool(_LOCALIZATION_PATH.search(path))


def localization_guidance() -> str:
    return load_prompt("localization_contracts")


def investigation_capabilities() -> str:
    return (
        "## Investigation capabilities\n"
        "The investigator can read the pinned PR repository with read_file, read_diff, grep, "
        "find_definition and find_callers. It has no internet/advisory lookup or runtime execution. "
        "Questions must be answerable from repository evidence and supplied contracts. "
        "An external claim such as a CVE needs a concrete advisory already supplied; "
        "a new dependency alone is not evidence."
    )
