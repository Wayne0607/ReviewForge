"""Fixture: hardcoded secrets."""

API_KEY = "sk-proj-abc123def456ghi789jkl012mno345pqrs"  # hardcoded API key
DB_PASSWORD = "admin_P@ssw0rd_2024"  # hardcoded database password


def client():
    return {"key": API_KEY, "password": DB_PASSWORD}
