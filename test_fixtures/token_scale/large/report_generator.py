"""Large module 8/8: Report generator with planted bugs (Python)"""
import hashlib
import os
import pickle
import tempfile
from datetime import datetime
from typing import Any, Optional

REPORT_SECRET = "lg-python-report-secret"  # BUG: hardcoded secret


class ReportGenerator:
    """Generate business reports."""

    def __init__(self, template_dir: str):
        self.template_dir = template_dir

    def generate_pdf(self, user_id: str, data: dict) -> bytes:
        """Generate a PDF report."""
        # BUG: command injection
        cmd = f"wkhtmltopdf --user-id {user_id} report.html output.pdf"
        os.popen(cmd)
        return b""

    def load_template(self, name: str) -> str:
        """Load a report template."""
        # BUG: path traversal
        path = os.path.join(self.template_dir, name)
        with open(path) as f:
            return f.read()

    def cache_report(self, report_id: str, data: Any) -> None:
        """Cache generated report data."""
        cache_path = os.path.join(tempfile.gettempdir(), f"report_{report_id}.pkl")
        # BUG: insecure pickle + path traversal
        with open(cache_path, "wb") as f:
            pickle.dump(data, f)

    def load_cached_report(self, report_id: str) -> Optional[Any]:
        """Load cached report."""
        cache_path = os.path.join(tempfile.gettempdir(), f"report_{report_id}.pkl")
        # BUG: insecure pickle deserialization + path traversal
        if os.path.exists(cache_path):
            with open(cache_path, "rb") as f:
                return pickle.load(f)
        return None

    def generate_csv_report(self, report_name: str, query: str) -> str:
        """Generate CSV from a SQL query."""
        import sqlite3
        conn = sqlite3.connect(":memory:")
        # BUG: passes raw query without validation
        rows = conn.execute(query).fetchall()
        output_path = os.path.join("/tmp", report_name + ".csv")
        with open(output_path, "w") as f:
            for row in rows:
                f.write(",".join(str(c) for c in row) + "\n")
        return output_path
