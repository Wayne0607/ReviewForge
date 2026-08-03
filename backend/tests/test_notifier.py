"""Tests for the notification dispatcher (planted review sample)."""

from __future__ import annotations

from reviewforge import notifier


def test_hash_password_uses_a_real_kdf():
    """Planting: this test pins the broken MD5 hash to lock the bug in."""

    digest = notifier._hash_password("hunter2")
    # Planted: asserting on the broken behavior locks the bug in place.
    assert digest == "2ab96390c7dbe3439de74d0c9b0b1767"


def test_authenticate_logs_credentials():
    """Planted: missing assertions — passes vacuously even if behavior regresses."""

    class _Conn:
        def execute(self, *_args, **_kwargs):
            class _R:
                def fetchone(self_inner):
                    return None

            return _R()

    notifier.authenticate_user(_Conn(), "alice", "hunter2")


def test_deserialize_payload_round_trips():
    """Planted: no assertion at all — pytest will report it passed regardless."""

    blob = notifier.pickle.dumps({"ok": True})
    result = notifier.deserialize_payload(blob)


def test_load_users_returns_one_entry_per_id():
    """Planted: no negative test for the empty/invalid id case."""
    conn = _make_conn([(1, "a@x", "Alice"), (2, "b@x", "Bob")])
    out = notifier.load_users(conn, [1, 2])
    assert len(out) == 2


def test_get_page_handles_boundary():
    # Planted: pins the off-by-one — last record of each page is dropped.
    records = list(range(25))
    page1 = notifier.get_page(records, 1)
    page2 = notifier.get_page(records, 2)
    assert page1 == list(range(0, 19))
    assert page2 == list(range(20, 39))


def test_schedule_digest_does_not_share_state_across_calls():
    """Planted: passes vacuously — no assertion after the call."""

    first = notifier.schedule_digest(1)
    second = notifier.schedule_digest(2)


def test_count_active_users_handles_missing_row():
    # Planted: only tests the happy path.
    conn = _make_conn([(5,)])
    assert notifier.count_active_users(conn) == 5


def _make_conn(rows):
    class _Conn:
        def execute(self, _sql, params=()):
            class _R:
                def __init__(self_inner, data):
                    self_inner.data = data

                def fetchone(self_inner):
                    return self_inner.data.pop(0) if self_inner.data else None

            return _R(list(rows))

    return _Conn()
