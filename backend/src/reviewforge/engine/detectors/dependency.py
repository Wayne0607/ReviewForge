"""Deterministic dependency/dependency-management risk detectors."""

from __future__ import annotations

import json
import re
from pathlib import PurePosixPath

from reviewforge.engine.detectors.advisories import detect_advisory_findings
from reviewforge.engine.detectors.base import DetectorFinding, dedupe_findings, normalize_category_for_detector
from reviewforge.engine.detectors.unified_diff import iter_added_lines, iter_right_lines
from reviewforge.engine.symbol_extractor import mask_comments, mask_non_code


class _Rule:
    """Simple structure for dependency regex checks."""

    __slots__ = ("pattern", "category", "severity", "message", "suggestion", "confidence", "flags")

    def __init__(
        self,
        pattern: str,
        category: str,
        severity: str,
        message: str,
        suggestion: str,
        confidence: float,
        flags: int = re.IGNORECASE,
    ) -> None:
        self.pattern = pattern
        self.category = category
        self.severity = severity
        self.message = message
        self.suggestion = suggestion
        self.confidence = confidence
        self.flags = flags


def _find_matches(lines: list[tuple[int, str]], pattern: str, flags: int = re.IGNORECASE) -> list[tuple[int, str]]:
    """Return mapped ``(new_file_line, content)`` matches for a regex pattern."""

    out: list[tuple[int, str]] = []
    for line_no, line in lines:
        if re.search(pattern, line, flags):
            out.append((line_no, line))
    return out


_RULES_BY_NAME: dict[str, list[_Rule]] = {
    "package.json": [
        _Rule(
            r'["\'][^"\']+["\']\s*:\s*["\'](?:\*|latest|next|dev|[\^~<>]=?[^"\']*)["\']',
            "dependency-version-range",
            "warning",
            "Mutable dependency version in package.json.",
            "Pin to semver ranges or fixed versions as policy requires.",
            0.94,
        ),
        _Rule(
            r'"postinstall"\s*:\s*["\'].*\|?\s*(?:curl|wget|bash)\s+',
            "supply-chain-risk",
            "error",
            "Postinstall script executes external network commands.",
            "Avoid remote shell in postinstall; use vetted package scripts.",
            0.98,
        ),
    ],
    "requirements.txt": [
        _Rule(
            r"^[^#\n][A-Za-z0-9_.-]+\s*(?:>=|<=|>|<|~=|!=|\*)\s*[^\n]+",
            "dependency-version-range",
            "warning",
            "requirements.txt dependency uses mutable constraints.",
            "Prefer pinned versions when possible.",
            0.93,
        ),
        _Rule(
            r"\*",
            "dependency-version-range",
            "warning",
            "Wildcard dependency in requirements.txt.",
            "Avoid wildcard dependency ranges.",
            0.9,
        ),
        _Rule(
            r"^\s*[A-Za-z0-9][A-Za-z0-9_.-]*(?:\[[^\]]+\])?\s*(?:;[^#]+)?\s*$",
            "dependency-version-range",
            "warning",
            "requirements.txt dependency has no version constraint.",
            "Pin the dependency to an exact reviewed version.",
            0.92,
        ),
    ],
    "pyproject.toml": [
        _Rule(
            r'^\s*[\w.-]+\s*=\s*["\']\s*(?:\*|\^|~|>=|<=|>|<|!=)',
            "dependency-version-range",
            "warning",
            "Potential non-pinned dependency constraint in pyproject.",
            "Keep explicit or policy-compliant constraints.",
            0.92,
        ),
        _Rule(
            r"git\s*=\s*['\"](https://|git\\+https://)",
            "supply-chain-risk",
            "warning",
            "VCS dependency source in pyproject.",
            "Pin commits/references instead of floating branches.",
            0.9,
        ),
    ],
    "go.mod": [
        _Rule(
            r"\breplace\s+(?:\(|[A-Za-z0-9_.\-/]+)",
            "supply-chain-risk",
            "warning",
            "go.mod replace directive changed.",
            "Review replaced module provenance.",
            0.9,
        ),
    ],
    "pom.xml": [
        _Rule(
            r"<version>\s*(?:LATEST|RELEASE|[^<]*-SNAPSHOT)\s*</version>",
            "dependency-version-range",
            "warning",
            "Maven non-reproducible version tag used.",
            "Prefer explicit pinned versions.",
            0.93,
        ),
        _Rule(
            r"<version>\s*[\[(][^,<]+,\s*[)\]]\s*</version>",
            "dependency-version-range",
            "warning",
            "Maven dependency uses an open-ended version range.",
            "Pin the dependency to an exact reviewed version.",
            0.93,
        ),
        _Rule(
            r"<scope>system</scope>",
            "dependency-deprecated",
            "warning",
            "System-scope dependency in Maven.",
            "Avoid system scope dependencies.",
            0.9,
        ),
    ],
    "Gemfile": [
        _Rule(
            r"^\s*gem\s+['\"][^'\"]+['\"]\s*$",
            "dependency-version-range",
            "warning",
            "Gem dependency has no version constraint.",
            "Pin the gem to an exact reviewed version.",
            0.91,
        ),
        _Rule(
            r"^\s*gem\s+['\"][^'\"]+['\"]\s*,\s*['\"]\s*(?:~>|>=|<=|>|<|\*)",
            "dependency-version-range",
            "warning",
            "Gem dependency uses a mutable version range.",
            "Pin the gem to an exact reviewed version.",
            0.93,
        ),
        _Rule(
            r"\sgit\s*[:=]\s*['\"]",
            "supply-chain-risk",
            "warning",
            "Gemfile uses git dependency.",
            "Pin to secure commit.",
            0.91,
        ),
        _Rule(
            r"\bpath\s*(?:[:=]|\s)\s*['\"][^'\"]+['\"]",
            "supply-chain-risk",
            "warning",
            "Gemfile uses local path dependency.",
            "Avoid local path deps in production.",
            0.87,
        ),
    ],
    "Cargo.toml": [
        _Rule(
            r'=\s*"\*"',
            "dependency-version-range",
            "warning",
            "Cargo uses wildcard dependency version.",
            "Prefer explicit semver bounds.",
            0.95,
        ),
        _Rule(
            r"^(?![^\n]*\brev\s*=)[^\n]*\bgit\s*=\s*['\"][^'\"]+['\"]",
            "supply-chain-risk",
            "warning",
            "Cargo git dependency detected.",
            "Pin commit revision in git specs.",
            0.9,
        ),
        _Rule(
            r"path\s*=\s*['\"][^'\"]+['\"]",
            "supply-chain-risk",
            "warning",
            "Cargo local path dependency used.",
            "Avoid local path dependencies.",
            0.87,
        ),
        _Rule(
            r'^\s*(?!rust-version\s*=)[\w.-]+\s*=\s*(?:["\']\s*(?:>=|<=|>|<|\^|~)|\{[^}]*\bversion\s*=\s*["\']\s*(?:>=|<=|>|<|\^|~))',
            "dependency-version-range",
            "warning",
            "Cargo dependency uses a mutable version range.",
            "Use an exact `=x.y.z` requirement for reproducible resolution.",
            0.93,
        ),
        _Rule(
            r'^\s*(?!(?:version|edition|rust-version|resolver)\s*=)[\w.-]+\s*=\s*(?:["\']0\.\d+(?:\.\d+)?["\']|\{[^}]*\bversion\s*=\s*["\']0\.\d+(?:\.\d+)?["\'])',
            "dependency-version-range",
            "warning",
            "Cargo 0.x dependency uses the default broad compatibility range.",
            "Use an exact `=0.x.y` requirement when reproducibility is required.",
            0.9,
        ),
    ],
    "workflow": [
        _Rule(
            r"\bcurl\s+.*\|\s*(?:sh|bash)",
            "supply-chain-risk",
            "error",
            "Workflow executes remote script via piped shell.",
            "Avoid curl|bash network installs.",
            0.99,
        ),
        _Rule(
            r"\bapt-get\s+install\s+",
            "supply-chain-risk",
            "warning",
            "Workflow installs OS packages dynamically.",
            "Pin apt package versions and package source.",
            0.86,
        ),
        _Rule(
            r"\bwget\s+.*\|",
            "supply-chain-risk",
            "warning",
            "Workflow downloads content through shell pipe.",
            "Use checksummed artifacts only.",
            0.9,
        ),
    ],
}


def _classify_manifest(file_path: str) -> str:
    """Map file path to dependency scanner key."""

    fp = (file_path or "").replace("\\", "/").lower()
    if fp.startswith(".github/workflows/"):
        return "workflow"
    name = PurePosixPath(fp).name
    if name == "package.json":
        return "package.json"
    if name == "requirements.txt":
        return "requirements.txt"
    if name == "pyproject.toml":
        return "pyproject.toml"
    if name == "go.mod":
        return "go.mod"
    if name == "pom.xml":
        return "pom.xml"
    if name == "gemfile":
        return "Gemfile"
    if name == "cargo.toml":
        return "Cargo.toml"
    return ""


def _is_low_signal_manifest_path(file_path: str) -> bool:
    parts = [part for part in (file_path or "").replace("\\", "/").lower().split("/") if part]
    return any(part in {"example", "examples", "fixture", "fixtures", "test", "tests", "vendor"} for part in parts[:-1])


def _manifest_section(postimage: list[tuple[int, str]], line_no: int) -> str:
    structural_rows: list[tuple[int, str]] = []
    group: list[tuple[int, str]] = []

    def flush() -> None:
        if not group:
            return
        structural = mask_non_code("\n".join(content for _line, content in group), "python").split("\n")
        structural_rows.extend((row[0], structural[index]) for index, row in enumerate(group))
        group.clear()

    for row in postimage:
        if group and row[0] != group[-1][0] + 1:
            flush()
        group.append(row)
    flush()

    section = ""
    previous_line = 0
    for candidate_line, content in structural_rows:
        if candidate_line > line_no:
            break
        if previous_line and candidate_line != previous_line + 1:
            section = ""
        previous_line = candidate_line
        match = re.match(r"^\s*\[([^]]+)]\s*(?:#.*)?$", content)
        if match:
            section = match.group(1).strip().lower()
    return section


def _package_dependency_entry(postimage: list[tuple[int, str]], line_no: int) -> bool:
    active_depth: int | None = None
    depth = 0
    previous_line = 0
    for candidate_line, content in postimage:
        if candidate_line > line_no:
            break
        if previous_line and candidate_line != previous_line + 1:
            active_depth = None
            depth = 0
        previous_line = candidate_line
        header = re.search(
            r'["\'](?:dependencies|devDependencies|peerDependencies|optionalDependencies)["\']\s*:\s*\{',
            content,
        )
        if header:
            header_depth = _json_depth_after(content[: header.start()], depth)
            if header_depth == 1:
                active_depth = header_depth + 1
            if candidate_line == line_no and active_depth is not None:
                effective = _package_effective_range(postimage, content)
                return True if effective is None else effective
        if candidate_line == line_no:
            structural_match = active_depth is not None and depth >= active_depth
            if not structural_match:
                return False
            effective = _package_effective_range(postimage, content)
            return structural_match if effective is None else effective
        depth = _json_depth_after(content, depth)
        if active_depth is not None and depth < active_depth:
            active_depth = None
    return False


def _package_effective_range(
    postimage: list[tuple[int, str]],
    content: str,
) -> bool | None:
    """Use strict JSON last-key semantics when the complete manifest is visible."""

    if (
        not postimage
        or postimage[0][0] != 1
        or any(right != left + 1 for (left, _), (right, _) in zip(postimage, postimage[1:], strict=False))
    ):
        return None
    candidate_names = {
        match.group("name")
        for match in re.finditer(
            r'["\'](?P<name>[^"\']+)["\']\s*:\s*["\']'
            r'(?:\*|latest|next|dev|[\^~<>]=?)[^"\']*["\']',
            content,
            re.IGNORECASE,
        )
    }
    if not candidate_names:
        return None
    try:
        manifest = json.loads("\n".join(text for _line, text in postimage))
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(manifest, dict):
        return None
    values: list[object] = []
    for key in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
        section = manifest.get(key)
        if not isinstance(section, dict):
            continue
        values.extend(section[name] for name in candidate_names if name in section)
    if not values:
        return False
    return any(
        isinstance(value, str) and bool(re.match(r"^(?:\*|latest|next|dev|[\^~<>]=?)", value.strip(), re.IGNORECASE))
        for value in values
    )


def _json_depth_after(content: str, depth: int) -> int:
    quote = ""
    escaped = False
    for char in content:
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            continue
        if char in {'"', "'"}:
            quote = char
        elif char in "{[":
            depth += 1
        elif char in "}]":
            depth = max(0, depth - 1)
    return depth


def _masked_manifest_rows(rows: list[tuple[int, str]], kind: str) -> list[tuple[int, str]]:
    """Remove manifest comments while preserving RIGHT-side coordinates."""

    language = {
        "package.json": "javascript",
        "pyproject.toml": "python",
        "Cargo.toml": "python",
        "Gemfile": "ruby",
        "workflow": "python",
    }.get(kind, "")
    masked: list[tuple[int, str]] = []
    group: list[tuple[int, str]] = []

    def flush() -> None:
        if not group:
            return
        source = "\n".join(content for _line, content in group)
        if kind == "pom.xml":
            characters = list(source)
            for match in re.finditer(r"<!--.*?-->|<!\[CDATA\[.*?]]>", source, re.DOTALL):
                for index in range(match.start(), match.end()):
                    if characters[index] not in {"\n", "\r"}:
                        characters[index] = " "
            rendered = "".join(characters)
        elif language:
            rendered = mask_comments(source, language)
        else:
            rendered = source
        lines = rendered.split("\n")
        masked.extend((row[0], lines[index]) for index, row in enumerate(group))
        group.clear()

    for row in rows:
        if group and row[0] != group[-1][0] + 1:
            flush()
        group.append(row)
    flush()
    return masked


def _gemfile_dependency_entry(postimage: list[tuple[int, str]], line_no: int) -> bool:
    """Reject declarations nested under a runtime conditional."""

    code_by_line: dict[int, str] = {}
    code_group: list[tuple[int, str]] = []

    def flush_code() -> None:
        if not code_group:
            return
        code = mask_non_code("\n".join(content for _line, content in code_group), "ruby").split("\n")
        code_by_line.update((row[0], code[index]) for index, row in enumerate(code_group))
        code_group.clear()

    for row in postimage:
        if code_group and row[0] != code_group[-1][0] + 1:
            flush_code()
        code_group.append(row)
    flush_code()

    conditional_depths: list[bool] = []
    previous = 0
    for candidate_line, _content in postimage:
        if candidate_line > line_no:
            break
        if previous and candidate_line != previous + 1:
            conditional_depths.clear()
        previous = candidate_line
        stripped = code_by_line.get(candidate_line, "").strip()
        if not stripped:
            continue
        if re.match(r"^end\b", stripped):
            if conditional_depths:
                conditional_depths.pop()
            continue
        opens_block = bool(
            re.match(r"^(?:if|unless|case|begin|while|until|for|class|module|def)\b", stripped)
            or re.search(r"\bdo\s*(?:\|[^|]*\|)?\s*$", stripped)
        )
        conditional = bool(re.match(r"^(?:if|unless|case|while|until|for)\b", stripped))
        non_dsl_scope = bool(
            re.match(r"^(?:class|module|def)\b", stripped) or re.search(r"=\s*(?:proc|lambda)\b.*\bdo\s*$", stripped)
        )
        if candidate_line == line_no:
            return (
                bool(re.match(r"^\s*gem\b", code_by_line.get(candidate_line, "")))
                and not any(conditional_depths)
                and not conditional
                and not bool(re.search(r"\b(?:if|unless)\b", stripped))
            )
        if opens_block:
            conditional_depths.append(conditional or non_dsl_scope or any(conditional_depths))
    return False


def _go_mod_dependency_entry(postimage: list[tuple[int, str]], line_no: int) -> bool:
    """Require a module version inside a real ``require`` directive."""

    in_require = False
    previous = 0
    for candidate_line, content in postimage:
        if candidate_line > line_no:
            break
        if previous and candidate_line != previous + 1:
            in_require = False
        previous = candidate_line
        code = content.split("//", 1)[0].strip()
        if re.match(r"^require\s*\($", code):
            in_require = True
            continue
        if in_require and code == ")":
            in_require = False
            continue
        if candidate_line == line_no:
            return bool(re.match(r"^require\s+\S+\s+v\S+$", code) or (in_require and re.match(r"^\S+\s+v\S+$", code)))
    return False


def _semantic_dependency_range_line(
    kind: str,
    line_no: int,
    content: str,
    postimage: list[tuple[int, str]],
) -> bool:
    """Require a real dependency declaration, not merely version-shaped text."""

    stripped = content.strip()
    if not stripped or stripped.startswith(("#", "//")):
        return False
    if kind == "requirements.txt":
        requirement = content.split("#", 1)[0].strip()
        return bool(
            re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9_.-]*(?:\[[^]]+])?"
                r"(?:\s*(?:(?:>=|<=|>|<|~=|!=)\s*[^;\s]+|==\s*\*)|\s*(?:;.*)?)",
                requirement,
            )
        )
    if kind == "package.json":
        return _package_dependency_entry(postimage, line_no)
    if kind == "go.mod":
        return _go_mod_dependency_entry(postimage, line_no)
    if kind == "pyproject.toml":
        section = _manifest_section(postimage, line_no)
        if re.match(r"^\s*python\s*=", content, re.IGNORECASE):
            return False
        return bool(
            section == "tool.poetry.dependencies" or re.fullmatch(r"tool\.poetry\.group\.[^.]+\.dependencies", section)
        )
    if kind == "Cargo.toml":
        section = _manifest_section(postimage, line_no)
        return bool(
            re.fullmatch(
                r"(?:(?:dependencies|dev-dependencies|build-dependencies)(?:\.[^.]+)?|"
                r"workspace\.dependencies(?:\.[^.]+)?|"
                r"target\..+\.(?:dependencies|dev-dependencies|build-dependencies)(?:\.[^.]+)?)",
                section,
            )
        )
    if kind == "pom.xml":
        if re.search(r"<dependency\b.*<version>", content, re.IGNORECASE):
            return True
        visible_rows = [(candidate, text) for candidate, text in postimage if candidate <= line_no]
        segment_start = 0
        for index, ((left, _), (right, _)) in enumerate(zip(visible_rows, visible_rows[1:])):
            if right != left + 1:
                segment_start = index + 1
        visible = "\n".join(text for _candidate, text in visible_rows[segment_start:])
        visible = re.sub(r"<dependency\b[^>]*/\s*>", "", visible, flags=re.IGNORECASE)
        return len(re.findall(r"<dependency\b", visible, re.IGNORECASE)) > len(
            re.findall(r"</dependency\s*>", visible, re.IGNORECASE)
        )
    if kind == "Gemfile":
        return _gemfile_dependency_entry(postimage, line_no)
    return kind == "workflow"


def _dependency_range_evidence(kind: str, content: str) -> str:
    """Return a compact package/spec coordinate for a proven range rule."""

    patterns: dict[str, str] = {
        "package.json": r'^[\s]*["\'](?P<name>[^"\']+)["\']\s*:\s*["\'](?P<spec>[^"\']+)["\']',
        "requirements.txt": (
            r"^\s*(?P<name>[A-Za-z0-9][A-Za-z0-9_.-]*(?:\[[^]]+])?)\s*"
            r"(?P<spec>(?:(?:>=|<=|>|<|~=|!=|==)\s*[^;#\s]+|\*))"
        ),
        "Gemfile": r"^\s*gem\s+['\"](?P<name>[^'\"]+)['\"]\s*,\s*['\"](?P<spec>[^'\"]+)['\"]",
        "Cargo.toml": (
            r"^\s*(?P<name>[A-Za-z0-9_.-]+)\s*=\s*(?:['\"](?P<simple>[^'\"]+)['\"]|"
            r"\{[^}]*\bversion\s*=\s*['\"](?P<inline>[^'\"]+)['\"])"
        ),
    }
    pattern = patterns.get(kind)
    if pattern is None:
        return ""
    match = re.match(pattern, content, re.IGNORECASE)
    if match is None:
        return ""
    groups = match.groupdict()
    spec = groups.get("spec") or groups.get("simple") or groups.get("inline") or ""
    name = groups.get("name") or ""
    return f" Declaration: {name} {spec.strip()}." if name and spec else ""


def _collect_dependency_findings(
    file_path: str,
    added_lines: list[tuple[int, str]],
    kind: str,
    postimage: list[tuple[int, str]] | None = None,
) -> list[DetectorFinding]:
    findings: list[DetectorFinding] = []
    visible_lines = _masked_manifest_rows(postimage or added_lines, kind)
    added_coordinates = {line for line, _content in added_lines}
    scan_lines = [(line, content) for line, content in visible_lines if line in added_coordinates]
    rules = _RULES_BY_NAME.get(kind, [])
    for rule in rules:
        for line_no, line in _find_matches(scan_lines, rule.pattern, rule.flags):
            if rule.category == "dependency-version-range" and not _semantic_dependency_range_line(
                kind, line_no, line, visible_lines
            ):
                continue
            findings.append(
                DetectorFinding(
                    file=file_path,
                    line=line_no,
                    severity=rule.severity,
                    category=normalize_category_for_detector(rule.category),
                    message=(
                        rule.message + _dependency_range_evidence(kind, line)
                        if rule.category == "dependency-version-range"
                        else rule.message
                    ),
                    suggestion=rule.suggestion,
                    confidence=rule.confidence,
                )
            )
    return findings


def _is_complete_new_file_patch(diff: str) -> bool:
    headers = list(re.finditer(r"^@@ .* @@", diff, re.MULTILINE))
    new_file = re.search(r"^@@ -0,0 \+1(?:,(?P<count>\d+))? @@", diff, re.MULTILINE)
    if new_file is None or len(headers) != 1 or headers[0].start() != new_file.start():
        return False
    expected = int(new_file.group("count") or 1)
    additions = iter_added_lines(diff)
    return len(additions) == expected and [line for line, _content in additions] == list(range(1, expected + 1))


def is_deterministic_dependency_version_range(file_path: str, line: int, diff: str) -> bool:
    """Return whether a local dependency rule proves a mutable ref at ``line``.

    This uses only syntax-specific manifest constraints and workflow action
    refs; advisory lookups are deliberately excluded.  The result is narrow
    enough to use as zero-token evidence without broadening an LLM's generic
    dependency guess.
    """

    kind = _classify_manifest(file_path)
    if not kind or _is_low_signal_manifest_path(file_path) or not _is_complete_new_file_patch(diff):
        return False
    findings = (
        _workflow_context_findings(file_path, diff)
        if kind == "workflow"
        else _collect_dependency_findings(
            file_path,
            iter_added_lines(diff),
            kind,
            iter_right_lines(diff),
        )
    )
    return any(finding.line == line and finding.category == "dependency-version-range" for finding in findings)


def _make_finding(
    file_path: str,
    line: int,
    *,
    category: str,
    severity: str,
    message: str,
    suggestion: str,
    confidence: float,
) -> DetectorFinding:
    return DetectorFinding(
        file=file_path,
        line=line,
        severity=severity,
        category=normalize_category_for_detector(category),
        message=message,
        suggestion=suggestion,
        confidence=confidence,
    )


_WORKFLOW_HUNK_HEADER = re.compile(
    r"^@@ -\d+(?:,(?P<old_count>\d+))? \+(?P<new_start>\d+)(?:,(?P<new_count>\d+))? @@(?:.*)$"
)


def _workflow_postimage_hunks(diff: str) -> list[list[tuple[int, str, bool]]]:
    """Return YAML post-image hunk lines with their RIGHT-side coordinates."""

    hunks: list[list[tuple[int, str, bool]]] = []
    current: list[tuple[int, str, bool]] = []
    new_line = 0
    old_remaining = 0
    new_remaining = 0
    in_hunk = False

    for raw_line in (diff or "").splitlines():
        header = _WORKFLOW_HUNK_HEADER.match(raw_line)
        if header:
            if current:
                hunks.append(current)
            current = []
            new_line = int(header.group("new_start"))
            old_remaining = int(header.group("old_count") or 1)
            new_remaining = int(header.group("new_count") or 1)
            in_hunk = True
            continue
        if raw_line.startswith("@@") or raw_line.startswith("diff --git "):
            if current:
                hunks.append(current)
            current = []
            in_hunk = False
            continue
        if not in_hunk or raw_line.startswith("\\ No newline at end of file"):
            continue
        if not raw_line:
            in_hunk = False
            continue

        prefix = raw_line[0]
        if prefix == "+" and new_remaining > 0:
            current.append((new_line, raw_line[1:], True))
            new_line += 1
            new_remaining -= 1
        elif prefix == " " and old_remaining > 0 and new_remaining > 0:
            current.append((new_line, raw_line[1:], False))
            new_line += 1
            old_remaining -= 1
            new_remaining -= 1
        elif prefix == "-" and old_remaining > 0:
            old_remaining -= 1
        else:
            in_hunk = False

        if old_remaining == 0 and new_remaining == 0:
            in_hunk = False

    if current:
        hunks.append(current)
    return hunks


def _is_pull_request_target_line(content: str) -> bool:
    return bool(
        re.search(
            r"^\s*(?:on\s*:\s*(?:\[[^]]*\b)?pull_request_target\b|pull_request_target\s*:)",
            content,
            re.IGNORECASE,
        )
    )


def _workflow_action_refs(diff: str) -> list[tuple[int, str, bool]]:
    """Return real step/reusable-workflow ``uses`` entries, excluding scalars."""

    refs: list[tuple[int, str, bool]] = []
    for raw_hunk in _workflow_postimage_hunks(diff):
        masked = mask_comments(
            "\n".join(content for _line, content, _added in raw_hunk),
            "python",
        ).split("\n")
        hunk = [(line_no, masked[index], added) for index, (line_no, _content, added) in enumerate(raw_hunk)]
        stack: list[tuple[int, str]] = []
        scalar_indent: int | None = None
        quoted_indent: int | None = None
        quoted_delimiter = ""

        def closes_quote(value: str, delimiter: str) -> bool:
            escaped = False
            for char in value[1:]:
                if escaped:
                    escaped = False
                elif char == "\\" and delimiter == '"':
                    escaped = True
                elif char == delimiter:
                    return True
            return False

        for line_no, content, added in hunk:
            stripped = content.strip()
            if not stripped:
                continue
            indent = len(content) - len(content.lstrip())
            if scalar_indent is not None:
                if indent > scalar_indent:
                    continue
                scalar_indent = None
            if quoted_indent is not None:
                if indent > quoted_indent:
                    if quoted_delimiter in stripped:
                        quoted_indent = None
                        quoted_delimiter = ""
                    continue
                quoted_indent = None
                quoted_delimiter = ""

            while stack and indent <= stack[-1][0]:
                stack.pop()
            path = [key for _level, key in stack]

            uses = re.match(r"^(?:-\s*)?uses\s*:\s*(?P<spec>[^\s#]+)", stripped, re.IGNORECASE)
            in_steps = len(path) == 3 and path[0].lower() == "jobs" and path[-1].lower() == "steps"
            reusable_job = len(path) == 2 and path[0].lower() == "jobs" and not stripped.startswith("-")
            if uses and (in_steps or reusable_job):
                refs.append((line_no, uses.group("spec").strip("\"'"), added))

            key_match = re.match(
                r"^(?P<list>-\s+)?(?P<key>[^:#][^:]*)\s*:\s*(?P<value>.*)$",
                stripped,
            )
            if key_match is None:
                continue
            key = key_match.group("key").strip("\"'")
            value = key_match.group("value").strip()
            if re.match(r"^[|>]", value):
                scalar_indent = indent
            elif value.startswith(("'", '"')) and not closes_quote(value, value[0]):
                quoted_indent = indent
                quoted_delimiter = value[0]
            elif not value and not key_match.group("list"):
                stack.append((indent, key))
    return refs


def _workflow_context_findings(file_path: str, diff: str) -> list[DetectorFinding]:
    """Detect actionable workflow risks that require more than a path match."""

    findings: list[DetectorFinding] = []
    added_lines = iter_added_lines(diff)

    for line_no, spec, added in _workflow_action_refs(diff):
        if not added or "@" not in spec:
            continue
        ref = spec.rsplit("@", 1)[1]
        if re.fullmatch(r"[0-9a-fA-F]{40}", ref) or re.fullmatch(r"docker://[^\s@]+@sha256:[0-9a-fA-F]{64}", spec):
            continue
        findings.append(
            _make_finding(
                file_path,
                line_no,
                category="dependency-version-range",
                severity="warning",
                message="Workflow action is not pinned to a full commit SHA.",
                suggestion="Pin third-party actions to an immutable 40-character commit SHA.",
                confidence=0.94,
            )
        )

    postimage_hunks = _workflow_postimage_hunks(diff)
    triggers = [row for hunk in postimage_hunks for row in hunk if _is_pull_request_target_line(row[1])]
    if triggers:
        trigger_added = next((line_no for line_no, _content, added in triggers if added), 0)
        for hunk in postimage_hunks:
            for checkout_index, (checkout_line, checkout_content, checkout_added) in enumerate(hunk):
                if not re.search(r"\buses\s*:\s*actions/checkout@", checkout_content, re.IGNORECASE):
                    continue

                checkout_indent = len(checkout_content) - len(checkout_content.lstrip())
                step_indent = checkout_indent
                if not checkout_content.lstrip().startswith("-"):
                    for _line_no, prior_content, _added in reversed(hunk[:checkout_index]):
                        stripped = prior_content.lstrip()
                        prior_indent = len(prior_content) - len(stripped)
                        if stripped.startswith("-") and prior_indent < checkout_indent:
                            step_indent = prior_indent
                            break

                ref_row: tuple[int, str, bool] | None = None
                for candidate in hunk[checkout_index + 1 :]:
                    _line_no, candidate_content, _added = candidate
                    stripped = candidate_content.lstrip()
                    candidate_indent = len(candidate_content) - len(stripped)
                    if stripped and candidate_indent <= step_indent:
                        break
                    if re.search(
                        r"^\s*ref\s*:\s*.*(?:github\.event\.pull_request\.head\.(?:sha|ref)|github\.head_ref)",
                        candidate_content,
                        re.IGNORECASE,
                    ):
                        ref_row = candidate
                        break

                if ref_row is None:
                    continue
                ref_line, _ref_content, ref_added = ref_row
                if not (trigger_added or checkout_added or ref_added):
                    continue
                anchor = ref_line if ref_added else checkout_line if checkout_added else trigger_added
                findings.append(
                    _make_finding(
                        file_path,
                        anchor,
                        category="ci-security",
                        severity="error",
                        message="pull_request_target checks out code from the untrusted pull request head.",
                        suggestion="Do not execute PR-head code in a privileged pull_request_target workflow.",
                        confidence=0.94,
                    )
                )

    for line_no, content in added_lines:
        if re.search(r"\brun\s*:.*github\.event\.pull_request\.title", content, re.IGNORECASE):
            findings.append(
                _make_finding(
                    file_path,
                    line_no,
                    category="ci-security",
                    severity="error",
                    message="Pull-request title is interpolated directly into a workflow shell command.",
                    suggestion="Pass event text through an environment variable and quote it as data.",
                    confidence=0.94,
                )
            )

        exposes_secret = re.search(
            r"\b(?:echo|printf|printenv|write-output)\b[^\n]*(?:\$\{\{\s*secrets\.|\$[A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|KEY)\b)",
            content,
            re.IGNORECASE,
        )
        if exposes_secret and "::add-mask::" not in content:
            findings.append(
                _make_finding(
                    file_path,
                    line_no,
                    category="data-leak",
                    severity="error",
                    message="Workflow prints a secret value to command output.",
                    suggestion="Remove secret output and pass credentials only to the command that consumes them.",
                    confidence=0.93,
                )
            )

    return findings


def detect_dependency_findings(diffs: dict[str, str]) -> list[DetectorFinding]:
    """Scan dependency-related files and workflow files for deterministic findings."""

    findings: list[DetectorFinding] = []
    for file_path, diff in diffs.items():
        kind = _classify_manifest(file_path)
        if not kind or _is_low_signal_manifest_path(file_path):
            continue

        added_lines = iter_added_lines(diff)
        if not added_lines:
            # A deletion-only or unanchored patch has no trustworthy RIGHT-side
            # location for an inline finding.
            continue

        findings.extend(
            _collect_dependency_findings(
                file_path,
                added_lines,
                kind,
                iter_right_lines(diff),
            )
        )
        findings.extend(
            _make_finding(
                file_path,
                detection.line,
                category=detection.category,
                severity=detection.severity,
                message=detection.message,
                suggestion=detection.suggestion,
                confidence=detection.confidence,
            )
            for detection in detect_advisory_findings(file_path, kind, diff)
        )
        if kind == "workflow":
            findings.extend(_workflow_context_findings(file_path, diff))

    return dedupe_findings(findings)
