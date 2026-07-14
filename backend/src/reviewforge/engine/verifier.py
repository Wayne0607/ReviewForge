"""Verifier — pure-logic auditor: merges duplicate findings and drops low-confidence noise.

This is the documented Verifier stage (去误报 / 合并重复). It runs BEFORE the LLM
Calibrator: deterministically merging duplicates across reviewers and dropping
sub-floor findings means the (token-costly) calibrator only judges distinct,
plausible candidates. No LLM call — pure reasoning over the candidate set.
"""

from __future__ import annotations

import logging
import re
from pathlib import PurePosixPath

from reviewforge.core.state import Finding
from reviewforge.engine.security_categories import normalize_category

logger = logging.getLogger(__name__)

_NEARBY_LINE_TOLERANCE = 3
_DETECTOR_PROVENANCE = {"detector", "detector-auto"}
_DEPENDENCY_MANIFESTS = {
    "build.gradle",
    "build.gradle.kts",
    "bun.lockb",
    "cargo.toml",
    "composer.json",
    "composer.lock",
    "deno.json",
    "deno.lock",
    "directory.packages.props",
    "flake.lock",
    "gemfile",
    "gemfile.lock",
    "go.mod",
    "go.sum",
    "gradle.lockfile",
    "mix.exs",
    "mix.lock",
    "npm-shrinkwrap.json",
    "package-lock.json",
    "package.json",
    "packages.lock.json",
    "pipfile",
    "pipfile.lock",
    "pnpm-lock.yaml",
    "poetry.lock",
    "pom.xml",
    "pubspec.lock",
    "pubspec.yaml",
    "pyproject.toml",
    "requirements.txt",
    "setup.cfg",
    "setup.py",
    "uv.lock",
    "yarn.lock",
}
_FUZZY_CATEGORY_PAIRS = {
    frozenset({"insecure-download", "supply-chain-risk"}),
    frozenset({"hardcoded-secrets", "data-leak"}),
    frozenset({"command-injection", "ci-security"}),
}

# These are sink identities, not general vulnerability words.  A shared word such
# as "injection" is deliberately insufficient: adjacent functions can contain two
# independent vulnerabilities of the same category.
_SINK_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("backtick", re.compile(r"\bbackticks?\b|\u53cd\u5f15\u53f7", re.IGNORECASE)),
    ("dangerously-set-inner-html", re.compile(r"dangerously\s*set\s*inner\s*html|dangerouslysetinnerhtml", re.I)),
    ("dom-html-render", re.compile(r"unsafe\s+dom\s+render|v-html", re.IGNORECASE)),
    ("eval", re.compile(r"(?<![a-z0-9_.])eval(?![a-z0-9_])", re.IGNORECASE)),
    ("f-string", re.compile(r"\bf[- ]?string\b", re.IGNORECASE)),
    ("fmt.sprintf", re.compile(r"fmt\s*\.\s*sprintf", re.IGNORECASE)),
    ("os.popen", re.compile(r"os\s*\.\s*popen", re.IGNORECASE)),
    ("pickle.loads", re.compile(r"pickle\s*\.\s*loads?", re.IGNORECASE)),
    (
        "pr-title",
        re.compile(r"pull[- ]?request\s+title|\bpr\s+title\b|拉取请求标题|合并请求标题", re.IGNORECASE),
    ),
    ("runtime.exec", re.compile(r"runtime(?:\s*\.\s*getruntime\s*\(\s*\))?\s*\.\s*exec", re.IGNORECASE)),
    ("subprocess", re.compile(r"subprocess\s*\.\s*(?:run|popen|call|check_output)", re.IGNORECASE)),
    ("template.html", re.compile(r"template\s*\.\s*html", re.IGNORECASE)),
    ("yaml.load", re.compile(r"yaml\s*\.\s*load", re.IGNORECASE)),
    (
        "workflow-secret-output",
        re.compile(
            r"(?:print|echo|log|output).{0,30}(?:secret|token|password|key)|"
            r"(?:secret|token|password|key).{0,30}(?:print|echo|log|output)|输出.{0,20}(?:密钥|令牌)",
            re.IGNORECASE,
        ),
    ),
)

_DOTTED_IDENTIFIER = re.compile(
    r"(?<![a-z0-9_$])[a-z_$][a-z0-9_$]*(?:\s*\.\s*[a-z_$][a-z0-9_$]*)+(?![a-z0-9_$])",
    re.IGNORECASE,
)


class Verifier:
    """Deterministic de-duplication + confidence-floor filtering of candidate findings."""

    def __init__(self, confidence_floor: float = 0.0) -> None:
        self._floor = confidence_floor

    def verify(self, findings: list[Finding]) -> tuple[list[Finding], list[str]]:
        """Return (survivors, dropped_ids).

        - Findings below the confidence floor are dropped.
        - Categories are normalized before identity comparison.
        - Findings sharing (file, line, canonical category) are merged.
        - A deterministic detector finding can absorb an LLM report up to three
          lines away only when both messages identify the same concrete sink.
          Dependency manifests are line-oriented and therefore never fuzzy-merged.
        - Detector evidence wins over LLM confidence; otherwise the
          highest-confidence finding wins. Reviewer attribution is unioned.
        """
        survivors: dict[tuple, Finding] = {}
        dropped: list[str] = []

        for f in findings:
            if f.confidence < self._floor:
                dropped.append(f.id)
                continue
            f.category = normalize_category(f.category)
            key = (f.file, f.line, f.category)
            existing = survivors.get(key)
            if existing is None:
                survivors[key] = f
                continue
            winner, loser = self._merge(existing, f)
            survivors[key] = winner
            dropped.append(loser.id)

        out = list(survivors.values())

        # Exact identities above are unambiguous.  For shifted LLM line numbers,
        # match each non-detector report to the closest compatible detector.  We do
        # not fuzzy-merge two detectors: nearby scanner hits are independent sinks.
        detector_findings = [finding for finding in out if self._is_detector(finding)]
        fuzzy_dropped: set[str] = set()
        for finding in out:
            if self._is_detector(finding):
                continue
            matches = [detector for detector in detector_findings if self._is_nearby_duplicate(detector, finding)]
            if not matches:
                continue
            detector = min(matches, key=lambda item: (abs(item.line - finding.line), -item.confidence, item.id))
            detector.reviewer = self._union_reviewers(detector.reviewer, finding.reviewer)
            fuzzy_dropped.add(finding.id)
            dropped.append(finding.id)

        if fuzzy_dropped:
            out = [finding for finding in out if finding.id not in fuzzy_dropped]
        if dropped:
            logger.info(f"Verifier: {len(out)} kept, {len(dropped)} merged/dropped as duplicate/low-confidence")
        return out, dropped

    @classmethod
    def _merge(cls, first: Finding, second: Finding) -> tuple[Finding, Finding]:
        """Merge an exact duplicate, preferring deterministic evidence."""

        first_detector = cls._is_detector(first)
        second_detector = cls._is_detector(second)
        if first_detector != second_detector:
            winner, loser = (first, second) if first_detector else (second, first)
        elif second.confidence > first.confidence:
            winner, loser = second, first
        else:
            winner, loser = first, second
        winner.reviewer = cls._union_reviewers(winner.reviewer, loser.reviewer)
        return winner, loser

    @staticmethod
    def _is_detector(finding: Finding) -> bool:
        return finding.verified_by.strip().lower() in _DETECTOR_PROVENANCE

    @classmethod
    def _is_nearby_duplicate(cls, detector: Finding, finding: Finding) -> bool:
        if detector.file != finding.file:
            return False
        same_category = detector.category == finding.category
        compatible_categories = frozenset({detector.category, finding.category}) in _FUZZY_CATEGORY_PAIRS
        if not same_category and not compatible_categories:
            return False
        distance = abs(detector.line - finding.line)
        if distance > _NEARBY_LINE_TOLERANCE or (distance == 0 and same_category):
            return False
        if distance > 0 and cls._is_dependency_manifest(detector.file):
            return False
        detector_sinks = cls._sink_fingerprint(f"{detector.message}\n{detector.suggestion}")
        finding_sinks = cls._sink_fingerprint(f"{finding.message}\n{finding.suggestion}")
        return bool(detector_sinks & finding_sinks)

    @staticmethod
    def _is_dependency_manifest(file_path: str) -> bool:
        name = PurePosixPath(file_path.replace("\\", "/")).name.lower()
        return name in _DEPENDENCY_MANIFESTS or name.startswith("requirements") and name.endswith(".txt")

    @staticmethod
    def _sink_fingerprint(message: str) -> set[str]:
        text = str(message or "")
        sinks = {name for name, pattern in _SINK_PATTERNS if pattern.search(text)}
        for match in _DOTTED_IDENTIFIER.findall(text):
            sinks.add(re.sub(r"\s+", "", match).lower())
        lowered = text.lower()
        remote_marker = r"curl|wget|remote|external\s+url|download|piped|\u4e0b\u8f7d|\u5916\u90e8"
        if "shell" in lowered and re.search(remote_marker, lowered):
            sinks.add("remote-shell")
        execution_marker = r"shell|bash|execute|executed|execution|pipe[sd]?|\u6267\u884c|\u8fd0\u884c"
        if re.search(remote_marker, lowered) and re.search(execution_marker, lowered):
            sinks.add("remote-exec")
        return sinks

    @staticmethod
    def _union_reviewers(a: str, b: str) -> str:
        names = sorted({name.strip() for value in (a, b) for name in value.split(",") if name.strip()})
        return ",".join(names)
