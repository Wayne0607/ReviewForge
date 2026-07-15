"""Conservative repair of reviewer anchors for structured markup findings.

Review models sometimes identify the correct accessibility defect but anchor the
finding on the surrounding ``return``/container line.  Inline GitHub comments and
benchmark matching both need the concrete changed element.  This module only
reanchors when the added markup exposes an unambiguous, still-unlabelled sink; it
does not create or confirm findings.
"""

from __future__ import annotations

import ast
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
_DETECTOR_PROVENANCE = {"detector", "detector-auto"}
_CODE_IDENTIFIER = re.compile(r"(?<![A-Za-z0-9_$])(?:[A-Za-z_$][A-Za-z0-9_$]*)(?:\s*\.\s*[A-Za-z_$][A-Za-z0-9_$]*)*")
_IDENTIFIER_STOPWORDS = {
    "allow",
    "application",
    "code",
    "command",
    "data",
    "database",
    "directly",
    "execute",
    "file",
    "function",
    "input",
    "method",
    "password",
    "query",
    "secret",
    "shell",
    "sql",
    "string",
    "token",
    "user",
    "value",
}
_SECURITY_ANCHOR_CATEGORY_PAIRS = {
    frozenset({"hardcoded-secrets", "data-leak"}),
    frozenset({"command-injection", "ci-security"}),
}
_REMOTE_DOWNLOAD_CATEGORIES = {"insecure-download", "supply-chain-risk"}
_PYTHON_REDIRECT_CALLS = {
    "httpresponseredirect",
    "httpresponseredirectpermanent",
    "redirect",
    "redirect_to",
    "redirectresponse",
    "sendredirect",
}


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


def _message_code_identifiers(message: str, patch_lines: list[tuple[int, str]]) -> set[str]:
    """Return code-like message terms that identify exactly one line in the patch."""

    code = "\n".join(content for _line, content in patch_lines)
    identifiers: set[str] = set()
    for match in _CODE_IDENTIFIER.finditer(message or ""):
        raw = re.sub(r"\s+", "", match.group(0))
        lowered = raw.lower()
        if len(raw) < 4 or lowered in _IDENTIFIER_STOPWORDS:
            continue
        occurrence = re.compile(rf"(?<![A-Za-z0-9_$]){re.escape(raw)}(?![A-Za-z0-9_$])", re.IGNORECASE)
        if len(occurrence.findall(code)) == 1:
            identifiers.add(lowered)
    return identifiers


def _window_contains_identifier(patch_lines: list[tuple[int, str]], detector_line: int, identifiers: set[str]) -> bool:
    window = "\n".join(content for line, content in patch_lines if abs(line - detector_line) <= 3).lower()
    return any(
        re.search(rf"(?<![a-z0-9_$]){re.escape(identifier)}(?![a-z0-9_$])", window, re.IGNORECASE)
        for identifier in identifiers
    )


def _security_anchor_categories_compatible(
    detector_category: str,
    reviewer_category: str,
    patch_lines: list[tuple[int, str]],
    detector_line: int,
) -> bool:
    if detector_category == reviewer_category:
        return True
    if frozenset({detector_category, reviewer_category}) in _SECURITY_ANCHOR_CATEGORY_PAIRS:
        return True
    categories = {detector_category, reviewer_category}
    if "code-injection" not in categories or not categories.intersection(_REMOTE_DOWNLOAD_CATEGORIES):
        return False
    window = "\n".join(content for line, content in patch_lines if abs(line - detector_line) <= 3)
    return bool(re.search(r"\b(?:curl|wget)\b[^\n]*\|", window, re.IGNORECASE))


def reanchor_security_detector_duplicates(findings: list[Finding], diff_summary: str) -> list[Finding]:
    """Move an LLM duplicate onto a detector line only with unique diff evidence.

    Reviewer line numbers often point at a method declaration while a detector
    points at the concrete sink a few lines later.  A detector is eligible only
    when a code identifier/API named by the reviewer occurs exactly once in the
    file patch and inside that detector's three-line context window.  Candidate
    matching is one-to-one, so adjacent independent sinks are never consumed by
    one deterministic finding.
    """

    grouped: dict[str, dict[str, list[Finding]]] = defaultdict(lambda: {"detectors": [], "reviewers": []})
    for finding in findings:
        bucket = "detectors" if finding.verified_by.strip().lower() in _DETECTOR_PROVENANCE else "reviewers"
        grouped[finding.file][bucket].append(finding)

    proposals: list[tuple[int, int, str, Finding, Finding]] = []
    for file_path, group in grouped.items():
        detectors = group["detectors"]
        reviewers = group["reviewers"]
        if not detectors or not reviewers:
            continue
        patch_lines = iter_right_lines(_extract_file_patch(diff_summary, file_path))
        if not patch_lines:
            continue
        for finding in reviewers:
            identifiers = _message_code_identifiers(finding.message, patch_lines)
            if not identifiers:
                continue
            matching = [
                detector
                for detector in detectors
                if _security_anchor_categories_compatible(
                    normalize_category(detector.category),
                    normalize_category(finding.category),
                    patch_lines,
                    detector.line,
                )
                and _window_contains_identifier(patch_lines, detector.line, identifiers)
            ]
            if len(matching) != 1:
                continue
            detector = matching[0]
            shared_count = sum(
                1 for identifier in identifiers if _window_contains_identifier(patch_lines, detector.line, {identifier})
            )
            proposals.append((-shared_count, abs(finding.line - detector.line), finding.id, finding, detector))

    changed: list[Finding] = []
    claimed_detectors: set[str] = set()
    claimed_reviewers: set[str] = set()
    for _score, _distance, _finding_id, finding, detector in sorted(proposals):
        if finding.id in claimed_reviewers or detector.id in claimed_detectors:
            continue
        finding.line = detector.line
        finding.category = normalize_category(detector.category)
        claimed_reviewers.add(finding.id)
        claimed_detectors.add(detector.id)
        changed.append(finding)
    return changed


def _complete_python_tree(patch: str) -> tuple[ast.Module, list[tuple[int, str]]] | None:
    """Parse a complete post-image beginning at line one, otherwise fail open."""

    new_file_hunk = re.search(r"^@@ -0,0 \+1(?:,(?P<count>\d+))? @@", patch or "", re.MULTILINE)
    if new_file_hunk is None:
        return None
    patch_lines = iter_right_lines(patch)
    if not patch_lines:
        return None
    line_numbers = [line for line, _content in patch_lines]
    declared_count = int(new_file_hunk.group("count") or 1)
    if line_numbers != list(range(1, declared_count + 1)):
        return None
    try:
        return ast.parse("\n".join(content for _line, content in patch_lines)), patch_lines
    except SyntaxError:
        return None


def _python_function_has_redirect_call(function: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Whether a function body itself invokes a known redirect API."""

    stack: list[ast.AST] = list(function.body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                call_name = node.func.id.lower()
            elif isinstance(node.func, ast.Attribute):
                call_name = node.func.attr.lower()
            else:
                call_name = ""
            if call_name in _PYTHON_REDIRECT_CALLS:
                return True
        stack.extend(ast.iter_child_nodes(node))
    return False


def unsupported_python_open_redirect_findings(findings: list[Finding], diff_summary: str) -> list[Finding]:
    """Reject LLM open-redirect claims whose Python function has no redirect sink.

    The gate intentionally requires a complete, parseable post-image and one
    unambiguous function scope.  Truncated patches and uncertain anchors remain
    untouched so a URL builder is rejected only when the diff proves it never
    performs a redirect.
    """

    rejected: list[Finding] = []
    parsed_files: dict[str, tuple[ast.Module, list[tuple[int, str]]] | None] = {}
    for finding in findings:
        if finding.verified_by.strip().lower() in _DETECTOR_PROVENANCE:
            continue
        if normalize_category(finding.category) != "open-redirect" or not finding.file.lower().endswith(".py"):
            continue
        if finding.file not in parsed_files:
            patch = _extract_file_patch(diff_summary, finding.file)
            parsed_files[finding.file] = _complete_python_tree(patch)
        parsed = parsed_files[finding.file]
        if parsed is None:
            continue
        tree, patch_lines = parsed
        functions = [node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
        scopes = [
            function
            for function in functions
            if function.lineno <= finding.line <= (function.end_lineno or function.lineno)
        ]
        if not scopes:
            identifiers = _message_code_identifiers(finding.message, patch_lines)
            scopes = [function for function in functions if function.name.lower() in identifiers]
        if len(scopes) != 1:
            continue
        if not _python_function_has_redirect_call(scopes[0]):
            rejected.append(finding)
    return rejected
