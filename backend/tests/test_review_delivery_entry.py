import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import PrivateAttr

from reviewforge.core.config import PipelineV4Config, ReviewForgeConfig
from reviewforge.core.state import StateStore
from reviewforge.engine.orchestrator import Orchestrator
from reviewforge.review_delivery.contracts import Binding
from reviewforge.review_delivery.integration import _recover_corrections
from reviewforge.review_delivery.store import DeliveryStore
from reviewforge.tools.github_api import GitHubClient

BASE, HEAD = "a" * 40, "b" * 40


def call(name, args, turn):
    return AIMessage(content="", tool_calls=[{"id": f"call-{turn}", "name": name, "args": args}])


def source_id(messages):
    for message in reversed(messages):
        if isinstance(message, ToolMessage):
            result = json.loads(message.content)
            if "source_id" in result:
                return result["source_id"]
            if isinstance(result.get("id"), str) and result["id"].startswith("SRC-"):
                return result["id"]
    raise AssertionError("original source was not delivered")


class NativeModel(BaseChatModel):
    _reply = PrivateAttr()
    _inputs = PrivateAttr(default_factory=list)
    _forbidden = PrivateAttr(default=False)

    def __init__(self, reply):
        super().__init__()
        self._reply = reply

    @property
    def _llm_type(self):
        return "delivery-entry-offline"

    def _generate(self, *args, **kwargs):
        raise AssertionError("unexpected synchronous model request")

    def bind_tools(self, tools, **kwargs):
        return self.bind(tools=[convert_to_openai_tool(t) for t in tools], **kwargs)

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        assert not self._forbidden, "resumption must not resample a persisted verdict"
        self._inputs.append((list(messages), kwargs))
        message = self._reply(messages, len(self._inputs))
        message.usage_metadata = {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12}
        return ChatResult(generations=[ChatGeneration(message=message)])


class Workspace:
    source = "tarball"
    truncated = False

    def __init__(self, sha, content):
        self.head_sha, self.content = sha, content
        self.cleaned = 0

    def list_paths(self):
        return ["logic.py"]

    def read_bytes(self, path):
        assert path == "logic.py"
        return self.content

    def cleanup(self):
        self.cleaned += 1


def state():
    return StateStore(
        repo="example/repo",
        pr_number=7,
        base_sha=BASE,
        head_sha=HEAD,
        pr_title="Update arithmetic",
        pr_body="Keep callers working",
        linked_issue_context={
            "schema": "review-v42-no-external-context",
            "repo": "example/repo",
            "pr_number": 7,
            "head_sha": HEAD,
            "status": "excluded-historical-leakage",
            "issues": [],
        },
    )


def product(tmp_path, monkeypatch, lead, verifier, *, lose_post=False, actor_fails=False):
    monkeypatch.setenv("REVIEWFORGE_DELIVERY_STATE_DIR", str(tmp_path))
    remote = {"comments": [], "posts": 0, "patches": 0, "visible": not lose_post, "head": HEAD, "state": "open"}

    def http(request):
        path = request.url.path
        if path == "/user":
            return httpx.Response(403 if actor_fails else 200, json={"id": 81, "type": "User"})
        if path == "/repos/example/repo/pulls/7":
            return httpx.Response(
                200,
                json={
                    "number": 7,
                    "state": remote["state"],
                    "base": {"sha": BASE, "repo": {"full_name": "example/repo"}},
                    "head": {"sha": remote["head"]},
                },
            )
        if request.method == "PATCH":
            assert path.startswith("/repos/example/repo/issues/comments/")
            remote["patches"] += 1
            comment = next(c for c in remote["comments"] if c["id"] == int(path.rsplit("/", 1)[-1]))
            comment["body"] = json.loads(request.content)["body"]
            return httpx.Response(200, json=comment)
        assert path == "/repos/example/repo/issues/7/comments", str(request.url)
        if request.method == "GET":
            return httpx.Response(200, json=remote["comments"] if remote["visible"] else [])
        assert request.method == "POST"
        remote["posts"] += 1
        comment = {
            "id": 100 + remote["posts"],
            "body": json.loads(request.content)["body"],
            "user": {"id": 81},
            "issue_url": "https://api.github.com/repos/example/repo/issues/7",
        }
        remote["comments"].append(comment)
        if lose_post:
            raise httpx.ReadTimeout("response lost after server commit", request=request)
        return httpx.Response(201, json=comment)

    github = GitHubClient("test-only")
    github._client = httpx.AsyncClient(
        base_url=github.BASE_URL, transport=httpx.MockTransport(http), headers={"Authorization": "Bearer test-only"}
    )
    github.get_comparison_base = AsyncMock(return_value=BASE)
    head = Workspace(HEAD, b"result = 0\nother = None\n")
    base = Workspace(BASE, b"result = 1\nother = 2\n")
    monkeypatch.setattr("reviewforge.review_delivery.integration.PRHeadWorkspace.build", AsyncMock(return_value=base))
    gateway = SimpleNamespace(_github=github, workspace_for=AsyncMock(return_value=head), cleanup_workspace=AsyncMock())
    obj = object.__new__(Orchestrator)
    obj._db = None
    obj._gateway = gateway
    obj._model_router = SimpleNamespace(get_llm=lambda name: lead if name == "v42_reviewer" else verifier)
    obj._pipeline_v4_config = PipelineV4Config(
        mode="review_delivery", output_language="zh-CN", v42_provider_max_attempts=1
    )
    obj._events = SimpleNamespace(set_run_id=lambda _: None, emit=lambda *_: None)
    return obj, remote, github


def lead_script(count=1):
    def reply(messages, turn):
        if turn == 1:
            return call("read_source", {"path": "logic.py"}, turn)
        if turn <= count + 1:
            # Read the actual source reference from this session, not a fixture answer ID.
            source = source_id(messages) if turn == 2 else None
            if source is None:
                # Later findings explicitly re-read the durable original.
                working = next(m.content for m in messages if "Current session working set" in str(m.content))
                data = json.loads(working.split(": ", 1)[1])
                source = data["retained_sources"][0]["id"]
            response = call(
                "submit_finding",
                {
                    "claim": {"title": f"问题 {turn - 1}", "body": f"触发场景 {turn - 1} 会返回错误结果。"},
                    "source_ids": [source],
                },
                turn,
            )
            if turn == 2:
                response.tool_calls.insert(
                    0, {"id": "retain-original", "name": "retain_source", "args": {"source_id": source}}
                )
            return response
        return call("finish_review", {"summary": "检查完成", "gaps": []}, turn)

    return reply


def verifier_script(messages, turn):
    if turn % 2:
        task = json.loads(messages[1].content)
        return call("read_saved_source", {"source_id": task["lead_source_ids"][0]}, turn)
    return call(
        "submit_verdict",
        {"verdict": "accept", "source_ids": [source_id(messages)], "reason": "已检查反例，问题仍成立"},
        turn,
    )


async def test_real_entry_two_findings_two_comments_and_restart_no_sampling(tmp_path, monkeypatch):
    lead, verifier = NativeModel(lead_script(2)), NativeModel(verifier_script)
    obj, remote, github = product(tmp_path, monkeypatch, lead, verifier)
    try:
        result = await obj.run(state())
        assert result["status"] == "completed", result["runtime"]
        assert result["confirmed"] == result["comments_sent"] == remote["posts"] == 2
        assert "问题 1" in remote["comments"][0]["body"]
        assert "问题 2" not in remote["comments"][0]["body"]
        assert "问题 2" in remote["comments"][1]["body"]
        assert all(f"/blob/{HEAD}/logic.py" in c["body"] for c in remote["comments"])
        assert result["metrics"]["usage"]["physical_requests"] == 8
        assert result["metrics"]["usage"]["unknown_usage_requests"] == 0
        lead._forbidden = verifier._forbidden = True
        again = await obj.run(state())
        assert again["status"] == "completed", again
        assert remote["posts"] == 2
    finally:
        await github.close()


async def test_actual_entry_lost_post_reconciles_without_resampling(tmp_path, monkeypatch):
    lead, verifier = NativeModel(lead_script()), NativeModel(verifier_script)
    obj, remote, github = product(tmp_path, monkeypatch, lead, verifier, lose_post=True)
    try:
        result = await obj.run(state())
        assert result["status"] == "partial", result
        assert result["confirmed"] == 1 and result["comments_sent"] == 0, result["runtime"]
        assert remote["posts"] == 1
        lead._forbidden = verifier._forbidden = True
        remote["visible"] = True
        result = await obj.run(state())
        assert result["status"] == "completed", result
        assert result["comments_sent"] == remote["posts"] == 1
    finally:
        await github.close()


async def test_unknown_actor_blocks_before_any_model_request(tmp_path, monkeypatch):
    lead, verifier = NativeModel(lead_script()), NativeModel(verifier_script)
    obj, remote, github = product(tmp_path, monkeypatch, lead, verifier, actor_fails=True)
    try:
        result = await obj.run(state())
        assert result["status"] == "partial"
        assert result["runtime"]["error"]["type"] == "HTTPStatusError"
        assert not lead._inputs and not verifier._inputs and remote["posts"] == 0
    finally:
        await github.close()


async def test_actual_entry_delivers_a_while_b_has_missing_policy(tmp_path, monkeypatch):
    def verify(messages, turn):
        if turn < 4:
            return verifier_script(messages, turn)
        return call(
            "submit_verdict",
            {
                "verdict": "needs_evidence",
                "source_ids": [source_id(messages)],
                "reason": "缺少批准的业务规则",
                "missing_fact": "批准的业务规则",
                "attempted": ["仓库文档与原文"],
                "recovery_condition": "提供已批准规则后继续同一发现的核验",
            },
            turn,
        )

    lead, verifier = NativeModel(lead_script(2)), NativeModel(verify)
    obj, remote, github = product(tmp_path, monkeypatch, lead, verifier)
    try:
        result = await obj.run(state())
        assert result["status"] == "partial", result["runtime"]
        assert result["confirmed"] == result["comments_sent"] == remote["posts"] == 1
        assert [item["state"] for item in result["findings"]] == ["accept", "needs_evidence"]
        assert result["verdicts"][1]["missing_fact"] == "批准的业务规则"
        lead._forbidden = verifier._forbidden = True
        resumed = await obj.run(state())
        assert resumed["status"] == "partial" and remote["posts"] == 1
    finally:
        await github.close()


async def test_model_transport_failure_preserves_delivered_finding_and_pending_request(tmp_path, monkeypatch):
    ordinary = lead_script()

    def review(messages, turn):
        if turn == 3:
            raise httpx.ReadError("offline injected model interruption")
        if turn > 3:
            return call("finish_review", {"summary": "检查完成", "gaps": []}, turn)
        return ordinary(messages, turn)

    lead, verifier = NativeModel(review), NativeModel(verifier_script)
    obj, remote, github = product(tmp_path, monkeypatch, lead, verifier)
    try:
        result = await obj.run(state())
        assert result["status"] == "partial" and "error" in result["runtime"]
        assert result["confirmed"] == result["comments_sent"] == remote["posts"] == 1
        assert result["metrics"]["usage"]["unknown_usage_requests"] == 1
        verifier._forbidden = True
        resumed = await obj.run(state())
        assert resumed["status"] == "completed", resumed["runtime"]
        assert resumed["comments_sent"] == remote["posts"] == 1
        assert resumed["metrics"]["usage"]["unknown_usage_requests"] == 1
    finally:
        await github.close()


@pytest.mark.parametrize("exception", [httpx.ConnectError("offline source unavailable"), BlockingIOError("file busy")])
async def test_preflight_failure_is_saved_as_current_partial_not_duplicate(tmp_path, monkeypatch, exception):
    lead, verifier = NativeModel(lead_script()), NativeModel(verifier_script)
    obj, remote, github = product(tmp_path, monkeypatch, lead, verifier)
    obj._gateway.workspace_for = AsyncMock(side_effect=exception)
    try:
        result = await obj.run(state())
        assert result["status"] == "partial"
        assert result["runtime"]["error"]["type"] == type(exception).__name__
        assert json.loads(Path(result["report_path"]).read_text(encoding="utf-8"))["status"] == "partial"
        assert not lead._inputs and not verifier._inputs and remote["posts"] == 0
    finally:
        await github.close()


async def test_same_process_duplicate_does_not_enter_source_or_model_work(tmp_path, monkeypatch):
    lead, verifier = NativeModel(lead_script()), NativeModel(verifier_script)
    obj, remote, github = product(tmp_path, monkeypatch, lead, verifier)
    started, release = asyncio.Event(), asyncio.Event()
    workspace = await obj._gateway.workspace_for(state())

    async def waiting_workspace(_):
        started.set()
        await release.wait()
        return workspace

    obj._gateway.workspace_for = AsyncMock(side_effect=waiting_workspace)
    running = asyncio.create_task(obj.run(state()))
    try:
        await asyncio.wait_for(started.wait(), 5)
        assert (await obj.run(state()))["status"] == "duplicate_skipped"
        assert obj._gateway.workspace_for.await_count == 1 and not lead._inputs
        release.set()
        assert (await running)["status"] == "completed"
        assert remote["posts"] == 1
    finally:
        release.set()
        if not running.done():
            await running
        await github.close()


async def test_entry_recovers_existing_withdrawal_after_pr_closed_and_head_changed(tmp_path, monkeypatch):
    lead, verifier = NativeModel(lead_script()), NativeModel(verifier_script)
    obj, remote, github = product(tmp_path, monkeypatch, lead, verifier)
    try:
        result = await obj.run(state())
        assert result["status"] == "completed", result["runtime"]
        store = DeliveryStore.reopen(Path(result["report_path"]).parent / "review.sqlite")
        try:
            finding = store.list_findings()[0]
            sources = finding["source_ids"]
            store.challenge_finding(finding["id"], "存在反证", sources)
            session = store.create_session("verifier")
            task = store.create_verification(finding["id"], session["id"])
            store.record_delivery(session["id"], sources)
            store.record_verdict(task["id"], "reject", sources, reason="新增反证已推翻原始主张")
        finally:
            store.close()
        lead._forbidden = verifier._forbidden = True
        remote["head"], remote["state"] = "c" * 40, "closed"
        resumed = await obj.run(state())
        assert resumed["status"] == "partial"  # New investigation is correctly refused.
        assert remote["posts"] == remote["patches"] == 1
        assert "新增反证已推翻原始主张" in remote["comments"][0]["body"]
        assert resumed["recovered_corrections"][0]["status"] == "delivered"
        assert resumed["findings"][0]["state"] == "reject"
    finally:
        await github.close()


async def test_copied_foreign_ledger_cannot_publish_corrections(tmp_path):
    store = DeliveryStore(
        tmp_path / "review.sqlite",
        Binding("foreign", "other/repo", 7, BASE, HEAD, {"target_base": BASE}, {"publication_actor": 81}),
    )
    store.close()
    with pytest.raises(ValueError, match="frozen run identity"):
        await _recover_corrections(tmp_path, object(), 81, state(), "current-run")


def test_explicit_mode_and_optional_actor_configuration(tmp_path, monkeypatch):
    path = tmp_path / "reviewforge.yaml"
    path.write_text("pipeline_v4:\n  mode: review_delivery\ngithub:\n  bot_user_id: 12\n", encoding="utf-8")
    assert ReviewForgeConfig.load(path).pipeline_v4.mode == "review_delivery"
    monkeypatch.setenv("GITHUB_BOT_USER_ID", "81")
    assert ReviewForgeConfig.load(path).github.bot_user_id == 81
    assert PipelineV4Config().mode == "legacy"
