"""Semantic Diff — compile a PR into semantic units for coverage-driven review.

Deterministic and side-effect-free compilation from ``StateStore`` plus the
existing impact manifest produced by ``ContextEngine``.  No LLM or repository
tool is ever called.  Every changed file is preserved; files that the
``ContextEngine`` truncated or that have no extractable symbol still produce a
useful resource/config unit so coverage is never silently dropped.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Any

from reviewforge.core.state import StateStore
from reviewforge.engine.symbol_extractor import detect_language

# ── Enums ─────────────────────────────────────────────────────


class UnitKind(StrEnum):
    """The broad category a semantic unit represents."""

    SYMBOL = "symbol"
    RESOURCE = "resource"


# ── Data classes ──────────────────────────────────────────────


@dataclass(frozen=True)
class Provenance:
    """Tracks where the unit's data originated."""

    source: str = "manifest"  # manifest | diff-only | resource-suffix
    manifest_version: int = 0
    note: str = ""


@dataclass
class SemanticUnit:
    """One reviewable unit inside a ``SemanticChangeSet``.

    A unit may represent a changed symbol (function, class) or a resource/config
    region that has no extractable symbol.  ``id`` is deterministic and stable
    for the same (repo, pr, path, symbol, kind) tuple.
    """

    id: str = ""
    path: str = ""
    language: str = ""
    kind: UnitKind = UnitKind.SYMBOL
    symbol: str = ""
    start_line: int = 0
    end_line: int = 0
    added_lines: list[int] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)
    imports: list[dict[str, Any]] = field(default_factory=list)
    references: list[dict[str, Any]] = field(default_factory=list)
    candidate_tests: list[str] = field(default_factory=list)
    risk_signals: list[dict[str, Any]] = field(default_factory=list)
    wiki_facts: list[dict[str, Any]] = field(default_factory=list)
    provenance: Provenance = field(default_factory=Provenance)
    risk_score: float = 0.0
    risk_reasons: list[str] = field(default_factory=list)

    # ── JSON serialization ────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        """Stable JSON-friendly representation."""
        data: dict[str, Any] = {
            "id": self.id,
            "path": self.path,
            "language": self.language,
            "kind": self.kind.value,
            "symbol": self.symbol,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "added_lines": list(self.added_lines),
            "calls": [dict(c) for c in self.calls],
            "imports": [dict(i) for i in self.imports],
            "references": [dict(r) for r in self.references],
            "candidate_tests": list(self.candidate_tests),
            "risk_signals": [dict(s) for s in self.risk_signals],
            "wiki_facts": [dict(f) for f in self.wiki_facts],
            "provenance": asdict(self.provenance),
            "risk_score": self.risk_score,
            "risk_reasons": list(self.risk_reasons),
        }
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SemanticUnit:
        """Reconstruct from ``to_dict`` output."""
        prov_data = data.get("provenance", {})
        provenance = Provenance(
            source=prov_data.get("source", "manifest"),
            manifest_version=prov_data.get("manifest_version", 0),
            note=prov_data.get("note", ""),
        )
        return cls(
            id=data.get("id", ""),
            path=data.get("path", ""),
            language=data.get("language", ""),
            kind=UnitKind(data.get("kind", "symbol")),
            symbol=data.get("symbol", ""),
            start_line=data.get("start_line", 0),
            end_line=data.get("end_line", 0),
            added_lines=list(data.get("added_lines", [])),
            calls=[dict(c) for c in data.get("calls", [])],
            imports=[dict(i) for i in data.get("imports", [])],
            references=[dict(r) for r in data.get("references", [])],
            candidate_tests=list(data.get("candidate_tests", [])),
            risk_signals=[dict(s) for s in data.get("risk_signals", [])],
            wiki_facts=[dict(f) for f in data.get("wiki_facts", [])],
            provenance=provenance,
            risk_score=float(data.get("risk_score", 0.0)),
            risk_reasons=list(data.get("risk_reasons", [])),
        )


@dataclass
class SemanticChangeSet:
    """A PR compiled into semantic units for coverage-driven review.

    ``units`` cover every changed file.  ``unresolved_files`` lists files that
    could not be fully resolved (e.g. binary, deleted, beyond manifest cap) but
    still received at least a resource unit.
    """

    repo: str = ""
    pr_number: int = 0
    head_sha: str = ""
    units: list[SemanticUnit] = field(default_factory=list)
    unresolved_files: list[str] = field(default_factory=list)

    # ── JSON serialization ────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo": self.repo,
            "pr_number": self.pr_number,
            "head_sha": self.head_sha,
            "units": [u.to_dict() for u in self.units],
            "unresolved_files": list(self.unresolved_files),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SemanticChangeSet:
        return cls(
            repo=data.get("repo", ""),
            pr_number=data.get("pr_number", 0),
            head_sha=data.get("head_sha", ""),
            units=[SemanticUnit.from_dict(u) for u in data.get("units", [])],
            unresolved_files=list(data.get("unresolved_files", [])),
        )


# ── Risk scoring ──────────────────────────────────────────────

_SENSITIVE_NAME = re.compile(
    r"(?:auth|admin|permission|token|secret|password|encrypt|decrypt|"
    r"query|execute|upload|download|redirect|deserialize)",
    re.IGNORECASE,
)

_RESOURCE_SUFFIXES = frozenset(
    {".properties", ".po", ".pot", ".arb", ".strings", ".resx", ".ftl"}
)


def _compute_risk(
    unit: SemanticUnit,
    *,
    has_test_evidence_gap: bool,
) -> tuple[float, list[str]]:
    """Return ``(score, reasons)`` for a unit.

    Score is in [0.0, 1.0].  Each reason is a short machine-readable tag.
    The formula is deterministic and every heuristic carries an explicit reason.
    """

    score = 0.0
    reasons: list[str] = []

    # Blast radius — callers/references outside the PR
    ref_count = len(unit.references)
    if ref_count > 0:
        blast = min(ref_count / 10.0, 0.3)
        score += blast
        reasons.append(f"blast-radius:{ref_count}")

    # Security-sensitive symbol name
    if unit.symbol and _SENSITIVE_NAME.search(unit.symbol):
        score += 0.25
        reasons.append("security-sensitive-symbol")

    # Test evidence gap
    if has_test_evidence_gap:
        score += 0.15
        reasons.append("test-evidence-not-discovered")

    # Risk signals from manifest (blast-radius, security-sensitive-symbol, etc.)
    for signal in unit.risk_signals:
        sig_type = str(signal.get("type", ""))
        if sig_type == "blast-radius" and "blast-radius" not in "".join(reasons):
            ref_ct = int(signal.get("reference_count", 0))
            if ref_ct > 0:
                score += min(ref_ct / 10.0, 0.2)
                reasons.append(f"signal-blast-radius:{ref_ct}")
        elif sig_type == "security-sensitive-symbol":
            if "security-sensitive-symbol" not in reasons:
                score += 0.2
                reasons.append("signal-security-sensitive")

    # Localization/resource changes carry moderate risk
    if unit.kind == UnitKind.RESOURCE:
        score += 0.1
        reasons.append("resource-change")

    # Multiple added lines increase risk
    added = len(unit.added_lines)
    if added > 20:
        score += 0.1
        reasons.append(f"large-change:{added}")
    elif added > 5:
        score += 0.05
        reasons.append(f"moderate-change:{added}")

    # Wiki facts may surface historical risk
    for fact in unit.wiki_facts:
        fact_kind = str(fact.get("kind", ""))
        if fact_kind in {"return-or-error", "side-effect", "security"}:
            score += 0.05
            reasons.append(f"wiki-fact:{fact_kind}")
            break  # one bonus max

    return min(score, 1.0), reasons


# ── Stable ID generation ──────────────────────────────────────


def _stable_id(
    repo: str,
    pr_number: int,
    path: str,
    symbol: str,
    kind: UnitKind,
) -> str:
    """Deterministic, collision-resistant unit ID.

    Uses a SHA-256 prefix so the same compilation inputs always produce the
    same ID, regardless of process or timestamp.
    """
    seed = f"{repo}|{pr_number}|{path}|{symbol}|{kind.value}"
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]
    return f"su_{digest}"


# ── Resource detection (mirrors ContextEngine._resource_context) ────


def _is_resource_path(path: str) -> bool:
    """Return ``True`` if the file extension is a known resource/localization type."""
    suffix = PurePosixPath(path).suffix.lower()
    return suffix in _RESOURCE_SUFFIXES


_LOCALE_NAME = re.compile(
    r"(?:messages|strings|locale)[_-]([a-z]{2,3}(?:[_-][A-Z]{2})?)",
    re.IGNORECASE,
)


def _extract_locale(path: str) -> str:
    """Best-effort locale tag from a resource file name."""
    match = _LOCALE_NAME.search(PurePosixPath(path).name)
    if match:
        return match.group(1).replace("-", "_")
    return "unknown"


# ── Compiler ──────────────────────────────────────────────────


def compile_semantic_changeset(state: StateStore) -> SemanticChangeSet:
    """Compile ``StateStore`` into a ``SemanticChangeSet``.

    Deterministic and side-effect-free.  Reads ``files_changed``,
    ``file_diffs``, and ``impact_manifest`` from *state* without mutating it.
    Every changed file is represented: files the ``ContextEngine`` capped still
    receive a unit so coverage is never silently dropped.

    Tolerates partial or malformed manifests: missing keys, truncated file
    lists, and absent resource sections all degrade gracefully.
    """

    manifest: dict[str, Any] = state.impact_manifest or {}
    try:
        manifest_version = int(manifest.get("version", 0))
    except (TypeError, ValueError):
        manifest_version = 0

    # Guard against None values for list-typed manifest keys.
    def _list_or_empty(value: Any) -> list:
        return value if isinstance(value, list) else []

    # Index manifest files by path for O(1) lookup.
    manifest_files: dict[str, dict[str, Any]] = {}
    for item in _list_or_empty(manifest.get("files", [])):
        path = str(item.get("path", ""))
        if path:
            manifest_files[path] = item

    # Index manifest resource files by path.
    manifest_resources: dict[str, dict[str, Any]] = {}
    for item in _list_or_empty(manifest.get("resource_files", [])):
        path = str(item.get("path", ""))
        if path:
            manifest_resources[path] = item

    # Global manifest-level data indexed by symbol name.
    refs_by_symbol: dict[str, list[dict[str, Any]]] = {}
    for ref in _list_or_empty(manifest.get("references", [])):
        sym = str(ref.get("symbol", ""))
        if sym:
            refs_by_symbol.setdefault(sym, []).append(ref)

    wiki_by_title: dict[str, list[dict[str, Any]]] = {}
    for page in _list_or_empty(manifest.get("wiki_pages", [])):
        title = str(page.get("title", ""))
        if title:
            wiki_by_title.setdefault(title, []).append(page)

    candidate_tests: list[str] = [str(p) for p in _list_or_empty(manifest.get("candidate_tests", []))]

    signals_by_file: dict[str, list[dict[str, Any]]] = {}
    global_signals: list[dict[str, Any]] = []
    for signal in _list_or_empty(manifest.get("risk_signals", [])):
        file_path = str(signal.get("file", ""))
        if file_path:
            signals_by_file.setdefault(file_path, []).append(signal)
        else:
            global_signals.append(signal)

    has_test_evidence_gap = any(
        str(s.get("type", "")) == "test-evidence-not-discovered"
        for s in _list_or_empty(manifest.get("risk_signals", []))
    )

    # Build units for every changed file.
    units: list[SemanticUnit] = []
    unresolved: list[str] = []
    seen_paths: set[str] = set()

    # 1. Files that appear in the manifest with symbol data.
    for item in _list_or_empty(manifest.get("files", [])):
        path = str(item.get("path", ""))
        if not path or path in seen_paths:
            continue
        seen_paths.add(path)
        language = str(item.get("language", "") or detect_language(path))
        changed_symbols = _list_or_empty(item.get("changed_symbols", []))
        file_calls = _list_or_empty(item.get("calls", []))
        file_imports = _list_or_empty(item.get("imports", []))
        file_added_lines = _list_or_empty(item.get("added_lines", []))
        file_signals = signals_by_file.get(path, [])

        if not changed_symbols:
            # No symbols extracted — create a resource-type unit so the file is
            # not silently dropped from coverage.
            unit = _make_resource_unit(
                repo=state.repo,
                pr_number=state.pr_number,
                path=path,
                language=language,
                added_lines=file_added_lines,
                signals=file_signals,
                manifest_version=manifest_version,
            )
            _finalize_risk(unit, has_test_evidence_gap=has_test_evidence_gap)
            units.append(unit)
            unresolved.append(path)
            continue

        for sym_data in changed_symbols:
            sym_name = str(sym_data.get("name", ""))
            sym_type = str(sym_data.get("type", "function"))
            try:
                start_line = int(sym_data.get("start_line", sym_data.get("line", 0)))
            except (TypeError, ValueError):
                start_line = 0
            try:
                end_line = int(sym_data.get("end_line", 0))
            except (TypeError, ValueError):
                end_line = 0
            raw_added = sym_data.get("added_lines", [])
            sym_added = list(raw_added) if isinstance(raw_added, list) else []

            # Attach calls relevant to this symbol.
            sym_calls = [
                dict(c)
                for c in file_calls
                if str(c.get("callee", "")) == sym_name or str(c.get("caller", "")) == sym_name
            ]

            # Attach imports relevant to this symbol.
            sym_imports = [
                dict(i)
                for i in file_imports
                if str(i.get("name", "")) == sym_name or str(i.get("local_name", "")) == sym_name
            ]

            # Attach references.
            sym_refs = refs_by_symbol.get(sym_name, [])
            ref_paths = []
            for ref in sym_refs:
                ref_paths.extend(ref.get("paths", []))

            # Attach candidate tests — paths that look like test files and
            # are referenced by this symbol.
            sym_tests = [p for p in candidate_tests if _is_test_path(p) and p in ref_paths]
            if not sym_tests:
                # Broaden: any referenced test path is useful.
                sym_tests = [p for p in ref_paths if _is_test_path(p)]

            # Attach wiki facts.
            sym_wiki = []
            for page in wiki_by_title.get(sym_name, []):
                facts = page.get("facts", [])
                if isinstance(facts, list):
                    sym_wiki.extend(facts)

            unit = SemanticUnit(
                id=_stable_id(state.repo, state.pr_number, path, sym_name, UnitKind.SYMBOL),
                path=path,
                language=language,
                kind=UnitKind.SYMBOL,
                symbol=sym_name,
                start_line=start_line,
                end_line=end_line,
                added_lines=sym_added or file_added_lines,
                calls=sym_calls,
                imports=sym_imports,
                references=[dict(r) for r in sym_refs],
                candidate_tests=sym_tests,
                risk_signals=file_signals + global_signals,
                wiki_facts=sym_wiki,
                provenance=Provenance(
                    source="manifest",
                    manifest_version=manifest_version,
                    note=f"symbol_type={sym_type}",
                ),
            )
            _finalize_risk(unit, has_test_evidence_gap=has_test_evidence_gap)
            units.append(unit)

    # 2. Files in manifest resource_files.
    for item in _list_or_empty(manifest.get("resource_files", [])):
        path = str(item.get("path", ""))
        if not path or path in seen_paths:
            continue
        seen_paths.add(path)
        locale = str(item.get("locale", "unknown"))
        raw_added = item.get("added_lines", [])
        added = list(raw_added) if isinstance(raw_added, list) else []
        try:
            entries = int(item.get("added_entries", len(added)))
        except (TypeError, ValueError):
            entries = len(added)

        unit = SemanticUnit(
            id=_stable_id(state.repo, state.pr_number, path, "", UnitKind.RESOURCE),
            path=path,
            language="",
            kind=UnitKind.RESOURCE,
            symbol="",
            added_lines=added,
            provenance=Provenance(
                source="manifest",
                manifest_version=manifest_version,
                note=f"locale={locale},entries={entries}",
            ),
        )
        _finalize_risk(unit, has_test_evidence_gap=has_test_evidence_gap)
        units.append(unit)

    # 3. Changed files NOT in the manifest (ContextEngine cap, unsupported
    #    language, missing diff, etc.).  Create a unit so coverage is never
    #    silently dropped.
    diffs = state.file_diffs or {}
    for path in state.files_changed:
        if path in seen_paths:
            continue
        seen_paths.add(path)
        language = detect_language(path)
        diff = diffs.get(path, "")
        added_lines = _extract_added_line_numbers(diff)

        if _is_resource_path(path):
            unit = SemanticUnit(
                id=_stable_id(state.repo, state.pr_number, path, "", UnitKind.RESOURCE),
                path=path,
                language=language,
                kind=UnitKind.RESOURCE,
                symbol="",
                added_lines=added_lines,
                provenance=Provenance(
                    source="resource-suffix",
                    manifest_version=manifest_version,
                    note=f"locale={_extract_locale(path)}",
                ),
            )
        else:
            # Code file outside manifest — create a symbol unit with an empty
            # symbol name so reviewers know the file exists but no symbol was
            # extracted.
            unit = SemanticUnit(
                id=_stable_id(state.repo, state.pr_number, path, "<file>", UnitKind.SYMBOL),
                path=path,
                language=language,
                kind=UnitKind.SYMBOL,
                symbol="",
                added_lines=added_lines,
                provenance=Provenance(
                    source="diff-only",
                    manifest_version=manifest_version,
                    note="file not in manifest; no symbol extracted",
                ),
            )
        _finalize_risk(unit, has_test_evidence_gap=has_test_evidence_gap)
        units.append(unit)
        unresolved.append(path)

    return SemanticChangeSet(
        repo=state.repo,
        pr_number=state.pr_number,
        head_sha=state.head_sha,
        units=units,
        unresolved_files=unresolved,
    )


# ── Internal helpers ──────────────────────────────────────────


def _make_resource_unit(
    *,
    repo: str,
    pr_number: int,
    path: str,
    language: str,
    added_lines: list[int],
    signals: list[dict[str, Any]],
    manifest_version: int,
) -> SemanticUnit:
    """Create a resource-kind unit for a file with no extractable symbol."""
    locale = _extract_locale(path) if _is_resource_path(path) else ""
    return SemanticUnit(
        id=_stable_id(repo, pr_number, path, "", UnitKind.RESOURCE),
        path=path,
        language=language,
        kind=UnitKind.RESOURCE,
        symbol="",
        added_lines=list(added_lines),
        risk_signals=list(signals),
        provenance=Provenance(
            source="manifest",
            manifest_version=manifest_version,
            note=f"no symbol extracted; locale={locale}" if locale else "no symbol extracted",
        ),
    )


def _finalize_risk(unit: SemanticUnit, *, has_test_evidence_gap: bool) -> None:
    """Compute and attach risk score/reasons to *unit* in-place."""
    score, reasons = _compute_risk(unit, has_test_evidence_gap=has_test_evidence_gap)
    unit.risk_score = score
    unit.risk_reasons = reasons


def _is_test_path(path: str) -> bool:
    """Heuristic: does *path* look like a test file?"""
    normalized = str(PurePosixPath(path)).lower()
    name = PurePosixPath(normalized).name
    return (
        "/test/" in f"/{normalized}/"
        or "/tests/" in f"/{normalized}/"
        or name.startswith("test_")
        or ".test." in name
        or ".spec." in name
        or name.endswith("_test.go")
    )


def _extract_added_line_numbers(diff: str) -> list[int]:
    """Extract RIGHT-side added-line numbers from a unified diff.

    Lightweight alternative to ``iter_added_lines`` that avoids importing the
    full detector machinery for files outside the manifest.
    """
    from reviewforge.engine.detectors.unified_diff import iter_added_lines

    return sorted({line for line, _content in iter_added_lines(diff)})
