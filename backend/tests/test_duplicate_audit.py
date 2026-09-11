from __future__ import annotations

from reviewforge.eval.duplicate_audit import duplicate_count


def test_duplicate_count_ignores_singletons() -> None:
    entries = [
        {"path": "a.py", "mechanism": "null-path"},
        {"path": "b.py", "mechanism": "null-path"},
    ]
    assert duplicate_count(entries) == 0


def test_duplicate_count_sums_extra_items_per_cluster() -> None:
    entries = [
        {"path": "a.py", "mechanism": "null-path"},
        {"path": "a.py", "mechanism": "null-path"},
        {"path": "a.py", "mechanism": "null-path"},
        {"path": "b.py", "mechanism": "missing-await"},
        {"path": "b.py", "mechanism": "missing-await"},
    ]
    assert duplicate_count(entries) == (3 - 1) + (2 - 1)


def test_duplicate_count_skips_malformed_entries() -> None:
    entries = [
        {"path": "a.py", "mechanism": "null-path"},
        {"path": "a.py", "mechanism": "null-path"},
        None,
        {"path": "", "mechanism": ""},
        "not-a-dict",
    ]
    assert duplicate_count(entries) == 1
