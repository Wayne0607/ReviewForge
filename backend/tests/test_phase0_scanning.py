from __future__ import annotations

import json
import re
from typing import Any

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from reviewforge.core.database import Database
from reviewforge.core.events import EventBus
from reviewforge.core.specs import build_registry
from reviewforge.core.state import Finding, ReviewTask, StateStore
from reviewforge.engine.mock_llm import MockChatLLM
from reviewforge.engine.orchestrator import Orchestrator
from reviewforge.engine.phase0 import finding_identity, scan_changed_files
from reviewforge.tools.gateway import ToolGateway
from reviewforge.tools.mock_github import MockGitHubClient


def _diff(content: str) -> str:
    lines = content.splitlines()
    return f"@@ -0,0 +1,{len(lines)} @@\n" + "\n".join("+" + line for line in lines)


class _DiffGitHub(MockGitHubClient):
    def __init__(self, diffs: dict[str, str | Exception]) -> None:
        super().__init__()
        self.diffs = diffs
        self.pr_files_calls = 0

    async def get_pr_files(self, repo: str, pr_number: int) -> list[dict[str, Any]]:
        self.pr_files_calls += 1
        return [
            {"filename": file_path, "patch": value} for file_path, value in self.diffs.items() if isinstance(value, str)
        ]

    async def get_file_diff(self, repo: str, pr_number: int, file_path: str) -> str:
        value = self.diffs[file_path]
        if isinstance(value, Exception):
            raise value
        return value


class _RejectedCommentGitHub(_DiffGitHub):
    async def post_review_comment(self, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("422 Unprocessable Entity")


class _FailedCommentGitHub(_DiffGitHub):
    async def post_review_comment(self, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("network failure")


class _NoTaskPlanner:
    async def plan(self, state: StateStore, notes: list[Any] | None = None) -> list[ReviewTask]:
        return []


class _ExplodingPlanner:
    async def plan(self, state: StateStore, notes: list[Any] | None = None) -> list[ReviewTask]:
        raise RuntimeError("planner provider unavailable")


class _SecurityOnlyPlanner:
    async def plan(self, state: StateStore, notes: list[Any] | None = None) -> list[ReviewTask]:
        if state.list_tasks():
            return []
        return [ReviewTask(reviewer="security_reviewer", files=["app.py"], rationale="semantic review")]


class _NeverCalledLLM:
    async def ainvoke(self, messages: list[Any]) -> Any:
        raise AssertionError("Planner/reviewer LLM must not be invoked")


class _ConfirmingCalibratorLLM(BaseChatModel):
    calls: int = 0

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        self.calls += 1
        finding_ids = re.findall(r"^- \[([^]]+)]", str(messages[-1].content), re.MULTILINE)
        if '"adjusted_confidence"' not in str(messages[-1].content):
            payload = [
                {
                    "finding_id": finding_id,
                    "verdict": "confirmed",
                    "confidence": 0.98,
                    "reason": "动态用户输入进入危险命令执行接口。",
                }
                for finding_id in finding_ids
            ]
        else:
            payload = [
                {
                    "finding_id": finding_id,
                    "verdict": "confirmed",
                    "adjusted_confidence": 0.98,
                    "challenge": "无法推翻动态输入到危险 sink 的完整证据。",
                }
                for finding_id in finding_ids
            ]
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content=json.dumps(payload, ensure_ascii=False)))]
        )

    @property
    def _llm_type(self) -> str:
        return "confirming-calibrator-test"


@pytest.fixture
async def db_factory():
    opened: list[Database] = []

    async def create(path: Any) -> Database:
        db = Database(path)
        await db.connect()
        opened.append(db)
        return db

    yield create

    for db in reversed(opened):
        await db.close()


def _orchestrator(
    github: _DiffGitHub,
    *,
    reviewer_llm: Any = None,
    calibrator_llm: Any = None,
    db: Database | None = None,
):
    registry = build_registry()
    events = EventBus()
    orchestrator = Orchestrator(
        registry=registry,
        gateway=ToolGateway(registry, github),
        event_bus=events,
        planner_llm=_NeverCalledLLM(),
        reviewer_llm=reviewer_llm or _NeverCalledLLM(),
        calibrator_llm=calibrator_llm or _ConfirmingCalibratorLLM(),
        db=db,
        agentic_default=False,
    )
    return orchestrator, events


def _state(files: list[str]) -> StateStore:
    return StateStore(
        pr_number=11,
        repo="owner/repo",
        head_sha="head",
        base_sha="base",
        files_changed=files,
        diff_summary="phase-zero test diff",
    )


async def test_phase0_scans_security_dependencies_and_quality_and_isolates_read_errors():
    github = _DiffGitHub(
        {
            "app.py": _diff("import os\nos.system(user_command)"),
            "requirements.txt": _diff("requests==2.31.0\nflask>=2.0"),
            "quality.rs": _diff("pub fn parse(raw: &str) -> u32 { raw.parse::<u32>().unwrap() }"),
            "unavailable.py": RuntimeError("patch unavailable"),
        }
    )
    registry = build_registry()
    result = await scan_changed_files(
        ToolGateway(registry, github),
        _state(["app.py", "requirements.txt", "quality.rs", "unavailable.py"]),
    )

    categories = {finding.category for finding in result.findings}
    assert "command-injection" in categories
    assert "dependency-version-range" in categories
    assert "panic-risk" in categories
    assert result.files_scanned == 3
    assert set(result.file_errors) == {"unavailable.py"}
    assert not result.scanner_errors
    assert github.pr_files_calls == 1


async def test_phase0_survives_planner_omission_without_reviewer_llm_tokens():
    github = _DiffGitHub({"app.py": _diff("import os\nos.system(user_command)")})
    calibrator_llm = _ConfirmingCalibratorLLM()
    orchestrator, _ = _orchestrator(github, calibrator_llm=calibrator_llm)
    orchestrator._planner = _NoTaskPlanner()
    state = _state(["app.py"])

    summary = await orchestrator.run(state)

    command_findings = [finding for finding in state.list_findings() if finding.category == "command-injection"]
    assert len(command_findings) == 1
    assert command_findings[0].status == "reported"
    # A stable affirmative adversarial verdict is consensus; the candidate ->
    # confirmed lifecycle transition no longer spends a redundant Judge call.
    assert command_findings[0].verified_by == "adversarial"
    assert calibrator_llm.calls == 1
    assert summary["confirmed"] == 1
    assert not state.list_tasks()


async def test_phase0_survives_planner_exception_and_emits_failure_event():
    github = _DiffGitHub({"app.py": _diff("import os\nos.system(user_command)")})
    orchestrator, events = _orchestrator(github)
    orchestrator._planner = _ExplodingPlanner()
    seen: list[str] = []
    events.subscribe(lambda event: seen.append(event.event_type))
    state = _state(["app.py"])

    summary = await orchestrator.run(state)

    assert summary["confirmed"] == 1
    assert "deterministic_scan.completed" in seen
    assert "planner.failed" in seen
    assert "planner.completed" not in seen


async def test_planner_exception_delivers_phase0_but_run_remains_retryable(tmp_path, db_factory):
    db = await db_factory(tmp_path / "planner-retry.db")
    github = _DiffGitHub({"app.py": _diff("import os\nos.system(user_command)")})
    orchestrator, events = _orchestrator(github, db=db)
    orchestrator._planner = _ExplodingPlanner()
    seen: list[str] = []
    events.subscribe(lambda event: seen.append(event.event_type))

    first = await orchestrator.run(_state(["app.py"]))

    assert first["status"] == "partial"
    assert first["retryable"] is True
    assert first["confirmed"] == 1
    assert len(github.posted_comments) == 1
    runs = await db.get_runs(repo="owner/repo")
    assert len(runs) == 1
    assert runs[0]["status"] == "failed"
    assert '"retryable": true' in runs[0]["summary_json"]
    assert await db.has_active_run_for_head("owner/repo", 11, "head") is False
    assert (await db.get_resumable_run("owner/repo", 11, "head"))["run_id"] == runs[0]["run_id"]
    assert "review.partial" in seen

    orchestrator._planner = _NoTaskPlanner()
    second = await orchestrator.run(_state(["app.py"]))

    assert second.get("status") != "partial"
    assert second["confirmed"] == 1
    assert len(github.posted_comments) == 1, "reported Phase-0 finding must not be posted twice"
    resumed = await db.get_run(runs[0]["run_id"])
    assert resumed["status"] == "completed"
    assert await db.has_active_run_for_head("owner/repo", 11, "head") is True


async def test_phase0_survives_reviewer_llm_failure():
    github = _DiffGitHub({"app.py": _diff("import os\nos.system(user_command)")})
    orchestrator, _ = _orchestrator(github)
    orchestrator._planner = _SecurityOnlyPlanner()
    state = _state(["app.py"])

    summary = await orchestrator.run(state)

    command_findings = [finding for finding in state.list_findings() if finding.category == "command-injection"]
    assert len(command_findings) == 1
    assert command_findings[0].status == "reported"
    assert summary["confirmed"] == 1
    assert summary["tasks_failed"] == 1


async def test_reviewer_detector_overlap_is_ingested_once():
    github = _DiffGitHub({"app.py": _diff("import os\nos.system(user_command)")})
    orchestrator, _ = _orchestrator(
        github,
        reviewer_llm=MockChatLLM(),
        calibrator_llm=_ConfirmingCalibratorLLM(),
    )
    orchestrator._planner = _SecurityOnlyPlanner()
    state = _state(["app.py"])

    await orchestrator.run(state)

    identities = [finding_identity(finding) for finding in state.list_findings()]
    assert len(identities) == len(set(identities))
    assert identities.count(("app.py", 2, "command-injection")) == 1
    assert any(task.reviewer == "security_reviewer" and task.status == "completed" for task in state.list_tasks())
    assert github.pr_files_calls == 1


def _comment_orchestrator(github: _DiffGitHub, db: Database) -> Orchestrator:
    registry = build_registry()
    return Orchestrator(
        registry=registry,
        gateway=ToolGateway(registry, github),
        event_bus=EventBus(),
        planner_llm=MockChatLLM(),
        reviewer_llm=MockChatLLM(),
        calibrator_llm=MockChatLLM(),
        db=db,
        agentic_default=False,
    )


async def _seed_confirmed_finding(db: Database, state: StateStore) -> Finding:
    finding = Finding(
        id="delivery-finding",
        file="app.py",
        line=2,
        severity="error",
        category="command-injection",
        message="dynamic command",
        confidence=0.96,
        reviewer="security_reviewer",
        status="confirmed",
        verified_by="judge",
    )
    state.add_finding(finding)
    await db.create_run("delivery-run", state.repo, state.pr_number, state.head_sha, state.base_sha)
    await db.insert_finding("delivery-run", finding.to_dict())
    return finding


async def test_successful_comment_updates_state_and_database_to_reported(tmp_path, db_factory):
    db = await db_factory(tmp_path / "success.db")
    github = _DiffGitHub({"app.py": _diff("import os\nos.system(user_command)")})
    state = _state(["app.py"])
    finding = await _seed_confirmed_finding(db, state)
    orchestrator = _comment_orchestrator(github, db)

    result = await orchestrator._post_comments([finding], state)
    assert result.reported == 1
    assert result.transient_failures == 0

    assert state.get_finding(finding.id).status == "reported"
    rows = await db.get_findings(run_id="delivery-run")
    assert rows[0]["status"] == "reported"
    assert rows[0]["verified_by"] == "judge"


async def test_permanent_legacy_validation_retires_finding_in_state_and_database(tmp_path, db_factory):
    db = await db_factory(tmp_path / "rejected.db")
    github = _RejectedCommentGitHub({"app.py": _diff("import os\nos.system(user_command)")})
    state = _state(["app.py"])
    finding = await _seed_confirmed_finding(db, state)
    orchestrator = _comment_orchestrator(github, db)

    result = await orchestrator._post_comments([finding], state)

    assert result.permanent_rejections == 1
    assert result.transient_failures == 0
    rejected = state.get_finding(finding.id)
    assert rejected.status == "false_positive"
    assert rejected.verified_by == "github-comment-validation"
    assert "permanently rejected" in rejected.verify_reason
    rows = await db.get_findings(run_id="delivery-run")
    assert rows[0]["status"] == "false_positive"
    assert rows[0]["verified_by"] == "github-comment-validation"


async def test_transient_legacy_failure_leaves_finding_confirmed(tmp_path, db_factory):
    db = await db_factory(tmp_path / "network.db")
    github = _FailedCommentGitHub({"app.py": _diff("import os\nos.system(user_command)")})
    state = _state(["app.py"])
    finding = await _seed_confirmed_finding(db, state)
    orchestrator = _comment_orchestrator(github, db)

    result = await orchestrator._post_comments([finding], state)

    assert result.permanent_rejections == 0
    assert result.transient_failures == 1
    assert result.retryable is True
    assert state.get_finding(finding.id).status == "confirmed"
    rows = await db.get_findings(run_id="delivery-run")
    assert rows[0]["status"] == "confirmed"


async def test_transient_comment_failure_marks_run_partial_and_resumable(tmp_path, db_factory):
    db = await db_factory(tmp_path / "comment-retry.db")
    github = _FailedCommentGitHub({"app.py": _diff("import os\nos.system(user_command)")})
    orchestrator, _events = _orchestrator(github, db=db)
    orchestrator._planner = _NoTaskPlanner()
    state = _state(["app.py"])

    summary = await orchestrator.run(state)

    assert summary["status"] == "partial"
    assert summary["retryable"] is True
    assert summary["comment_delivery"]["transient_failures"] == 1
    assert summary["comment_delivery"]["reported"] == 0
    assert state.list_findings(status="confirmed")
    runs = await db.get_runs(repo=state.repo)
    assert runs[0]["status"] == "failed"
    assert await db.has_active_run_for_head(state.repo, state.pr_number, state.head_sha) is False
    assert await db.get_resumable_run(state.repo, state.pr_number, state.head_sha) is not None


async def test_permanent_comment_validation_allows_run_to_complete(tmp_path, db_factory):
    db = await db_factory(tmp_path / "comment-permanent.db")
    github = _RejectedCommentGitHub({"app.py": _diff("import os\nos.system(user_command)")})
    orchestrator, _events = _orchestrator(github, db=db)
    orchestrator._planner = _NoTaskPlanner()
    state = _state(["app.py"])

    summary = await orchestrator.run(state)

    assert summary.get("status") != "partial"
    assert summary["comment_delivery"]["permanent_rejections"] == 1
    assert summary["comment_delivery"]["transient_failures"] == 0
    assert summary["false_positives"] == 1
    rejected = state.list_findings(status="false_positive")[0]
    assert rejected.verified_by == "github-comment-validation"
    assert rejected.verify_reason
    runs = await db.get_runs(repo=state.repo)
    assert runs[0]["status"] == "completed"
    assert await db.has_active_run_for_head(state.repo, state.pr_number, state.head_sha) is True
