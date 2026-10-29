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
async def test_new_benchmark_without_token_limit_still_records_usage(runner, monkeypatch, tmp_path):
    from test_request_budget import _Model

    from reviewforge.engine.request_budget import BudgetedChatLLM, RequestBudget

    meter = RequestBudget(None)

    async def exercise(_args):
        await BudgetedChatLLM(_Model(), meter, phase="editor").ainvoke([])

    monkeypatch.setattr(runner, "_main_async", exercise)
    await runner.main_async(SimpleNamespace(output=str(tmp_path / "results.json"), pipeline="hypothesis"))
    assert meter.available("editor") is None
    assert meter.snapshot()["reported_tokens"] == 12
    assert not (tmp_path / "authorized-budget.json").exists()


@pytest.mark.asyncio
async def test_runner_native_grant_persists_calls_and_rejects_missing_scope_on_resume(runner, monkeypatch, tmp_path):
    from test_request_budget import _Model

    from reviewforge.engine.request_budget import BudgetedChatLLM, RequestBudget

    invocations = []

    async def exercise(_args):
        invocations.append(True)
        await BudgetedChatLLM(_Model(), RequestBudget(250_000), phase="editor").ainvoke([])

    monkeypatch.setattr(runner, "_main_async", exercise)
    args = SimpleNamespace(output=str(tmp_path / "results.json"), token_budget=10_000, pipeline="hypothesis")
    await runner.main_async(args)
    await runner.main_async(args)
    saved = json.loads((tmp_path / "authorized-budget.json").read_text())
    assert saved["budget"]["reported_tokens"] == 24 and saved["budget"]["request_count"] == 2
    args.token_budget = 0
    with pytest.raises(ValueError, match="original"):
        await runner.main_async(args)
    args.token_budget = 20_000
    with pytest.raises(ValueError, match="changed"):
        await runner.main_async(args)
    args.pipeline = "legacy"
    with pytest.raises(ValueError, match="hypothesis"):
        await runner.main_async(args)
    assert len(invocations) == 2


@pytest.mark.asyncio
async def test_runner_refuses_fresh_credit_for_existing_results(runner, tmp_path):
    output = tmp_path / "results.json"
    output.write_text("[]")
    with pytest.raises(ValueError, match="fresh grant"):
        await runner.main_async(SimpleNamespace(output=str(output), token_budget=10_000, pipeline="hypothesis"))


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
async def test_explicit_legacy_retry_traces_each_sdk_attempt_and_exhausts_locally(runner, monkeypatch, tmp_path):
    from openai import RateLimitError

    from reviewforge.engine import model_router

    requests = []

    def reject(request):
        requests.append(request.content)
        return httpx.Response(429, json={"error": {"message": "temporary"}})

    http = httpx.AsyncClient(transport=httpx.MockTransport(reject))
    original = model_router.ChatOpenAI
    monkeypatch.setattr(model_router, "ChatOpenAI", lambda **kwargs: original(**kwargs, http_async_client=http))
    monkeypatch.setenv("GITHUB_TOKEN", "placeholder")
    monkeypatch.setenv("LLM_API_KEY", "placeholder")
    monkeypatch.setenv("REVIEWFORGE_SETTINGS_DIR", str(tmp_path))
    orchestrator, db, raw = await runner._build_runtime(tmp_path, "test-model", llm_min_interval=0, llm_max_attempts=3)
    try:
        with pytest.raises(RateLimitError):
            await orchestrator._model_router.get_llm("hypothesis_generator").ainvoke("fixed")
        assert len(requests) == 3 and requests[0] == requests[1] == requests[2]
        errors = [json.loads(path.read_text()) for path in (tmp_path / "llm-traces").glob("*-error.json")]
        assert len(errors) == 3
        assert {error["provider_attempt"]["attempt"] for error in errors} == {1, 2, 3}
        assert all(error["usage_status"] == "unknown" for error in errors)
        assert len(list((tmp_path / "provider-attempts").glob("*-finished.json"))) == 3
    finally:
        await db.close()
        await raw.close()
        await http.aclose()


@pytest.mark.asyncio
async def test_pr_failure_does_not_abort_independent_work_and_metadata_freezes_retry(runner, monkeypatch, tmp_path):
    import argparse

    workload = tmp_path / "workload.json"
    output = tmp_path / "new" / "results.json"
    workload.write_text(
        json.dumps(
            [
                {"golden_url": "one", "repo": "owner/repo", "pr_number": 1},
                {"golden_url": "two", "repo": "owner/repo", "pr_number": 2},
            ]
        )
    )
    config = SimpleNamespace(
        generator_max_input_chars=50000,
        protocol_version=3,
        json_output_mode="auto",
        investigator_closure_thinking="default",
        max_tokens_per_pr=250000,
    )
    gateway = SimpleNamespace(cleanup_workspaces=lambda: None)
    router = SimpleNamespace(_config=SimpleNamespace(base_url="https://relay.example/v1", model="same"))
    orchestrator = SimpleNamespace(_model_router=router, _pipeline_v4_config=config, _gateway=gateway)
    calls = []

    class Resource:
        async def close(self):
            pass

    async def runtime(*args):
        assert args[-2:] == (3, True)
        return orchestrator, Resource(), Resource()

    async def run_one(item, *args, **kwargs):
        calls.append(item["golden_url"])
        if item["golden_url"] == "one":
            raise RuntimeError("do not persist private exception body")
        return {
            **item,
            "status": "completed",
            "summary": {"status": "completed"},
            "review_comments": [],
            "tokens": 0,
            "duration_seconds": 0,
        }

    monkeypatch.setattr(runner, "_build_runtime", runtime)
    monkeypatch.setattr(runner, "_run_one", run_one)
    args = argparse.Namespace(
        reviewer_concurrency=4,
        publication_gate_concurrency=1,
        output=str(output),
        workload=str(workload),
        capture_invalid_outputs=False,
        shard_count=1,
        shard_index=0,
        limit=0,
        pipeline="hypothesis",
        model_override="same",
        output_language="en",
        reasoning_effort="",
        llm_min_interval=0,
        thinking="default",
        investigator_thinking="default",
        generator_max_input_chars=0,
        require_complete_workspace=True,
        llm_max_attempts=3,
        llm_retry_404=True,
    )
    await runner.main_async(args)
    results = json.loads(output.read_text())
    assert calls == ["one", "two"]
    assert [row["status"] for row in results] == ["failed", "completed"]
    assert results[0]["error"] == "RuntimeError" and "private" not in output.read_text()
    metadata = json.loads((output.parent / "metadata.json").read_text())
    assert metadata["v4_protocol"]["version"] == 3
    assert metadata["provider_retry"]["max_attempts"] == 3 and metadata["provider_retry"]["retry_404"]
    assert metadata["sdk_max_retries"] == 0


@pytest.mark.asyncio
async def test_v3_runtime_passes_retry_policy_to_product_without_double_wrapping(runner, monkeypatch, tmp_path):
    from reviewforge.engine.provider_retry import RetryChatLLM

    monkeypatch.setenv("GITHUB_TOKEN", "placeholder")
    monkeypatch.setenv("LLM_API_KEY", "placeholder")
    monkeypatch.setenv("REVIEWFORGE_SETTINGS_DIR", str(tmp_path))
    orchestrator, db, raw = await runner._build_runtime(
        tmp_path, "test-model", pipeline="hypothesis", llm_min_interval=0, llm_max_attempts=3, llm_retry_404=True
    )
    try:
        config = orchestrator._pipeline_v4_config
        if getattr(config, "protocol_version", 2) < 3:
            pytest.skip("Protocol v3 configuration is being integrated independently")
        assert config.provider_max_attempts == 3 and config.provider_retry_404
        llm = orchestrator._model_router.get_llm("investigator", max_retries=0)
        assert isinstance(llm, runner.BenchmarkLLM) and not isinstance(llm, RetryChatLLM)
        assert llm._inner.root_async_client.max_retries == 0
    finally:
        await db.close()
        await raw.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "global_mode,investigator_mode", [("disabled", "enabled"), ("enabled", "disabled"), ("disabled", "default")]
)
async def test_benchmark_stage_modes_stay_isolated_in_actual_sdk_requests(
    runner, monkeypatch, tmp_path, global_mode, investigator_mode
):
    from langchain_core.messages import HumanMessage

    from reviewforge.engine import model_router

    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "mode-probe",
                "object": "chat.completion",
                "model": "test-model",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "[]"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    original = model_router.ChatOpenAI
    monkeypatch.setattr(model_router, "ChatOpenAI", lambda **kwargs: original(**kwargs, http_async_client=http))
    monkeypatch.setenv("GITHUB_TOKEN", "test-placeholder")
    monkeypatch.setenv("LLM_API_KEY", "test-placeholder")
    monkeypatch.setenv("REVIEWFORGE_SETTINGS_DIR", str(tmp_path))
    orchestrator, db, raw = await runner._build_runtime(
        tmp_path,
        "test-model",
        llm_min_interval=0,
        thinking=global_mode,
        reasoning_effort="low",
        investigator_thinking=investigator_mode,
    )
    try:
        agents = ["hypothesis_generator", "investigator", "editor", "hypothesis_generator", "investigator"]
        for agent in agents:
            await orchestrator._model_router.get_llm(agent).ainvoke([HumanMessage(content="probe")])
        for agent, payload in zip(agents, requests, strict=True):
            expected = investigator_mode if agent == "investigator" and investigator_mode != "default" else global_mode
            assert payload["thinking"] == {"type": expected}
            assert payload.get("reasoning_effort") == ("low" if expected == "enabled" else None)
            assert payload["model"] == "test-model"
    finally:
        await db.close()
        await raw.close()
        await http.aclose()


@pytest.mark.asyncio
async def test_invalid_investigator_mode_is_rejected_before_opening_clients(runner, monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "GitHubClient", lambda *args, **kwargs: pytest.fail("client opened"))
    with pytest.raises(ValueError, match="investigator thinking"):
        await runner._build_runtime(tmp_path, investigator_thinking="invalid")


@pytest.mark.asyncio
@pytest.mark.parametrize("expected", ["b" * 40, "invalid"])
async def test_changed_predeclared_head_stops_before_files_workspace_or_model(runner, expected):
    class GitHub:
        async def get_pr_info(self, repo, pr_number):
            return {"head": {"sha": "a" * 40}}

        async def get_pr_files(self, *args):
            pytest.fail("Head mismatch must stop before fetching diff or running a model")

    with pytest.raises(RuntimeError, match="predeclared workload; zero model calls"):
        await runner._run_one(
            {"repo": "owner/repo", "pr_number": 42, "expected_head_sha": expected}, None, None, GitHub()
        )


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
async def test_provider_trace_preserves_original_tool_arguments_and_bound_schema(runner, tmp_path):
    from langchain_core.messages import HumanMessage

    from reviewforge.engine.openai_compat import ReasoningContentChatOpenAI

    arguments = '{"unit_id":"foreign","unit_id":"target"}'
    tool = {"type": "function", "function": {"name": "submit", "parameters": {"type": "object"}}}
    reply = {
        "id": "offline",
        "object": "chat.completion",
        "created": 1,
        "model": "offline",
        "choices": [
            {
                "index": 0,
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "one",
                            "type": "function",
                            "function": {"name": "submit", "arguments": arguments},
                        }
                    ],
                },
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=reply))
    ) as client:
        model = ReasoningContentChatOpenAI(
            api_key="offline-test-only",
            base_url="https://example.invalid/v1",
            model="offline",
            max_retries=0,
            http_async_client=client,
        )
        traced = runner.BenchmarkLLM(model, tmp_path, "test", interval=0)
        await traced.ainvoke([HumanMessage(content="review")], tools=[tool], tool_choice="required")
    request = json.loads(next(tmp_path.glob("*-input.json")).read_text())
    response = json.loads(next(tmp_path.glob("*-output.json")).read_text())["responses"][0]
    assert request["request_options"]["tools"] == [tool]
    assert request["request_options"]["tool_choice"] == "required"
    assert response["tool_calls"][0]["args"] == {"unit_id": "target"}
    assert response["raw_tool_calls"][0]["function"]["arguments"] == arguments


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
@pytest.mark.parametrize("case", ["complete", "partial", "download-error", "truncated", "run-error"])
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
            assert state.file_changes["src/f.py"].head_sha == state.head_sha
            assert state.file_changes["src/f.py"].patch_state == "present"
            # Production v4 calls this same public API with the same StateStore.
            workspace = await self._gateway.workspace_for(state)
            assert workspace.read("src/f.py") == data.decode()
            if case == "run-error":
                raise RuntimeError("graph interrupted")
            return {"status": "partial" if case == "partial" else "completed"}

    github = GitHub()
    gateway = ToolGateway(
        build_registry(), github, pipeline_mode="hypothesis", workspace_max_bytes=1 if case == "truncated" else 10000
    )
    orchestrator = Orchestrator(gateway)
    item = {"repo": "owner/repo", "pr_number": 1, "golden_url": "https://example.test/pr/1"}
    preflight_dir = tmp_path / "new" / "workspace-preflight"
    assert not preflight_dir.exists()
    if case in {"complete", "partial"}:
        row = await runner._run_one(item, orchestrator, DB(), github, workspace_preflight_dir=preflight_dir)
        assert row["summary"]["status"] == ("partial" if case == "partial" else "completed")
        assert row["status"] == row["summary"]["status"] and row["tokens"] == 0
    else:
        with pytest.raises(RuntimeError, match="graph interrupted" if case == "run-error" else "before model calls"):
            await runner._run_one(item, orchestrator, DB(), github, workspace_preflight_dir=preflight_dir)
    assert github.downloads == 1
    assert orchestrator.model_calls == (1 if case in {"complete", "partial", "run-error"} else 0)
    assert not gateway._workspaces and not gateway._workspace_states
    receipt = json.loads(next(preflight_dir.glob("*.json")).read_text())
    assert receipt["head_sha"] == "head" and receipt["before_model_requests"]
    assert receipt["source"] == ("api-fallback" if case == "download-error" else "tarball")
    assert receipt["truncated"] == (case == "truncated")
