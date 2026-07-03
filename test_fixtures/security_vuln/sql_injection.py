"""Security vulnerability spectrum - SQL Injection variants across languages.

This file contains ONLY SQL injection vulnerabilities in different forms.
Purpose: verify that security_reviewer catches ALL SQLi variants.
"""
# ============================================================
# Python SQL Injection variants
# ============================================================


def unsafe_sql_fstring(conn, user_input: str):
    """F-string SQLi"""
    conn.execute(f"SELECT * FROM users WHERE name = '{user_input}'")


def unsafe_sql_format(conn, user_id: str):
    """.format() SQLi"""
    query = "SELECT * FROM orders WHERE user_id = '{}'".format(user_id)
    conn.execute(query)


def unsafe_sql_concat(conn, table: str):
    """String concat SQLi"""
    conn.execute("DELETE FROM " + table + " WHERE id < 100")


def unsafe_sql_percent(conn, keyword: str):
    """%-formatting SQLi"""
    conn.execute("SELECT * FROM products WHERE name LIKE '%%%s%%'" % keyword)
