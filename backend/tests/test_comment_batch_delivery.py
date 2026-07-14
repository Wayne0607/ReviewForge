"""Regression coverage for recoverable, batched review-comment delivery."""

from __future__ import annotations

from typing import Any

from reviewforge.core.database import Database
from reviewforge.core.events import EventBus
from reviewforge.core.specs import build_registry
from reviewforge.core.state import Finding, StateStore
from reviewforge.engine.mock_llm import MockChatLLM
from reviewforge.engine.orchestrator import Orchestrator
from reviewforge.tools.gateway import ToolGateway
from reviewforge.tools.github_api import GitHubAPIError


def _patch(line_count: int) -> str:
    return f"@@ -0,0 +1,{line_count} @@\n" + "\n".join(f"+line {line}" for line in range(1, line_count + 1))


def _finding(line: int) -> Finding:
    return Finding(
        id=f"finding-{line}",
        file="app.py",
        line=line,
        severity="error",
        category="security",
        message=f"issue on line {line}",
        suggestion="fix it",
        confidence=0.9,
        reviewer="security_reviewer",
        status="confirmed",
        verified_by="detector-auto",
    )


def _state(line_count: int) -> StateStore:
    return StateStore(
        repo="owner/repo",
        pr_number=17,
        head_sha="head",
        base_sha="base",
        files_changed=["app.py"],
        file_diffs={"app.py": _patch(line_count)},
    )


def _orchestrator(github: Any, db: Database | None = None) -> Orchestrator:
    registry = build_registry()
    llm = MockChatLLM()
    return Orchestrator(
        registry=registry,
        gateway=ToolGateway(registry, github),
        event_bus=EventBus(),
        planner_llm=llm,
        reviewer_llm=llm,
        calibrator_llm=llm,
        db=db,
    )


class _RecordingBatchGitHub:
    def __init__(self) -> None:
        self.batches: list[list[dict[str, Any]]] = []

    async def post_review_comments(self, **kwargs: Any) -> dict[str, Any]:
        comments = kwargs["comments"]
        self.batches.append(comments)
        return {"id": len(self.batches)}


async def test_mixed_valid_invalid_comments_chunk_and_persist_individual_statuses(tmp_path):
    db = Database(tmp_path / "delivery.db")
    await db.connect()
    github = _RecordingBatchGitHub()
    state = _state(85)
    valid = [_finding(line) for line in range(1, 86)]
    invalid = _finding(999)
    findings = [*valid, invalid]

    await db.create_run("run", state.repo, state.pr_number, state.head_sha, state.base_sha)
    for finding in findings:
        state.add_finding(finding)
        await db.insert_finding("run", finding.to_dict())

    result = await _orchestrator(github, db)._post_comments(findings, state)

    assert result.reported == 85
    assert result.permanent_rejections == 1
    assert result.transient_failures == 0
    assert result.retryable is False
    assert [len(batch) for batch in github.batches] == [40, 40, 5]
    assert state.get_finding(invalid.id).status == "false_positive"
    assert state.get_finding(invalid.id).verified_by == "comment-coordinate-validator"
    assert "RIGHT-side diff coordinate" in state.get_finding(invalid.id).verify_reason
    assert all(state.get_finding(finding.id).status == "reported" for finding in valid)
    rows = {row["id"]: row for row in await db.get_findings(run_id="run", limit=200)}
    assert rows[invalid.id]["status"] == "false_positive"
    assert rows[invalid.id]["verified_by"] == "comment-coordinate-validator"
    assert all(rows[finding.id]["status"] == "reported" for finding in valid)
    await db.close()


class _OneInvalidCoordinateGitHub(_RecordingBatchGitHub):
    async def post_review_comments(self, **kwargs: Any) -> dict[str, Any]:
        comments = kwargs["comments"]
        self.batches.append(comments)
        if any(comment["line"] == 5 for comment in comments):
            raise GitHubAPIError(
                "invalid review coordinate",
                status_code=422,
                kind="validation",
                response_body='{"message":"Validation Failed"}',
            )
        return {"id": len(self.batches)}


async def test_batch_validation_failure_is_bisected_and_bad_finding_is_retired():
    github = _OneInvalidCoordinateGitHub()
    state = _state(8)
    findings = [_finding(line) for line in range(1, 9)]
    for finding in findings:
        state.add_finding(finding)

    result = await _orchestrator(github)._post_comments(findings, state)

    assert result.reported == 7
    assert result.permanent_rejections == 1
    assert result.transient_failures == 0
    rejected = state.get_finding("finding-5")
    assert rejected.status == "false_positive"
    assert rejected.verified_by == "github-comment-validation"
    assert "permanently rejected" in rejected.verify_reason
    assert all(state.get_finding(f"finding-{line}").status == "reported" for line in range(1, 9) if line != 5)
    assert [len(batch) for batch in github.batches] == [8, 4, 4, 2, 1, 1, 2]


class _RetryableFailureGitHub(_RecordingBatchGitHub):
    async def post_review_comments(self, **kwargs: Any) -> dict[str, Any]:
        comments = kwargs["comments"]
        self.batches.append(comments)
        raise GitHubAPIError(
            "secondary rate limit persisted after retries",
            status_code=422,
            kind="spam",
            retryable=True,
            response_body='{"message":"spammed the endpoint"}',
        )


async def test_exhausted_retryable_failure_is_not_split_or_marked_reported():
    github = _RetryableFailureGitHub()
    state = _state(3)
    findings = [_finding(line) for line in range(1, 4)]
    for finding in findings:
        state.add_finding(finding)

    result = await _orchestrator(github)._post_comments(findings, state)

    assert result.reported == 0
    assert result.permanent_rejections == 0
    assert result.transient_failures == 3
    assert result.retryable is True
    assert result.errors
    assert len(github.batches) == 1
    assert all(state.get_finding(finding.id).status == "confirmed" for finding in findings)


class _PatchLoadFailureGitHub(_RecordingBatchGitHub):
    async def get_pr_files(self, _repo: str, _pr_number: int) -> list[dict[str, Any]]:
        raise RuntimeError("shared patch cache unavailable")


async def test_patch_load_failure_is_transient_and_leaves_findings_confirmed():
    github = _PatchLoadFailureGitHub()
    state = _state(2)
    state.file_diffs = None
    findings = [_finding(1), _finding(2)]
    for finding in findings:
        state.add_finding(finding)

    result = await _orchestrator(github)._post_comments(findings, state)

    assert result.reported == 0
    assert result.permanent_rejections == 0
    assert result.transient_failures == 2
    assert result.retryable is True
    assert "Unable to load PR patches" in result.errors[0]
    assert github.batches == []
    assert all(state.get_finding(finding.id).status == "confirmed" for finding in findings)


async def test_empty_github_patch_is_transient_not_a_permanent_coordinate_rejection():
    github = _RecordingBatchGitHub()
    state = _state(1)
    state.file_diffs = {"app.py": ""}
    finding = _finding(1)
    state.add_finding(finding)

    result = await _orchestrator(github)._post_comments([finding], state)

    assert result.transient_failures == 1
    assert result.permanent_rejections == 0
    assert result.retryable is True
    assert state.get_finding(finding.id).status == "confirmed"
