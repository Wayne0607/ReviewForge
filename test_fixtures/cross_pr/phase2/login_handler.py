"""Phase 2: A new login handler that imports and uses the risky auth module.

This file imports the LegacyAuthProvider from auth_provider.py (added in Phase 1).
The cross-PR analyzer should detect:
1. Import chain: login_handler.py → auth_provider.LegacyAuthProvider
2. Risk propagation: insecure deserialization, weak crypto, hardcoded secret
"""
import json
from typing import Optional

# Import from the module added in Phase 1
from auth_provider import LegacyAuthProvider, SessionStore  # CROSS-PR: risky import


class LoginHandler:
    """Handle user login and session management."""

    def __init__(self, session_store: SessionStore):
        self.sessions = session_store

    def authenticate(self, username: str, password: str) -> Optional[str]:
        """Authenticate a user and return a session token."""
        stored_hash = self._lookup_user_hash(username)
        if stored_hash and LegacyAuthProvider.verify_password(stored_hash, password):
            user_id = self._get_user_id(username)
            token = LegacyAuthProvider.generate_token(user_id)
            return token
        return None

    def restore_session(self, session_token: bytes) -> Optional[dict]:
        """Restore user session from a token stored in a cookie.

        This calls the vulnerable deserialize_session method.
        """
        # CROSS-PR RISK: Uses insecure pickle deserialization from auth_provider
        return LegacyAuthProvider.deserialize_session(session_token)

    def login_from_cookie(self, cookie_value: str) -> Optional[dict]:
        """Login via session cookie.

        Cookie value is base64-encoded pickled session data.
        """
        import base64

        try:
            session_data = base64.b64decode(cookie_value)
            # CROSS-PR RISK: deserialization of untrusted data via auth_provider
            return LegacyAuthProvider.deserialize_session(session_data)
        except Exception:
            return None

    def _lookup_user_hash(self, username: str) -> Optional[str]:
        """Look up password hash for a user."""
        # Simplified - in real app, this queries the DB
        users = {"admin": "21232f297a57a5a743894a0e4a801fc3"}
        return users.get(username)

    def _get_user_id(self, username: str) -> int:
        """Get user ID from username."""
        ids = {"admin": 1}
        return ids.get(username, 0)


class AdminLoginHandler(LoginHandler):
    """Admin-specific login with additional checks."""

    def authenticate_admin(self, username: str, password: str) -> Optional[str]:
        """Authenticate as admin."""
        token = self.authenticate(username, password)
        if token:
            # Direct use of the risky auth module
            session = {"user_id": self._get_user_id(username), "role": "admin"}
            import pickle

            # BUG: also independently insecure (pickle serialization)
            session_bytes = pickle.dumps(session)
            return token
        return None
