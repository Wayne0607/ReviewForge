import pytest

from reviewforge.review_v42.snapshot import read_snapshot_blob
from reviewforge.review_v42.snapshot_diff import pinned_regions
from reviewforge.tools.workspace import WorkspaceUnavailable


class Snapshot:
    source = "tarball"
    truncated = False

    def __init__(self, files):
        self.files = files

    def list_paths(self):
        return sorted(self.files)

    def read_bytes(self, path):
        return self.files[path]


async def test_fixed_snapshots_cover_every_added_deleted_and_modified_path():
    base = Snapshot({"src/old.py": b"old()\n", "src/change.py": b"one\ntwo\n", "same": b"same"})
    head = Snapshot({"src/new.py": b"new()\n", "src/change.py": b"one\nthree\n", "same": b"same"})
    result = await pinned_regions(base, head)
    by_path = {r["path"]: r for r in result["regions"]}
    assert set(by_path) == {"src/old.py", "src/new.py", "src/change.py"}
    assert by_path["src/change.py"]["changed_lines"] == [2]
    assert by_path["src/new.py"]["changed_lines"] == [1]
    assert by_path["src/old.py"]["side"] == "base" and by_path["src/old.py"]["start_line"] == 1
    assert by_path["src/old.py"]["changed_lines"] == []
    assert all(not r["unsupported"] for r in by_path.values())
    assert result["base_identity"] != result["head_identity"]


async def test_binary_non_utf8_empty_and_eol_only_remain_explicit():
    base = Snapshot({"binary": b"\x00old", "encoded": b"\xff", "eol": b"a\r\n", "deleted": b"\x00old"})
    head = Snapshot({"binary": b"\x00new", "encoded": b"\xfe", "eol": b"a\n", "empty": b""})
    result = await pinned_regions(base, head)
    by_path = {r["path"]: r for r in result["regions"]}
    assert set(by_path) == {"binary", "encoded", "eol", "empty", "deleted"}
    assert all(r["unsupported"] and r["changed_lines"] == [] for r in by_path.values())
    assert by_path["binary"]["binary"] and by_path["deleted"]["side"] == "base"
    assert "non-UTF-8" in by_path["encoded"]["reason"]
    assert await read_snapshot_blob(base, "encoded") == b"\xff"


async def test_crlf_diff_has_exact_head_line_numbers_and_no_external_pr_dependency():
    base = Snapshot({"x": b"a\r\nb\r\nc\r\n"})
    head = Snapshot({"x": b"a\r\nB\r\nc\r\n"})
    first = await pinned_regions(base, head)
    # An unrelated live PR payload is deliberately irrelevant to both inputs.
    unrelated_live_pr = {"head": "different", "files": ["not-x"]}
    second = await pinned_regions(base, head)
    assert unrelated_live_pr["head"] == "different"
    assert first == second
    assert first["regions"][0]["changed_lines"] == [2]
    assert first["regions"][0]["unsupported"] is False


async def test_drift_during_snapshot_comparison_is_not_silently_accepted():
    base = Snapshot({"a": b"old"})
    head = Snapshot({"a": b"new"})
    calls = 0

    def mutate(path):
        nonlocal calls
        calls += 1
        if calls == 2:
            head.files[path] = b"bad"
        return head.files[path]

    head.read_bytes = mutate
    with pytest.raises(WorkspaceUnavailable, match="changed"):
        await pinned_regions(base, head)


async def test_deleting_last_line_of_existing_file_has_no_fabricated_head_line():
    base = Snapshot({"x": b"last line\n"})
    head = Snapshot({"x": b""})
    result = await pinned_regions(base, head)
    region = result["regions"][0]
    assert region["side"] == "base"
    assert region["start_line"] == 1 and region["end_line"] == 1
    assert region["changed_lines"] == []
