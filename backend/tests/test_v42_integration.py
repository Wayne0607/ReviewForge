import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import PrivateAttr

from reviewforge.core.config import LLMConfig, PipelineV4Config
from reviewforge.core.state import StateStore
from reviewforge.engine.model_router import ModelRouter
from reviewforge.engine.orchestrator import Orchestrator
from reviewforge.review_v42.adapter import changed_regions
from reviewforge.review_v42.integration import PhysicalMeter, _model_signature, _raw_models


@pytest.mark.parametrize(
    ("base_url", "model", "stream"),
    [("https://compatible.example/v1", "review-model", True), ("https://api.minimax.io/v1", "MiniMax-M2", False)],
)
def test_transport_selection_preserves_native_anthropic_route(base_url, model, stream, monkeypatch):
    router = ModelRouter(LLMConfig(base_url=base_url, model=model, api_key="test"))
    calls = []

    def get_llm(name, **options):
        calls.append((name, options))
        return object()

    monkeypatch.setattr(router, "get_llm", get_llm)
    assert set(_raw_models(router)) == {"v42_reviewer", "v42_verifier"}
    assert all(options == {"max_retries": 0, "stream_transport": stream} for _, options in calls)
    calls.clear()
    _raw_models(router, request_timeout=900)
    assert all(
        options == {"max_retries": 0, "stream_transport": stream, "request_timeout": 900} for _, options in calls
    )


def test_stream_and_idle_timeout_are_part_of_run_binding():
    original = SimpleNamespace(model_name="model", streaming=False, stream_usage=False, request_timeout=120)
    for key, value in (("streaming", True), ("stream_usage", True), ("request_timeout", 180)):
        changed = SimpleNamespace(**{**vars(original), key: value})
        assert _model_signature(original) != _model_signature(changed)


class NativeModel(BaseChatModel):
    _reply = PrivateAttr()
    _inputs = PrivateAttr(default_factory=list)

    def __init__(self, reply):
        super().__init__()
        self._reply = reply

    @property
    def _llm_type(self):
        return "scripted-native-model"

    def _generate(self, *args, **kwargs):
        raise AssertionError("sync call")

    def bind_tools(self, tools, **kwargs):
        return self.bind(tools=[convert_to_openai_tool(t) for t in tools], **kwargs)

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        self._inputs.append((list(messages), kwargs))
        message = self._reply(messages, len(self._inputs))
        message.usage_metadata = {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12}
        return ChatResult(generations=[ChatGeneration(message=message)])


def tool(name, args):
    return AIMessage(content="", tool_calls=[{"id": "call", "name": name, "args": args}])


def working_state():
    return {
        "question": "Does the changed result preserve its contract?",
        "observations": [],
        "open_questions": [],
        "next_step": "Read the fixed source revision",
    }


class Workspace:
    head_sha = "head"
    digest = "workspace-digest"
    source = "tarball"
    truncated = False

    async def read_async(self, path):
        return {"a.py": "result = 0\n"}.get(path)

    def read_bytes(self, path):
        return {"a.py": b"result = 0\n"}[path]

    def list_paths(self):
        return ["a.py"]


def state():
    return StateStore(
        repo="example/repo",
        pr_number=1,
        head_sha="head",
        base_sha="base",
        files_changed=["a.py"],
        file_diffs={"a.py": "@@ -1 +1 @@\n-result = 1\n+result = 0"},
        pr_title="Change result",
        pr_body="Preserve behavior",
    )


def product(tmp_path, monkeypatch, reviewer, verifier, workspace=None):
    monkeypatch.setenv("REVIEWFORGE_V42_STATE_DIR", str(tmp_path))
    events = []

    class Gateway:
        _github = None
        cleaned = 0

        async def workspace_for(self, state):
            return workspace or Workspace()

        async def ensure_file_diffs(self, state):
            pass

        async def cleanup_workspace(self, state):
            self.cleaned += 1

    obj = object.__new__(Orchestrator)
    obj._db = None
    obj._gateway = Gateway()
    obj._events = SimpleNamespace(set_run_id=lambda run: None, emit=lambda name, data: events.append((name, data)))
    obj._pipeline_v4_config = PipelineV4Config(mode="review_v42", v42_provider_max_attempts=1)
    obj._model_router = SimpleNamespace(get_llm=lambda name: reviewer if name == "v42_reviewer" else verifier)
    return obj, events


async def test_native_product_path_persists_report_and_resumes_without_model_calls(tmp_path, monkeypatch):
    region = changed_regions(state())[0]["id"]

    def review(messages, turn):
        if turn == 1:
            return tool("read_source", {"path": "a.py", "side": "head"})
        if turn == 2:
            receipt = json.loads(next(m.content for m in reversed(messages) if isinstance(m, ToolMessage)))
            assert sum("result = 0" in str(message.content) for message in messages) == 1
            return tool(
                "submit_case",
                {
                    "trigger": "read result",
                    "behavior": "returns zero",
                    "impact": "wrong result",
                    "evidence_refs": [receipt["evidence_ref"]],
                },
            )
        assert any("result = 0" in str(message.content) for message in messages if isinstance(message, HumanMessage))
        return tool("finish_scope", {"checked_regions": [region]})

    def verify(messages, turn):
        packet = json.loads(next(m.content for m in messages if isinstance(m, HumanMessage)))
        ref = packet["evidence"][0]["ref"]["id"]
        citation = {"ref": ref, "quote": "result = 0", "start_line": 1, "end_line": 1}
        return tool(
            "submit_verdict",
            {
                "verdict": "accept",
                "reason": "independently checked",
                "gaps": [],
                "facts": {
                    key: {"statement": key + " behavior", "citations": [citation]}
                    for key in ("trigger", "actual", "expected", "change", "impact")
                },
            },
        )

    reviewer, verifier = NativeModel(review), NativeModel(verify)
    obj, events = product(tmp_path, monkeypatch, reviewer, verifier)
    summary = await obj.run(state())
    assert summary["status"] == "completed", summary
    assert summary["confirmed"] == 1 and summary["comments_sent"] == 0 and summary["local_only"]
    report = json.loads(Path(summary["report_path"]).read_text(encoding="utf-8"))
    assert report["comments"][0]["anchor"] == {"path": "a.py", "line": 1, "side": "RIGHT"}
    assert report["metrics"]["usage"]["physical_requests"] == 4
    assert report["metrics"]["usage"]["input_tokens"] == 40
    assert report["metrics"]["quality"] is None
    assert report["publication_batch_id"]
    schemas = reviewer._inputs[0][1]["tools"]
    read = next(t for t in schemas if t["function"]["name"] == "read_source")
    assert "side" in read["function"]["parameters"]["properties"]
    assert "review_state" in read["function"]["parameters"]["properties"]
    import sqlite3

    with sqlite3.connect(Path(summary["report_path"]).parent / "board.sqlite") as db:
        binding = json.loads(db.execute("SELECT payload FROM run WHERE id=1").fetchone()[0])
        assert binding["policy"] == binding["metadata"]["execution_policy"] == "v42-frontier-4"
        assert db.execute("SELECT count(*) FROM turn_audit").fetchone()[0] >= 3
    again = await obj.run(state())
    assert again["status"] == "completed" and again["run_id"] == summary["run_id"]
    assert len(reviewer._inputs) == 3 and len(verifier._inputs) == 1
    assert obj._gateway.cleaned == 2 and events[-1][0] == "review.completed"


async def test_product_retains_open_navigation_and_does_not_report_clean(tmp_path, monkeypatch):
    import sqlite3

    region = changed_regions(state())[0]["id"]

    def review(messages, turn):
        if turn == 1:
            note = {**working_state(), "open_questions": ["Does the result preserve its existing contract?"]}
            return tool("read_source", {"path": "a.py", "side": "head", "review_state": note})
        return tool("finish_scope", {"checked_regions": [region]})

    reviewer = NativeModel(review)
    verifier = NativeModel(lambda *_: pytest.fail("no candidate requires verification"))
    obj, _ = product(tmp_path, monkeypatch, reviewer, verifier)
    summary = await obj.run(state())
    assert summary["status"] == "partial" and summary["comments_sent"] == 0
    board = Path(summary["report_path"]).parent / "board.sqlite"
    with sqlite3.connect(board) as db:
        notes = [json.loads(row[0]) for row in db.execute("SELECT payload FROM objects WHERE kind='NavigationItem'")]
    assert any(note["kind"] == "question" and "existing contract" in note["text"] for note in notes)


async def test_product_invalid_evidence_reference_is_correctable(tmp_path, monkeypatch):
    region = changed_regions(state())[0]["id"]

    def review(messages, turn):
        if turn == 1:
            return tool("read_evidence", {"ref": "ev_missing"})
        receipt = json.loads(next(m.content for m in reversed(messages) if isinstance(m, ToolMessage)))
        assert "error" in receipt and "question_id" not in receipt
        return tool("finish_scope", {"checked_regions": [region]})

    reviewer = NativeModel(review)
    verifier = NativeModel(lambda *_: pytest.fail("no candidate requires verification"))
    obj, _ = product(tmp_path, monkeypatch, reviewer, verifier)
    summary = await obj.run(state())
    assert summary["status"] == "completed"
    assert not summary["review_v42_report"]["open_questions"]


async def test_incomplete_workspace_fails_before_any_model_request(tmp_path, monkeypatch):
    broken = Workspace()
    broken.truncated = True
    model = NativeModel(lambda *_: pytest.fail("must not call the model"))
    obj, _ = product(tmp_path, monkeypatch, model, model, broken)
    with pytest.raises(ValueError, match="complete pinned"):
        await obj.run(state())
    assert obj._gateway.cleaned == 1
    assert not list(tmp_path.rglob("requests.jsonl"))


async def test_account_failure_is_partial_and_usage_unknown(tmp_path, monkeypatch):
    class AccountError(Exception):
        status_code = 402

    def fail(*_):
        raise AccountError()

    obj, events = product(tmp_path, monkeypatch, NativeModel(fail), NativeModel(fail))
    summary = await obj.run(state())
    assert summary["status"] == "partial"
    report = summary["review_v42_report"]
    assert report["runtime"]["status"] == "provider_blocked"
    assert report["metrics"]["usage"]["unknown_requests"] == 1
    assert not report["comments"] and events[-1][0] == "review.partial"


def test_crashed_meter_tail_stays_explicitly_unknown(tmp_path):
    path = tmp_path / "requests.jsonl"
    path.write_bytes(b'{"logical_request_id":"r","attempt":1,"phase":"started"}\n{"interrupted":')
    meter = PhysicalMeter(path)
    meter.append({"logical_request_id": "new", "attempt": 1, "usage": {"input_tokens": 1, "output_tokens": 2}})
    rows = meter.requests()
    assert len(rows) == 3 and sum(bool(row.get("meter_fragment_unknown")) for row in rows) == 1
    assert rows[0]["phase"] == "started" and rows[-1]["usage"]["output_tokens"] == 2


async def test_product_derives_diff_from_fixed_snapshots_and_reads_merge_base(tmp_path, monkeypatch):
    import io
    import tarfile

    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w:gz") as output:
        entry = tarfile.TarInfo("repo/a.py")
        entry.size = len(b"result = 1\n")
        output.addfile(entry, io.BytesIO(b"result = 1\n"))

    class GitHub:
        async def get_comparison_base(self, repo, base, head_repo, head):
            assert base == "base" and head == "head"
            return "merge-base"

        async def get_repo_tarball(self, repo, sha):
            assert sha == "merge-base"
            return archive.getvalue()

        async def get_pr_files(self, *_):
            pytest.fail("mutable PR file endpoint cannot define a pinned review")

    def review(messages, turn):
        if turn == 1:
            return tool("read_source", {"path": "a.py", "side": "base", "review_state": working_state()})
        if turn == 2:
            receipt = json.loads(next(m.content for m in reversed(messages) if isinstance(m, ToolMessage)))
            assert receipt["content"] == "result = 1\n"
            return tool("read_source", {"path": "a.py", "side": "head", "review_state": working_state()})
        receipt = json.loads(next(m.content for m in reversed(messages) if isinstance(m, ToolMessage)))
        assert receipt["content"] == "result = 0\n"
        context = json.loads(next(m.content for m in messages if isinstance(m, HumanMessage)))
        return tool("finish_scope", {"checked_regions": [r["id"] for r in context["scope"]["regions"]]})

    obj, _ = product(tmp_path, monkeypatch, NativeModel(review), NativeModel(lambda *_: pytest.fail("no case")))
    obj._gateway._github = GitHub()
    prepared = state()
    prepared.file_diffs = {"wrong.py": "unrelated mutable diff"}
    prepared.linked_issue_context = {
        "schema": "review-v42-no-external-context",
        "repo": prepared.repo,
        "pr_number": 1,
        "head_sha": "head",
        "issues": [],
        "status": "excluded-historical-leakage",
    }
    summary = await obj.run(prepared)
    assert summary["status"] == "completed", summary
    run = summary["review_v42_report"]["run"]
    assert run["base"] == "merge-base" and run["metadata"]["target_base"] == "base"
    assert run["metadata"]["base_workspace_digest"].startswith("sha256:")
    assert all(r["path"] == "a.py" for r in run["metadata"]["changed_regions"])
