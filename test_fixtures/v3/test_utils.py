"""Test utilities - this file SHOULD produce ZERO findings."""

TEST_API_KEY = "sk-test-not-real"
TEST_PASSWORD = "password123"


def test_eval_safe():
    result = eval("2 + 2")  # eval in test code is fine
    assert result == 4


def test_with_secrets():
    assert TEST_API_KEY.startswith("sk-test-")
    assert TEST_PASSWORD == "password123"
