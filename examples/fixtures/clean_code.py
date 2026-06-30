"""Clean code — no security vulnerabilities.

This file tests false positive rate. The reviewer should report 0 findings.
"""

import hashlib
import json
import sqlite3
from typing import Any


def get_user(user_id: int) -> dict[str, Any] | None:
    """Fetch user by ID using parameterized query."""
    conn = sqlite3.connect("app.db")
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE id = ?", (user_id,))
    row = cursor.fetchone()
    if row:
        return {"id": row[0], "name": row[1], "email": row[2]}
    return None


def hash_data(data: str) -> str:
    """Hash data with SHA-256."""
    return hashlib.sha256(data.encode()).hexdigest()


def process_config(config_path: str) -> dict:
    """Load and validate config from a trusted path."""
    with open(config_path, "r") as f:
        config = json.load(f)
    required_keys = ["host", "port", "database"]
    for key in required_keys:
        if key not in config:
            raise ValueError(f"Missing required config key: {key}")
    return config


def format_greeting(name: str) -> str:
    """Format a greeting message."""
    safe_name = name.strip()[:100]
    return f"Hello, {safe_name}!"


def calculate_sum(numbers: list[int]) -> int:
    """Calculate sum of a list."""
    return sum(numbers)
