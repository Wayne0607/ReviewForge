"""Cross-PR positive control: calls a helper made risky in another PR."""

import sqlite3

from cross_pr_live.risky_ops import run_report_query


def export_report(conn: sqlite3.Connection, account_id: str) -> list[tuple]:
    return run_report_query(conn, "reports", account_id)
