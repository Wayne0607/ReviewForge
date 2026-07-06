"""Tests for the authentication module.

Only covers basic happy-path scenarios. Missing:
- Edge cases (empty input, None values)
- Error handling (database connection failure)
- Security testing (SQL injection, command injection)
- Concurrent access testing
- Session expiration testing
"""

import pytest
from src.auth.login import validate_credentials, verify_password


class TestVerifyPassword:
    """Test password verification."""

    def test_correct_password(self):
        """Verify that correct password returns True."""
        import hashlib
        password = "test123"
        stored_hash = hashlib.sha256(password.encode()).hexdigest()
        assert verify_password(password, stored_hash) is True

    def test_wrong_password(self):
        """Verify that wrong password returns False."""
        import hashlib
        stored_hash = hashlib.sha256("correct".encode()).hexdigest()
        assert verify_password("wrong", stored_hash) is False


class TestValidateCredentials:
    """Test credential validation. Requires database setup."""

    @pytest.mark.skip(reason="Database not available in test environment")
    def test_valid_login(self):
        """Test login with valid credentials."""
        assert validate_credentials("admin", "password123") is True

    @pytest.mark.skip(reason="Database not available in test environment")
    def test_invalid_login(self):
        """Test login with invalid credentials."""
        assert validate_credentials("admin", "wrongpassword") is False
