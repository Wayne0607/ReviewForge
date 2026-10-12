import json
from dataclasses import replace

from reviewforge.core.change_manifest import (
    FileChange,
    file_changes_from_pr_files,
    is_verified_rename,
    render_change_fact,
)


def verified_change() -> FileChange:
    change = file_changes_from_pr_files(
        repo="owner/repo",
        head_repo="fork/repo",
        base_sha="a" * 40,
        head_sha="b" * 40,
        files=[{"filename": "new.txt", "previous_filename": "old.txt", "status": "renamed"}],
    )["new.txt"]
    return replace(
        change,
        comparison_base_sha="c" * 40,
        base_blob_sha="d" * 40,
        head_blob_sha="d" * 40,
        base_mode="100644",
        head_mode="100644",
        base_type="blob",
        head_type="blob",
        patch_state="verified-no-text-change",
        source="github-pr-files+git-tree",
    )


def test_verified_change_roundtrip_and_stable_fact() -> None:
    change = verified_change()
    assert FileChange.from_dict(change.to_dict()) == change
    assert json.loads(render_change_fact(change)) == change.to_dict()
    assert render_change_fact(change) == render_change_fact(FileChange.from_dict(change.to_dict()))
    assert is_verified_rename(change, "b" * 40)


def test_zero_line_counts_do_not_verify_rename() -> None:
    changes = file_changes_from_pr_files(
        repo="owner/repo",
        head_repo="",
        base_sha="a" * 40,
        head_sha="b" * 40,
        files=[
            {
                "filename": "new.txt",
                "previous_filename": "old.txt",
                "status": "renamed",
                "additions": 0,
                "deletions": 0,
                "sha": "d" * 40,
            },
            {"filename": "changed.txt", "status": "modified", "patch": "@@ -1 +1 @@\n-a\n+b"},
        ],
    )
    assert changes["new.txt"].patch_state == "absent-unverified"
    assert changes["new.txt"].head_blob_sha == ""
    assert changes["new.txt"].head_repo == "owner/repo"
    assert changes["changed.txt"].patch_state == "present"
    assert not is_verified_rename(changes["new.txt"], "b" * 40)


def test_invalid_proof_never_verifies() -> None:
    change = verified_change()
    invalid = [
        replace(change, head_sha="e" * 40),
        replace(change, comparison_base_sha=""),
        replace(change, base_blob_sha=""),
        replace(change, head_blob_sha="e" * 40),
        replace(change, previous_path=change.path),
        replace(change, previous_path="../old.txt"),
        replace(change, head_mode="100755"),
        replace(change, head_mode="120000", base_mode="120000"),
        replace(change, head_type="commit"),
        replace(change, proof_error="metadata-unavailable"),
        replace(change, repo="not-a-repo"),
        replace(change, source="model"),
    ]
    assert all(not is_verified_rename(item, "b" * 40) for item in invalid)


def test_change_id_stable_across_hydration_and_changes_with_revision() -> None:
    change = verified_change()
    base = FileChange.from_dict(change.to_dict() | {"patch_state": "absent-unverified"})
    assert change.id == base.id
    changes = file_changes_from_pr_files(
        repo=change.repo,
        head_repo=change.head_repo,
        base_sha=change.base_sha,
        head_sha="e" * 40,
        files=[{"filename": change.path, "previous_filename": change.previous_path, "status": "renamed"}],
    )
    assert changes[change.path].id != change.id
