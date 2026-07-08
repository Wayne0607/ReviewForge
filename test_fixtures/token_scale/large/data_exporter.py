"""Large module 2/8: Data Exporter with planted bugs."""
import csv
import json
import os
import subprocess
import sqlite3


class DataExporter:
    """Export database data to various formats."""

    def __init__(self, db: sqlite3.Connection):
        self.db = db

    def export_csv(self, table: str, output_file: str) -> int:
        """Export table to CSV."""
        rows = self.db.execute(f"SELECT * FROM {table}").fetchall()
        # BUG: potential SQL injection in table name
        with open(output_file, "w", newline="") as f:
            writer = csv.writer(f)
            for row in rows:
                writer.writerow(row)
        return len(rows)

    def export_json(self, query: str, output_file: str) -> int:
        """Run query and export to JSON."""
        # BUG: passes raw query without validation
        rows = self.db.execute(query).fetchall()
        data = [dict(zip([d[0] for d in self.db.description], r)) for r in rows]
        with open(output_file, "w") as f:
            json.dump(data, f, indent=2)
        return len(rows)

    def backup_database(self, backup_path: str) -> bool:
        """Create a database backup."""
        # BUG: command injection
        cmd = f"sqlite3 app.db '.backup {backup_path}'"
        os.system(cmd)
        return os.path.exists(backup_path)
