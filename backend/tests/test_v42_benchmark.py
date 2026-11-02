from types import SimpleNamespace

import pytest
from test_martian_runner import runner  # noqa: F401

from reviewforge.engine.provider_retry import RetryChatLLM
from reviewforge.review_v42.contracts import Run
from reviewforge.review_v42.integration import PhysicalMeter, _models, _raw_models


async def test_benchmark_uses_one_retry_owner_and_native_v42_models(runner, monkeypatch, tmp_path):  # noqa: F811
    monkeypatch.setenv("GITHUB_TOKEN", "test-placeholder")
    monkeypatch.setenv("LLM_API_KEY", "test-placeholder")
    monkeypatch.setenv("REVIEWFORGE_SETTINGS_DIR", str(tmp_path))
    obj, db, github = await runner._build_runtime(
        tmp_path, "test-model", "en", "review_v42", llm_min_interval=0, llm_max_attempts=3
    )
    try:
        raw = _raw_models(obj._model_router)
        assert not any(isinstance(model, RetryChatLLM) for model in raw.values())
        bound = _models(obj, Run("run", "a/b", "base", "head"), PhysicalMeter(tmp_path / "meter"), raw)
        assert all(isinstance(model, RetryChatLLM) for model in bound)
        assert all(model._inner._inner._inner.root_async_client.max_retries == 0 for model in bound)
    finally:
        await db.close()
        await github.close()


async def test_benchmark_uses_local_verified_comments_and_cumulative_meter(runner):  # noqa: F811
    class GitHub:
        comments, bodies = {}, {}

        async def get_pr_info(self, *_):
            return {"head": {"sha": "head"}, "base": {"sha": "base"}}

        async def get_pr_files(self, *_):
            return [{"filename": "a.py", "patch": "@@ -0,0 +1 @@\n+x"}]

    class DB:
        async def get_runs(self, **_):
            return []

    report = {
        "comments": [{"body": "Verified bug", "anchor": {"path": "a.py", "line": 1, "side": "RIGHT"}}],
        "metrics": {"usage": {"input_tokens": 100, "output_tokens": 20, "unknown_requests": 1}},
    }

    async def run(state):
        assert state.linked_issue_context["status"] == "excluded-historical-leakage"
        assert not state.linked_issues
        return {"mode": "review_v42", "status": "partial", "review_v42_report": report}

    github = GitHub()
    obj = SimpleNamespace(
        _pipeline_v4_config=SimpleNamespace(mode="review_v42"), _gateway=SimpleNamespace(_github=github), run=run
    )
    row = await runner._run_one({"repo": "a/b", "pr_number": 1, "golden_url": "test"}, obj, DB(), github)
    assert row["status"] == "partial" and row["tokens"] == 120
    assert row["review_comments"][0]["body"] == "Verified bug"
    assert row["review_v42_metrics"]["usage"]["unknown_requests"] == 1
    assert row["comment_delivery"] == "local-only" and not github.comments


def _write_source_cache(path, repo, sha, data):
    import hashlib
    import json

    key = hashlib.sha256(f"{repo}:{sha}".encode()).hexdigest()
    manifest = {
        "schema": "reviewforge-source-cache-v1",
        "repo": repo,
        "sha": sha,
        "archive_sha256": hashlib.sha256(data).hexdigest(),
    }
    (path / f"{key}.json").write_text(json.dumps(manifest), encoding="utf-8")
    (path / f"{key}.tar.gz").write_bytes(data)
    return key, manifest


async def test_benchmark_source_cache_reads_verified_fixed_archive_without_network(runner, tmp_path, monkeypatch):  # noqa: F811
    class Transport:
        async def get(self, *_args):
            raise AssertionError("configured cache must never call network")

    repo, sha, data = "owner/repository", "a" * 40, b"original compressed bytes\x00\xff"
    _write_source_cache(tmp_path, repo, sha, data)
    monkeypatch.setenv("REVIEWFORGE_SOURCE_CACHE_DIR", str(tmp_path))
    client = runner.ReadOnlyGitHub(SimpleNamespace(_client=Transport()))
    assert await client.get_repo_tarball(repo, sha) == data


async def test_benchmark_source_cache_rejects_missing_bad_identity_or_tampering(runner, tmp_path, monkeypatch):  # noqa: F811
    import json

    from source_cache import SourceCacheError

    class Transport:
        async def get(self, *_args):
            raise AssertionError("bad cache must not fall back to network")

    repo, sha, data = "owner/repository", "b" * 40, b"source archive"
    monkeypatch.setenv("REVIEWFORGE_SOURCE_CACHE_DIR", str(tmp_path))
    client = runner.ReadOnlyGitHub(SimpleNamespace(_client=Transport()))
    with pytest.raises(SourceCacheError, match="manifest"):
        await client.get_repo_tarball(repo, sha)
    key, manifest = _write_source_cache(tmp_path, repo, sha, data)
    manifest_path = tmp_path / f"{key}.json"
    for field, value in (("repo", "other/repository"), ("sha", "c" * 40), ("schema", "wrong")):
        manifest_path.write_text(json.dumps({**manifest, field: value}), encoding="utf-8")
        with pytest.raises(SourceCacheError, match="identity"):
            await client.get_repo_tarball(repo, sha)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    (tmp_path / f"{key}.tar.gz").write_bytes(b"source archivf")
    with pytest.raises(SourceCacheError, match="hash"):
        await client.get_repo_tarball(repo, sha)
    (tmp_path / f"{key}.tar.gz").unlink()
    with pytest.raises(SourceCacheError, match="archive unavailable"):
        await client.get_repo_tarball(repo, sha)
    with pytest.raises(SourceCacheError, match="complete"):
        await client.get_repo_tarball(repo, "main")


async def test_unconfigured_benchmark_cache_preserves_network_path(runner, monkeypatch):  # noqa: F811
    class Transport:
        async def get(self, path):
            assert path == "/repos/owner/repository/tarball/main"
            return SimpleNamespace(content=b"network", raise_for_status=lambda: None)

    monkeypatch.delenv("REVIEWFORGE_SOURCE_CACHE_DIR", raising=False)
    client = runner.ReadOnlyGitHub(SimpleNamespace(_client=Transport()))
    assert await client.get_repo_tarball("owner/repository", "main") == b"network"


async def test_bad_cache_cannot_trigger_workspace_contents_fallback(runner, tmp_path, monkeypatch):  # noqa: F811
    from reviewforge.core.state import StateStore
    from reviewforge.tools.workspace import PRHeadWorkspace, WorkspaceUnavailable

    class Client:
        calls = 0

        async def get_file_content(self, *_args):
            self.calls += 1
            return "fallback must not run"

    repo, sha = "owner/repository", "d" * 40
    key, _ = _write_source_cache(tmp_path, repo, sha, b"good")
    (tmp_path / f"{key}.tar.gz").write_bytes(b"bad")
    monkeypatch.setenv("REVIEWFORGE_SOURCE_CACHE_DIR", str(tmp_path))
    original = Client()
    client = runner.ReadOnlyGitHub(original)
    state = StateStore(repo=repo, head_sha=sha, files_changed=["file.py"])
    with pytest.raises(WorkspaceUnavailable):
        await PRHeadWorkspace.build(state, client)
    assert original.calls == 0
    # Even bytes matching their manifest can be an invalid tarball. Archive
    # extraction must not fall back to a different network source either.
    _write_source_cache(tmp_path, repo, sha, b"not-a-valid-tarball")
    client = runner.ReadOnlyGitHub(original)
    with pytest.raises(WorkspaceUnavailable):
        await PRHeadWorkspace.build(state, client)
    assert original.calls == 0
