"""Database query helpers."""
import sqlite3


def parameterized_query(conn: sqlite3.Connection, user_id: int) -> list:
    """Look up a user by numeric identifier."""
    cursor = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,))
    return cursor.fetchall()


def named_parameter_query(conn: sqlite3.Connection, name: str) -> list:
    """Look up users by name."""
    cursor = conn.execute(
        "SELECT * FROM users WHERE name = :name",
        {"name": name}
    )
    return cursor.fetchall()


def orm_style_query(conn: sqlite3.Connection, email: str) -> list:
    """Look up users by email address."""
    return conn.execute(
        "SELECT * FROM users WHERE email = ?", (email,)
    ).fetchall()


def query_with_escaping(conn: sqlite3.Connection, search: str) -> list:
    """Search product names with escaped wildcard characters."""
    safe_search = search.replace("%", "\\%").replace("_", "\\_")
    return conn.execute(
        "SELECT * FROM products WHERE name LIKE ? ESCAPE '\\'",
        (f"%{safe_search}%",)
    ).fetchall()


def build_user_query(conn: sqlite3.Connection, fields: dict) -> list:
    """Build a query from an allowlisted set of fields."""
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
