"""Portable process coordination and provenance checks for read-only evaluation."""

from __future__ import annotations

import errno
import json
import os
import time
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def exclusive_file_lock(path: Path, *, blocking: bool = True):
    """Lock a separate byte file so truncating the timestamp cannot unlock it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        if os.name == "nt":
            import msvcrt

            if handle.seek(0, os.SEEK_END) == 0:
                handle.write(b"\0")
                handle.flush()
            while True:
                handle.seek(0)
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError as exc:
                    if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLOCK}:
                        raise
                    if not blocking:
                        raise BlockingIOError("Another process owns the file lock") from exc
                    time.sleep(0.05)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
            fcntl.flock(handle.fileno(), flags)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def wait_for_request_slot(path: Path, interval: float) -> None:
    if interval <= 0:
        return
    with exclusive_file_lock(path.with_suffix(".lock")):
        with path.open("a+", encoding="utf-8") as handle:
            handle.seek(0)
            previous = float(handle.read().strip() or 0)
            now = time.monotonic()
            # A persisted monotonic timestamp can belong to the previous boot.
            delay = interval - (now - previous) if 0 < previous <= now else 0
            if delay > 0:
                time.sleep(delay)
            handle.seek(0)
            handle.truncate()
            handle.write(str(time.monotonic()))
            handle.flush()


def is_complete_result(row: dict) -> bool:
    summary = row.get("summary")
    if row.get("status") != "completed" or not isinstance(summary, dict):
        return False
    if "status" in summary:
        return summary["status"] == "completed"
    # Legacy success retains its public shape without a status field.
    return "total_findings" in summary and summary.get("tasks_failed") == 0


def require_complete_results(rows: list[dict], workload: list[dict]) -> dict[str, dict]:
    by_url = {row["golden_url"]: row for row in rows}
    missing = [item["golden_url"] for item in workload if not is_complete_result(by_url.get(item["golden_url"], {}))]
    if missing:
        raise RuntimeError(f"{len(missing)} requested reviews are missing or incomplete; no quality score produced")
    return by_url


def validate_resume_metadata(existing: dict | None, expected: dict, *, has_results: bool) -> None:
    if existing is not None and existing != expected:
        raise RuntimeError("Evaluation provenance changed; use a new output directory")
    if has_results and existing is None:
        raise RuntimeError("Existing results have no provenance; use a new output directory")


def read_metadata(path: Path) -> dict | None:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
