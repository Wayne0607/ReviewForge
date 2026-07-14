"""Deterministic dependency/dependency-management risk detectors."""

from __future__ import annotations

import re

from reviewforge.engine.detectors.advisories import detect_advisory_findings
from reviewforge.engine.detectors.base import DetectorFinding, dedupe_findings, normalize_category_for_detector
from reviewforge.engine.detectors.unified_diff import iter_added_lines


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
            r"\s+v[0-9]+\.[0-9]+\.[0-9]+[-]?(?:(?:SNAPSHOT|dev|beta|rc)\b|(?:[A-Za-z]+\.[0-9]+))",
            "dependency-version-range",
            "warning",
            "Pre-release/preferred risky version modifier in go.mod.",
            "Prefer stable dependency versions.",
            0.82,
        ),
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
            r"<version>([^<]*)(LATEST|RELEASE|SNAPSHOT)</version>",
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
    if fp.endswith("package.json"):
        return "package.json"
    if fp.endswith("requirements.txt"):
        return "requirements.txt"
    if fp.endswith("pyproject.toml"):
        return "pyproject.toml"
    if fp.endswith("go.mod"):
        return "go.mod"
    if fp.endswith("pom.xml"):
        return "pom.xml"
    if fp.endswith("gemfile"):
        return "Gemfile"
    if fp.endswith("cargo.toml"):
        return "Cargo.toml"
    return ""


def _collect_dependency_findings(
    file_path: str, added_lines: list[tuple[int, str]], kind: str
) -> list[DetectorFinding]:
    findings: list[DetectorFinding] = []
    rules = _RULES_BY_NAME.get(kind, [])
    for rule in rules:
        for line_no, _line in _find_matches(added_lines, rule.pattern, rule.flags):
            findings.append(
                DetectorFinding(
                    file=file_path,
                    line=line_no,
                    severity=rule.severity,
                    category=normalize_category_for_detector(rule.category),
                    message=rule.message,
                    suggestion=rule.suggestion,
                    confidence=rule.confidence,
                )
            )
    return findings


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


def _workflow_context_findings(file_path: str, diff: str) -> list[DetectorFinding]:
    """Detect actionable workflow risks that require more than a path match."""

    findings: list[DetectorFinding] = []
    added_lines = iter_added_lines(diff)

    for line_no, content in added_lines:
        match = re.search(r"^\s*-\s*uses:\s*(?P<spec>[^\s#]+)", content, re.IGNORECASE)
        if not match or "@" not in match.group("spec"):
            continue
        ref = match.group("spec").rsplit("@", 1)[1]
        if re.fullmatch(r"[0-9a-fA-F]{40}", ref):
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
        if not kind:
            continue

        added_lines = iter_added_lines(diff)
        if not added_lines:
            # A deletion-only or unanchored patch has no trustworthy RIGHT-side
            # location for an inline finding.
            continue

        findings.extend(_collect_dependency_findings(file_path, added_lines, kind))
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
