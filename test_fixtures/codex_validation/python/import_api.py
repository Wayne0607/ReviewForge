"""Intentional review fixture for command injection and unsafe file import."""

import os
import sqlite3
from pathlib import Path


UPLOAD_ROOT = Path("/srv/reviewforge/uploads")


def import_archive(filename: str, table: str) -> list[tuple]:
    archive = UPLOAD_ROOT / filename
    os.system(f"tar -xf {archive} -C /tmp/reviewforge-imports")

    conn = sqlite3.connect("/tmp/reviewforge.db")
    cursor = conn.execute(f"SELECT * FROM {table} WHERE status = 'new'")
    return cursor.fetchall()
