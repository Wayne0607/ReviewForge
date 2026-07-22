"""Cross-module contract tests for the v3 semantic → coverage pipeline.

These tests exercise the real data flow:
  StateStore → compile_semantic_changeset → .to_dict()
  → CoverageLedger.from_change_set()

They verify that the contract between semantic_diff and coverage_ledger is
honoured end-to-end: risk_score floats, start_line propagation, signal-driven
dimension creation, and no silent file dropping.
"""

from __future__ import annotations

from reviewforge.core.state import StateStore
from reviewforge.engine.coverage_ledger import (
    CoverageDimension,
    CoverageLedger,
)
from reviewforge.engine.semantic_diff import compile_semantic_changeset


def _make_state(**kwargs) -> StateStore:
    """Build a minimal StateStore with sensible defaults."""
    defaults = {
        "repo": "owner/repo",
        "pr_number": 77,
        "head_sha": "deadbeef",
        "files_changed": [],
        "file_diffs": {},
        "impact_manifest": {},
    }
    defaults.update(kwargs)
    return StateStore(**defaults)


def _full_manifest(**overrides) -> dict:
    """A manifest with security-sensitive symbols, test evidence gaps, and
    localization resources."""
    base = {
        "version": 2,
        "files": [
            {
                "path": "src/auth.py",
                "language": "python",
                "added_lines": [5, 6, 12],
                "changed_symbols": [
                    {
                        "name": "authenticate",
                        "type": "function",
                        "line": 4,
                        "start_line": 4,
                        "end_line": 10,
                        "added_lines": [5, 6],
                    },
                ],
                "imports": [],
                "calls": [],
            },
            {
                "path": "src/utils.py",
                "language": "python",
                "added_lines": [3],
                "changed_symbols": [
                    {
                        "name": "helper",
                        "type": "function",
                        "line": 1,
                        "start_line": 1,
                        "end_line": 5,
                        "added_lines": [3],
                    },
                ],
                "imports": [],
                "calls": [],
            },
            {
                "path": "src/service.py",
                "language": "python",
                "added_lines": [10, 20],
                "changed_symbols": [
                    {
                        "name": "process",
                        "type": "function",
                        "line": 8,
                        "start_line": 8,
                        "end_line": 25,
                        "added_lines": [10, 20],
                    },
                ],
                "imports": [],
                "calls": [],
            },
        ],
        "references": [],
        "candidate_tests": [],
        "risk_signals": [
            {"type": "security-sensitive-symbol", "file": "src/auth.py", "symbol": "authenticate"},
            {"type": "test-evidence-not-discovered", "note": "no test file found"},
        ],
        "wiki_pages": [],
        "resource_files": [
            {
                "path": "locales/en.properties",
                "kind": "localization",
                "locale": "en",
                "added_lines": [1, 2],
                "added_entries": 2,
            },
        ],
        "historical_graph": [],
    }
    base.update(overrides)
    return base


# ── Cross-module contract ────────────────────────────────────────────────────


class TestCrossModuleContract:
    """Verify that compile_semantic_changeset → to_dict → from_change_set
    preserves the contract between semantic_diff and coverage_ledger."""

    def test_end_to_end_pipeline(self):
        """Full pipeline: StateStore → SemanticChangeSet → CoverageLedger."""
        state = _make_state(
            files_changed=["src/auth.py", "src/utils.py", "src/service.py", "locales/en.properties"],
            file_diffs={
                "src/auth.py": "@@ -4,3 +4,5 @@\n-old\n+new1\n+new2\n",
                "src/utils.py": "@@ -1,2 +1,3 @@\n-a\n+b\n+c\n",
                "src/service.py": "@@ -8,5 +8,7 @@\n-old\n+new\n",
                "locales/en.properties": "@@ -1 +1,3 @@\n-a=b\n+c=d\n+e=f\n",
            },
            impact_manifest=_full_manifest(),
        )

        # Compile and convert to dict (the real pipeline shape)
        cs = compile_semantic_changeset(state)
        cs_dict = cs.to_dict()

        # Build ledger from the dict
        ledger = CoverageLedger.from_change_set(cs_dict)

        # At least one cell per unit
        assert len(ledger.cells) >= len(cs_dict["units"])

    def test_nonzero_float_risk_preserved(self):
        """risk_score float from SemanticUnit survives into CoverageCell.risk."""
        state = _make_state(
            files_changed=["src/auth.py"],
            impact_manifest=_full_manifest(),
        )
        cs = compile_semantic_changeset(state)
        cs_dict = cs.to_dict()

        # Find the authenticate unit — it should have nonzero risk (security-
        # sensitive name + security-sensitive-symbol signal)
        auth_unit = next(u for u in cs_dict["units"] if u.get("symbol") == "authenticate")
        assert auth_unit["risk_score"] > 0, "authenticate should have nonzero risk_score"
        assert isinstance(auth_unit["risk_score"], float)

        ledger = CoverageLedger.from_change_set(cs_dict)
        cell = ledger.get_cell(auth_unit["id"], CoverageDimension.CORRECTNESS)
        assert cell is not None
        assert cell.risk > 0
        assert isinstance(cell.risk, float)
        assert cell.risk == auth_unit["risk_score"]

    def test_start_line_becomes_cell_line(self):
        """start_line from SemanticUnit.to_dict() becomes CoverageCell.line."""
        state = _make_state(
            files_changed=["src/auth.py"],
            impact_manifest=_full_manifest(),
        )
        cs = compile_semantic_changeset(state)
        cs_dict = cs.to_dict()

        auth_unit = next(u for u in cs_dict["units"] if u.get("symbol") == "authenticate")
        assert auth_unit["start_line"] == 4

        ledger = CoverageLedger.from_change_set(cs_dict)
        cell = ledger.get_cell(auth_unit["id"], CoverageDimension.CORRECTNESS)
        assert cell is not None
        assert cell.line == 4

    def test_every_changed_file_has_correctness_cell(self):
        """Every changed file must have at least one correctness cell."""
        files = ["src/auth.py", "src/utils.py", "src/service.py", "locales/en.properties"]
        state = _make_state(
            files_changed=files,
            file_diffs={f: "@@ -1 +1 @@\n-a\n+b\n" for f in files},
            impact_manifest=_full_manifest(),
        )
        cs = compile_semantic_changeset(state)
        cs_dict = cs.to_dict()
        ledger = CoverageLedger.from_change_set(cs_dict)

        unit_paths = {u["path"] for u in cs_dict["units"]}
        for path in files:
            assert path in unit_paths, f"missing unit for changed file {path}"

        # Every unit has a correctness cell
        correctness_cells = ledger.cells_by_dimension(CoverageDimension.CORRECTNESS)
        correctness_unit_ids = {c.unit_id for c in correctness_cells}
        all_unit_ids = {u["id"] for u in cs_dict["units"]}
        assert correctness_unit_ids == all_unit_ids

    def test_security_signal_creates_security_cells(self):
        """Security-sensitive symbol signal → mandatory security cells."""
        state = _make_state(
            files_changed=["src/auth.py"],
            impact_manifest=_full_manifest(),
        )
        cs = compile_semantic_changeset(state)
        cs_dict = cs.to_dict()

        # The security-sensitive-symbol signal is present
        auth_unit = next(u for u in cs_dict["units"] if u.get("symbol") == "authenticate")
        signal_types = [s.get("type") for s in auth_unit.get("risk_signals", [])]
        assert "security-sensitive-symbol" in signal_types

        ledger = CoverageLedger.from_change_set(cs_dict)
        sec_cell = ledger.get_cell(auth_unit["id"], CoverageDimension.SECURITY)
        assert sec_cell is not None
        assert sec_cell.mandatory is True

    def test_test_evidence_gap_creates_testing_cells(self):
        """test-evidence-not-discovered signal → mandatory testing cells."""
        manifest = _full_manifest()
        # The manifest already has test-evidence-not-discovered signal
        state = _make_state(
            files_changed=["src/auth.py", "src/utils.py", "src/service.py"],
            file_diffs={f: "@@ -1 +1 @@\n-a\n+b\n" for f in ["src/auth.py", "src/utils.py", "src/service.py"]},
            impact_manifest=manifest,
        )
        cs = compile_semantic_changeset(state)
        cs_dict = cs.to_dict()

        # Verify the signal is present in the risk_signals
        auth_unit = next(u for u in cs_dict["units"] if u.get("symbol") == "authenticate")
        signal_types = [s.get("type") for s in auth_unit.get("risk_signals", [])]
        assert "test-evidence-not-discovered" in signal_types

        ledger = CoverageLedger.from_change_set(cs_dict)
        # The test-evidence-not-discovered signal maps to TESTING dimension
        testing_cell = ledger.get_cell(auth_unit["id"], CoverageDimension.TESTING)
        assert testing_cell is not None
        assert testing_cell.mandatory is True

    def test_localization_resource_creates_localization_cells(self):
        """Localization resource files → mandatory localization cells."""
        files = ["src/auth.py", "locales/en.properties"]
        state = _make_state(
            files_changed=files,
            file_diffs={f: "@@ -1 +1 @@\n-a\n+b\n" for f in files},
            impact_manifest=_full_manifest(),
        )
        cs = compile_semantic_changeset(state)
        cs_dict = cs.to_dict()

        # Find the localization unit
        locale_unit = next(u for u in cs_dict["units"] if u["path"] == "locales/en.properties")
        assert locale_unit is not None

        ledger = CoverageLedger.from_change_set(cs_dict)
        loc_cell = ledger.get_cell(locale_unit["id"], CoverageDimension.LOCALIZATION)
        assert loc_cell is not None
        assert loc_cell.mandatory is True

    def test_more_than_16_files_not_dropped(self):
        """All changed files produce units and cells — no silent cap."""
        # 20 files: 3 in manifest, 17 beyond manifest
        manifest_files = [f"src/module_{i}.py" for i in range(3)]
        beyond_manifest = [f"src/extra_{i}.py" for i in range(17)]
        all_files = manifest_files + beyond_manifest

        manifest = _full_manifest()
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
        manifest["resource_files"] = []

        state = _make_state(
            files_changed=all_files,
            file_diffs={f: "@@ -1 +1 @@\n-a\n+b\n" for f in all_files},
            impact_manifest=manifest,
        )
        cs = compile_semantic_changeset(state)
        cs_dict = cs.to_dict()

        # All 20 files must have units
        unit_paths = {u["path"] for u in cs_dict["units"]}
        assert unit_paths == set(all_files), "some files were silently dropped"

        ledger = CoverageLedger.from_change_set(cs_dict)

        # Every unit must have a correctness cell
        correctness_cells = ledger.cells_by_dimension(CoverageDimension.CORRECTNESS)
        correctness_unit_ids = {c.unit_id for c in correctness_cells}
        all_unit_ids = {u["id"] for u in cs_dict["units"]}
        assert correctness_unit_ids == all_unit_ids

        # Total cell count must be >= number of units
        assert len(ledger.cells) >= len(cs_dict["units"])

    def test_risk_score_in_zero_one_range(self):
        """All risk scores are in [0.0, 1.0]."""
        files = ["src/auth.py", "src/utils.py", "src/service.py", "locales/en.properties"]
        state = _make_state(
            files_changed=files,
            file_diffs={f: "@@ -1 +1 @@\n-a\n+b\n" for f in files},
            impact_manifest=_full_manifest(),
        )
        cs = compile_semantic_changeset(state)
        cs_dict = cs.to_dict()

        for unit in cs_dict["units"]:
            score = unit["risk_score"]
            assert isinstance(score, float), f"risk_score is {type(score)}, not float"
            assert 0.0 <= score <= 1.0, f"risk_score {score} out of [0, 1]"

        ledger = CoverageLedger.from_change_set(cs_dict)
        for cell in ledger.cells:
            assert isinstance(cell.risk, float)
            assert 0.0 <= cell.risk <= 1.0, f"cell.risk {cell.risk} out of [0, 1]"

    def test_diff_only_files_get_cells(self):
        """Files beyond the manifest (diff-only) still get coverage cells."""
        files = ["src/extra.py"]
        state = _make_state(
            files_changed=files,
            file_diffs={"src/extra.py": "@@ -1 +1 @@\n-old\n+new\n"},
            impact_manifest={
                "version": 2,
                "files": [],
                "references": [],
                "candidate_tests": [],
                "risk_signals": [],
                "wiki_pages": [],
                "resource_files": [],
            },
        )
        cs = compile_semantic_changeset(state)
        cs_dict = cs.to_dict()

        assert len(cs_dict["units"]) == 1
        assert cs_dict["units"][0]["path"] == "src/extra.py"
        assert cs_dict["units"][0]["provenance"]["source"] == "diff-only"

        ledger = CoverageLedger.from_change_set(cs_dict)
        unit_id = cs_dict["units"][0]["id"]
        cell = ledger.get_cell(unit_id, CoverageDimension.CORRECTNESS)
        assert cell is not None
        assert cell.mandatory is True

    def test_json_round_trip_preserves_contract(self):
        """Full serialize → deserialize cycle preserves the contract."""
        files = ["src/auth.py", "src/service.py"]
        state = _make_state(
            files_changed=files,
            file_diffs={f: "@@ -1 +1 @@\n-a\n+b\n" for f in files},
            impact_manifest=_full_manifest(),
        )
        cs = compile_semantic_changeset(state)
        cs_dict = cs.to_dict()

        import json

        json_str = json.dumps(cs_dict)
        restored_dict = json.loads(json_str)

        ledger = CoverageLedger.from_change_set(restored_dict)
        assert len(ledger.cells) >= len(cs_dict["units"])

        # Risk scores still preserved after round-trip
        for cell in ledger.cells:
            assert isinstance(cell.risk, float)
            assert 0.0 <= cell.risk <= 1.0
