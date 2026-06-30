"""Clean web handlers — safe patterns that look suspicious but are correct.

Tests false positive rate. All inputs are validated or safely handled.
"""

from flask import Flask, request, abort
from markupsafe import escape
import subprocess

app = Flask(__name__)


@app.route("/search")
def search():
    """Safe: user input is escaped before rendering."""
    query = request.args.get("q", "")
    safe_query = escape(query)
    return f"<h1>Search results for: {safe_query}</h1>"


@app.route("/run", methods=["POST"])
def run_command():
    """Safe: uses allowlist, no shell=True."""
    allowed_commands = {"status", "restart", "reload"}
    cmd = request.form.get("cmd", "")
    if cmd not in allowed_commands:
        abort(400, "Invalid command")
    result = subprocess.run(["systemctl", cmd, "myapp"], capture_output=True, text=True)
    return result.stdout


@app.route("/file")
def read_file():
    """Safe: validates path stays within allowed directory."""
    import os
    filename = request.args.get("name", "")
    base_dir = "/var/app/uploads"
    full_path = os.path.realpath(os.path.join(base_dir, filename))
    if not full_path.startswith(base_dir):
        abort(403, "Access denied")
    with open(full_path, "r") as f:
        return f.read()
