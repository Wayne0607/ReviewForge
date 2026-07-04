"""Intentional false-positive control for parameterized SQL."""

import sqlite3


def load_user(conn: sqlite3.Connection, user_id: str) -> tuple | None:
    cursor = conn.execute("SELECT id, email FROM users WHERE id = ?", (user_id,))
    return cursor.fetchone()
