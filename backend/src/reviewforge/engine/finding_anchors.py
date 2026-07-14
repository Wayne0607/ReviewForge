"""Conservative repair of reviewer anchors for structured markup findings.

Review models sometimes identify the correct accessibility defect but anchor the
finding on the surrounding ``return``/container line.  Inline GitHub comments and
benchmark matching both need the concrete changed element.  This module only
reanchors when the added markup exposes an unambiguous, still-unlabelled sink; it
does not create or confirm findings.
"""

from __future__ import annotations

import re
from collections import defaultdict

from reviewforge.core.state import Finding
from reviewforge.engine.detectors.unified_diff import iter_added_lines, iter_right_lines
from reviewforge.engine.security_categories import normalize_category

_SUMMARY_FILE_HEADER = re.compile(r"^--- (?P<file>.+?) \(\+\d+ -\d+\)$")
_ALT_TAG_START = re.compile(r"<(?:img|Image)\b")
_CONTROL_TAG_START = re.compile(r"<(?:input|select|textarea)\b")
_ALT_ATTRIBUTE = re.compile(r"(?:\balt|:alt|v-bind:alt|\[alt\]|\[attr\.alt\])\s*=", re.IGNORECASE)
_ACCESSIBLE_NAME = re.compile(
    r"(?:aria-label|aria-labelledby|:aria-label|v-bind:aria-label|"
    r"\[attr\.aria-label\]|\[attr\.aria-labelledby\])\s*=",
    re.IGNORECASE,
)
_DYNAMIC_PROPS = re.compile(r"\{\s*\.\.\.|\bv-bind\s*=", re.IGNORECASE)
_DECORATIVE_IMAGE = re.compile(
    r"\brole\s*=\s*['\"](?:presentation|none)['\"]|"
    r"\baria-hidden\s*=\s*(?:['\"]?true['\"]?|\{true\})",
    re.IGNORECASE,
)
_NON_TEXT_INPUT = re.compile(
    r"\btype\s*=\s*['\"](?:hidden|submit|reset|button|image)['\"]",
    re.IGNORECASE,
)
_STATIC_ID = re.compile(r"\bid\s*=\s*['\"](?P<id>[A-Za-z][\w:.-]*)['\"]", re.IGNORECASE)


def _extract_file_patch(diff_summary: str, file_path: str) -> str:
    """Extract a file patch from ReviewForge's concatenated diff summary."""

    lines = (diff_summary or "").splitlines()
    if not any(_SUMMARY_FILE_HEADER.match(line) for line in lines):
        return diff_summary or ""

    selected: list[str] = []
    in_target = False
    for line in lines:
        header = _SUMMARY_FILE_HEADER.match(line)
        if header:
            if in_target:
                break
            in_target = header.group("file") == file_path
            continue
        if in_target:
            selected.append(line)
    return "\n".join(selected)


def _added_tags(patch: str, start_pattern: re.Pattern[str]) -> list[tuple[int, str]]:
    """Return added opening tags, joining short contiguous multiline tags."""

    added = iter_added_lines(patch)
    tags: list[tuple[int, str]] = []
    for index, (line_no, content) in enumerate(added):
        match = start_pattern.search(content)
        if match is None:
            continue

        parts = [content[match.start() :]]
        previous_line = line_no
        cursor = index + 1
        while ">" not in "\n".join(parts) and cursor < len(added) and len(parts) < 8:
            next_line, next_content = added[cursor]
            if next_line != previous_line + 1:
                break
            parts.append(next_content)
            previous_line = next_line
            cursor += 1
        tag = "\n".join(parts)
        if ">" in tag:
            tags.append((line_no, tag.split(">", 1)[0] + ">"))
    return tags


def _missing_alt_candidates(patch: str) -> list[int]:
    candidates: list[int] = []
    for line, tag in _added_tags(patch, _ALT_TAG_START):
        if _DYNAMIC_PROPS.search(tag):
            continue
        if _ALT_ATTRIBUTE.search(tag) or _ACCESSIBLE_NAME.search(tag) or _DECORATIVE_IMAGE.search(tag):
            continue
        candidates.append(line)
    return candidates


def _has_associated_label(control_tag: str, patch: str, control_line: int) -> bool:
    identifier = _STATIC_ID.search(control_tag)
    if identifier is not None:
        value = re.escape(identifier.group("id"))
        label_for = re.compile(
            rf"<label\b[^>]*(?:for|htmlFor)\s*=\s*['\"]{value}['\"]",
            re.IGNORECASE,
        )
        if label_for.search("\n".join(content for _line, content in iter_right_lines(patch))):
            return True

    nearby = [content for line, content in iter_right_lines(patch) if control_line - 4 <= line <= control_line + 2]
    before_control = "\n".join(nearby).split(control_tag.split("\n", 1)[0], 1)[0]
    if before_control.lower().rfind("<label") > before_control.lower().rfind("</label"):
        return True
    # ``mat-label`` names a control only through Angular Material's
    # ``mat-form-field``/``matInput`` contract.  The caller already exempts an
    # actual ``matInput`` tag; an unrelated nearby ``mat-label`` must not hide a
    # native input that still lacks a label.
    return False


def _missing_label_candidates(patch: str) -> list[int]:
    candidates: list[int] = []
    for line, tag in _added_tags(patch, _CONTROL_TAG_START):
        if _DYNAMIC_PROPS.search(tag) or _ACCESSIBLE_NAME.search(tag) or _NON_TEXT_INPUT.search(tag):
            continue
        if "matinput" in tag.lower() or _has_associated_label(tag, patch, line):
            continue
        candidates.append(line)
    return candidates


def _assign_nearest(findings: list[Finding], candidates: list[int]) -> list[Finding]:
    """Assign unique sinks to nearby findings without guessing through ties."""

    if not findings or not candidates:
        return []

    available = set(candidates)
    changed: list[Finding] = []

    # Preserve correct anchors first so another drifted finding cannot claim them.
    for finding in findings:
        if finding.line in available:
            available.remove(finding.line)

    proposals: list[tuple[int, int, str, Finding]] = []
    for finding in findings:
        if finding.line in candidates:
            continue
        ranked = sorted((abs(line - finding.line), line) for line in available)
        if not ranked:
            continue
        if len(ranked) > 1 and ranked[0][0] == ranked[1][0]:
            continue
        # A single sink is intrinsically unambiguous. With several sinks, keep
        # the repair local to the surrounding JSX/template block.
        if len(candidates) > 1 and ranked[0][0] > 12:
            continue
        proposals.append((ranked[0][0], ranked[0][1], finding.id, finding))

    assigned_findings: set[str] = set()
    for _distance, target, _finding_id, finding in sorted(proposals):
        if finding.id in assigned_findings or target not in available:
            continue
        finding.line = target
        available.remove(target)
        assigned_findings.add(finding.id)
        changed.append(finding)
    return changed


def reanchor_accessibility_findings(findings: list[Finding], diff_summary: str) -> list[Finding]:
    """Move known accessibility findings onto their concrete added markup sink.

    The supplied findings are mutated and the subset whose line changed is
    returned so callers can persist the repaired coordinate in their state store.
    """

    grouped: dict[tuple[str, str], list[Finding]] = defaultdict(list)
    for finding in findings:
        category = normalize_category(finding.category)
        if category in {"missing-alt", "missing-label"}:
            finding.category = category
            grouped[(finding.file, category)].append(finding)

    changed: list[Finding] = []
    for (file_path, category), group in grouped.items():
        patch = _extract_file_patch(diff_summary, file_path)
        candidates = _missing_alt_candidates(patch) if category == "missing-alt" else _missing_label_candidates(patch)
        changed.extend(_assign_nearest(group, candidates))
    return changed
