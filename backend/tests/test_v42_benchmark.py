from types import SimpleNamespace

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
