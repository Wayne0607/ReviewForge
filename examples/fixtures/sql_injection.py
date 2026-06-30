"""SQL injection patterns for evaluation."""

import sqlite3


def get_user_unsafe(user_id):
    """String concatenation SQL injection."""
    conn = sqlite3.connect("app.db")
    cursor = conn.cursor()
    query = "SELECT * FROM users WHERE id = '" + str(user_id) + "'"
    cursor.execute(query)
    return cursor.fetchall()


def get_user_format(name):
    """f-string SQL injection."""
    conn = sqlite3.connect("app.db")
    cursor = conn.cursor()
    cursor.execute(f"SELECT * FROM users WHERE name = '{name}'")
    return cursor.fetchall()


def get_user_percent(email):
    """%-format SQL injection."""
    conn = sqlite3.connect("app.db")
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE email = '%s'" % email)
    return cursor.fetchall()


def get_user_safe(user_id):
    """Safe: parameterized query. Should NOT be flagged."""
    conn = sqlite3.connect("app.db")
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE id = ?", (user_id,))
    return cursor.fetchall()
