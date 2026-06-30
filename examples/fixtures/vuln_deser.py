"""Fixture: insecure deserialization via pickle."""
import pickle


def load_session(raw_bytes):
    # Deserializing untrusted bytes with pickle → arbitrary code execution.
    return pickle.loads(raw_bytes)
