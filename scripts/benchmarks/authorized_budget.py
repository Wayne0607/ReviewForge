"""Durable external authorization envelope for a bounded hypothesis run.

This is a spending limit, not an authorization request or a quality criterion.
Callers must already have permission for the given amount. Resume restores all
reported and unknown charges. A stale lock requires inspecting the old process
and receipts; this module never resets a grant automatically.
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path

from reviewforge.engine.request_budget import RequestBudget, external_request_budget_scope


@contextmanager
def authorized_budget(path: Path, *, limit: int, identity: str):
    if type(limit) is not int or limit <= 0 or not identity.strip():
        raise ValueError("positive authorized token limit and identity required")
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_suffix(path.suffix + ".lock")
    handle = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    try:
        os.write(handle, str(os.getpid()).encode("ascii"))
        saved = json.loads(path.read_text("utf-8")) if path.exists() else None
        if saved and (saved.get("authorization_id") != identity or saved["budget"]["limit"] != limit):
            raise ValueError("saved authorization identity or limit changed")
        budget = RequestBudget(limit, editor_reserve=0, saved=saved["budget"] if saved else None)

        def persist():
            temporary = path.with_suffix(path.suffix + ".tmp")
            temporary.write_text(
                json.dumps({"authorization_id": identity, "budget": budget.snapshot()}, indent=2), encoding="utf-8"
            )
            os.replace(temporary, path)

        async def changed(_snapshot):
            persist()

        persist()
        with external_request_budget_scope(budget, identity=identity, on_change=changed):
            try:
                yield budget
            finally:
                persist()
    finally:
        os.close(handle)
        lock.unlink()
