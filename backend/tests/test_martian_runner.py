"""Benchmark bootstrap and write-interception regressions on both platforms."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

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
    orchestrator, db, raw = await runner._build_runtime(
        tmp_path, "test-model", "en", "hypothesis", generator_max_input_chars=50_000
    )
    try:
        assert orchestrator._pipeline_v4_config.mode == "hypothesis"
        assert orchestrator._pipeline_v4_config.generator_max_input_chars == 50_000
        assert orchestrator._gateway._pipeline_mode == "hypothesis"
        router = orchestrator._model_router
        assert not router._config.profiles
        llm = router.get_llm("hypothesis_generator")._inner
        assert llm.root_async_client.max_retries == 0
        assert llm.root_client.max_retries == 0
        for agent in ("planner", "security_reviewer", "verifier", "hypothesis_generator", "investigator", "editor"):
            _, effective, profile, _, max_tokens = router._resolve(agent)
            assert effective["model"] == "test-model"
            assert profile is None and max_tokens is None
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
async def test_benchmark_actual_sdk_sends_one_http_attempt_on_429(runner, monkeypatch, tmp_path):
    from langchain_core.messages import HumanMessage
    from openai import RateLimitError

    from reviewforge.engine import model_router

    requests = []

    def reject(request):
        requests.append(request)
        return httpx.Response(429, json={"error": {"message": "rpm exhausted", "type": "rate_limit_error"}})

    http = httpx.AsyncClient(transport=httpx.MockTransport(reject))
    original = model_router.ChatOpenAI

    def with_mock_http(**kwargs):
        return original(**kwargs, http_async_client=http)

    monkeypatch.setattr(model_router, "ChatOpenAI", with_mock_http)
    monkeypatch.setenv("GITHUB_TOKEN", "test-placeholder")
    monkeypatch.setenv("LLM_API_KEY", "test-placeholder")
    monkeypatch.setenv("REVIEWFORGE_SETTINGS_DIR", str(tmp_path))
    orchestrator, db, raw = await runner._build_runtime(tmp_path, "test-model", llm_min_interval=0)
    try:
        with pytest.raises(RateLimitError):
            await orchestrator._model_router.get_llm("hypothesis_generator").ainvoke([HumanMessage(content="test")])
        assert len(requests) == 1
        traces = list((tmp_path / "llm-traces").glob("*-error.json"))
        assert len(traces) == 1
        assert json.loads(traces[0].read_text())["status_code"] == 429
    finally:
        await db.close()
        await raw.close()
        await http.aclose()


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


@pytest.mark.asyncio
async def test_bootstrap_failure_closes_database_thread_and_http_client(runner, tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "test-placeholder")
    monkeypatch.setenv("LLM_API_KEY", "test-placeholder")
    monkeypatch.setenv("REVIEWFORGE_SETTINGS_DIR", str(tmp_path))
    databases, clients = [], []
    original_database, original_client = runner.Database, runner.GitHubClient

    class Database(original_database):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            databases.append(self)

    class Client(original_client):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            clients.append(self)

    class FailingRouter(runner.ModelRouter):
        def get_llm(self, *args, **kwargs):
            raise RuntimeError("Model initialization failed")

    monkeypatch.setattr(runner, "Database", Database)
    monkeypatch.setattr(runner, "GitHubClient", Client)
    monkeypatch.setattr(runner, "ModelRouter", FailingRouter)
    with pytest.raises(RuntimeError, match="Model initialization failed"):
        await runner._build_runtime(tmp_path)
    assert databases[0]._db is None
    assert clients[0]._client.is_closed


@pytest.mark.asyncio
async def test_context_runtime_needs_no_model_key_and_blocks_github_writes(runner, tmp_path, monkeypatch):
    import importlib

    module = importlib.import_module("context_snapshot")
    monkeypatch.setattr(module, "REPO_ROOT", runner.REPO_ROOT)
    monkeypatch.setenv("GITHUB_TOKEN", "test-placeholder")
    monkeypatch.setenv("LLM_API_KEY", "")
    gateway, config, db, github = await module.build_context_runtime(tmp_path)
    try:
        assert gateway._pipeline_mode == "hypothesis"
        assert gateway._workspace_max_bytes == config.workspace_max_bytes
        with pytest.raises(RuntimeError, match="blocked a GitHub write"):
            await github._client.post("/repos/example/repo/issues", json={"title": "blocked"})
    finally:
        await db.close()
        await github.close()


@pytest.mark.asyncio
async def test_context_audit_saves_degradation_but_does_not_report_success(runner, tmp_path, monkeypatch):
    import importlib

    from reviewforge.core.config import PipelineV4Config
    from reviewforge.core.database import Database
    from reviewforge.core.specs import build_registry
    from reviewforge.tools.gateway import ToolGateway

    module = importlib.import_module("context_snapshot")

    class GitHub:
        closed = False

        async def get_pr_info(self, *args):
            return {"head": {"sha": "head", "repo": {"full_name": "fork/repo"}}, "base": {"sha": "base"}}

        async def get_pr_files(self, *args):
            return [{"filename": "src/f.py", "patch": "@@ -0,0 +1 @@\n+enabled = True", "additions": 1}]

        async def get_repo_tarball(self, *args):
            raise OSError("archive unavailable")

        async def get_file_content(self, *args):
            return "enabled = True\n"

        async def close(self):
            self.closed = True

    github = GitHub()
    gateway = ToolGateway(build_registry(), github, pipeline_mode="hypothesis")
    db = Database(tmp_path / "context.db")
    await db.connect()

    async def runtime(*args):
        return gateway, PipelineV4Config(), db, github

    monkeypatch.setattr(module, "build_context_runtime", runtime)
    output = tmp_path / "context.json"
    args = SimpleNamespace(repo="owner/repo", pr=1, output=str(output), require_tarball=True)
    with pytest.raises(RuntimeError, match="saved degraded diagnostic only"):
        await module.capture(args)
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["workspace"]["source"] == "api-fallback"
    assert payload["llm_calls"] == 0
    assert all(context["truncated_kinds"] == ["all"] for context in payload["units"].values())
    assert db._db is None
    assert github.closed
    assert not gateway._workspaces


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["complete", "download-error", "truncated", "run-error"])
async def test_benchmark_workspace_preflight_precedes_models_and_reuses_one_snapshot(runner, tmp_path, case):
    import io
    import tarfile

    from reviewforge.core.specs import build_registry
    from reviewforge.tools.gateway import ToolGateway

    data = b"enabled = True\n"
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w:gz") as tar:
        directory = tarfile.TarInfo("repo/")
        directory.type = tarfile.DIRTYPE
        tar.addfile(directory)
        member = tarfile.TarInfo("repo/src/f.py")
        member.size = len(data)
        tar.addfile(member, io.BytesIO(data))

    class GitHub:
        downloads = 0
        comments = {}
        bodies = {}

        async def get_pr_info(self, *args):
            return {"head": {"sha": "head", "repo": {"full_name": "fork/repo"}}, "base": {"sha": "base"}}

        async def get_pr_files(self, *args):
            return [{"filename": "src/f.py", "patch": "@@ -0,0 +1 @@\n+enabled = True", "additions": 1}]

        async def get_repo_tarball(self, *args):
            self.downloads += 1
            if case == "download-error":
                raise OSError("codeload unavailable")
            return archive.getvalue()

        async def get_file_content(self, *args):
            return data.decode()

    class DB:
        async def get_runs(self, **kwargs):
            return []

    class Orchestrator:
        model_calls = 0

        def __init__(self, gateway):
            self._gateway = gateway

        async def run(self, state):
            self.model_calls += 1
            # Production v4 calls this same public API with the same StateStore.
            workspace = await self._gateway.workspace_for(state)
            assert workspace.read("src/f.py") == data.decode()
            if case == "run-error":
                raise RuntimeError("graph interrupted")
            return {"status": "completed"}

    github = GitHub()
    gateway = ToolGateway(
        build_registry(), github, pipeline_mode="hypothesis", workspace_max_bytes=1 if case == "truncated" else 10000
    )
    orchestrator = Orchestrator(gateway)
    item = {"repo": "owner/repo", "pr_number": 1, "golden_url": "https://example.test/pr/1"}
    if case == "complete":
        row = await runner._run_one(item, orchestrator, DB(), github, workspace_preflight_dir=tmp_path)
        assert row["summary"]["status"] == "completed" and row["tokens"] == 0
    else:
        with pytest.raises(RuntimeError, match="graph interrupted" if case == "run-error" else "before model calls"):
            await runner._run_one(item, orchestrator, DB(), github, workspace_preflight_dir=tmp_path)
    assert github.downloads == 1
    assert orchestrator.model_calls == (1 if case in {"complete", "run-error"} else 0)
    assert not gateway._workspaces and not gateway._workspace_states
    receipt = json.loads(next(tmp_path.glob("*.json")).read_text())
    assert receipt["head_sha"] == "head" and receipt["before_model_requests"]
    assert receipt["source"] == ("api-fallback" if case == "download-error" else "tarball")
    assert receipt["truncated"] == (case == "truncated")
