"""Offline dependency-advisory matching for deterministic review findings.

The small snapshot below was refreshed on 2026-07-14 from OSV/GitHub advisory
records and official lifecycle documentation.  Runtime review deliberately does
not call OSV: network availability must not change detector output or latency.
``AdvisoryProvider`` is the narrow extension point for a future asynchronously
refreshed/cached provider; callers currently use only ``SnapshotAdvisoryProvider``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Protocol

from reviewforge.engine.detectors.unified_diff import iter_added_lines

SNAPSHOT_DATE = "2026-07-14"
OSV_SOURCE = "https://google.github.io/osv.dev/data/"
SERDE_YAML_SOURCE = "https://docs.rs/serde_yaml/latest/serde_yaml/"
RAILS_POLICY_SOURCE = "https://guides.rubyonrails.org/v7.2.2.1/maintenance_policy.html"


@dataclass(frozen=True)
class DependencyCoordinate:
    """A dependency declaration anchored to an added RIGHT-side diff line."""

    file: str
    line: int
    ecosystem: str
    name: str
    version: str
    exact: bool


@dataclass(frozen=True)
class VersionInterval:
    """An affected release interval (introduced inclusive, fixed exclusive)."""

    introduced: str | None = None
    fixed: str | None = None
    last_affected: str | None = None


@dataclass(frozen=True)
class AdvisoryRecord:
    """One immutable advisory entry from the dated local snapshot."""

    ecosystem: str
    package: str
    advisory_id: str
    intervals: tuple[VersionInterval, ...]
    source: str = OSV_SOURCE


@dataclass(frozen=True)
class LifecycleRecord:
    """Official package-lifecycle notice represented as an affected range."""

    ecosystem: str
    package: str
    advisory_id: str
    intervals: tuple[VersionInterval, ...]
    source: str


@dataclass(frozen=True)
class AdvisoryDetection:
    """Detector-neutral output converted by ``dependency.py`` at its boundary."""

    line: int
    category: str
    severity: str
    message: str
    suggestion: str
    confidence: float


class AdvisoryProvider(Protocol):
    """Interface for deterministic advisory providers.

    A future OSV-backed implementation should refresh a validated local snapshot
    outside the request path and expose the same synchronous matching contract.
    """

    def detect(self, coordinates: list[DependencyCoordinate]) -> list[AdvisoryDetection]: ...


_ADVISORIES: tuple[AdvisoryRecord, ...] = (
    AdvisoryRecord(
        "Go",
        "github.com/dgrijalva/jwt-go",
        "GHSA-w73w-5m7g-f7qc",
        (VersionInterval(last_affected="3.2.0"),),
    ),
    AdvisoryRecord("Go", "gopkg.in/yaml.v2", "GHSA-r88r-gmrh-7j83", (VersionInterval(fixed="2.2.3"),)),
    AdvisoryRecord("Go", "gopkg.in/yaml.v2", "GHSA-6q6q-88xp-6f2r", (VersionInterval(fixed="2.2.4"),)),
    AdvisoryRecord("Go", "gopkg.in/yaml.v2", "GHSA-wxc4-f4m6-wwqv", (VersionInterval(fixed="2.2.8"),)),
    AdvisoryRecord("Go", "github.com/gin-gonic/gin", "GHSA-3vp4-m3rf-835h", (VersionInterval(fixed="1.9.0"),)),
    AdvisoryRecord("npm", "serialize-javascript", "GHSA-h9rv-jmmf-4pgx", (VersionInterval(fixed="2.1.1"),)),
    AdvisoryRecord("npm", "serialize-javascript", "GHSA-hxcc-f52p-wc94", (VersionInterval(fixed="3.1.0"),)),
    AdvisoryRecord(
        "Maven",
        "org.apache.logging.log4j:log4j-core",
        "GHSA-jfh8-c2jp-5v3q",
        (
            VersionInterval(introduced="2.0-beta9", fixed="2.3.1"),
            VersionInterval(introduced="2.4", fixed="2.12.2"),
            VersionInterval(introduced="2.13", fixed="2.15.0"),
        ),
    ),
    AdvisoryRecord("PyPI", "django", "PYSEC-2010-12", (VersionInterval(introduced="1.2", fixed="1.2.2"),)),
    AdvisoryRecord("PyPI", "jinja2", "PYSEC-2019-217", (VersionInterval(fixed="2.10.1"),)),
)

_LIFECYCLE: tuple[LifecycleRecord, ...] = (
    LifecycleRecord(
        "crates.io",
        "serde_yaml",
        "serde-yaml-unmaintained",
        (VersionInterval(),),
        SERDE_YAML_SOURCE,
    ),
    LifecycleRecord(
        "RubyGems",
        "rails",
        "rails-4.2-eol",
        (VersionInterval(introduced="4.2.0", fixed="4.3.0"),),
        RAILS_POLICY_SOURCE,
    ),
)

_HUNK_HEADER = re.compile(r"^@@ -\d+(?:,(?P<old_count>\d+))? \+(?P<new_start>\d+)(?:,(?P<new_count>\d+))? @@(?:.*)$")
_PLAIN_VERSION = re.compile(r"^v?\d+(?:\.\d+)*(?:[-+][0-9A-Za-z.-]+)?$")


@dataclass(frozen=True)
class _PostImageLine:
    line: int
    content: str
    added: bool


def _canonical_name(ecosystem: str, name: str) -> str:
    value = name.strip()
    if ecosystem == "PyPI":
        return re.sub(r"[-_.]+", "-", value).lower()
    return value.lower()


def _release_tuple(version: str) -> tuple[int, ...] | None:
    """Return a conservative numeric release key, or ``None`` if ambiguous."""

    value = version.strip().lower()
    value = re.sub(r"^(?:==|===|=|v)", "", value)
    value = value.removesuffix("+incompatible")
    match = re.fullmatch(r"(?P<release>\d+(?:\.\d+)*)(?:[-+][0-9a-z.-]+)?", value)
    if not match:
        return None
    return tuple(int(part) for part in match.group("release").split("."))


def _compare_versions(left: tuple[int, ...], right: tuple[int, ...]) -> int:
    width = max(len(left), len(right))
    padded_left = left + (0,) * (width - len(left))
    padded_right = right + (0,) * (width - len(right))
    return (padded_left > padded_right) - (padded_left < padded_right)


def _in_interval(version: str, interval: VersionInterval) -> bool:
    candidate = _release_tuple(version)
    if candidate is None:
        return False
    if interval.introduced:
        introduced = _release_tuple(interval.introduced)
        if introduced is None or _compare_versions(candidate, introduced) < 0:
            return False
    if interval.fixed:
        fixed = _release_tuple(interval.fixed)
        if fixed is None or _compare_versions(candidate, fixed) >= 0:
            return False
    if interval.last_affected:
        last_affected = _release_tuple(interval.last_affected)
        if last_affected is None or _compare_versions(candidate, last_affected) > 0:
            return False
    return True


def _matches_record(coordinate: DependencyCoordinate, record: AdvisoryRecord | LifecycleRecord) -> bool:
    return (
        coordinate.ecosystem == record.ecosystem
        and _canonical_name(coordinate.ecosystem, coordinate.name) == _canonical_name(record.ecosystem, record.package)
        and any(_in_interval(coordinate.version, interval) for interval in record.intervals)
    )


class SnapshotAdvisoryProvider:
    """Match dependency coordinates against the bundled, dated snapshot."""

    def detect(self, coordinates: list[DependencyCoordinate]) -> list[AdvisoryDetection]:
        detections: list[AdvisoryDetection] = []
        for coordinate in coordinates:
            advisories = [record for record in _ADVISORIES if coordinate.exact and _matches_record(coordinate, record)]
            if advisories:
                ids = sorted({record.advisory_id for record in advisories})
                detections.append(
                    AdvisoryDetection(
                        line=coordinate.line,
                        category="dependency-vulnerability",
                        severity="error",
                        message=(
                            f"{coordinate.name} {coordinate.version} matches {', '.join(ids)} "
                            f"in the offline advisory snapshot dated {SNAPSHOT_DATE}."
                        ),
                        suggestion=(
                            "Migrate to a maintained replacement and a supported release."
                            if coordinate.name == "github.com/dgrijalva/jwt-go"
                            else "Upgrade to a release outside every affected range and regenerate dependency locks."
                        ),
                        confidence=0.99,
                    )
                )

            lifecycle = [record for record in _LIFECYCLE if _matches_record(coordinate, record)]
            if lifecycle:
                ids = sorted({record.advisory_id for record in lifecycle})
                detections.append(
                    AdvisoryDetection(
                        line=coordinate.line,
                        category="dependency-deprecated",
                        severity="warning",
                        message=(
                            f"{coordinate.name} {coordinate.version} is covered by the official lifecycle notice "
                            f"{', '.join(ids)} (snapshot {SNAPSHOT_DATE})."
                        ),
                        suggestion="Move to a maintained package or a currently supported release line.",
                        confidence=0.97,
                    )
                )
        return detections


def _postimage_hunks(diff: str) -> list[list[_PostImageLine]]:
    """Parse post-image hunk content while preserving trustworthy RIGHT lines."""

    hunks: list[list[_PostImageLine]] = []
    current: list[_PostImageLine] = []
    new_line = 0
    old_remaining = 0
    new_remaining = 0
    in_hunk = False

    for raw_line in (diff or "").splitlines():
        header = _HUNK_HEADER.match(raw_line)
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
            current.append(_PostImageLine(new_line, raw_line[1:], True))
            new_line += 1
            new_remaining -= 1
        elif prefix == " " and old_remaining > 0 and new_remaining > 0:
            current.append(_PostImageLine(new_line, raw_line[1:], False))
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


def _requirements_coordinates(file_path: str, diff: str) -> list[DependencyCoordinate]:
    coordinates: list[DependencyCoordinate] = []
    pattern = re.compile(
        r"^\s*(?P<name>[A-Za-z0-9][A-Za-z0-9_.-]*)(?:\[[^]]+\])?\s*"
        r"(?P<operator>===|==|~=|>=|<=|!=|>|<)\s*(?P<version>[^\s;#]+)"
    )
    for line, content in iter_added_lines(diff):
        match = pattern.match(content)
        if not match:
            continue
        version = match.group("version")
        exact = match.group("operator") in {"==", "==="} and "*" not in version and bool(_PLAIN_VERSION.match(version))
        coordinates.append(DependencyCoordinate(file_path, line, "PyPI", match.group("name"), version, exact))
    return coordinates


def _go_coordinates(file_path: str, diff: str) -> list[DependencyCoordinate]:
    coordinates: list[DependencyCoordinate] = []
    pattern = re.compile(
        r"^\s*(?:require\s+)?(?P<name>[A-Za-z0-9][A-Za-z0-9._~/-]*)\s+"
        r"(?P<version>v?\d+(?:\.\d+)*(?:[-+][0-9A-Za-z.-]+)?)\b"
    )
    for line, content in iter_added_lines(diff):
        match = pattern.match(content)
        if not match or match.group("name") in {"go", "toolchain"}:
            continue
        coordinates.append(
            DependencyCoordinate(file_path, line, "Go", match.group("name"), match.group("version"), True)
        )
    return coordinates


def _json_coordinates(file_path: str, diff: str) -> list[DependencyCoordinate]:
    dependency_sections = {"dependencies", "devDependencies", "optionalDependencies", "peerDependencies"}
    hunks = _postimage_hunks(diff)
    coordinates: list[DependencyCoordinate] = []
    seen: set[tuple[int, str]] = set()

    for hunk in hunks:
        text = "\n".join(row.content for row in hunk)
        try:
            document = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            document = None
        if isinstance(document, dict):
            for section in dependency_sections:
                values = document.get(section)
                if not isinstance(values, dict):
                    continue
                for name, spec in values.items():
                    if not isinstance(name, str) or not isinstance(spec, str):
                        continue
                    key_pattern = re.compile(rf'["\']{re.escape(name)}["\']\s*:')
                    anchor = next((row for row in hunk if row.added and key_pattern.search(row.content)), None)
                    if anchor is None:
                        continue
                    exact = bool(_PLAIN_VERSION.match(spec)) and not re.search(r"[\^~<>=*|\s]", spec)
                    coordinates.append(DependencyCoordinate(file_path, anchor.line, "npm", name, spec, exact))
                    seen.add((anchor.line, name))

        active_indent: int | None = None
        for row in hunk:
            section_match = re.match(
                r'^\s*["\'](?P<section>dependencies|devDependencies|optionalDependencies|peerDependencies)["\']\s*:\s*\{',
                row.content,
            )
            if section_match:
                active_indent = len(row.content) - len(row.content.lstrip())
                continue
            if active_indent is None:
                continue
            stripped = row.content.lstrip()
            indent = len(row.content) - len(stripped)
            if stripped.startswith("}") and indent <= active_indent:
                active_indent = None
                continue
            property_match = re.match(r'^\s*["\'](?P<name>[^"\']+)["\']\s*:\s*["\'](?P<spec>[^"\']+)["\']', row.content)
            if not row.added or not property_match:
                continue
            name = property_match.group("name")
            if (row.line, name) in seen:
                continue
            spec = property_match.group("spec")
            exact = bool(_PLAIN_VERSION.match(spec)) and not re.search(r"[\^~<>=*|\s]", spec)
            coordinates.append(DependencyCoordinate(file_path, row.line, "npm", name, spec, exact))
            seen.add((row.line, name))
    return coordinates


def _maven_coordinates(file_path: str, diff: str) -> list[DependencyCoordinate]:
    coordinates: list[DependencyCoordinate] = []
    tag_patterns = {
        "group": re.compile(r"<groupId>\s*([^<]+?)\s*</groupId>"),
        "artifact": re.compile(r"<artifactId>\s*([^<]+?)\s*</artifactId>"),
        "version": re.compile(r"<version>\s*([^<]+?)\s*</version>"),
    }
    for hunk in _postimage_hunks(diff):
        in_dependency = False
        group = artifact = version = ""
        version_line = 0
        version_added = False
        for row in hunk:
            if "<dependency" in row.content:
                in_dependency = True
                group = artifact = version = ""
                version_line = 0
                version_added = False
            if not in_dependency:
                continue
            for key, pattern in tag_patterns.items():
                match = pattern.search(row.content)
                if not match:
                    continue
                if key == "group":
                    group = match.group(1)
                elif key == "artifact":
                    artifact = match.group(1)
                else:
                    version = match.group(1)
                    version_line = row.line
                    version_added = row.added
            if "</dependency>" not in row.content:
                continue
            if group and artifact and version and version_added and _PLAIN_VERSION.match(version):
                coordinates.append(
                    DependencyCoordinate(file_path, version_line, "Maven", f"{group}:{artifact}", version, True)
                )
            in_dependency = False
    return coordinates


def _gemfile_coordinates(file_path: str, diff: str) -> list[DependencyCoordinate]:
    coordinates: list[DependencyCoordinate] = []
    pattern = re.compile(r"""^\s*gem\s+["'](?P<name>[^"']+)["']\s*,\s*["'](?P<spec>[^"']+)["']""")
    for line, content in iter_added_lines(diff):
        match = pattern.match(content)
        if not match:
            continue
        spec = match.group("spec").strip()
        exact = bool(_PLAIN_VERSION.match(spec)) and not re.search(r"[~<>=*|\s]", spec)
        coordinates.append(DependencyCoordinate(file_path, line, "RubyGems", match.group("name"), spec, exact))
    return coordinates


def _cargo_coordinates(file_path: str, diff: str) -> list[DependencyCoordinate]:
    coordinates: list[DependencyCoordinate] = []
    for hunk in _postimage_hunks(diff):
        in_dependencies = False
        for row in hunk:
            section = re.match(r"^\s*\[(?P<section>[^]]+)]", row.content)
            if section:
                name = section.group("section").lower()
                dependency_sections = {
                    "dependencies",
                    "dev-dependencies",
                    "build-dependencies",
                    "workspace.dependencies",
                }
                in_dependencies = name in dependency_sections or name.endswith(".dependencies")
                continue
            if not in_dependencies or not row.added:
                continue
            declaration = re.match(r"^\s*(?P<name>[A-Za-z0-9_-]+)\s*=\s*(?P<value>.+?)\s*$", row.content)
            if not declaration:
                continue
            name = declaration.group("name")
            value = declaration.group("value")
            simple = re.fullmatch(r'["\'](?P<spec>[^"\']+)["\']', value)
            inline_version = re.search(r'\bversion\s*=\s*["\'](?P<spec>[^"\']+)["\']', value)
            package_alias = re.search(r'\bpackage\s*=\s*["\'](?P<package>[^"\']+)["\']', value)
            match = simple or inline_version
            if not match:
                continue
            spec = match.group("spec").strip()
            package = package_alias.group("package") if package_alias else name
            exact = spec.startswith("=") and bool(_PLAIN_VERSION.match(spec[1:]))
            coordinates.append(
                DependencyCoordinate(file_path, row.line, "crates.io", package, spec.removeprefix("="), exact)
            )
    return coordinates


def extract_dependency_coordinates(file_path: str, kind: str, diff: str) -> list[DependencyCoordinate]:
    """Extract supported manifest declarations without interpreting advisories."""

    extractors = {
        "requirements.txt": _requirements_coordinates,
        "go.mod": _go_coordinates,
        "package.json": _json_coordinates,
        "pom.xml": _maven_coordinates,
        "Gemfile": _gemfile_coordinates,
        "Cargo.toml": _cargo_coordinates,
    }
    extractor = extractors.get(kind)
    if extractor is None:
        return []
    coordinates = extractor(file_path, diff)
    deduped: dict[tuple[int, str, str], DependencyCoordinate] = {}
    for coordinate in coordinates:
        key = (coordinate.line, coordinate.ecosystem, _canonical_name(coordinate.ecosystem, coordinate.name))
        deduped[key] = coordinate
    return list(deduped.values())


def detect_advisory_findings(
    file_path: str,
    kind: str,
    diff: str,
    provider: AdvisoryProvider | None = None,
) -> list[AdvisoryDetection]:
    """Detect vulnerable or retired dependencies from an anchored manifest diff."""

    coordinates = extract_dependency_coordinates(file_path, kind, diff)
    return (provider or SnapshotAdvisoryProvider()).detect(coordinates)
