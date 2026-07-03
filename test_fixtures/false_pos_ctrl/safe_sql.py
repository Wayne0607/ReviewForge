"""Safe SQL patterns — should NOT produce SQL injection findings.

Purpose: verify the reviewer does NOT flag parameterized queries or safe patterns.
"""
import sqlite3


def parameterized_query(conn: sqlite3.Connection, user_id: int) -> list:
    """Parameterized query — SAFE, no SQL injection."""
    cursor = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,))
    return cursor.fetchall()


def named_parameter_query(conn: sqlite3.Connection, name: str) -> list:
    """Named parameter query — SAFE."""
    cursor = conn.execute(
        "SELECT * FROM users WHERE name = :name",
        {"name": name}
    )
    return cursor.fetchall()


def orm_style_query(conn: sqlite3.Connection, email: str) -> list:
    """Using conn.execute with proper parameterization — SAFE."""
    return conn.execute(
        "SELECT * FROM users WHERE email = ?", (email,)
    ).fetchall()


def query_with_escaping(conn: sqlite3.Connection, search: str) -> list:
    """Properly escaped LIKE query — SAFE."""
    safe_search = search.replace("%", "\\%").replace("_", "\\_")
    return conn.execute(
        "SELECT * FROM products WHERE name LIKE ? ESCAPE '\\'",
        (f"%{safe_search}%",)
    ).fetchall()


def safe_builder_pattern(conn: sqlite3.Connection, fields: dict) -> list:
    """Safe query builder — each field is parameterized."""
    allowed = {"name", "email", "status"}
    conditions = []
    params = []
    for k, v in fields.items():
        if k in allowed:
            conditions.append(f"{k} = ?")
            params.append(v)
    if conditions:
        query = f"SELECT * FROM users WHERE {' AND '.join(conditions)}"
        return conn.execute(query, params).fetchall()
    return []
