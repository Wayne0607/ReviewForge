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
from reviewforge.engine.security_categories import is_security_category, normalize_category

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
_ROOT_CAUSE_LINE_TOLERANCE = 20
_VALIDATION_ROOT_CATEGORIES = frozenset(
    {
        "incorrect-argument",
        "incorrect-null-check",
        "input-validation",
        "missing-input-validation",
        "missing-validation",
        "null-check",
        "null-check-wrong-argument",
        "null-safety",
        "parameter-validation",
        "wrong-argument",
        "wrong-variable",
    }
)
_MANIFEST_GUESS_CATEGORIES = {
    "dependency-deprecated",
    "dependency-version-range",
    "dependency-vulnerability",
    "insecure-download",
    "security",
    "supply-chain-risk",
}
_MANIFEST_CATEGORY_ALIASES = {
    # Reviewer vocabularies are intentionally broader than the stable public
    # taxonomy.  Keep these aliases manifest-scoped so a generic
    # ``vulnerability`` label in application code is not reclassified as a
    # dependency advisory.
    "unmaintained": "dependency-deprecated",
    "unmaintained-dependency": "dependency-deprecated",
    "unsafe-script": "supply-chain-risk",
    "vulnerability": "dependency-vulnerability",
}
_ADVISORY_ID = re.compile(
    r"\b(?:CVE-(?:\d{4}-)?\d+|GHSA-[a-z0-9-]+|"
    r"(?:GO|PYSEC|RUSTSEC|MAL)-\d{4}-\d+|OSV-[A-Z0-9-]+|SNYK-[A-Z0-9-]+)\b",
    re.IGNORECASE,
)
_ADVISORY_CANONICAL_IDS = {
    # Curated equivalences from the advisory records used by the deterministic
    # dependency detector. Keep this explicit: unrelated advisories affecting
    # the same package/version must remain independent findings.
    "CVE-2019-16769": "GHSA-H9RV-JMMF-4PGX",
    "CVE-2020-7660": "GHSA-HXCC-F52P-WC94",
    "CVE-2021-44228": "GHSA-JFH8-C2JP-5V3Q",
}
_PACKAGE_COORDINATE_PATTERNS = (
    re.compile(
        r"^\s*(?P<name>@?[a-z0-9][a-z0-9_.-]*(?:[/:][a-z0-9_.-]+)*)\s+"
        r"(?:v?\d|has\s+recorded|matches\b|is\s+covered\b)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?<![\w./@-])(?P<name>@?[a-z0-9][a-z0-9_.-]*(?:/[a-z0-9_.-]+)?)"
        r"\s*(?:==|~=|>=|<=|\^|~|@[\^~]?)\s*v?\d+(?:[.a-z0-9+-]*)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?<![\w./-])(?P<name>[a-z0-9][a-z0-9_.-]*(?::[a-z0-9_.-]+)+)"
        r"\s*:\s*v?\d+(?:[.a-z0-9+-]*)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?<![\w./-])(?P<name>(?:github\.com|gitlab\.com|gopkg\.in)/[a-z0-9_.\-/]+)"
        r"\s+v?\d+(?:[.a-z0-9+-]*)",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bartifact\s+[`'\"]?(?P<name>[a-z0-9_.-]+:[a-z0-9_.-]+)"
        r"\s*:\s*v?\d+(?:[.a-z0-9+-]*)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?<![\w./@-])(?P<name>@?[a-z][a-z0-9_.-]*(?:/[a-z0-9_.-]+)?)"
        r"\s+v?\d+(?:[.a-z0-9+-]*)",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:package|dependency|module|crate|gem|artifact)\s+[`'\"]?"
        r"(?P<name>@?[a-z0-9][a-z0-9_.\-/]*(?::[a-z0-9_.-]+)?)",
        re.IGNORECASE,
    ),
)
_PACKAGE_NAME_STOPWORDS = {
    "advisory",
    "confidence",
    "dependency",
    "line",
    "package",
    "severity",
    "version",
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
        re.compile(r"pull[- ]?request\s+title|\bpr\s+title\b|PR\s*标题|拉取请求标题|合并请求标题", re.IGNORECASE),
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
_CALL_IDENTIFIER = re.compile(
    r"(?<![a-z0-9_$])([a-z_$][a-z0-9_$]*(?:\s*[.:]\s*[a-z_$][a-z0-9_$]*)*)\s*\(",
    re.IGNORECASE,
)
_QUOTED_IDENTIFIER = re.compile(
    r"[`']([a-z_$][a-z0-9_$]*(?:[.:][a-z_$][a-z0-9_$]*)*)[`']",
    re.IGNORECASE,
)
_METRIC_RECORDER_CALL = re.compile(r"\b(record(?:Legacy|Storage)Duration)\b", re.IGNORECASE)
_SINK_FAMILY_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "family:sql-query",
        re.compile(
            r"(?:\bsql\b|\bquery\b).{0,80}(?:execut|built|construct|rawquery|queryrow|sprintf|"
            r"f[- ]?string|concat|interpolat|format)|"
            r"(?:execut|built|construct|rawquery|queryrow|sprintf|f[- ]?string|concat|interpolat|"
            r"format).{0,80}(?:\bsql\b|\bquery\b)",
            re.IGNORECASE,
        ),
    ),
    (
        "family:filesystem-path",
        re.compile(
            r"(?:os\.path\.join|filepath\.join|path\.join|path\.of|\bopen\s*\(|"
            r"readfile|fileinputstream|send_file|read_to_string|\bpath\s+(?:join|construction)|"
            r"filesystem\s+(?:sink|api|read)|\bfile\s+path\b)",
            re.IGNORECASE,
        ),
    ),
    (
        "family:deserialization",
        re.compile(
            r"(?:pickle\.loads?|yaml\.load|readobject|objectinputstream|jsonpickle\.decode|"
            r"marshal\.loads?|bincode(?:::|\.)deserialize|gob(?:::|\.)decode|deserializ)",
            re.IGNORECASE,
        ),
    ),
    (
        "family:command-exec",
        re.compile(
            r"(?:os\.system|os\.popen|subprocess\.(?:run|popen|call|check_output)|"
            r"runtime(?:\.getruntime\(\))?\.exec|exec\.command|child_process\.(?:exec|spawn)|"
            r"processbuilder|shell\s+command|dynamic\s+command|command\s+(?:api|execution|spawn)|"
            r"execute\w*\s+(?:an?\s+)?external\s+command)",
            re.IGNORECASE,
        ),
    ),
)

_VULNERABILITY_MECHANISM_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("prototype-pollution", re.compile(r"prototype\s+pollution|原型污染", re.IGNORECASE)),
    (
        "remote-code-execution",
        re.compile(r"remote\s+code\s+execution|arbitrary\s+code\s+execution|\bRCE\b|远程代码执行|任意代码执行", re.I),
    ),
    ("deserialization", re.compile(r"deserializ|反序列化", re.IGNORECASE)),
    ("cross-site-scripting", re.compile(r"cross[- ]site\s+scripting|\bXSS\b|跨站脚本", re.IGNORECASE)),
    ("denial-of-service", re.compile(r"denial\s+of\s+service|\bDoS\b|拒绝服务", re.IGNORECASE)),
    ("authentication-bypass", re.compile(r"auth(?:entication)?\s+bypass|认证绕过|身份验证绕过", re.IGNORECASE)),
    ("path-traversal", re.compile(r"path\s+traversal|directory\s+traversal|路径穿越|目录遍历", re.IGNORECASE)),
    ("command-injection", re.compile(r"command\s+injection|命令注入", re.IGNORECASE)),
    ("sql-injection", re.compile(r"sql\s+injection|SQL\s*注入", re.IGNORECASE)),
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
        survivors: list[Finding] = []
        dropped: list[str] = []
        exact_matched_detectors: set[str] = set()

        for f in findings:
            if f.confidence < self._floor:
                dropped.append(f.id)
                continue
            f.category = normalize_category(f.category)
            if self._is_dependency_manifest(f.file):
                semantic = self._manifest_semantic_category(f)
                if semantic is not None:
                    f.category = semantic
            duplicate_index = next(
                (
                    index
                    for index, existing in enumerate(survivors)
                    if (existing.file, existing.line, existing.category) == (f.file, f.line, f.category)
                    and self._can_merge_exact(existing, f)
                ),
                None,
            )
            if duplicate_index is None:
                survivors.append(f)
                continue
            existing = survivors[duplicate_index]
            existing_is_detector = self._is_detector(existing)
            finding_is_detector = self._is_detector(f)
            carries_exact_match = existing.id in exact_matched_detectors or f.id in exact_matched_detectors
            winner, loser = self._merge(existing, f)
            survivors[duplicate_index] = winner
            dropped.append(loser.id)
            if existing_is_detector != finding_is_detector or carries_exact_match:
                exact_matched_detectors.discard(loser.id)
                exact_matched_detectors.add(winner.id)

        out = list(survivors)

        # Exact identities above are unambiguous. For shifted LLM line numbers,
        # globally rank compatible detector/LLM pairs and greedily consume each
        # side once. A detector therefore cannot erase several adjacent sinks.
        detector_findings = [finding for finding in out if self._is_detector(finding)]
        fuzzy_dropped: set[str] = set()
        pair_candidates: list[tuple[int, int, float, str, str, Finding, Finding]] = []
        for finding in out:
            if not self._is_detector(finding):
                for detector in detector_findings:
                    quality = self._nearby_duplicate_quality(detector, finding)
                    if quality is not None:
                        pair_candidates.append(
                            (
                                quality,
                                abs(detector.line - finding.line),
                                -detector.confidence,
                                detector.id,
                                finding.id,
                                detector,
                                finding,
                            )
                        )

        # A detector already paired with an exact LLM duplicate must not absorb a
        # second, merely nearby report. Carry that ownership into fuzzy matching.
        matched_detectors = set(exact_matched_detectors)
        matched_findings: set[str] = set()
        for _quality, _distance, _confidence, _detector_id, _finding_id, detector, finding in sorted(pair_candidates):
            reusable_manifest_duplicate = detector.id in matched_detectors and self._is_reusable_manifest_duplicate(
                detector, finding
            )
            if (detector.id in matched_detectors and not reusable_manifest_duplicate) or finding.id in matched_findings:
                continue
            detector.reviewer = self._union_reviewers(detector.reviewer, finding.reviewer)
            matched_detectors.add(detector.id)
            matched_findings.add(finding.id)
            fuzzy_dropped.add(finding.id)
            dropped.append(finding.id)

        # Manifest-only dependency/security claims are particularly prone to LLM
        # guessing. If no deterministic finding matched their package coordinate,
        # retain them only when the message itself contains auditable evidence.
        for finding in out:
            if (
                finding.id not in fuzzy_dropped
                and not self._is_detector(finding)
                and self._is_dependency_manifest(finding.file)
                and (finding.category in _MANIFEST_GUESS_CATEGORIES or is_security_category(finding.category))
                and not self._has_explicit_manifest_evidence(finding)
                and not self._has_nearby_manifest_detector_claim(finding, detector_findings)
            ):
                fuzzy_dropped.add(finding.id)
                dropped.append(finding.id)

        if fuzzy_dropped:
            out = [finding for finding in out if finding.id not in fuzzy_dropped]

        # Agentic closure can report one validation root cause from several
        # nearby lines with reviewer-invented category names. Collapse those
        # only when both messages cite the same concrete code symbol/API; a
        # generic shared word such as "validation" is deliberately insufficient.
        out, root_dropped = self._merge_llm_root_duplicates(out)
        dropped.extend(root_dropped)

        # Repeated copy/paste metric-recorder swaps are one root cause even when
        # they occur in several sibling methods. Keep opposite swap directions
        # independent (legacy->storage versus storage->legacy).
        repeated_roots: dict[tuple[str, str, str], int] = {}
        consolidated: list[Finding] = []
        for finding in out:
            root = self._repeated_root_fingerprint(finding)
            if root is None:
                consolidated.append(finding)
                continue
            key = (finding.file, *root)
            existing_index = repeated_roots.get(key)
            if existing_index is None:
                repeated_roots[key] = len(consolidated)
                consolidated.append(finding)
                continue
            winner, loser = self._merge(consolidated[existing_index], finding)
            consolidated[existing_index] = winner
            dropped.append(loser.id)
        out = consolidated
        if dropped:
            logger.info(f"Verifier: {len(out)} kept, {len(dropped)} merged/dropped as duplicate/low-confidence")
        return out, dropped

    @classmethod
    def _merge_llm_root_duplicates(cls, findings: list[Finding]) -> tuple[list[Finding], list[str]]:
        consolidated: list[Finding] = []
        dropped: list[str] = []
        for finding in findings:
            duplicate_index = next(
                (index for index, existing in enumerate(consolidated) if cls._same_validation_root(existing, finding)),
                None,
            )
            if duplicate_index is None:
                consolidated.append(finding)
                continue
            winner, loser = cls._merge(consolidated[duplicate_index], finding)
            consolidated[duplicate_index] = winner
            dropped.append(loser.id)
        return consolidated, dropped

    @classmethod
    def _same_validation_root(cls, first: Finding, second: Finding) -> bool:
        if cls._is_detector(first) or cls._is_detector(second):
            return False
        if first.file != second.file or abs(first.line - second.line) > _ROOT_CAUSE_LINE_TOLERANCE:
            return False
        if first.category not in _VALIDATION_ROOT_CATEGORIES or second.category not in _VALIDATION_ROOT_CATEGORIES:
            return False
        first_sinks = cls._sink_fingerprint(f"{first.message}\n{first.suggestion}")
        second_sinks = cls._sink_fingerprint(f"{second.message}\n{second.suggestion}")
        shared = first_sinks & second_sinks
        first_symbols = {sink for sink in first_sinks if sink.startswith("symbol:")}
        second_symbols = {sink for sink in second_sinks if sink.startswith("symbol:")}
        if first_symbols and second_symbols:
            return bool(first_symbols & second_symbols)
        return any(sink.startswith(("call:", "symbol:")) or "." in sink for sink in shared)

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
    def _can_merge_exact(cls, first: Finding, second: Finding) -> bool:
        """Protect distinct manifest advisories that share one source line."""

        if not cls._is_dependency_manifest(first.file):
            return True
        first_packages = cls._dependency_coordinates(first)
        second_packages = cls._dependency_coordinates(second)
        if first_packages and second_packages and not first_packages & second_packages:
            return False
        if cls._is_detector(first) != cls._is_detector(second):
            detector, finding = (first, second) if cls._is_detector(first) else (second, first)
            detector_advisories = cls._advisory_ids(detector)
            finding_advisories = cls._advisory_ids(finding)
            if detector_advisories and finding_advisories and finding_advisories < detector_advisories:
                # Defer composite/subset matching to the globally ranked fuzzy
                # pass so one detector consumes the most specific overlap once.
                return False
            return cls._manifest_evidence_compatible(
                detector,
                finding,
                same_source_line=True,
            )
        first_advisories = cls._advisory_ids(first)
        second_advisories = cls._advisory_ids(second)
        return not (first_advisories and second_advisories and first_advisories != second_advisories)

    @staticmethod
    def _repeated_root_fingerprint(finding: Finding) -> tuple[str, str] | None:
        text = f"{finding.message}\n{finding.suggestion}"
        calls = [match.group(1).lower() for match in _METRIC_RECORDER_CALL.finditer(text)]
        if len(set(calls)) < 2:
            return None
        return "metric-recorder-swap", calls[0]

    @classmethod
    def _is_nearby_duplicate(cls, detector: Finding, finding: Finding) -> bool:
        return cls._nearby_duplicate_quality(detector, finding) is not None

    @classmethod
    def _nearby_duplicate_quality(cls, detector: Finding, finding: Finding) -> int | None:
        if detector.file != finding.file:
            return None
        distance = abs(detector.line - finding.line)
        if distance > _NEARBY_LINE_TOLERANCE:
            return None

        if (
            cls._is_dependency_manifest(detector.file)
            and detector.category in _MANIFEST_GUESS_CATEGORIES
            and finding.category in _MANIFEST_GUESS_CATEGORIES
        ):
            detector_packages = cls._dependency_coordinates(detector)
            finding_packages = cls._dependency_coordinates(finding)
            if not detector_packages or not detector_packages & finding_packages:
                return None
            if not cls._manifest_evidence_compatible(
                detector,
                finding,
                same_source_line=distance == 0,
            ):
                return None
            detector_advisories = sorted(cls._advisory_ids(detector))
            finding_advisories = cls._advisory_ids(finding)
            if detector_advisories and finding_advisories:
                return min(detector_advisories.index(advisory) for advisory in finding_advisories)
            return 0

        same_category = detector.category == finding.category
        compatible_categories = frozenset({detector.category, finding.category}) in _FUZZY_CATEGORY_PAIRS
        if not same_category and not compatible_categories:
            return None
        if distance > _NEARBY_LINE_TOLERANCE or (distance == 0 and same_category):
            return None
        detector_sinks = cls._sink_fingerprint(detector.message)
        finding_sinks = cls._sink_fingerprint(finding.message)
        shared = detector_sinks & finding_sinks
        if not shared:
            return None
        # Exact API/symbol identity outranks a broader concrete sink family when
        # several nearby findings compete for detector ownership.
        if any(not sink.startswith("family:") for sink in shared):
            return 0
        detector_specific = cls._conflicting_sink_identities(detector_sinks)
        finding_specific = cls._conflicting_sink_identities(finding_sinks)
        if detector_specific and finding_specific:
            return None
        return 1

    @staticmethod
    def _conflicting_sink_identities(sinks: set[str]) -> set[str]:
        """Return concrete terminal sinks, excluding construction mechanisms."""

        mechanisms = {
            "f-string",
            "fmt.sprintf",
            "os.path.join",
            "filepath.join",
            "path.join",
            "path.of",
            "call:os.path.join",
            "call:filepath.join",
            "call:path.join",
            "call:path.of",
        }
        return {sink for sink in sinks if not sink.startswith("family:") and sink not in mechanisms}

    @staticmethod
    def _is_dependency_manifest(file_path: str) -> bool:
        name = PurePosixPath(file_path.replace("\\", "/")).name.lower()
        return name in _DEPENDENCY_MANIFESTS or name.startswith("requirements") and name.endswith(".txt")

    @classmethod
    def _dependency_coordinates(cls, finding: Finding) -> set[str]:
        """Extract normalized package/module identities from finding evidence."""

        text = finding.message
        coordinates: set[str] = set()
        for pattern in _PACKAGE_COORDINATE_PATTERNS:
            for match in pattern.finditer(text):
                name = match.group("name").strip("`'\".,:;()[]{} ").lower()
                if name and name not in _PACKAGE_NAME_STOPWORDS:
                    coordinates.add(name)
        return coordinates

    @classmethod
    def _dependency_version_coordinates(cls, finding: Finding) -> set[tuple[str, str]]:
        """Extract package + normalized version/range evidence from a message."""

        text = finding.message.lower()
        coordinates: set[tuple[str, str]] = set()
        version_pattern = (
            r"\s*(?P<spec>(?:==={0,1}|=|@[\^~]?|\^|~|>=|<=|>|<|:)?"
            r"\s*v?\d+(?:[.a-z0-9+*\-]*))"
        )
        for package in cls._dependency_coordinates(finding):
            pattern = re.compile(re.escape(package) + version_pattern, re.IGNORECASE)
            for match in pattern.finditer(text):
                spec = re.sub(r"\s+", "", match.group("spec")).lower()
                spec = spec.removeprefix("@").removeprefix(":")
                if spec.startswith("="):
                    spec = spec.lstrip("=")
                if spec.startswith("v") and len(spec) > 1 and spec[1].isdigit():
                    spec = spec[1:]
                coordinates.add((package, spec))
        return coordinates

    @classmethod
    def _manifest_evidence_compatible(
        cls,
        detector: Finding,
        finding: Finding,
        *,
        same_source_line: bool,
    ) -> bool:
        """Whether deterministic evidence fully covers the LLM manifest claim."""

        detector_semantic = cls._manifest_semantic_category(detector)
        finding_semantic = cls._manifest_semantic_category(finding)
        if detector_semantic is None or detector_semantic != finding_semantic:
            return False

        detector_advisories = cls._advisory_ids(detector)
        finding_advisories = cls._advisory_ids(finding)
        if finding_advisories:
            # Direction matters: a composite detector can cover a narrower LLM
            # report, but an LLM-only advisory must never be discarded.
            return bool(detector_advisories) and finding_advisories <= detector_advisories

        if detector_advisories or detector_semantic in {
            "dependency-deprecated",
            "dependency-version-range",
            "dependency-vulnerability",
        }:
            detector_versions = cls._dependency_version_coordinates(detector)
            finding_versions = cls._dependency_version_coordinates(finding)
            return same_source_line or bool(detector_versions & finding_versions)
        return True

    @classmethod
    def _has_nearby_manifest_detector_claim(
        cls,
        finding: Finding,
        detectors: list[Finding],
    ) -> bool:
        """Keep ambiguous same-package claims rather than guessing duplicate."""

        finding_packages = cls._dependency_coordinates(finding)
        finding_semantic = cls._manifest_semantic_category(finding)
        if not finding_packages or finding_semantic is None:
            return False
        for detector in detectors:
            if detector.file != finding.file or abs(detector.line - finding.line) > _NEARBY_LINE_TOLERANCE:
                continue
            if cls._manifest_semantic_category(detector) != finding_semantic:
                continue
            if finding_packages & cls._dependency_coordinates(detector):
                return True
        return False

    @staticmethod
    def _manifest_semantic_category(finding: Finding) -> str | None:
        """Resolve generic manifest security reports to a concrete claim type."""

        category = normalize_category(finding.category)
        category = _MANIFEST_CATEGORY_ALIASES.get(category, category)
        if category != "security":
            return category
        text = f"{finding.message}\n{finding.suggestion}".lower()
        if re.search(r"supply[- ]?chain|package[- ]?compromise|malicious\s+package", text):
            return "supply-chain-risk"
        if re.search(r"version\s+range|not\s+pinned|unpinned|wildcard|mutable\s+(?:version|constraint)", text):
            return "dependency-version-range"
        if re.search(r"deprecated|end[- ]?of[- ]?life|unmaintained", text):
            return "dependency-deprecated"
        if re.search(r"insecure\s+download|unverified\s+download", text):
            return "insecure-download"
        if re.search(r"vulnerab|\badvisory\b|\binsecure\b|unsafe\s+version", text) or _ADVISORY_ID.search(text):
            return "dependency-vulnerability"
        return None

    @classmethod
    def _is_reusable_manifest_duplicate(cls, detector: Finding, finding: Finding) -> bool:
        """Allow one advisory detector to absorb repeated generic restatements.

        A detector remains one-to-one for distinct CVE/GHSA claims.  Reuse is
        safe only for an advisory-free reviewer restatement that names the same
        package *and exact version* and has the same semantic category.  This
        handles several reviewers independently reporting one manifest entry
        without collapsing separate advisories for that entry.
        """

        if not cls._is_dependency_manifest(detector.file) or cls._advisory_ids(finding):
            return False
        if cls._manifest_semantic_category(detector) != cls._manifest_semantic_category(finding):
            return False
        detector_packages = cls._dependency_coordinates(detector)
        finding_packages = cls._dependency_coordinates(finding)
        if not detector_packages or not detector_packages & finding_packages:
            return False
        detector_versions = cls._dependency_version_coordinates(detector)
        finding_versions = cls._dependency_version_coordinates(finding)
        finding_mechanisms = cls._vulnerability_mechanisms(finding)
        if finding_mechanisms:
            detector_mechanisms = cls._vulnerability_mechanisms(detector)
            if not detector_mechanisms.intersection(finding_mechanisms):
                return False
        return bool(detector_versions & finding_versions) and cls._manifest_evidence_compatible(
            detector,
            finding,
            same_source_line=detector.line == finding.line,
        )

    @staticmethod
    def _vulnerability_mechanisms(finding: Finding) -> set[str]:
        """Return concrete vulnerability mechanisms named by a manifest claim."""

        text = f"{finding.message}\n{finding.suggestion}"
        return {name for name, pattern in _VULNERABILITY_MECHANISM_PATTERNS if pattern.search(text)}

    @staticmethod
    def _advisory_ids(finding: Finding) -> set[str]:
        text = f"{finding.message}\n{finding.suggestion}"
        advisory_ids = {match.group(0).upper() for match in _ADVISORY_ID.finditer(text)}
        return {_ADVISORY_CANONICAL_IDS.get(advisory_id, advisory_id) for advisory_id in advisory_ids}

    @classmethod
    def _has_explicit_manifest_evidence(cls, finding: Finding) -> bool:
        """Whether an unmatched manifest claim is independently auditable."""

        text = f"{finding.message}\n{finding.suggestion}"
        if _ADVISORY_ID.search(text):
            return True
        coordinates = cls._dependency_coordinates(finding)
        if finding.category == "dependency-version-range":
            return bool(coordinates) and bool(re.search(r"(?:\^|~|>=|<=|>|<|\*|\bx\b)\s*v?\d*", text, re.IGNORECASE))
        sinks = cls._sink_fingerprint(finding.message)
        if finding.category in {"supply-chain-risk", "insecure-download"}:
            return "remote-exec" in sinks or "remote-shell" in sinks
        # Non-dependency security findings in package manifests require a named
        # executable/file/serialization sink, not a generic risk assertion.
        if is_security_category(finding.category) and finding.category not in _MANIFEST_GUESS_CATEGORIES:
            named_sinks = {name for name, _pattern in _SINK_PATTERNS}
            return bool(sinks & named_sinks) or any(sink.startswith("call:") for sink in sinks)
        return False

    @staticmethod
    def _sink_fingerprint(message: str) -> set[str]:
        text = str(message or "")
        sinks = {name for name, pattern in _SINK_PATTERNS if pattern.search(text)}
        sinks.update(name for name, pattern in _SINK_FAMILY_PATTERNS if pattern.search(text))
        for match in _DOTTED_IDENTIFIER.findall(text):
            sinks.add(re.sub(r"\s+", "", match).lower())
        for match in _CALL_IDENTIFIER.findall(text):
            sinks.add("call:" + re.sub(r"\s+", "", match).lower())
        for match in _QUOTED_IDENTIFIER.findall(text):
            sinks.add("symbol:" + match.lower())
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
