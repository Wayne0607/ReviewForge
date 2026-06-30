"""Core DB helpers (PR-A of the cross-PR demo).

Introduces two risky primitives that later PRs import and call:
  run_query  -> sql-injection (string-concatenated query)
  cache_load -> insecure-deserialization (pickle.loads)
"""

import pickle
import sqlite3


def connect(path="app.db"):
    return sqlite3.connect(path)


def run_query(conn, table, raw_filter):
    cur = conn.cursor()
    # VULN: table + filter concatenated straight into SQL → injection
    cur.execute("SELECT * FROM " + table + " WHERE " + raw_filter)
    return cur.fetchall()


def cache_load(blob):
    # VULN: deserializing untrusted bytes with pickle → RCE
    return pickle.loads(blob)
