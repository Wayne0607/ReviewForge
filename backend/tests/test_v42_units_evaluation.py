"""Unit coverage reports the fixed manifest, not integration or duplicate ownership."""

from reviewforge.review_v42.evaluation import summarize_run


def test_units_coverage_does_not_count_integration_or_duplicate_owners():
    a = {"id": "a", "path": "a.py"}
    b = {"id": "b", "path": "b.py"}
    report = {
        "status": "partial",
        "run": {"policy": "v42-units-6", "metadata": {"changed_regions": [a, b]}},
        "scopes": [
            {"id": "one", "status": "reviewed", "regions": [a], "checked_regions": [a]},
            {"id": "two", "status": "reviewed", "regions": [b], "checked_regions": [b]},
            {"id": "duplicate", "status": "reviewed", "regions": [a], "checked_regions": [a]},
            {"id": "integration", "status": "reviewed", "regions": [], "checked_regions": []},
        ],
        "verified": [],
        "comments": [],
    }
    summary = summarize_run(report, [{"usage": None}])
    assert summary["coverage"] == {"scopes": 3, "reviewed": 3, "regions": 2, "checked_regions": 1}
    assert summary["quality"] is None
    assert summary["usage"]["unknown_requests"] == 1
