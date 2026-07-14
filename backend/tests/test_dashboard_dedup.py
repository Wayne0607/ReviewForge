"""Dashboard/webhook regression: de-dup runs per commit, batch counts, webhook skip.

Covers the "one PR shows many records" console bug: redelivered webhooks and
opened+synchronize pairs create multiple runs for the same commit. The DB layer
collapses same-commit runs when listing, the webhook skips already-reviewed
commits, and finding/token counts are batched (no per-run N+1).
"""

import asyncio
import threading
import time
from datetime import UTC, datetime, timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient

from reviewforge.api.dashboard import router as dashboard_router
from reviewforge.core.database import Database


async def _make_run(db, run_id, repo, pr, sha, findings=0, status="completed", tokens=0):
    await db.create_run(run_id=run_id, repo=repo, pr_number=pr, head_sha=sha, base_sha="B")
    for i in range(findings):
        await db.insert_finding(
            run_id,
            {
                "id": f"{run_id}-{i}",
                "file": "src/app.py",
                "line": 10 + i,
                "severity": "error",
                "category": "security",
                "message": f"issue {i}",
                "confidence": 0.9,
                "reviewer": "security_reviewer",
                "status": "confirmed" if i % 2 == 0 else "candidate",
            },
        )
    if tokens:
        await db.record_token_usage(run_id, "security_reviewer", total_tokens=tokens)
    if status == "completed":
        await db.complete_run(run_id, {"total_findings": findings})
    elif status == "failed":
        await db.fail_run(run_id, "boom")


# ── DB de-dup ─────────────────────────────────────────────────


async def test_get_runs_collapses_same_commit_duplicates(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.connect()
    # Two completed runs for the SAME commit (redelivered webhook) + a distinct commit.
    await _make_run(db, "old", "o/r", 42, "sha_a", findings=3)
    await _make_run(db, "new", "o/r", 42, "sha_a", findings=3)  # duplicate commit
    await _make_run(db, "other", "o/r", 42, "sha_b", findings=1)

    runs = await db.get_runs(repo="o/r")
    keys = [(r["pr_number"], r["head_sha"]) for r in runs]
    assert keys.count((42, "sha_a")) == 1, "duplicate commit not collapsed"
    assert (42, "sha_b") in keys
    # The kept row is the most recent run for that commit.
    kept = next(r for r in runs if r["head_sha"] == "sha_a")
    assert kept["run_id"] == "new"
    await db.close()


async def test_get_findings_counts_batches(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.connect()
    await _make_run(db, "r1", "o/r", 1, "s1", findings=4)  # 2 confirmed, 2 candidate
    await _make_run(db, "r2", "o/r", 2, "s2", findings=0)
    counts = await db.get_findings_counts(["r1", "r2", "missing"])
    assert counts["r1"] == {"total": 4, "confirmed": 2, "false_positives": 0}
    assert "r2" not in counts  # no findings → not in grouped result
    assert await db.get_findings_counts([]) == {}
    await db.close()


# ── webhook dedup helper ──────────────────────────────────────


async def test_has_active_run_for_head(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.connect()
    await _make_run(db, "done", "o/r", 1, "completed_sha", status="completed")
    await _make_run(db, "failed", "o/r", 2, "failed_sha", status="failed")
    await _make_run(db, "run", "o/r", 3, "running_sha", status="running")

    assert await db.has_active_run_for_head("o/r", 1, "completed_sha") is True  # already reviewed
    assert await db.has_active_run_for_head("o/r", 3, "running_sha") is True  # in-flight
    assert await db.has_active_run_for_head("o/r", 2, "failed_sha") is False  # re-reviewable
    assert await db.has_active_run_for_head("o/r", 9, "never") is False  # unseen
    await db.close()


async def test_iso_started_at_fresh_and_stale_boundaries(tmp_path):
    db = Database(tmp_path / "iso-time.db")
    await db.connect()
    await _make_run(db, "fresh", "o/r", 10, "fresh_sha", status="running")
    await _make_run(db, "stale", "o/r", 11, "stale_sha", status="running")
    now = datetime.now(UTC)
    fresh_iso = (now - timedelta(minutes=14)).isoformat()
    stale_iso = (now - timedelta(minutes=16)).isoformat()
    await db._db.executemany(
        "UPDATE review_runs SET started_at=? WHERE run_id=?",
        [(fresh_iso, "fresh"), (stale_iso, "stale")],
    )
    await db._db.commit()

    assert await db.has_active_run_for_head("o/r", 10, "fresh_sha") is True
    assert await db.get_resumable_run("o/r", 10, "fresh_sha") is None
    assert await db.restart_run("fresh") is False

    assert await db.has_active_run_for_head("o/r", 11, "stale_sha") is False
    assert (await db.get_resumable_run("o/r", 11, "stale_sha"))["run_id"] == "stale"
    assert await db.restart_run("stale") is True
    assert await db.has_active_run_for_head("o/r", 11, "stale_sha") is True
    assert await db.get_resumable_run("o/r", 11, "stale_sha") is None
    await db.close()


# ── HTTP list endpoint: batched counts + tokens, no dupes ─────


async def test_list_reviews_endpoint_dedups_and_embeds_tokens(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.connect()
    await _make_run(db, "old", "o/r", 42, "sha_a", findings=3, tokens=1000)
    await _make_run(db, "new", "o/r", 42, "sha_a", findings=3, tokens=1200)  # dup commit
    await _make_run(db, "other", "o/r", 42, "sha_b", findings=1, tokens=500)

    app = FastAPI()
    app.include_router(dashboard_router)
    app.state.db = db
    client = TestClient(app)

    runs = client.get("/api/v1/dashboard/reviews").json()["runs"]
    assert len(runs) == 2, "same-commit duplicate should be collapsed in the list"
    kept = next(r for r in runs if r["head_sha"] == "sha_a")
    assert kept["run_id"] == "new"
    assert kept["summary"]["total_findings"] == 3
    assert kept["total_tokens"] == 1200  # embedded → frontend needs no per-run token fetch
    await db.close()


# ── webhook skips already-reviewed commits ────────────────────


async def test_webhook_skips_duplicate_delivery(tmp_path):
    import hashlib
    import hmac
    import json

    from reviewforge.api.webhook import router as webhook_router

    db = Database(tmp_path / "t.db")
    await db.connect()
    # This exact commit already has a completed review.
    await _make_run(db, "done", "acme/app", 7, "deadbeef", status="completed")

    class _GitHub:
        calls = 0

        async def get_pr_files(self, _repo, _pr_number):
            self.calls += 1
            return []

    class _Orchestrator:
        async def run(self, _state):
            raise AssertionError("completed heads must not start another review")

    github = _GitHub()
    app = FastAPI()
    app.include_router(webhook_router)
    app.state.db = db
    app.state.webhook_secret = "s3cret"
    app.state.review_semaphore = asyncio.Semaphore(1)
    app.state.review_tasks = set()
    app.state.github_client = github
    app.state.orchestrator = _Orchestrator()

    payload = {
        "action": "synchronize",
        "pull_request": {"number": 7, "head": {"sha": "deadbeef"}, "base": {"sha": "base"}},
        "repository": {"full_name": "acme/app"},
    }
    body = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()

    with TestClient(app) as client:
        resp = client.post(
            "/webhook/github",
            content=body,
            headers={"X-Hub-Signature-256": sig, "X-GitHub-Event": "pull_request"},
        )
        deadline = time.monotonic() + 2
        while app.state.review_tasks and time.monotonic() < deadline:
            time.sleep(0.01)

    assert resp.status_code == 200
    assert resp.json()["status"] == "review_triggered"
    assert github.calls == 0
    await db.close()


def test_webhook_response_does_not_wait_for_slow_persistent_dedup():
    import hashlib
    import hmac
    import json

    from reviewforge.api.webhook import router as webhook_router

    release = threading.Event()
    started = threading.Event()

    class _SlowDB:
        async def has_active_run_for_head(self, _repo, _pr_number, _head_sha):
            started.set()
            while not release.is_set():
                await asyncio.sleep(0.01)
            return True

    class _GitHub:
        async def get_pr_files(self, _repo, _pr_number):
            raise AssertionError("active head must be filtered before GitHub reads")

    class _Orchestrator:
        async def run(self, _state):
            raise AssertionError("active head must not be reviewed")

    app = FastAPI()
    app.include_router(webhook_router)
    app.state.db = _SlowDB()
    app.state.webhook_secret = "s3cret"
    app.state.review_semaphore = asyncio.Semaphore(1)
    app.state.review_tasks = set()
    app.state.github_client = _GitHub()
    app.state.orchestrator = _Orchestrator()

    payload = {
        "action": "opened",
        "pull_request": {"number": 9, "head": {"sha": "slowdb"}, "base": {"sha": "base"}},
        "repository": {"full_name": "acme/app"},
    }
    body = json.dumps(payload).encode()
    signature = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()

    # The timer prevents a broken synchronous implementation from hanging the
    # suite forever; such an implementation still takes ~1s and fails below.
    safety_release = threading.Timer(1.0, release.set)
    safety_release.start()
    try:
        with TestClient(app) as client:
            before = time.perf_counter()
            response = client.post(
                "/webhook/github",
                content=body,
                headers={"X-Hub-Signature-256": signature, "X-GitHub-Event": "pull_request"},
            )
            elapsed = time.perf_counter() - before
            assert started.wait(timeout=0.5)
            assert response.status_code == 200
            assert response.json()["status"] == "review_triggered"
            assert elapsed < 0.4
            release.set()
    finally:
        release.set()
        safety_release.cancel()


async def test_webhook_retriggers_failed_partial_run(tmp_path):
    import hashlib
    import hmac
    import json

    from reviewforge.api.webhook import router as webhook_router

    class _GitHub:
        async def get_pr_files(self, _repo, _pr_number):
            return []

    class _Orchestrator:
        async def run(self, _state):
            return {"status": "partial", "retryable": True}

    db = Database(tmp_path / "retry-webhook.db")
    await db.connect()
    await _make_run(db, "partial", "acme/app", 8, "retryme", status="failed")

    app = FastAPI()
    app.include_router(webhook_router)
    app.state.db = db
    app.state.webhook_secret = "s3cret"
    app.state.review_semaphore = asyncio.Semaphore(1)
    app.state.review_tasks = set()
    app.state.github_client = _GitHub()
    app.state.orchestrator = _Orchestrator()

    payload = {
        "action": "synchronize",
        "pull_request": {"number": 8, "head": {"sha": "retryme"}, "base": {"sha": "base"}},
        "repository": {"full_name": "acme/app"},
    }
    body = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()

    with TestClient(app) as client:
        resp = client.post(
            "/webhook/github",
            content=body,
            headers={"X-Hub-Signature-256": sig, "X-GitHub-Event": "pull_request"},
        )

    assert resp.status_code == 200
    assert resp.json()["status"] == "review_triggered"
    await db.close()
