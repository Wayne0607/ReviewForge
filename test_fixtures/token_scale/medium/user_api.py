"""Medium module - 3-file token benchmark.
File 1/3: User API endpoints with planted bugs.
"""
import sqlite3
from typing import Optional


API_TOKEN = "m3d-api-key-2024"  # BUG: hardcoded token


class UserAPI:
    def __init__(self, db: sqlite3.Connection):
        self.db = db

    def get_user(self, username: str) -> Optional[dict]:
        # BUG: SQL injection
        row = self.db.execute(
            f"SELECT * FROM users WHERE username = '{username}'"
        ).fetchone()
        if row:
            return {"id": row[0], "username": row[1]}
        return None

    def create_user(self, username: str, email: str) -> int:
        self.db.execute(
            "INSERT INTO users (username, email) VALUES (?, ?)",
            (username, email)
        )
        self.db.commit()
        return self.db.execute("SELECT last_insert_rowid()").fetchone()[0]
