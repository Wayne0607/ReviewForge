"""Tests for deterministic bounded slicing of oversized correctness_reviewer tasks."""

from reviewforge.core.state import ReviewTask
from reviewforge.engine.orchestrator import (
    _SLICE_MAX_DIFF_CHARS,
    _SLICE_MAX_FILES,
    _split_oversized_correctness_tasks,
)

# ── helpers ──────────────────────────────────────────────────────────────


def _task(reviewer: str = "correctness_reviewer", files: list[str] | None = None, rationale: str = "r") -> ReviewTask:
    return ReviewTask(reviewer=reviewer, files=files or [], rationale=rationale)


def _diffs(spec: dict[str, str]) -> dict[str, str]:
    """Build a file_diffs dict; missing keys mean no patch loaded."""
    return spec


def _char_cost(file_path: str, patch: str) -> int:
    return len(f"### {file_path}\n") + len(patch)


# ── identity: small correctness task unchanged ───────────────────────────


def test_small_correctness_task_unchanged():
    """A correctness task within both budgets is returned as-is (same id)."""
    task = _task(files=["a.py", "b.py"])
    diffs = _diffs({"a.py": "small", "b.py": "patch"})
    result = _split_oversized_correctness_tasks([task], diffs)
    assert result == [task]
    assert result[0] is task  # same object, not a copy


# ── non-correctness never split ──────────────────────────────────────────


def test_non_correctness_reviewer_unchanged():
    """A non-correctness reviewer is never split regardless of size."""
    files = [f"f{i}.py" for i in range(20)]
    diffs = _diffs({f"f{i}.py": "x" * 5000 for i in range(20)})
    task = _task(reviewer="security_reviewer", files=files)
    result = _split_oversized_correctness_tasks([task], diffs)
    assert result == [task]
    assert result[0] is task


# ── file-count split ─────────────────────────────────────────────────────


def test_file_count_split():
    """More than _SLICE_MAX_FILES files triggers a file-count split."""
    files = [f"f{i}.py" for i in range(_SLICE_MAX_FILES + 3)]
    diffs = _diffs({f: "tiny" for f in files})
    task = _task(files=files)
    result = _split_oversized_correctness_tasks([task], diffs)
    assert len(result) == 2
    assert result[0].reviewer == "correctness_reviewer"
    assert result[1].reviewer == "correctness_reviewer"
    assert len(result[0].files) == _SLICE_MAX_FILES
    assert len(result[1].files) == 3
    # All original files preserved in order.
    reconstructed = result[0].files + result[1].files
    assert reconstructed == files


# ── char-budget split ────────────────────────────────────────────────────


def test_char_budget_split():
    """Files within file-count limit but over char budget split correctly."""
    big_patch = "x" * _SLICE_MAX_DIFF_CHARS
    files = ["big.py", "small.py"]
    diffs = _diffs({"big.py": big_patch, "small.py": "ok"})
    task = _task(files=files)
    result = _split_oversized_correctness_tasks([task], diffs)
    assert len(result) == 2
    assert result[0].files == ["big.py"]
    assert result[1].files == ["small.py"]
    assert result[0].id != result[1].id


def test_oversized_single_file_stays_as_one_chunk():
    """A single file exceeding the char budget becomes a one-file chunk."""
    huge = "x" * (_SLICE_MAX_DIFF_CHARS * 2)
    task = _task(files=["huge.py"])
    diffs = _diffs({"huge.py": huge})
    result = _split_oversized_correctness_tasks([task], diffs)
    assert len(result) == 1
    assert result[0].files == ["huge.py"]
    # Same id because no splitting was needed (single file, single chunk).
    assert result[0].id == task.id


# ── preservation / no duplicates ─────────────────────────────────────────


def test_all_files_preserved_no_duplicates():
    """Every original file appears exactly once across all chunks, in order."""
    files = [f"f{i}.py" for i in range(_SLICE_MAX_FILES * 3)]
    diffs = _diffs({f: "data" for f in files})
    task = _task(files=files)
    result = _split_oversized_correctness_tasks([task], diffs)
    reconstructed = [f for chunk in result for f in chunk.files]
    assert reconstructed == files
    assert len(reconstructed) == len(set(reconstructed))  # no duplicates


# ── rationale and distinct IDs ───────────────────────────────────────────


def test_rationale_preserved_and_ids_distinct():
    """Chunks carry the original rationale and have distinct IDs."""
    rationale = "observe behavioral correctness"
    files = [f"f{i}.py" for i in range(_SLICE_MAX_FILES + 1)]
    diffs = _diffs({f: "p" for f in files})
    task = _task(files=files, rationale=rationale)
    result = _split_oversized_correctness_tasks([task], diffs)
    assert len(result) == 2
    ids = {t.id for t in result}
    assert len(ids) == 2
    for t in result:
        assert t.rationale == rationale
        assert t.reviewer == "correctness_reviewer"


# ── missing/empty patches count toward file-count limit ──────────────────


def test_missing_patches_still_count_for_file_limit():
    """Files with no loaded patch (empty string) still trigger file-count split."""
    files = [f"f{i}.py" for i in range(_SLICE_MAX_FILES + 2)]
    task = _task(files=files)
    result = _split_oversized_correctness_tasks([task], {})
    assert len(result) == 2
    assert len(result[0].files) == _SLICE_MAX_FILES
    assert len(result[1].files) == 2


# ── mixed reviewers in same batch ────────────────────────────────────────


def test_mixed_reviewers_only_correctness_splits():
    """Only correctness_reviewer tasks are split; others pass through."""
    files_correctness = [f"c{i}.py" for i in range(_SLICE_MAX_FILES + 1)]
    files_security = [f"s{i}.py" for i in range(_SLICE_MAX_FILES + 1)]
    diffs = _diffs({f: "p" for f in files_correctness + files_security})
    correctness_task = _task(reviewer="correctness_reviewer", files=files_correctness)
    security_task = _task(reviewer="security_reviewer", files=files_security)
    result = _split_oversized_correctness_tasks([correctness_task, security_task], diffs)
    # correctness split into 2, security unchanged
    assert len(result) == 3
    assert result[0].files == files_correctness[:_SLICE_MAX_FILES]
    assert result[1].files == files_correctness[_SLICE_MAX_FILES:]
    assert result[2] is security_task


# ── exact boundary: exactly at limits should NOT split ───────────────────


def test_exact_file_limit_no_split():
    """Exactly _SLICE_MAX_FILES files with small patches does not split."""
    files = [f"f{i}.py" for i in range(_SLICE_MAX_FILES)]
    diffs = _diffs({f: "x" for f in files})
    task = _task(files=files)
    result = _split_oversized_correctness_tasks([task], diffs)
    assert result == [task]
    assert result[0] is task


def test_exact_char_limit_no_split():
    """Total cost exactly at _SLICE_MAX_DIFF_CHARS does not split."""
    # Two files, each costing half the budget.
    per_file_budget = _SLICE_MAX_DIFF_CHARS // 2
    section_overhead_a = len("### a.py\n\n\n")
    section_overhead_b = len("### b.py\n\n\n")
    patch_a = "x" * (per_file_budget - section_overhead_a)
    patch_b = "x" * (per_file_budget - section_overhead_b)
    diffs = _diffs({"a.py": patch_a, "b.py": patch_b})
    task = _task(files=["a.py", "b.py"])
    result = _split_oversized_correctness_tasks([task], diffs)
    assert result == [task]


# ── empty input ──────────────────────────────────────────────────────────


def test_empty_task_list():
    assert _split_oversized_correctness_tasks([], {}) == []


def test_empty_files_pass_through():
    """A correctness task with no files is not split (it will just do nothing)."""
    task = _task(files=[])
    result = _split_oversized_correctness_tasks([task], {})
    assert result == [task]
    assert result[0] is task


# ── order preservation across multiple tasks ─────────────────────────────


def test_task_order_preserved():
    """Split and unsplit tasks appear in their original relative order."""
    big_files = [f"big{i}.py" for i in range(_SLICE_MAX_FILES + 2)]
    small_task = _task(reviewer="security_reviewer", files=["tiny.py"])
    big_task = _task(reviewer="correctness_reviewer", files=big_files)
    diffs = _diffs({f: "p" for f in big_files + ["tiny.py"]})
    result = _split_oversized_correctness_tasks([small_task, big_task], diffs)
    # small_task first (unchanged), then split big_task chunks
    assert result[0] is small_task
    assert result[1].reviewer == "correctness_reviewer"
    assert result[2].reviewer == "correctness_reviewer"
