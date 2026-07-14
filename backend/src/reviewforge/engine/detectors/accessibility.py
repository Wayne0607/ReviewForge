"""High-confidence deterministic accessibility checks for changed markup."""

from __future__ import annotations

import re
from pathlib import PurePosixPath

from reviewforge.engine.detectors.base import DetectorFinding, dedupe_findings
from reviewforge.engine.finding_anchors import (
    _added_tags,
    _missing_alt_candidates,
    _missing_label_candidates,
)

_MARKUP_SUFFIXES = {".html", ".htm", ".js", ".jsx", ".ts", ".tsx", ".vue", ".svelte"}
_LOW_SIGNAL_PATH_PARTS = {"example", "examples", "fixture", "fixtures", "spec", "specs", "test", "tests"}
_NATIVE_IMAGE_TAG = re.compile(r"<img\b")
_NATIVE_CONTROL_TAG = re.compile(r"<(?:input|select|textarea)\b", re.IGNORECASE)
_TITLE_ATTRIBUTE = re.compile(r"\btitle\s*=", re.IGNORECASE)


def _is_low_signal_path(file_path: str) -> bool:
    """Return whether markup is test/example evidence rather than shipped UI."""

    normalized = file_path.replace("\\", "/").lower()
    path = PurePosixPath(normalized)
    name = path.name
    return (
        any(part in _LOW_SIGNAL_PATH_PARTS for part in path.parts[:-1])
        or name.startswith(("test_", "spec_"))
        or bool(re.search(r"(?:^|[._-])(?:test|spec)(?:[._-]|$)", name))
    )


def _native_missing_alt_candidates(patch: str) -> list[int]:
    """Keep only native ``img`` tags, not JSX components such as ``Image``."""

    native_lines = {line for line, _tag in _added_tags(patch, _NATIVE_IMAGE_TAG)}
    return [line for line in _missing_alt_candidates(patch) if line in native_lines]


def _native_missing_label_candidates(patch: str) -> list[int]:
    """Treat an explicit title as a concrete accessible name for native controls."""

    title_named_lines = {line for line, tag in _added_tags(patch, _NATIVE_CONTROL_TAG) if _TITLE_ATTRIBUTE.search(tag)}
    return [line for line in _missing_label_candidates(patch) if line not in title_named_lines]


def detect_accessibility_findings(diffs: dict[str, str]) -> list[DetectorFinding]:
    """Detect concrete missing names on native image and form-control tags.

    The shared candidate parser deliberately skips spread/dynamic props,
    decorative images, labelled controls, and uncertain framework components.
    These findings therefore do not need an LLM to infer absent evidence.
    """

    findings: list[DetectorFinding] = []
    for file_path, patch in diffs.items():
        if PurePosixPath(file_path.replace("\\", "/")).suffix.lower() not in _MARKUP_SUFFIXES:
            continue
        # Tests, fixtures, and examples do not prove that the markup ships to
        # users. Leave those files to contextual review instead of emitting a
        # 0.99 finding that the calibrator would auto-confirm.
        if _is_low_signal_path(file_path):
            continue

        for line in _native_missing_alt_candidates(patch):
            findings.append(
                DetectorFinding(
                    file=file_path,
                    line=line,
                    severity="warning",
                    category="missing-alt",
                    message="An added image has no accessible alternative text.",
                    suggestion='Add a descriptive alt attribute, or alt="" when the image is decorative.',
                    confidence=0.99,
                )
            )

        for line in _native_missing_label_candidates(patch):
            findings.append(
                DetectorFinding(
                    file=file_path,
                    line=line,
                    severity="warning",
                    category="missing-label",
                    message="An added form control has no associated accessible label.",
                    suggestion="Associate a label element or provide an aria-label/aria-labelledby name.",
                    # An associated label can live outside the visible hunk,
                    # so this candidate must receive contextual calibration.
                    confidence=0.9,
                )
            )

    return dedupe_findings(findings)
