"""Insecure deserialization patterns for evaluation."""

import pickle
import yaml


def load_pickle(data):
    """Insecure deserialization via pickle.loads."""
    return pickle.loads(data)


def load_pickle_file(filepath):
    """Insecure deserialization via pickle.load."""
    with open(filepath, "rb") as f:
        return pickle.load(f)


def parse_yaml_unsafe(text):
    """Insecure YAML parsing via yaml.load without Loader."""
    return yaml.load(text)


def parse_yaml_safe(text):
    """Safe: yaml.safe_load. Should NOT be flagged."""
    return yaml.safe_load(text)
