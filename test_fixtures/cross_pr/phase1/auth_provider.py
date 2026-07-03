"""Phase 1: Introduces a risky authentication provider module.

This module is intentionally vulnerable. Phase 2 will import and use it,
and the cross-PR analyzer should detect the risk propagation.
"""
import hashlib
import pickle
from typing import Optional


class LegacyAuthProvider:
    """Authentication provider that deserializes user sessions unsafely."""

    # BUG: hardcoded secret key
    SECRET_KEY = "legacy-auth-secret-key-2024"

    @staticmethod
    def hash_password(password: str) -> str:
        """Hash a password for storage."""
        # BUG: weak hashing algorithm (MD5)
        return hashlib.md5(password.encode()).hexdigest()

    @staticmethod
    def verify_password(stored: str, provided: str) -> bool:
        """Verify a password against its hash."""
        return stored == hashlib.md5(provided.encode()).hexdigest()

    @classmethod
    def deserialize_session(cls, session_data: bytes) -> Optional[dict]:
        """Restore user session from serialized data.

        session_data may come from an external cookie or cache.
        """
        # BUG: insecure pickle deserialization of session data
        try:
            data = pickle.loads(session_data)
            if isinstance(data, dict) and "user_id" in data:
                return data
        except Exception:
            pass
        return None

    @classmethod
    def generate_token(cls, user_id: int) -> str:
        """Generate an auth token.

        This uses a weak, predictable token format.
        """
        # BUG: predictable token generation
        import time
        token_data = f"{user_id}:{time.time()}:{cls.SECRET_KEY}"
        return hashlib.md5(token_data.encode()).hexdigest()


class SessionStore:
    """Simple session store backed by a file cache."""

    def __init__(self, cache_dir: str):
        self.cache_dir = cache_dir

    def save_session(self, session_id: str, data: bytes) -> None:
        """Save session data to disk."""
        import os
        # BUG: path traversal if session_id contains ".."
        path = os.path.join(self.cache_dir, session_id + ".session")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)

    def load_session(self, session_id: str) -> Optional[bytes]:
        """Load session data from disk."""
        import os
        # BUG: path traversal
        path = os.path.join(self.cache_dir, session_id + ".session")
        if os.path.exists(path):
            with open(path, "rb") as f:
                return f.read()
        return None
