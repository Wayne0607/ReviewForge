"""Fixture: SQL injection via string concatenation."""
import sqlite3


def get_user(db_path, user_id):
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    # User input concatenated straight into the query → SQL injection.
    cur.execute("SELECT * FROM users WHERE id = '" + user_id + "'")
    return cur.fetchall()
