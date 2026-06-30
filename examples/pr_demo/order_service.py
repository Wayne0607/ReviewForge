"""Order service — dedicated test PR for escalation eval.

Ground truth (known by construction):
  VULN:  get_order (sql-injection), run_report (command-injection),
         calc_total (code-injection/eval), load_cart (insecure-deserialization),
         API_KEY (hardcoded-secrets)
  CLEAN: get_order_safe (parameterized — no sql-injection),
         run_report_safe (whitelist + shell=False — no command-injection)
"""

import os
import pickle
import sqlite3
import subprocess

API_KEY = "x7k9q2m4p8w1z5c3v6b0n8d2"  # VULN: hardcoded secret (generic, not a real provider key)


def get_order(db_path, order_id):
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("SELECT * FROM orders WHERE id = '" + order_id + "'")  # VULN: sql-injection
    return cur.fetchall()


def run_report(report_name):
    os.system("generate_report " + report_name)  # VULN: command-injection


def calc_total(formula):
    return eval(formula)  # VULN: code-injection


def load_cart(blob):
    return pickle.loads(blob)  # VULN: insecure-deserialization


# ---- deliberately safe (false-positive controls) ----

def get_order_safe(db_path, order_id: int):
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("SELECT * FROM orders WHERE id = ?", (order_id,))  # CLEAN: parameterized
    return cur.fetchall()


_ALLOWED_REPORTS = {"daily", "weekly", "monthly"}


def run_report_safe(report_name: str):
    if report_name not in _ALLOWED_REPORTS:  # CLEAN: whitelist + shell=False
        raise ValueError("unknown report")
    return subprocess.run(["generate_report", report_name], shell=False, capture_output=True)
