"""Vulnerable sample — contains multiple security issues for A/B evaluation."""

import os
import pickle
import sqlite3
import subprocess

# Hardcoded secrets
API_KEY = "sk-proj-abc123def456ghi789jkl012mno345"
DATABASE_PASSWORD = "admin123456"


def execute_command(user_input):
    """Command injection via os.system."""
    os.system("echo " + user_input)


def run_shell(cmd):
    """Command injection via subprocess."""
    result = subprocess.run("ls " + cmd, shell=True, capture_output=True)
    return result.stdout


def eval_expression(expr):
    """Code injection via eval."""
    return eval(expr)


def deserialize(data):
    """Insecure deserialization via pickle."""
    return pickle.loads(data)


def query_db(user_id):
    """SQL injection via string concatenation."""
    conn = sqlite3.connect("app.db")
    cursor = conn.cursor()
    query = "SELECT * FROM users WHERE id = '" + str(user_id) + "'"
    cursor.execute(query)
    return cursor.fetchall()


def hash_password(password):
    """Weak crypto — MD5."""
    import hashlib
    return hashlib.md5(password.encode()).hexdigest()
