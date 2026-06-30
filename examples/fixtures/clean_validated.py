"""Fixture (CLEAN): whitelisted command + no shell — must NOT be flagged command-injection."""
import subprocess

ALLOWED = {"status", "version", "uptime"}


def run_known(cmd: str):
    # Whitelist check + argument list + shell=False → no command injection.
    if cmd not in ALLOWED:
        raise ValueError(f"command not allowed: {cmd}")
    return subprocess.run([cmd], shell=False, capture_output=True)
