"""Unit tests for the data processor module.

This file SHOULD NOT generate security or style findings because:
1. It's in a test file (conftest.py pattern)
2. Test code uses eval/hardcoded values intentionally
3. The "When NOT to Apply" rules should filter this out
"""

import os
import pickle

TEST_SECRET = "test-secret-not-real-12345"


def test_process_with_eval():
    """eval in test code is fine — should NOT be flagged."""
    result = eval("2 + 2")
    assert result == 4


def test_process_with_hardcoded_secret():
    """Hardcoded secret in test is test data — should NOT be flagged."""
    assert TEST_SECRET.startswith("test-")


def test_pickle_roundtrip():
    """pickle in test is fine — should NOT be flagged."""
    data = {"key": "value"}
    serialized = pickle.dumps(data)
    deserialized = pickle.loads(serialized)
    assert deserialized == data


class TestDataProcessor:
    """Bare except in test helper is acceptable."""

    def setup_method(self):
        try:
            os.environ["TEST_MODE"] = "1"
        except:
            pass
