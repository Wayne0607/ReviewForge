"""Fixture (CLEAN): parameterized SQL — must NOT be flagged as sql-injection."""
import sqlite3


def get_user(db_path, user_id: int):
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    # Parameterized query — user input is bound, not concatenated. Safe.
    cur.execute("SELECT * FROM users WHERE id = ?", (user_id,))
    return cur.fetchall()
