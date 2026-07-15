"""High-confidence deterministic accessibility checks for changed markup."""

from __future__ import annotations

import re
from pathlib import PurePosixPath

from reviewforge.engine.detectors.base import DetectorFinding, dedupe_findings
from reviewforge.engine.detectors.unified_diff import iter_added_lines, iter_right_lines
from reviewforge.engine.finding_anchors import (
    _added_tags,
    _missing_alt_candidates,
    _missing_label_candidates,
)
from reviewforge.engine.symbol_extractor import mask_comments, mask_non_code

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


def _masked_markup_patch(file_path: str, patch: str) -> str:
    """Build equivalent visible hunks with comments and code strings blanked."""

    suffix = PurePosixPath(file_path.replace("\\", "/")).suffix.lower()
    language = "javascript" if suffix in {".js", ".jsx"} else "typescript"
    rows = iter_right_lines(patch)
    added = {line for line, _content in iter_added_lines(patch)}
    rendered: list[str] = []
    group: list[tuple[int, str]] = []

    def mask_markup_source(source: str) -> str:
        code = list(mask_non_code(source, language))

        # Angular's inline `template: `...`` value is executable markup, while
        # arbitrary template literals in TS/TSX are documentation/data. Restore
        # only this explicit framework contract, with HTML comments still blank.
        if suffix == ".ts":
            for marker in re.finditer(r"\btemplate\s*:\s*`", source):
                start = marker.end()
                cursor = start
                escaped = False
                while cursor < len(source):
                    if escaped:
                        escaped = False
                    elif source[cursor] == "\\":
                        escaped = True
                    elif source[cursor] == "`":
                        break
                    cursor += 1
                restored = mask_comments(source[start:cursor], "typescript")
                code[start:cursor] = restored

        # Preserve attribute values for real markup so label/id and explicit
        # alt/name relationships remain visible. A tag whose `<` was masked is
        # inside a comment/string and must stay hidden.
        snapshot = "".join(code)
        for tag in re.finditer(r"</?[A-Za-z][^<>]*?>", source, re.DOTALL):
            if tag.start() < len(snapshot) and not snapshot[tag.start()].isspace():
                code[tag.start() : tag.end()] = source[tag.start() : tag.end()]
        return "".join(code)

    def flush() -> None:
        if not group:
            return
        code = mask_markup_source("\n".join(content for _line, content in group)).split("\n")
        old_count = sum(1 for line, _content in group if line not in added)
        rendered.append(f"@@ -1,{old_count} +{group[0][0]},{len(group)} @@")
        rendered.extend(("+" if row[0] in added else " ") + code[index] for index, row in enumerate(group))
        group.clear()

    for row in rows:
        if group and row[0] != group[-1][0] + 1:
            flush()
        group.append(row)
    flush()
    return "\n".join(rendered)


def _is_complete_new_file_patch(patch: str) -> bool:
    headers = list(re.finditer(r"^@@ .* @@", patch, re.MULTILINE))
    new_file = re.search(r"^@@ -0,0 \+1(?:,(?P<count>\d+))? @@", patch, re.MULTILINE)
    if new_file is None or len(headers) != 1 or headers[0].start() != new_file.start():
        return False
    expected = int(new_file.group("count") or 1)
    additions = iter_added_lines(patch)
    return len(additions) == expected and [line for line, _content in additions] == list(range(1, expected + 1))


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

        patch = _masked_markup_patch(file_path, patch)

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


def is_deterministic_accessibility_finding(file_path: str, line: int, category: str, patch: str) -> bool:
    """Require a complete lexical replay before accessibility auto-confirm."""

    if not _is_complete_new_file_patch(patch):
        return False
    return any(
        finding.line == line and finding.category == category
        for finding in detect_accessibility_findings({file_path: patch})
    )
