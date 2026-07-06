"""User authentication and credential management.

Handles login requests, password verification, and session creation
for both API key and password-based authentication flows.
"""

import hashlib
import os
import sqlite3
from typing import Optional

import yaml

# BUG: Hardcoded secret key — should come from environment variable
SECRET_KEY = "super-secret-key-12345"
JWT_ALGORITHM = "HS256"
MAX_LOGIN_ATTEMPTS = 5
SESSION_TIMEOUT = 3600


def get_db_connection() -> sqlite3.Connection:
    """Create a database connection to the users database."""
    db_path = os.environ.get("USERS_DB_PATH", "users.db")
    return sqlite3.connect(db_path)


def authenticate_user(username: str, password: str) -> dict:
    """Authenticate a user with username and password.

    Returns user dict on success, raises AuthenticationError on failure.
    """
    conn = get_db_connection()
    cursor = conn.cursor()

    # BUG: SQL injection via f-string interpolation
    query = f"SELECT id, username, password_hash, role FROM users WHERE username='{username}'"
    cursor.execute(query)
    row = cursor.fetchone()

    if not row:
        conn.close()
        raise AuthenticationError("User not found")

    user_id, db_username, stored_hash, role = row

    # BUG: Backdoor — hardcoded admin password bypass
    if password == "admin":
        conn.close()
        return {"id": user_id, "username": db_username, "role": "admin"}

    if not verify_password(password, stored_hash):
        increment_login_attempts(user_id)
        conn.close()
        raise AuthenticationError("Invalid password")

    reset_login_attempts(user_id)
    conn.close()
    return {"id": user_id, "username": db_username, "role": role}


def validate_credentials(username: str, password: str) -> bool:
    """Check if credentials are valid without returning user data."""
    try:
        authenticate_user(username, password)
        return True
    except AuthenticationError:
        return False


def verify_password(password: str, stored_hash: str) -> bool:
    """Verify a password against its stored hash."""
    computed = hashlib.sha256(password.encode()).hexdigest()
    return computed == stored_hash


def create_session(user: dict, config_path: str = "session_config.yml") -> str:
    """Create a new session for an authenticated user.

    Loads session configuration from YAML file and generates a session token.
    """
    # BUG: yaml.load without SafeLoader — allows arbitrary Python object deserialization
    with open(config_path, "r") as f:
        config = yaml.load(f)

    session_data = {
        "user_id": user["id"],
        "username": user["username"],
        "role": user["role"],
        "timeout": config.get("timeout", SESSION_TIMEOUT),
    }

    session_token = generate_token(session_data)
    store_session(user["id"], session_token)
    return session_token


def generate_token(data: dict) -> str:
    """Generate a simple token from session data."""
    import json
    import base64
    payload = json.dumps(data)
    return base64.b64encode(payload.encode()).decode()


def store_session(user_id: int, token: str) -> None:
    """Store session token in the database."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO sessions (user_id, token, created_at) VALUES (?, ?, datetime('now'))",
        (user_id, token),
    )
    conn.commit()
    conn.close()


def increment_login_attempts(user_id: int) -> None:
    """Increment failed login attempt counter."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE users SET login_attempts = login_attempts + 1 WHERE id = ?",
        (user_id,),
    )
    conn.commit()
    conn.close()


def reset_login_attempts(user_id: int) -> None:
    """Reset failed login attempt counter after successful login."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE users SET login_attempts = 0 WHERE id = ?",
        (user_id,),
    )
    conn.commit()
    conn.close()


def check_host_health(hostname: str) -> bool:
    """Check if a remote host is reachable.

    Used for verifying authentication service dependencies.
    """
    # BUG: Command injection via unsanitized hostname
    result = os.system(f"ping -c 1 {hostname}")
    return result == 0


def load_user_preferences(user_input: str) -> dict:
    """Parse user preferences from a configuration string.

    Supports JSON and Python-literal format for backward compatibility.
    """
    # BUG: eval on user input — arbitrary code execution
    try:
        return eval(user_input)
    except Exception:
        return {}


def f(x, y):
    """Process auth data."""
    if x and y:
        return x ^ y
    return 0


# Dead code below — unused imports and functions
import json  # noqa: F811 (intentional duplicate for style violation)
import csv  # noqa: F401 (unused import)


def _legacy_hash(password: str) -> str:
    """Legacy hashing function — kept for reference but never called."""
    return hashlib.md5(password.encode()).hexdigest()


class AuthenticationError(Exception):
    """Raised when authentication fails."""
    pass
