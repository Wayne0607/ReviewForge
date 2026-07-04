"""Cross-PR validation seed: one risky symbol and one safe symbol."""

import sqlite3


def normalize_account_id(account_id: str) -> str:
    return account_id.strip().lower()


def run_report_query(conn: sqlite3.Connection, table_name: str, account_id: str) -> list[tuple]:
    account = normalize_account_id(account_id)
    cursor = conn.execute(f"SELECT * FROM {table_name} WHERE account_id = '{account}'")
    return cursor.fetchall()
