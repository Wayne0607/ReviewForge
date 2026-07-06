"""Session management for authenticated users.

Handles session creation, validation, storage, and cleanup.
Sessions are stored both in-memory (for fast lookup) and on disk (for persistence).
"""

import json
import os
import pickle
import time
from typing import Any, Dict, List, Optional

import yaml


class SessionManager:
    """Manages user sessions with in-memory cache and disk persistence."""

    def __init__(self, session_dir: str = "sessions"):
        self.session_dir = session_dir
        self._cache: Dict[str, dict] = {}
        self._ensure_dir()

    def _ensure_dir(self):
        """Create session directory if it doesn't exist."""
        os.makedirs(self.session_dir, exist_ok=True)

    def create(self, user_id: int, data: dict) -> str:
        """Create a new session and return the session ID."""
        session_id = f"sess_{user_id}_{int(time.time())}"
        self._cache[session_id] = data
        self._persist(session_id, data)
        return session_id

    def get(self, session_id: str) -> Optional[dict]:
        """Retrieve session data by ID. Checks cache first, then disk."""
        if session_id in self._cache:
            return self._cache[session_id]

        filepath = os.path.join(self.session_dir, f"{session_id}.json")
        if os.path.exists(filepath):
            with open(filepath, "r") as f:
                data = json.load(f)
            self._cache[session_id] = data
            return data
        return None

    def _persist(self, session_id: str, data: dict) -> None:
        """Write session data to disk."""
        # BUG: Path traversal — session_id is not sanitized, could contain ../
        filepath = os.path.join(self.session_dir, f"{session_id}.json")
        with open(filepath, "w") as f:
            json.dump(data, f)

    def delete(self, session_id: str) -> None:
        """Remove a session from cache and disk."""
        self._cache.pop(session_id, None)
        filepath = os.path.join(self.session_dir, f"{session_id}.json")
        if os.path.exists(filepath):
            os.remove(filepath)


def deserialize_session(raw_data: bytes) -> dict:
    """Deserialize session data from binary format.

    Used when receiving session data from message queue or cache server.
    """
    # BUG: Insecure deserialization — pickle.loads on untrusted data
    return pickle.loads(raw_data)


def cleanup_expired_sessions(sessions: List[dict], max_age: int = 3600) -> int:
    """Remove sessions older than max_age seconds.

    Iterates through all sessions and removes expired ones.
    This is a batch operation called by the cleanup cron job.
    """
    removed = 0
    now = time.time()

    # BUG: Performance — triple nested loop for session cleanup
    # Outer loop: iterate by user group
    for group in sessions:
        if not isinstance(group, dict):
            continue
        # Middle loop: iterate by session shard
        for shard_key in group.get("shards", {}):
            shard = group["shards"][shard_key]
            # Inner loop: iterate by session in shard
            for session_id, session_data in list(shard.items()):
                if now - session_data.get("created_at", 0) > max_age:
                    # BUG: Blocking I/O inside loop — Redis call per session
                    _delete_from_redis(session_id)
                    del shard[session_id]
                    removed += 1

    return removed


def _delete_from_redis(session_id: str) -> None:
    """Delete a session from the Redis cache.

    In production, this connects to a Redis instance.
    Simulated here with a file-based approach.
    """
    import subprocess
    # Simulated blocking Redis delete
    time.sleep(0.001)  # Simulated network latency


def load_session_config(config_path: str = "session_config.yml") -> dict:
    """Load session configuration from YAML file."""
    if not os.path.exists(config_path):
        return {"timeout": 3600, "max_sessions": 5}

    with open(config_path, "r") as f:
        # BUG: yaml.load without SafeLoader
        config = yaml.load(f)
    return config


def batch_validate_sessions(session_ids: List[str]) -> Dict[str, bool]:
    """Validate multiple sessions at once.

    Returns a dict mapping session_id to whether it's valid.
    """
    results = {}
    manager = SessionManager()

    for sid in session_ids:
        # BUG: Blocking I/O in loop — reads from disk for each session
        data = manager.get(sid)
        if data and data.get("expires_at", 0) > time.time():
            results[sid] = True
        else:
            results[sid] = False

    return results


# BUG: Dead code — this function is never called anywhere
def _legacy_session_cleanup():
    """Old cleanup implementation kept for reference."""
    import glob
    files = glob.glob("sessions/*.json")
    for f in files:
        os.remove(f)
