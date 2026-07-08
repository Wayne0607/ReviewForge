"""Additional security vulnerabilities: path traversal, SSRF, auth, secrets.

Purpose: verify all remaining security categories are covered.
"""
import hashlib
import os
import urllib.request


# ============================================================
# Path Traversal
# ============================================================

def read_user_file(base_dir: str, user_filename: str) -> bytes:
    """Read a user-specified file from a base directory."""
    path = os.path.join(base_dir, user_filename)
    with open(path, "rb") as f:  # BUG: path traversal
        return f.read()


def read_config(user_path: str) -> str:
    """Read config file with user-controlled path."""
    return open(user_path, "r").read()  # BUG: path traversal


# ============================================================
# SSRF (Server-Side Request Forgery)
# ============================================================

def fetch_webhook(user_url: str) -> str:
    """Fetch data from a user-supplied URL."""
    response = urllib.request.urlopen(user_url)  # BUG: SSRF
    return response.read().decode()


# ============================================================
# Hardcoded Secrets
# ============================================================

DATABASE_PASSWORD = "prod-db-password-2024!"  # BUG: hardcoded password
AWS_ACCESS_KEY = "AKIAIOSFODNN7EXAMPLE"  # BUG: hardcoded AWS key
STRIPE_SECRET = "sk_live_abc123xyz789"  # BUG: hardcoded Stripe key
JWT_SECRET = "my-super-secret-jwt-key-2024"  # BUG: hardcoded JWT secret
GITHUB_TOKEN = "ghp_abcdef1234567890abcdef1234567890"  # BUG: hardcoded GitHub token
OPENAI_API_KEY = "sk-proj-abcdef1234567890abcdef1234567890"  # BUG: hardcoded API key


# ============================================================
# Weak Cryptography
# ============================================================

def hash_password_weak(password: str) -> str:
    """Hash password with weak algorithm."""
    return hashlib.md5(password.encode()).hexdigest()  # BUG: weak crypto (MD5)


def encrypt_data(data: str) -> bytes:
    """Encrypt with hardcoded key."""
    key = b"1234567890123456"  # BUG: hardcoded encryption key
    from Crypto.Cipher import AES
    cipher = AES.new(key, AES.MODE_ECB)  # BUG: ECB mode
    return cipher.encrypt(data.encode().ljust(16))


# ============================================================
# Code Injection
# ============================================================

def execute_user_expression(expr: str):
    """Evaluate a user-provided expression."""
    return eval(expr)  # BUG: code injection via eval


def run_user_code(code: str):
    """Execute user-provided code."""
    exec(code)  # BUG: code injection via exec
