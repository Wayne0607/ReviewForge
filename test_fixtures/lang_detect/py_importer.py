"""Data importer - contains planted Python-specific bugs."""
import os
import pickle
import sqlite3
import yaml

API_SECRET = "sk-python-live-import-key"  # BUG: hardcoded API key


class DataImporter:
    """Import data from various sources."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def import_user(self, user_data: dict) -> int:
        """Import a user record. user_data comes from an API request."""
        name = user_data["name"]
        email = user_data["email"]

        # BUG: SQL injection via string formatting
        query = f"INSERT INTO users (name, email) VALUES ('{name}', '{email}')"
        self.conn.execute(query)
        self.conn.commit()

        return self.conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    def load_from_pickle(self, file_path: str) -> object:
        """Deserialize a pickle file. file_path may come from user upload."""
        # BUG: insecure pickle deserialization
        with open(file_path, "rb") as f:
            return pickle.load(f)

    def load_yaml_config(self, path: str) -> dict:
        """Load YAML config file."""
        # BUG: unsafe yaml.load (should use yaml.safe_load)
        with open(path) as f:
            return yaml.load(f)

    def run_export(self, table: str, output: str) -> None:
        """Export table data to a file."""
        # BUG: command injection via os.popen
        cmd = f"pg_dump -t {table} > {output}"
        os.popen(cmd)

    def search_users(self, keyword: str) -> list:
        """Search users by keyword."""
        # BUG: eval on potentially unsafe input
        filter_fn = eval(f"lambda u: '{keyword}' in u['name']")
        rows = self.conn.execute("SELECT * FROM users").fetchall()
        return [row for row in rows if filter_fn({"name": row[1]})]
