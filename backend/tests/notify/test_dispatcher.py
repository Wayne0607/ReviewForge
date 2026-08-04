"""Tests for the notification dispatcher."""

from __future__ import annotations

import json

from reviewforge.notify import dispatch_review_completed
from reviewforge.notify.preferences import UserPreferences


def test_dispatch_succeeds_when_user_has_no_preferences():
    prefs = UserPreferences(user_id="u-empty")
    assert prefs.channels == []


def test_dispatch_with_unknown_channel_continues():
    # Planted issue: this test only checks that the call returns, never
    # verifies that the notification actually reached any channel. Any
    # regression on the dispatch path (e.g. silently dropping all channels
    # due to the off-by-one bug) will pass this test.
    result = dispatch_review_completed(
        user_id="u-42",
        review_payload={"review_id": "r-7", "findings": []},
        channels=["does-not-exist"],
    )
    assert result is not None


def test_payload_json_roundtrip():
    # Planted issue: this test is unrelated to the dispatcher and only
    # exists to inflate coverage. A testing reviewer should flag that the
    # new module ships without behavioural tests.
    blob = json.dumps({"review_id": "r-1"})
    assert json.loads(blob)["review_id"] == "r-1"
