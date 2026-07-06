"""Deterministic dependency/dependency-management risk detectors."""

from __future__ import annotations

import re

from reviewforge.engine.detectors.base import DetectorFinding, dedupe_findings, normalize_category_for_detector


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


def _find_matches(text: str, pattern: str, flags: int = re.IGNORECASE) -> list[tuple[int, str]]:
    """Return line matches `(line_no, line)` for a regex pattern."""

    if not text:
        return []
    out: list[tuple[int, str]] = []
    for idx, line in enumerate(text.splitlines(), start=1):
        if re.search(pattern, line, flags):
            out.append((idx, line))
    return out


_PATH_RULES: list[_Rule] = [
    _Rule(
        r"\.github/workflows/.+\.ya?ml$",
        "dependency",
        "warning",
        "Workflow file changed in PR.",
        "Verify action source, pinned SHAs, and command execution sinks.",
        0.7,
        re.IGNORECASE,
    ),
    _Rule(
        r"\b(?:package\.json|requirements\.txt|pyproject\.toml|pom\.xml|Gemfile|Cargo\.toml|go\.mod)\b",
        "dependency",
        "warning",
        "Dependency manifest changed.",
        "Validate dependency updates and pinning strategy.",
        0.6,
    ),
]

_RULES_BY_NAME: dict[str, list[_Rule]] = {
    "package.json": [
        _Rule(
            r'["\'][^"\']+["\']\s*:\s*["\'](?:\*|latest|next|dev|[\^~<>]=?[^"\']+)\b',
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
    ],
    "pyproject.toml": [
        _Rule(
            r'^\s*(?:\w+)\s*=\s*["\']\s*([*~^<>!=]{1,2}|(?:\d+[!^][^"\']+))',
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
            r"\brequire\s*\(",
            "dependency-version-range",
            "info",
            "go.mod require block changed.",
            "Review transitive and replace edits carefully.",
            0.6,
        ),
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
        _Rule(
            r"\bsource\s+['\"][^'\"]+['\"]",
            "supply-chain-risk",
            "info",
            "Gem source changed.",
            "Prefer trusted package sources.",
            0.72,
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
            r"git\s*=\s*['\"][^'\"]+['\"]",
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
    ],
    "workflow": [
        _Rule(
            r"\buses:\s+[^@]+@(main|master|latest|dev|feature/.+|[0-9a-fA-F]{7,40})",
            "ci-security",
            "warning",
            "Workflow uses mutable action references.",
            "Pin action references to immutable SHAs.",
            0.95,
        ),
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
        _Rule(
            r"^\s*-\s*uses:\s*actions/setup-(?:node|python|ruby|java)",
            "dependency-version-range",
            "info",
            "Runner language setup action changed.",
            "Review pinned setup action versions.",
            0.71,
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


def _collect_dependency_findings(file_path: str, content: str, kind: str) -> list[DetectorFinding]:
    findings: list[DetectorFinding] = []
    rules = _RULES_BY_NAME.get(kind, [])
    for rule in rules:
        for line_no, _line in _find_matches(content, rule.pattern, rule.flags):
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


def detect_dependency_findings(diffs: dict[str, str]) -> list[DetectorFinding]:
    """Scan dependency-related files and workflow files for deterministic findings."""

    findings: list[DetectorFinding] = []
    for file_path, diff in diffs.items():
        kind = _classify_manifest(file_path)
        if not kind:
            continue

        # File-level signal.
        for rule in _PATH_RULES:
            if re.search(rule.pattern, file_path, rule.flags):
                findings.append(
                    DetectorFinding(
                        file=file_path,
                        line=1,
                        severity=rule.severity,
                        category=normalize_category_for_detector(rule.category),
                        message=rule.message,
                        suggestion=rule.suggestion,
                        confidence=rule.confidence,
                    )
                )

        if kind == "workflow":
            added_lines = "\n".join(
                line[1:] for line in diff.splitlines() if line.startswith("+") and not line.startswith("+++")
            )
            findings.extend(_collect_dependency_findings(file_path, added_lines, kind))
            continue

        # Focus on added content inside the diff, which mirrors reviewer visibility.
        snippet = "\n".join(
            line[1:] for line in diff.splitlines() if line.startswith("+") and not line.startswith("+++")
        )
        findings.extend(_collect_dependency_findings(file_path, snippet, kind))

    return dedupe_findings(findings)
