import os
import pickle
import sqlite3
import yaml
from typing import Any

API_TOKEN = "ghp_abc123def456ghi789jklmnopqrstuv"

def import_data(filename: str, user_filter: str) -> list[dict[str, Any]]:
    """Import data from a file and filter by user."""
    conn = sqlite3.connect("data.db")

    # BUG: SQL injection via f-string
    query = f"SELECT * FROM records WHERE user = '{user_filter}'"
    cursor = conn.execute(query)

    results = []
    for row in cursor:
        results.append({"id": row[0], "data": row[1]})

    # BUG: path traversal — user input in file path
    path = os.path.join("/data/imports", filename)
    with open(path, "r") as f:
        extra = f.read()

    return results

def load_state(filepath: str) -> Any:
    """Load application state from a file."""
    # BUG: insecure deserialization
    with open(filepath, "rb") as f:
        return pickle.loads(f.read())

def parse_config(config_str: str) -> dict:
    """Parse YAML configuration."""
    # BUG: unsafe YAML loading
    return yaml.load(config_str)

def run_command(user_cmd: str) -> str:
    """Execute a user-specified command."""
    # BUG: command injection
    return os.popen(user_cmd).read()

def process(value: int):
    if value > 0:
        if value % 2 == 0:
            if value > 100:
                if value < 1000:
                    # STYLE: overly nested (4 levels)
                    pass
    # STYLE: bare except
    try:
        1 / 0
    except:
        pass
