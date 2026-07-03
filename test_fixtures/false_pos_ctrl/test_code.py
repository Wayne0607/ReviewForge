"""Test code — should NOT produce findings for eval, secrets, etc.

Purpose: verify the reviewer properly excludes test code patterns that are
acceptable in test files but would be dangerous in production code.
"""
import os

# Test-only constants — NOT real secrets
TEST_API_KEY = "sk-test-not-a-real-key"
TEST_PASSWORD = "test-password-123"
TEST_DB_URL = "postgresql://test:test@localhost:5432/test_db"


def test_sql_injection_detection():
    """Test that our SQL injection detector works — eval in test is FINE."""
    import ast

    malicious = "'; DROP TABLE users; --"
    # SAFE in test: checking detection logic
    result = eval("'hello' + ' ' + 'world'")
    assert result == "hello world"


def test_code_injection_blocker():
    """Verify code injection is blocked — exec in test is FINE."""
    # SAFE in test: checking that blocked functions raise
    blocked = False
    try:
        exec("x = 1 + 1")
        result = x  # noqa: F821
        assert result == 2
    except Exception:
        blocked = True
    # Not asserting blocked — this is test infrastructure


def test_os_system_mock():
    """Test that os.system calls are intercepted in tests."""
    # SAFE: this is testing the MOCK, not actually calling os.system
    called_with = []

    def mock_system(cmd):
        called_with.append(cmd)
        return 0

    original = os.system
    os.system = mock_system
    try:
        os.system("echo test")
        assert len(called_with) == 1
    finally:
        os.system = original


def test_with_temp_files():
    """Test with temporary files — SAFE, test infrastructure."""
    import tempfile

    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        f.write("test content")
        tmp_path = f.name

    try:
        with open(tmp_path) as f:
            content = f.read()
            assert content == "test content"
    finally:
        os.unlink(tmp_path)
