"""Benchmark bootstrap and write-interception regressions on both platforms."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import httpx
import pytest


@pytest.fixture
def runner(monkeypatch):
    root = Path(__file__).resolve().parents[2]
    monkeypatch.syspath_prepend(str(root / "scripts/benchmarks"))
    monkeypatch.setenv("REVIEWFORGE_REPO_ROOT", str(root))
    # The runner intentionally sets process-local overrides. Register them
    # with pytest's environment restoration before invoking its bootstrap.
    monkeypatch.setenv("REVIEWFORGE_PIPELINE", "legacy")
    monkeypatch.setenv("REVIEWFORGE_OUTPUT_LANGUAGE", "en")
    spec = importlib.util.spec_from_file_location("martian_runner", root / "scripts/benchmarks/martian_runner.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_benchmark_runtime_loads_config_and_blocks_all_real_github_writes(runner, monkeypatch, tmp_path):
    monkeypatch.setenv("GITHUB_TOKEN", "test-placeholder")
    monkeypatch.setenv("LLM_API_KEY", "test-placeholder")
    monkeypatch.setenv("REVIEWFORGE_SETTINGS_DIR", str(tmp_path))
    orchestrator, db, raw = await runner._build_runtime(tmp_path, "test-model", "en", "hypothesis")
    try:
        assert orchestrator._pipeline_v4_config.mode == "hypothesis"
        assert orchestrator._gateway._pipeline_mode == "hypothesis"
        with pytest.raises(RuntimeError, match="blocked a GitHub write"):
            await raw._client.post("/repos/example/repo/issues", json={"title": "should be blocked"})
        client = orchestrator._gateway._github
        await client.post_review_comments(
            repo="example/repo",
            pr_number=1,
            commit_sha="abc",
            comments=[{"file_path": "app.py", "line": 2, "body": "issue"}],
            body="summary",
        )
        assert client.comments[("example/repo", 1)] == [{"path": "app.py", "line": 2, "body": "issue"}]
        assert client.bodies[("example/repo", 1)] == ["summary"]
    finally:
        await db.close()
        await raw.close()


@pytest.mark.asyncio
async def test_read_only_wrapper_preserves_head_tarball_transport(runner):
    from reviewforge.tools.github_api import GitHubClient

    raw = GitHubClient("test-placeholder")
    await raw._client.aclose()
    raw._client = httpx.AsyncClient(
        base_url=raw.BASE_URL, transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"archive"))
    )
    try:
        assert await runner.ReadOnlyGitHub(raw).get_repo_tarball("fork/repo", "head-sha") == b"archive"
    finally:
        await raw.close()


@pytest.mark.asyncio
async def test_provider_trace_survives_token_wrapper_private_call(runner, tmp_path):
    from langchain_core.messages import HumanMessage

    from reviewforge.engine.mock_llm import MockChatLLM
    from reviewforge.engine.token_tracker import RunContext, TrackedChatLLM

    traced = runner.BenchmarkLLM(MockChatLLM(), tmp_path, "test", interval=0)
    llm = TrackedChatLLM(traced, RunContext(), "test")
    await llm.ainvoke([HumanMessage(content="review this code")])
    inputs = list(tmp_path.glob("*-input.json"))
    outputs = list(tmp_path.glob("*-output.json"))
    assert len(inputs) == len(outputs) == 1
    assert json.loads(outputs[0].read_text())["responses"][0]["content"]
