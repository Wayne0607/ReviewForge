"""Tests for state store."""

from reviewforge.core.state import Finding, ReviewTask, StateStore


def test_state_add_finding():
    state = StateStore()
    f = Finding(file="test.py", line=10, severity="warning", message="test finding")
    fid = state.add_finding(f)
    assert fid.startswith("finding_")
    assert len(state.findings) == 1


def test_finding_persists_validated_line_coercion():
    finding = Finding(file="test.py", line="123", message="normalized")  # type: ignore[arg-type]

    assert finding.line == 123
    assert isinstance(finding.line, int)


def test_state_update_persists_normalized_provider_values():
    state = StateStore()
    finding = Finding(file="test.py", line=1, message="normalized update")
    state.add_finding(finding)

    state.update_finding(
        finding.id,
        line="42",
        verify_reason="证" * 700,
    )

    updated = state.findings[finding.id]
    assert updated.line == 42
    assert isinstance(updated.line, int)
    assert updated.verify_reason == "证" * 500


def test_state_deep_copy_isolation():
    state = StateStore()
    f = Finding(file="test.py", line=10, message="original")
    state.add_finding(f)

    # Get a deep copy
    copy = state.get_finding(f.id)
    copy.message = "modified"

    # Original should be unchanged
    assert state.findings[f.id].message == "original"


def test_state_list_by_status():
    state = StateStore()
    state.add_finding(Finding(file="a.py", status="candidate", message="finding a"))
    state.add_finding(Finding(file="b.py", status="confirmed", message="finding b"))
    state.add_finding(Finding(file="c.py", status="candidate", message="finding c"))

    candidates = state.list_findings(status="candidate")
    assert len(candidates) == 2

    confirmed = state.list_findings(status="confirmed")
    assert len(confirmed) == 1


def test_state_notes_consumed():
    from reviewforge.core.state import Note

    state = StateStore()
    state.add_note(Note(from_agent="reviewer", type="gap", content="need more info"))
    state.add_note(Note(from_agent="reviewer", type="gap", content="another"))

    notes = state.consume_notes()
    assert len(notes) == 2

    # Second call should be empty
    notes2 = state.consume_notes()
    assert len(notes2) == 0


def test_state_snapshot():
    state = StateStore(
        pr_number=42,
        repo="owner/repo",
        pr_title="Add review context",
        pr_body="Carries signed webhook metadata.",
        head_repo="contributor/fork",
        head_ref="feature/review-context",
        linked_issues=[{"number": 123}],
    )
    state.add_finding(Finding(file="test.py", status="candidate", message="test finding"))
    state.add_task(ReviewTask(reviewer="security_reviewer"))

    snap = state.snapshot()
    assert snap["pr_number"] == 42
    assert snap["pr_title"] == "Add review context"
    assert snap["pr_body"] == "Carries signed webhook metadata."
    assert snap["head_repo"] == "contributor/fork"
    assert snap["head_ref"] == "feature/review-context"
    assert snap["linked_issues"] == [{"number": 123}]
    snap["linked_issues"][0]["number"] = 999
    assert state.linked_issues == [{"number": 123}]
    assert len(snap["findings"]) == 1
    assert len(snap["tasks"]) == 1


def test_state_pr_context_defaults_keep_existing_callers_compatible():
    state = StateStore(pr_number=42, repo="owner/repo", head_sha="head")

    assert state.pr_title == ""
    assert state.pr_body == ""
    assert state.head_repo == ""
    assert state.head_ref == ""
    assert state.linked_issues == []

    snap = state.snapshot()
    assert snap["pr_title"] == ""
    assert snap["pr_body"] == ""
    assert snap["head_repo"] == ""
    assert snap["head_ref"] == ""
    assert snap["linked_issues"] == []


def test_state_pr_context_fields_do_not_shift_legacy_positional_arguments():
    state = StateStore(42, "owner/repo", "head", "base", ["src/app.py"], "diff")

    assert state.files_changed == ["src/app.py"]
    assert state.diff_summary == "diff"
    assert state.pr_title == ""
