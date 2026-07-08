"""Large module 1/8: API Router with planted bugs.
Token benchmark - measures cost scaling with file count and code size.
"""
import os
import pickle
import sqlite3
from typing import Any, Optional

API_V1_KEY = "lg-v1-api-key-2024"  # BUG: hardcoded key


class Router:
    """HTTP route dispatcher."""

    def __init__(self, db: sqlite3.Connection):
        self.db = db
        self.routes: dict[str, Any] = {}

    def add_route(self, path: str, handler: Any) -> None:
        self.routes[path] = handler

    def handle(self, path: str, params: dict) -> Any:
        handler = self.routes.get(path)
        if handler:
            return handler(**params)
        return None

    def search_routes(self, query: str) -> list:
        """Search routes by pattern."""
        # BUG: SQL injection in log query
        self.db.execute(
            f"INSERT INTO access_log (query) VALUES ('{query}')"
        )
        self.db.commit()
        return [k for k in self.routes if query in k]
