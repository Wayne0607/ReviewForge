from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from reviewforge.core.database import Database
from reviewforge.core.state import StateStore
from reviewforge.engine.publication_delivery import deliver_saved_publication
from reviewforge.tools.github_api import GitHubAPIError


@pytest.mark.asyncio
async def test_remote_acceptance_with_local_receipt_failure_recovers_by_reading(tmp_path, monkeypatch):
    db = Database(tmp_path / "db.sqlite")
    await db.connect()
    try:
        await db.create_run("run", "owner/repo", 1, "abc")
        await db.prepare_v4_publication("run", "abc", {"comments": [], "body": "summary"})
        modes = []

        async def invoke(name, params, state, **kwargs):
            modes.append(params["reconcile_only"])
            return {"delivered_indexes": [], "review": {"id": 42}, "compatibility": False}

        original = db.finish_v4_publication
        monkeypatch.setattr(db, "finish_v4_publication", AsyncMock(side_effect=RuntimeError("disk failure")))
        state = StateStore(repo="owner/repo", pr_number=1, head_sha="abc")
        with pytest.raises(RuntimeError, match="disk failure"):
            await deliver_saved_publication(db, SimpleNamespace(invoke=invoke), state, "run")
        monkeypatch.setattr(db, "finish_v4_publication", original)
        assert not (await deliver_saved_publication(db, SimpleNamespace(invoke=invoke), state, "run")).error
        assert modes == [False, True]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_restart_reuses_frozen_payload_and_delivered_receipt(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    await db.connect()
    try:
        await db.create_run("run", "owner/repo", 1, "abc")
        payload = {"comments": [{"file_path": "app.py", "line": 2, "body": "original"}], "body": "summary"}
        await db.prepare_v4_publication("run", "abc", payload)
        await db.prepare_v4_publication("run", "abc", {"comments": [], "body": "changed"})
        calls = []

        async def invoke(name, params, state, **kwargs):
            calls.append(params)
            return {"delivered_indexes": [0], "review": {"id": 42}, "compatibility": False}

        state = StateStore(repo="owner/repo", pr_number=1, head_sha="abc")
        gateway = SimpleNamespace(invoke=invoke)
        first = await deliver_saved_publication(db, gateway, state, "run")
        second = await deliver_saved_publication(db, gateway, state, "run")
        assert first.delivered == second.delivered == 1
        assert len(calls) == 1
        assert calls[0]["comments"][0]["body"] == "original"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_ambiguous_delivery_reconciles_without_allowing_another_post(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    await db.connect()
    try:
        await db.create_run("run", "owner/repo", 1, "abc")
        await db.prepare_v4_publication("run", "abc", {"comments": [], "body": "summary"})
        modes = []

        async def invoke(name, params, state, **kwargs):
            modes.append(params["reconcile_only"])
            if len(modes) == 1:
                raise GitHubAPIError("response lost", kind="network", retryable=True)
            return {"delivered_indexes": [], "review": {"id": 42}, "compatibility": False}

        state = StateStore(repo="owner/repo", pr_number=1, head_sha="abc")
        gateway = SimpleNamespace(invoke=invoke)
        first = await deliver_saved_publication(db, gateway, state, "run")
        assert first.retryable and first.error
        second = await deliver_saved_publication(db, gateway, state, "run")
        assert not second.error
        assert modes == [False, True]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_partial_or_malformed_receipt_never_marks_publication_delivered(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    await db.connect()
    try:
        await db.create_run("run", "owner/repo", 1, "abc")
        await db.prepare_v4_publication(
            "run", "abc", {"comments": [{"file_path": "app.py", "line": 2, "body": "issue"}], "body": ""}
        )

        async def invoke(*args, **kwargs):
            return {"delivered_indexes": [], "review": {}, "compatibility": True}

        state = StateStore(repo="owner/repo", pr_number=1, head_sha="abc")
        outcome = await deliver_saved_publication(db, SimpleNamespace(invoke=invoke), state, "run")
        assert outcome.error
        assert (await db.get_v4_publication("run"))["status"] == "sending"
        state.head_sha = "other"
        with pytest.raises(ValueError, match="head"):
            await deliver_saved_publication(db, SimpleNamespace(invoke=invoke), state, "run")
    finally:
        await db.close()
