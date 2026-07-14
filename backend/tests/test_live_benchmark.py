from reviewforge.eval.live_benchmark import score_live_benchmark


def _manifest():
    return {
        "prs": [
            {
                "pr_number": 10,
                "name": "positive",
                "changed_lines": 20,
                "issues": [
                    {
                        "id": "sql-1",
                        "file": "svc.py",
                        "line_start": 10,
                        "line_end": 11,
                        "category": "sql-injection",
                        "language": "python",
                    }
                ],
            },
            {"pr_number": 11, "name": "clean", "clean": True, "changed_lines": 50, "issues": []},
        ]
    }


def test_one_to_one_line_aware_scoring_and_clean_rate():
    findings = [
        {"pr_number": 10, "file": "svc.py", "line": 13, "category": "sql-injection"},
        {"pr_number": 10, "file": "svc.py", "line": 10, "category": "sql-injection"},
        {"pr_number": 11, "file": "ui.tsx", "line": 5, "category": "xss"},
        {"pr_number": 11, "file": "ignored.py", "line": 1, "category": "xss", "status": "false_positive"},
    ]
    result = score_live_benchmark(_manifest(), findings, {"10": 900, "11": 100}, line_tolerance=2)

    assert result["true_positives"] == 1
    assert result["false_positives"] == 2
    assert result["false_negatives"] == 0
    assert result["precision"] == 0.3333
    assert result["recall"] == 1.0
    assert result["clean_false_positives"] == 1
    assert result["clean_fp_per_100_changed_lines"] == 2.0
    assert result["token_total"] == 1000
    assert result["tokens_per_true_positive"] == 1000.0


def test_cross_pr_alias_and_missed_issue():
    manifest = {
        "prs": [
            {
                "pr_number": 12,
                "changed_lines": 10,
                "issues": [
                    {
                        "id": "cross-1",
                        "file": "consumer.py",
                        "line": 8,
                        "category": "cross-pr-sql-injection",
                        "accepted_categories": ["sql-injection"],
                    },
                    {"id": "cmd-1", "file": "consumer.py", "line": 9, "category": "command-injection"},
                ],
            }
        ]
    }
    result = score_live_benchmark(
        manifest,
        [{"pr": 12, "path": "consumer.py", "new_line": 8, "category": "sql-injection"}],
        [{"pr_number": 12, "total_tokens": 250}],
    )
    assert result["true_positives"] == 1
    assert result["false_negatives"] == 1
    assert result["per_category"]["command-injection"]["false_negatives"] == 1
    assert result["tokens_by_pr"] == {"12": 250}
