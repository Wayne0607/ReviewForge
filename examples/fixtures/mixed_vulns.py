"""Mixed vulnerabilities: multiple types in one file for evaluation."""

import os
import pickle
import sqlite3
import subprocess

# Hardcoded secret
API_TOKEN = "ghp_abcdefghijklmnop1234567890"


def process_data(user_input, data_blob):
    """Multiple vulns: command injection + deserialization."""
    os.system("process " + user_input)  # command injection
    obj = pickle.loads(data_blob)  # insecure deserialization
    return obj


def search_users(query):
    """SQL injection."""
    conn = sqlite3.connect("app.db")
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE name LIKE '%" + query + "%'")
    return cursor.fetchall()


def run_report(cmd):
    """Command injection via subprocess."""
    return subprocess.check_output(cmd, shell=True)


def render_page(username):
    """XSS-like: unsanitized output (would be XSS in web context)."""
    return f"Welcome back, {username}!"
