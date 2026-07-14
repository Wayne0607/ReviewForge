from reviewforge.engine.detectors.unified_diff import iter_added_lines


def test_maps_multiple_hunks_with_context_and_deletions():
    patch = (
        "@@ -10,3 +20,4 @@ function_one\n"
        " context\n"
        "-removed\n"
        "+added_one\n"
        " context_two\n"
        "+added_two\n"
        "@@ -100,2 +200,3 @@ function_two\n"
        " context\n"
        "+added_three\n"
        " context_two\n"
    )

    assert iter_added_lines(patch) == [
        (21, "added_one"),
        (23, "added_two"),
        (201, "added_three"),
    ]


def test_maps_rename_patch_to_new_file_right_side():
    patch = (
        "diff --git a/old.py b/new.py\n"
        "similarity index 90%\n"
        "rename from old.py\n"
        "rename to new.py\n"
        "--- a/old.py\n"
        "+++ b/new.py\n"
        "@@ -7,2 +7,3 @@\n"
        " context\n"
        "+renamed_addition\n"
        " tail\n"
    )

    assert iter_added_lines(patch) == [(8, "renamed_addition")]


def test_keeps_mapped_prefix_of_truncated_hunk():
    patch = "@@ -50,4 +80,5 @@\n context\n+known_anchor\n"

    assert iter_added_lines(patch) == [(81, "known_anchor")]


def test_does_not_invent_lines_without_valid_hunk_coordinates():
    assert iter_added_lines("@@ fixture @@\n+not_anchored") == []
    assert iter_added_lines("+not_anchored") == []


def test_does_not_treat_file_headers_as_added_source():
    patch = "--- a/file.py\n+++ b/file.py\n@@ -1 +1 @@\n-old\n+new\n"

    assert iter_added_lines(patch) == [(1, "new")]
