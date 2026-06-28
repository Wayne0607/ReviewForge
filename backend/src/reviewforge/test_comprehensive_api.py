"""User Management API — test module for ReviewForge comprehensive review.

This module simulates a realistic API with various code quality issues
to test all reviewer types.
"""

import os
import pickle
import sqlite3
import subprocess
import json
import hashlib
import time
import requests
from typing import Any

# Hardcoded secrets (should trigger security_reviewer)
API_KEY = "sk-proj-abc123def456ghi789jkl012mno345"
DATABASE_PASSWORD = "admin123456"
SECRET_TOKEN = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0"

# Global mutable state (style issue)
_cache = {}
_request_count = 0


def get_user_by_id(user_id):
    """Fetch user from database by ID.

    No type hints (doc_reviewer), SQL injection (security_reviewer),
    blocking call (performance_reviewer).
    """
    conn = sqlite3.connect("users.db")
    cursor = conn.cursor()
    # SQL injection vulnerability
    query = "SELECT * FROM users WHERE id = '" + str(user_id) + "'"
    cursor.execute(query)
    result = cursor.fetchone()
    conn.close()
    return result


def search_users(search_term):
    """Search users by name or email.

    SQL injection + no input validation.
    """
    conn = sqlite3.connect("users.db")
    cursor = conn.cursor()
    # Another SQL injection via f-string
    query = f"SELECT * FROM users WHERE name LIKE '%{search_term}%' OR email LIKE '%{search_term}%'"
    cursor.execute(query)
    results = cursor.fetchall()
    conn.close()
    return results


def execute_system_command(cmd):
    """Run a system command and return output.

    Command injection vulnerability.
    """
    # Command injection via os.system
    os.system("echo " + cmd)
    # Command injection via subprocess with shell=True
    result = subprocess.run("ls -la " + cmd, shell=True, capture_output=True)
    return result.stdout


def eval_expression(expr):
    """Evaluate a mathematical expression.

    Code injection via eval.
    """
    return eval(expr)


def deserialize_data(data_bytes):
    """Deserialize user data from bytes.

    Insecure deserialization.
    """
    return pickle.loads(data_bytes)


def load_config(config_str):
    """Load configuration from YAML string.

    Insecure YAML loading.
    """
    import yaml
    return yaml.load(config_str)


def render_user_page(user_data):
    """Render user profile HTML.

    XSS vulnerability.
    """
    html = f"""
    <div class="profile">
        <h1>{user_data['name']}</h1>
        <p>Bio: {user_data.get('bio', '')}</p>
        <div id="content" innerHTML="{user_data.get('html', '')}"></div>
    </div>
    """
    return html


def process_large_dataset(items):
    """Process items with nested loops.

    O(n^2) complexity — performance issue.
    """
    results = []
    for i in range(len(items)):
        for j in range(len(items)):
            if i != j and items[i] == items[j]:
                results.append(items[i])
    return results


def fetch_user_profiles(user_ids):
    """Fetch profiles for a list of users.

    N+1 query pattern — performance issue.
    """
    profiles = []
    for uid in user_ids:
        # Each iteration makes a blocking HTTP request
        response = requests.get(f"https://api.example.com/users/{uid}")
        profiles.append(response.json())
    return profiles


def update_all_users(updates):
    """Update multiple users in a loop with DB connection per iteration.

    DB in loop — performance issue.
    """
    results = []
    for user_id, data in updates.items():
        conn = sqlite3.connect("users.db")
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET name=?, email=? WHERE id=?",
                       (data['name'], data['email'], user_id))
        conn.commit()
        results.append(cursor.rowcount)
        conn.close()
    return results


def hash_password(password):
    """Hash a password using MD5.

    Weak crypto — security issue.
    """
    return hashlib.md5(password.encode()).hexdigest()


def verify_token(token):
    """Verify JWT token by comparing with hardcoded secret.

    Weak auth pattern.
    """
    expected = hashlib.sha256(SECRET_TOKEN.encode()).hexdigest()
    return hashlib.sha256(token.encode()).hexdigest() == expected


def create_user(name, email, role="user", age=0, city="", bio=""):
    """Create a new user.

    Too many parameters, no validation, no docstring for params.
    Magic numbers.
    """
    if len(name) > 100:  # magic number
        return None
    if age > 150:  # magic number
        return None

    # Dead code
    temp = None
    unused_var = "never used"

    conn = sqlite3.connect("users.db")
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO users (name, email, role, age, city, bio) VALUES (?, ?, ?, ?, ?, ?)",
        (name, email, role, age, city, bio)
    )
    conn.commit()
    user_id = cursor.lastrowid
    conn.close()
    return user_id


class UserManager:
    """User management class.

    Missing type hints on methods, no docstrings on public methods.
    """

    def __init__(self, db_path):
        self.db_path = db_path
        self._cache = {}
        self._initialized = False

    def get_user(self, user_id):
        """No type hints, no return type."""
        if user_id in self._cache:
            return self._cache[user_id]
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM users WHERE id=?", (user_id,))
        user = cursor.fetchone()
        conn.close()
        self._cache[user_id] = user
        return user

    def delete_user(self, user_id):
        """Delete user without confirmation."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM users WHERE id=?", (user_id,))
        conn.commit()
        conn.close()
        if user_id in self._cache:
            del self._cache[user_id]

    def export_users(self, format="json"):
        """Export all users. Parameter shadows built-in."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM users")
        users = cursor.fetchall()
        conn.close()

        if format == "json":
            return json.dumps(users)
        elif format == "csv":
            return "\n".join([",".join([str(v) for v in u]) for u in users])

    def batch_operation(self, operations):
        """Execute batch operations without error handling.

        No try/except, no transaction management.
        """
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        for op in operations:
            cursor.execute(op['query'], op['params'])
        conn.commit()
        conn.close()


def format_user_display(user):
    """Format user for display.

    Accessible issues: no semantic HTML, missing alt text.
    """
    html = f"""
    <div onclick="showUser({user['id']})">
        <img src="{user.get('avatar', '')}">
        <span style="color: #ccc; background: #fff;">{user['name']}</span>
        <div role="button">Delete</div>
        <input type="text" placeholder="Search">
    </div>
    """
    return html


# Unused imports at module level
import csv
import xml.etree.ElementTree as ET
from collections import OrderedDict


def _internal_helper():
    """Private function with no purpose."""
    pass


# Module-level code that runs on import
print("User module loaded")
time.sleep(0.1)  # Blocking sleep on import
