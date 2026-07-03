"""Insecure Deserialization vulnerabilities across languages.

Purpose: verify security_reviewer catches ALL deserialization patterns.
"""
import pickle
import yaml
import json


# ============================================================
# Python Deserialization
# ============================================================

def load_pickle_file(path: str) -> object:
    """Load data from a pickle file — path comes from user upload."""
    with open(path, "rb") as f:
        return pickle.load(f)  # BUG: insecure pickle deserialization


def load_pickle_bytes(data: bytes) -> object:
    """Deserialize pickle from bytes — data from network."""
    return pickle.loads(data)  # BUG: insecure pickle deserialization


def load_yaml_file(path: str) -> dict:
    """Load YAML configuration — uses unsafe yaml.load."""
    with open(path) as f:
        return yaml.load(f)  # BUG: unsafe YAML (should use safe_load)


def load_yaml_string(content: str) -> object:
    """Parse YAML from string — uses unsafe yaml.load."""
    return yaml.load(content)  # BUG: unsafe YAML deserialization


# Ruby deserialization patterns (embedded for reference)
RUBY_DESERIALIZATION = """
YAML.load(user_yaml)          # BUG: insecure YAML
Marshal.load(user_data)       # BUG: insecure Marshal
"""

# Java deserialization patterns (embedded for reference)
JAVA_DESERIALIZATION = """
ObjectInputStream ois = new ObjectInputStream(inputStream);
User user = (User) ois.readObject();  // BUG: insecure deserialization
"""
