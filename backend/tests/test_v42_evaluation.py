from reviewforge.review_v42.evaluation import prepare_review_input, summarize_run


def test_review_input_excludes_golden():
    row = {
        "repo": "r",
        "base": "b",
        "head": "h",
        "description": "intent",
        "diff": "@@",
        "golden": [{"secret": "gold"}],
        "metadata": {"golden": "hidden"},
        "labels": ["bug"],
    }
    assert prepare_review_input(row) == {"repo": "r", "base": "b", "head": "h", "description": "intent", "diff": "@@"}


def test_partial_and_unknown_usage_are_not_quality_zero():
    result = summarize_run(
        {
            "status": "partial",
            "scopes": [{"status": "partial", "regions": [1, 2], "checked_regions": [1]}],
            "verified": [{"verdict": "accept"}],
            "comments": [],
        },
        [{"usage": {"input_tokens": 5, "output_tokens": 3}}, {"usage": None}],
    )
    assert result["status"] == "partial" and result["quality"] is None
    assert result["usage"]["unknown_requests"] == 1 and result["usage"]["input_tokens"] == 5
    assert result["funnel"]["verdicts"]["accept"] == 1 and result["coverage"]["checked_regions"] == 1


def test_superseded_parent_is_not_double_counted_in_coverage():
    report = {
        "scopes": [
            {"status": "superseded", "regions": [1, 2], "checked_regions": []},
            {"status": "reviewed", "regions": [1], "checked_regions": [1]},
            {"status": "reviewed", "regions": [2], "checked_regions": [2]},
        ]
    }
    coverage = summarize_run(report, [])["coverage"]
    assert coverage == {"scopes": 2, "reviewed": 2, "regions": 2, "checked_regions": 2}
