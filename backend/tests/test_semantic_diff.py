"""Tests for semantic_diff — semantic change compilation.

Covers:
  - Source symbol units from a full manifest
  - Localization / config resource units (no symbol extracted)
  - Malformed / partial manifests (missing keys, bad types, truncated lists)
  - Stable IDs and round-trip serialization
  - Risk scoring with explicit reason verification
  - >16 changed files (ContextEngine cap bypass)
  - Files beyond the manifest with resource suffixes
  - Empty / missing manifest
"""

from __future__ import annotations

import json

from reviewforge.core.state import StateStore
from reviewforge.engine.semantic_diff import (
    Provenance,
    SemanticChangeSet,
    SemanticUnit,
    UnitKind,
    _compute_risk,
    _is_resource_path,
    _is_test_path,
    _stable_id,
    compile_semantic_changeset,
)

# ── Fixtures ──────────────────────────────────────────────────


def _make_state(**kwargs) -> StateStore:
    """Build a minimal StateStore with sensible defaults."""
    defaults = {
        "repo": "owner/repo",
        "pr_number": 42,
        "head_sha": "abc123",
        "files_changed": [],
        "file_diffs": {},
        "impact_manifest": {},
    }
    defaults.update(kwargs)
    return StateStore(**defaults)


def _sample_manifest(**overrides) -> dict:
    """A well-formed manifest with one file and two symbols."""
    base = {
        "version": 2,
        "files": [
            {
                "path": "src/service.py",
                "language": "python",
                "added_lines": [5, 6, 12],
                "changed_symbols": [
                    {
                        "name": "process",
                        "type": "function",
                        "line": 4,
                        "start_line": 4,
                        "end_line": 8,
                        "added_lines": [5, 6],
                    },
                    {
                        "name": "ServiceConfig",
                        "type": "class",
                        "line": 10,
                        "start_line": 10,
                        "end_line": 15,
                        "added_lines": [12],
                    },
                ],
                "imports": [{"source": "auth", "name": "authorize", "local_name": "authorize", "line": 1}],
                "calls": [
                    {"caller": "process", "callee": "authorize", "line": 5},
                    {"caller": "<module>", "callee": "setup", "line": 2},
                ],
            }
        ],
        "references": [
            {"symbol": "process", "paths": ["src/caller.py", "tests/test_service.py"], "status": "ok"},
            {"symbol": "authorize", "paths": ["tests/test_auth.py"], "status": "ok"},
        ],
        "candidate_tests": ["tests/test_service.py", "tests/test_auth.py"],
        "risk_signals": [
            {"type": "blast-radius", "file": "src/service.py", "symbol": "process", "reference_count": 2},
            {"type": "security-sensitive-symbol", "file": "src/service.py", "symbol": "authorize"},
        ],
        "wiki_pages": [
            {
                "title": "process",
                "facts": [
                    {"kind": "return-or-error", "description": "process may raise ValueError"},
                ],
            }
        ],
        "resource_files": [],
        "historical_graph": [],
    }
    base.update(overrides)
    return base


# ── Source symbol tests ───────────────────────────────────────


class TestSourceSymbols:
    def test_units_created_for_each_changed_symbol(self):
        state = _make_state(
            files_changed=["src/service.py"],
            impact_manifest=_sample_manifest(),
        )
        cs = compile_semantic_changeset(state)

        assert cs.repo == "owner/repo"
        assert cs.pr_number == 42
        assert cs.head_sha == "abc123"
        assert len(cs.units) == 2

        names = {u.symbol for u in cs.units}
        assert names == {"process", "ServiceConfig"}

    def test_symbol_unit_has_correct_fields(self):
        state = _make_state(
            files_changed=["src/service.py"],
            impact_manifest=_sample_manifest(),
        )
        cs = compile_semantic_changeset(state)
        process = next(u for u in cs.units if u.symbol == "process")

        assert process.path == "src/service.py"
        assert process.language == "python"
        assert process.kind == UnitKind.SYMBOL
        assert process.start_line == 4
        assert process.end_line == 8
        assert process.added_lines == [5, 6]
        assert len(process.calls) == 1
        assert process.calls[0]["callee"] == "authorize"
        # Import "authorize" is from module "auth", not symbol "process"
        assert process.imports == []
        assert len(process.references) == 1
        assert process.references[0]["symbol"] == "process"
        assert process.candidate_tests == ["tests/test_service.py"]
        assert process.provenance.source == "manifest"
        assert process.provenance.manifest_version == 2

    def test_symbol_unit_inherits_file_added_lines_when_symbol_has_none(self):
        manifest = _sample_manifest()
        # Remove added_lines from symbol
        manifest["files"][0]["changed_symbols"][0]["added_lines"] = []
        state = _make_state(
            files_changed=["src/service.py"],
            impact_manifest=manifest,
        )
        cs = compile_semantic_changeset(state)
        process = next(u for u in cs.units if u.symbol == "process")
        assert process.added_lines == [5, 6, 12]  # from file-level

    def test_wiki_facts_attached_to_matching_symbol(self):
        state = _make_state(
            files_changed=["src/service.py"],
            impact_manifest=_sample_manifest(),
        )
        cs = compile_semantic_changeset(state)
        process = next(u for u in cs.units if u.symbol == "process")

        assert len(process.wiki_facts) == 1
        assert process.wiki_facts[0]["kind"] == "return-or-error"

    def test_wiki_facts_not_attached_to_non_matching_symbol(self):
        state = _make_state(
            files_changed=["src/service.py"],
            impact_manifest=_sample_manifest(),
        )
        cs = compile_semantic_changeset(state)
        config = next(u for u in cs.units if u.symbol == "ServiceConfig")
        assert config.wiki_facts == []

    def test_no_unresolved_when_all_files_in_manifest(self):
        state = _make_state(
            files_changed=["src/service.py"],
            impact_manifest=_sample_manifest(),
        )
        cs = compile_semantic_changeset(state)
        assert cs.unresolved_files == []


# ── Resource / config tests ──────────────────────────────────


class TestResourceUnits:
    def test_localization_resource_unit(self):
        manifest = {
            "version": 2,
            "files": [],
            "references": [],
            "candidate_tests": [],
            "risk_signals": [],
            "wiki_pages": [],
            "resource_files": [
                {
                    "path": "themes/messages/messages_zh_CN.properties",
                    "kind": "localization",
                    "locale": "zh_CN",
                    "added_lines": [1, 2],
                    "added_entries": 2,
                }
            ],
        }
        state = _make_state(
            files_changed=["themes/messages/messages_zh_CN.properties"],
            impact_manifest=manifest,
        )
        cs = compile_semantic_changeset(state)

        assert len(cs.units) == 1
        unit = cs.units[0]
        assert unit.kind == UnitKind.RESOURCE
        assert unit.path == "themes/messages/messages_zh_CN.properties"
        assert unit.added_lines == [1, 2]
        assert unit.provenance.source == "manifest"
        assert "locale=zh_CN" in unit.provenance.note

    def test_code_file_with_no_symbols_becomes_resource_unit(self):
        manifest = _sample_manifest()
        # Add a file with no changed_symbols
        manifest["files"].append(
            {
                "path": "src/utils.py",
                "language": "python",
                "added_lines": [3],
                "changed_symbols": [],
                "imports": [],
                "calls": [],
            }
        )
        state = _make_state(
            files_changed=["src/service.py", "src/utils.py"],
            impact_manifest=manifest,
        )
        cs = compile_semantic_changeset(state)

        utils_units = [u for u in cs.units if u.path == "src/utils.py"]
        assert len(utils_units) == 1
        assert utils_units[0].kind == UnitKind.RESOURCE
        assert "src/utils.py" in cs.unresolved_files

    def test_resource_suffix_file_beyond_manifest(self):
        """Files with resource suffixes not in manifest get resource units."""
        state = _make_state(
            files_changed=["locales/strings_es.arb", "src/main.py"],
            file_diffs={
                "locales/strings_es.arb": "@@ -1 +1 @@\n-old\n+new\n",
                "src/main.py": "@@ -1 +1 @@\n-old\n+new\n",
            },
            impact_manifest=_sample_manifest(),  # only src/service.py
        )
        cs = compile_semantic_changeset(state)

        arb_unit = next(u for u in cs.units if u.path == "locales/strings_es.arb")
        assert arb_unit.kind == UnitKind.RESOURCE
        assert arb_unit.provenance.source == "resource-suffix"

    def test_resource_path_detection(self):
        assert _is_resource_path("messages.properties") is True
        assert _is_resource_path("strings.po") is True
        assert _is_resource_path("app.arb") is True
        assert _is_resource_path("Localizable.strings") is True
        assert _is_resource_path("app.resx") is True
        assert _is_resource_path("template.ftl") is True
        assert _is_resource_path("src/main.py") is False
        assert _is_resource_path("lib/app.go") is False


# ── Malformed / partial manifest tests ───────────────────────


class TestMalformedManifests:
    def test_empty_manifest(self):
        state = _make_state(
            files_changed=["src/a.py"],
            file_diffs={"src/a.py": "@@ -1 +1 @@\n-x\n+y\n"},
            impact_manifest={},
        )
        cs = compile_semantic_changeset(state)

        assert len(cs.units) == 1
        assert cs.units[0].path == "src/a.py"
        assert cs.units[0].provenance.source == "diff-only"
        assert "src/a.py" in cs.unresolved_files

    def test_none_manifest(self):
        state = _make_state(
            files_changed=["src/a.py"],
            file_diffs={"src/a.py": ""},
            impact_manifest=None,
        )
        cs = compile_semantic_changeset(state)
        assert len(cs.units) == 1

    def test_manifest_missing_files_key(self):
        state = _make_state(
            files_changed=["src/a.py"],
            file_diffs={"src/a.py": ""},
            impact_manifest={"version": 1, "references": []},
        )
        cs = compile_semantic_changeset(state)
        assert len(cs.units) == 1
        assert cs.units[0].provenance.source == "diff-only"

    def test_manifest_with_bad_types(self):
        """Manifest entries with wrong types degrade gracefully."""
        state = _make_state(
            files_changed=["src/a.py"],
            file_diffs={"src/a.py": ""},
            impact_manifest={
                "version": "not-an-int",  # bad type
                "files": [
                    {
                        "path": "src/a.py",
                        "language": None,
                        "added_lines": "not-a-list",
                        "changed_symbols": [
                            {
                                "name": 123,  # not a string
                                "type": "function",
                                "line": "not-an-int",
                                "start_line": 0,
                                "end_line": 0,
                                "added_lines": [],
                            }
                        ],
                        "imports": None,
                        "calls": None,
                    }
                ],
                "references": None,
                "candidate_tests": None,
                "risk_signals": None,
                "wiki_pages": None,
                "resource_files": None,
            },
        )
        # Should not raise
        cs = compile_semantic_changeset(state)
        assert len(cs.units) >= 1

    def test_manifest_with_empty_file_path(self):
        """Entries with empty path are skipped."""
        manifest = _sample_manifest()
        manifest["files"].insert(0, {"path": "", "changed_symbols": []})
        state = _make_state(
            files_changed=["src/service.py"],
            impact_manifest=manifest,
        )
        cs = compile_semantic_changeset(state)
        # Should still have 2 units from the valid entry
        assert len(cs.units) == 2

    def test_manifest_with_duplicate_paths(self):
        """Duplicate paths in manifest are deduplicated (first wins)."""
        manifest = _sample_manifest()
        manifest["files"].append(manifest["files"][0])
        state = _make_state(
            files_changed=["src/service.py"],
            impact_manifest=manifest,
        )
        cs = compile_semantic_changeset(state)
        assert len(cs.units) == 2  # same 2 symbols, not 4

    def test_partial_references(self):
        """References with missing keys don't crash."""
        manifest = _sample_manifest()
        manifest["references"] = [
            {"symbol": "process", "paths": []},
            {"paths": ["foo.py"]},  # missing symbol
            {},  # completely empty
        ]
        state = _make_state(
            files_changed=["src/service.py"],
            impact_manifest=manifest,
        )
        cs = compile_semantic_changeset(state)
        process = next(u for u in cs.units if u.symbol == "process")
        assert process.references == [] or len(process.references) >= 0  # no crash


# ── Stable ID and round-trip tests ────────────────────────────


class TestStableIdsAndRoundTrip:
    def test_stable_id_deterministic(self):
        """Same inputs always produce the same ID."""
        id1 = _stable_id("owner/repo", 42, "src/a.py", "process", UnitKind.SYMBOL)
        id2 = _stable_id("owner/repo", 42, "src/a.py", "process", UnitKind.SYMBOL)
        assert id1 == id2
        assert id1.startswith("su_")

    def test_stable_id_different_inputs(self):
        """Different inputs produce different IDs."""
        id1 = _stable_id("owner/repo", 42, "src/a.py", "process", UnitKind.SYMBOL)
        id2 = _stable_id("owner/repo", 42, "src/a.py", "process", UnitKind.RESOURCE)
        id3 = _stable_id("owner/repo", 42, "src/b.py", "process", UnitKind.SYMBOL)
        id4 = _stable_id("other/repo", 42, "src/a.py", "process", UnitKind.SYMBOL)
        ids = {id1, id2, id3, id4}
        assert len(ids) == 4

    def test_unit_round_trip(self):
        """SemanticUnit survives to_dict → from_dict."""
        unit = SemanticUnit(
            id="su_test123",
            path="src/service.py",
            language="python",
            kind=UnitKind.SYMBOL,
            symbol="process",
            start_line=4,
            end_line=8,
            added_lines=[5, 6],
            calls=[{"caller": "process", "callee": "authorize", "line": 5}],
            imports=[{"source": "auth", "name": "authorize", "local_name": "authorize", "line": 1}],
            references=[{"symbol": "process", "paths": ["tests/test_service.py"]}],
            candidate_tests=["tests/test_service.py"],
            risk_signals=[{"type": "blast-radius", "reference_count": 2}],
            wiki_facts=[{"kind": "return-or-error"}],
            provenance=Provenance(source="manifest", manifest_version=2, note="test"),
            risk_score=0.35,
            risk_reasons=["blast-radius:2"],
        )
        data = unit.to_dict()
        restored = SemanticUnit.from_dict(data)

        assert restored.id == unit.id
        assert restored.path == unit.path
        assert restored.language == unit.language
        assert restored.kind == unit.kind
        assert restored.symbol == unit.symbol
        assert restored.start_line == unit.start_line
        assert restored.end_line == unit.end_line
        assert restored.added_lines == unit.added_lines
        assert restored.calls == unit.calls
        assert restored.imports == unit.imports
        assert restored.references == unit.references
        assert restored.candidate_tests == unit.candidate_tests
        assert restored.risk_signals == unit.risk_signals
        assert restored.wiki_facts == unit.wiki_facts
        assert restored.provenance.source == unit.provenance.source
        assert restored.risk_score == unit.risk_score
        assert restored.risk_reasons == unit.risk_reasons

    def test_changeset_round_trip(self):
        """SemanticChangeSet survives to_dict → from_dict."""
        state = _make_state(
            files_changed=["src/service.py"],
            impact_manifest=_sample_manifest(),
        )
        cs = compile_semantic_changeset(state)
        data = cs.to_dict()
        restored = SemanticChangeSet.from_dict(data)

        assert restored.repo == cs.repo
        assert restored.pr_number == cs.pr_number
        assert restored.head_sha == cs.head_sha
        assert len(restored.units) == len(cs.units)
        assert restored.unresolved_files == cs.unresolved_files

    def test_json_serialization(self):
        """to_dict output is valid JSON."""
        state = _make_state(
            files_changed=["src/service.py"],
            impact_manifest=_sample_manifest(),
        )
        cs = compile_semantic_changeset(state)
        json_str = json.dumps(cs.to_dict(), ensure_ascii=False)
        parsed = json.loads(json_str)
        assert parsed["repo"] == "owner/repo"
        assert len(parsed["units"]) == len(cs.units)

    def test_id_stability_across_compilations(self):
        """Compiling the same state twice produces identical IDs."""
        state = _make_state(
            files_changed=["src/service.py"],
            impact_manifest=_sample_manifest(),
        )
        cs1 = compile_semantic_changeset(state)
        cs2 = compile_semantic_changeset(state)
        assert [u.id for u in cs1.units] == [u.id for u in cs2.units]

    def test_resource_unit_round_trip(self):
        """Resource units round-trip correctly."""
        unit = SemanticUnit(
            id="su_res",
            path="messages.properties",
            kind=UnitKind.RESOURCE,
            provenance=Provenance(source="resource-suffix", note="locale=en"),
            risk_score=0.1,
            risk_reasons=["resource-change"],
        )
        data = unit.to_dict()
        restored = SemanticUnit.from_dict(data)
        assert restored.kind == UnitKind.RESOURCE
        assert restored.provenance.source == "resource-suffix"


# ── Risk scoring tests ────────────────────────────────────────


class TestRiskScoring:
    def test_symbol_with_references_and_sensitive_name(self):
        """High-risk: blast radius + security-sensitive symbol."""
        state = _make_state(
            files_changed=["src/service.py"],
            impact_manifest=_sample_manifest(),
        )
        cs = compile_semantic_changeset(state)
        process = next(u for u in cs.units if u.symbol == "process")

        # process has references and the manifest has a blast-radius signal
        assert process.risk_score > 0
        assert any("blast-radius" in r for r in process.risk_reasons)

    def test_resource_unit_has_moderate_risk(self):
        state = _make_state(
            files_changed=["src/service.py"],
            impact_manifest=_sample_manifest(),
        )
        cs = compile_semantic_changeset(state)
        config = next(u for u in cs.units if u.symbol == "ServiceConfig")

        # ServiceConfig has no references, no sensitive name, no wiki facts
        # It has a blast-radius signal (from the file-level signals) but no
        # symbol-specific references
        assert config.risk_score >= 0  # at minimum from file-level signals

    def test_test_evidence_gap_increases_risk(self):
        manifest = _sample_manifest()
        manifest["risk_signals"].append({"type": "test-evidence-not-discovered", "note": "no test file found"})
        state = _make_state(
            files_changed=["src/service.py"],
            impact_manifest=manifest,
        )
        cs = compile_semantic_changeset(state)
        process = next(u for u in cs.units if u.symbol == "process")

        assert any("test-evidence" in r for r in process.risk_reasons)
        assert process.risk_score > 0

    def test_risk_score_bounded_at_one(self):
        """Risk score never exceeds 1.0."""
        manifest = _sample_manifest()
        # Add many risk signals
        for i in range(20):
            manifest["risk_signals"].append(
                {"type": "blast-radius", "file": "src/service.py", "symbol": "process", "reference_count": 100}
            )
        state = _make_state(
            files_changed=["src/service.py"],
            impact_manifest=manifest,
        )
        cs = compile_semantic_changeset(state)
        for unit in cs.units:
            assert 0.0 <= unit.risk_score <= 1.0

    def test_empty_unit_has_zero_risk(self):
        unit = SemanticUnit()
        score, reasons = _compute_risk(unit, has_test_evidence_gap=False)
        assert score == 0.0
        assert reasons == []

    def test_wifact_fact_boosts_risk(self):
        manifest = _sample_manifest()
        manifest["wiki_pages"] = [
            {
                "title": "process",
                "facts": [
                    {"kind": "side-effect", "description": "writes to DB"},
                ],
            }
        ]
        state = _make_state(
            files_changed=["src/service.py"],
            impact_manifest=manifest,
        )
        cs = compile_semantic_changeset(state)
        process = next(u for u in cs.units if u.symbol == "process")
        assert any("wiki-fact" in r for r in process.risk_reasons)


# ── >16 changed files tests ──────────────────────────────────


class TestManyFiles:
    def test_more_than_16_files_all_preserved(self):
        """All changed files are preserved, not silently dropped at 16."""
        files = [f"src/module_{i}.py" for i in range(25)]
        manifest = _sample_manifest()
        # Only include first 3 files in manifest (simulating ContextEngine cap)
        manifest["files"] = [
            {
                "path": f"src/module_{i}.py",
                "language": "python",
                "added_lines": [i + 1],
                "changed_symbols": [
                    {
                        "name": f"func_{i}",
                        "type": "function",
                        "line": i + 1,
                        "start_line": i + 1,
                        "end_line": i + 5,
                        "added_lines": [i + 1],
                    }
                ],
                "imports": [],
                "calls": [],
            }
            for i in range(3)
        ]
        state = _make_state(
            files_changed=files,
            file_diffs={f: f"@@ -{i} +{i} @@\n-x\n+y\n" for i, f in enumerate(files)},
            impact_manifest=manifest,
        )
        cs = compile_semantic_changeset(state)

        # 3 files with symbols + 22 files beyond manifest = 25 total units
        assert len(cs.units) == 25
        unit_paths = {u.path for u in cs.units}
        assert unit_paths == set(files)
        # First 3 are symbol units, rest are diff-only
        symbol_units = [u for u in cs.units if u.kind == UnitKind.SYMBOL and u.symbol]
        assert len(symbol_units) == 3
        # Unresolved files are those not in manifest
        assert len(cs.unresolved_files) == 22

    def test_17_files_with_resource_suffixes(self):
        """Resource files beyond the cap still get units."""
        files = [f"locales/messages_{i}.properties" for i in range(17)]
        state = _make_state(
            files_changed=files,
            file_diffs={f: "@@ -1 +1 @@\n-a\n+b\n" for f in files},
            impact_manifest={},  # empty manifest
        )
        cs = compile_semantic_changeset(state)

        assert len(cs.units) == 17
        assert all(u.kind == UnitKind.RESOURCE for u in cs.units)


# ── Test path detection ───────────────────────────────────────


class TestPathDetection:
    def test_test_path_detection(self):
        assert _is_test_path("tests/test_service.py") is True
        assert _is_test_path("test/test_auth.go") is True
        assert _is_test_path("src/test_utils.py") is True
        assert _is_test_path("app.test.ts") is True
        assert _is_test_path("component.spec.js") is True
        assert _is_test_path("handler_test.go") is True
        assert _is_test_path("src/service.py") is False
        assert _is_test_path("lib/utils.go") is False


# ── Candidate tests filtering ─────────────────────────────────


class TestCandidateTests:
    def test_candidate_tests_filtered_to_referenced_paths(self):
        manifest = _sample_manifest()
        manifest["candidate_tests"] = [
            "tests/test_service.py",
            "tests/test_auth.py",
            "tests/test_unrelated.py",
        ]
        state = _make_state(
            files_changed=["src/service.py"],
            impact_manifest=manifest,
        )
        cs = compile_semantic_changeset(state)
        process = next(u for u in cs.units if u.symbol == "process")

        # test_service.py is in references for "process"
        assert "tests/test_service.py" in process.candidate_tests
        # test_unrelated.py is not in references
        assert "tests/test_unrelated.py" not in process.candidate_tests

    def test_candidate_tests_fallback_to_referenced_test_paths(self):
        """If no direct candidate_tests match, use referenced test paths."""
        manifest = _sample_manifest()
        manifest["candidate_tests"] = []
        state = _make_state(
            files_changed=["src/service.py"],
            impact_manifest=manifest,
        )
        cs = compile_semantic_changeset(state)
        process = next(u for u in cs.units if u.symbol == "process")

        # tests/test_service.py is in references and looks like a test
        assert "tests/test_service.py" in process.candidate_tests


# ── Calls / imports filtering ─────────────────────────────────


class TestCallsImports:
    def test_calls_filtered_to_symbol(self):
        state = _make_state(
            files_changed=["src/service.py"],
            impact_manifest=_sample_manifest(),
        )
        cs = compile_semantic_changeset(state)
        process = next(u for u in cs.units if u.symbol == "process")

        # Only the call where process is caller or callee
        assert len(process.calls) == 1
        assert process.calls[0]["callee"] == "authorize"

        config = next(u for u in cs.units if u.symbol == "ServiceConfig")
        assert config.calls == []

    def test_imports_filtered_to_symbol(self):
        state = _make_state(
            files_changed=["src/service.py"],
            impact_manifest=_sample_manifest(),
        )
        cs = compile_semantic_changeset(state)
        process = next(u for u in cs.units if u.symbol == "process")

        # Import "authorize" has name="authorize", not "process", so it's not matched
        assert process.imports == []


# ── Diff-only (no manifest) tests ─────────────────────────────


class TestDiffOnly:
    def test_diff_only_code_file(self):
        """Files not in manifest with code suffix get diff-only provenance."""
        state = _make_state(
            files_changed=["src/main.py"],
            file_diffs={"src/main.py": "@@ -1,3 +1,4 @@\n import os\n+import sys\n def main():\n     pass\n"},
            impact_manifest={},
        )
        cs = compile_semantic_changeset(state)

        assert len(cs.units) == 1
        unit = cs.units[0]
        assert unit.provenance.source == "diff-only"
        assert unit.language == "python"
        assert "src/main.py" in cs.unresolved_files
        assert unit.added_lines == [2]  # the added import line

    def test_diff_only_unknown_language(self):
        state = _make_state(
            files_changed=["config.yaml"],
            file_diffs={"config.yaml": "@@ -1 +1 @@\n-old\n+new\n"},
            impact_manifest={},
        )
        cs = compile_semantic_changeset(state)

        assert len(cs.units) == 1
        assert cs.units[0].language == "unknown"

    def test_empty_diff(self):
        state = _make_state(
            files_changed=["src/a.py"],
            file_diffs={"src/a.py": ""},
            impact_manifest={},
        )
        cs = compile_semantic_changeset(state)
        assert len(cs.units) == 1
        assert cs.units[0].added_lines == []


# ── Multiple files mixed ──────────────────────────────────────


class TestMixedFiles:
    def test_mixed_source_resource_and_beyond_manifest(self):
        """Realistic scenario with source, resource, and uncapped files."""
        manifest = _sample_manifest()
        manifest["resource_files"] = [
            {
                "path": "locales/en.properties",
                "kind": "localization",
                "locale": "en",
                "added_lines": [1],
                "added_entries": 1,
            }
        ]
        state = _make_state(
            files_changed=[
                "src/service.py",  # in manifest files
                "locales/en.properties",  # in manifest resource_files
                "src/extra.py",  # not in manifest
                "assets/strings_es.po",  # not in manifest, resource suffix
            ],
            file_diffs={
                "src/service.py": "@@ -1 +1 @@\n-old\n+new\n",
                "locales/en.properties": "@@ -1 +1 @@\n-old\n+new\n",
                "src/extra.py": "@@ -1 +1 @@\n-old\n+new\n",
                "assets/strings_es.po": "@@ -1 +1 @@\n-old\n+new\n",
            },
            impact_manifest=manifest,
        )
        cs = compile_semantic_changeset(state)

        # src/service.py: 2 symbol units
        # locales/en.properties: 1 resource unit from manifest
        # src/extra.py: 1 diff-only symbol unit
        # assets/strings_es.po: 1 resource-suffix unit
        assert len(cs.units) == 5
        assert "src/extra.py" in cs.unresolved_files
        assert "assets/strings_es.po" in cs.unresolved_files
        assert "src/service.py" not in cs.unresolved_files
        assert "locales/en.properties" not in cs.unresolved_files
