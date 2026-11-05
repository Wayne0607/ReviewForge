"""Pilot isolation: scorer answers never enter the fixed source workspace."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from reviewforge.core.state import StateStore
from reviewforge.review_v42.snapshot import read_snapshot_bytes
from reviewforge.review_v42.snapshot_diff import pinned_regions
from reviewforge.tools.workspace import PRHeadWorkspace

SPEC = importlib.util.spec_from_file_location(
    "v42_delivery_pilot", Path(__file__).resolve().parents[2] / "scripts/benchmarks/v42_delivery_pilot.py"
)
pilot = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pilot)


def test_pilot_rejects_scorer_fields_and_escaping_source_paths(tmp_path):
    record = {
        "id": "sample",
        "title": "Change",
        "description": "Review",
        "base_files": {"a.py": "old\n"},
        "head_files": {"a.py": "new\n"},
    }
    path = tmp_path / "input.json"
    path.write_text(json.dumps({**record, "golden": "answer"}), encoding="utf-8")
    with pytest.raises(ValueError, match="only the declared"):
        pilot.input_material(path)
    path.write_text(json.dumps({**record, "head_files": {"../rubric.json": "answer"}}), encoding="utf-8")
    with pytest.raises(ValueError, match="invalid source path"):
        pilot.input_material(path)
    path.write_text(json.dumps(record), encoding="utf-8")
    assert pilot.input_material(path) == record


async def test_pilot_uses_complete_fixed_tarballs_and_exact_base_head_bytes():
    before = {"a.py": "value = 1\r\n", "helper.py": "units = 'seconds'\n"}
    after = {**before, "a.py": "value = 2\r\n"}
    base, head = pilot.digest(before), pilot.digest(after)
    source = pilot.FixtureArchives({base: before, head: after})
    left = await PRHeadWorkspace.build(StateStore(repo="pilot/test", head_sha=base), source)
    right = await PRHeadWorkspace.build(StateStore(repo="pilot/test", head_sha=head), source)
    try:
        fixed = await pinned_regions(left, right, run=SimpleNamespace(metadata={"source_line_policy": "git-lf-1"}))
        assert len(fixed["regions"]) == 1
        assert {row["path"] for row in fixed["regions"]} == {"a.py"}
        assert await read_snapshot_bytes(left, "a.py") == b"value = 1\r\n"
        assert await read_snapshot_bytes(right, "a.py") == b"value = 2\r\n"
        assert set(right.list_paths()) == {"a.py", "helper.py"}
        assert fixed["base_identity"] != fixed["head_identity"]
    finally:
        left.cleanup()
        right.cleanup()
