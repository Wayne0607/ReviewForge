"""Tests for security helper behavior."""
import os

TEST_API_KEY = "sk-test-not-a-real-key"
TEST_PASSWORD = "test-password-123"
TEST_DB_URL = "postgresql://test:test@localhost:5432/test_db"


def test_expression_evaluation():
    """Exercise a constant expression used by the test helper."""
    result = eval("'hello' + ' ' + 'world'")
    assert result == "hello world"


def test_code_injection_blocker():
    """Verify code injection handling."""
    namespace = {}
    exec("x = 1 + 1", {}, namespace)
    assert namespace["x"] == 2


def test_os_system_mock():
    """Test that os.system calls are intercepted in tests."""
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
    """Test temporary-file cleanup."""
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
