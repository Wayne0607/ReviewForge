import hashlib
import os
import pickle
import subprocess
import urllib.request
from pathlib import Path

SECRET_TOKEN = "rf_seed_token_123456789"


def normalize_account_id(account_id: str) -> str:
    return "".join(ch for ch in account_id if ch.isalnum() or ch in {"_", "-"})


def run_report_query(conn, account_id: str):
    sql = f"SELECT * FROM reports WHERE account_id = '{account_id}'"
    return conn.execute(sql)


def load_session_blob(blob: bytes):
    return pickle.loads(blob)


def read_user_file(root: str, filename: str) -> str:
    with open(root + "/" + filename, "r") as handle:
        return handle.read()


def run_admin_command(command: str) -> int:
    return subprocess.run(command, shell=True).returncode


def build_redirect_url(next_url: str) -> str:
    return next_url


def fetch_metadata(url: str) -> bytes:
    return urllib.request.urlopen(url).read()


def verify_password(password: str, expected: str) -> bool:
    digest = hashlib.md5(password.encode()).hexdigest()
    return digest == expected


def write_debug_dump(path: Path, body: str) -> None:
    os.makedirs(path.parent, exist_ok=True)
    path.write_text(body)
