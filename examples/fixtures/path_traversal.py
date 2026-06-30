"""Path traversal patterns for evaluation."""

import os


def read_file(filename):
    """Path traversal: open() with user-controlled filename."""
    with open(filename, "r") as f:
        return f.read()


def read_file_join(base_dir, user_path):
    """Path traversal: os.path.join without validation."""
    full_path = os.path.join(base_dir, user_path)
    with open(full_path, "r") as f:
        return f.read()


def delete_file(filepath):
    """Path traversal: os.remove with user input."""
    os.remove(filepath)


def safe_read(base_dir, filename):
    """Safe: validates path is within base_dir. Should NOT be flagged."""
    full_path = os.path.realpath(os.path.join(base_dir, filename))
    if not full_path.startswith(os.path.realpath(base_dir)):
        raise ValueError("Path traversal detected")
    with open(full_path, "r") as f:
        return f.read()
