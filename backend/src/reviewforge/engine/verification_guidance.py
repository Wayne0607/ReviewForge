"""Shared v4 contract knowledge; selection never decides a verdict.

Keep contract facts available across generation, lenses and investigation;
guidance selects relevant knowledge, never marks a defect as proved.
The legacy skill is intentionally independent of this v4 overlay.
"""

from __future__ import annotations

import re

from reviewforge.engine.context_pack import ContextPack
from reviewforge.engine.prompts_v4 import load_prompt

_LOCALIZATION_PATH = re.compile(r"\.(properties|po)$|messages_[^/]+\.json$|/locale/", re.IGNORECASE)
_PYTHON_CONCURRENCY = re.compile(
    r"\b(?:multiprocessing|threading|concurrent\.futures|get_context)\b|"
    r"\b(?:Process|Thread|ProcessPoolExecutor|ThreadPoolExecutor)\s*\("
)


def is_localization_path(path: str) -> bool:
    return bool(_LOCALIZATION_PATH.search(path))


def localization_guidance() -> str:
    return load_prompt("localization_contracts")


def has_python_concurrency(path: str, source: str) -> bool:
    return path.lower().endswith(".py") and bool(_PYTHON_CONCURRENCY.search(source))


def python_concurrency_guidance() -> str:
    return load_prompt("python_concurrency_contracts")


def defect_scope_guidance() -> str:
    return load_prompt("defect_scope")


def has_state_navigation(pack: ContextPack, unit_id: str) -> bool:
    context = pack.units.get(unit_id)
    return bool(context and any("State navigation (not evidence;" in slice_.reason for slice_ in context.slices))


def state_guidance() -> str:
    return load_prompt("state_contracts")


def investigation_capabilities() -> str:
    return (
        "## Investigation capabilities\n"
        "The investigator can read the pinned PR repository with read_file, read_diff, grep, "
        "find_definition and find_callers. It has no internet/advisory lookup or runtime execution. "
        "Questions must be answerable from repository evidence and supplied contracts. "
        "An external claim such as a CVE needs a concrete advisory already supplied; "
        "a new dependency alone is not evidence."
    )
