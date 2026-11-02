"""Frozen v4.2 PR knowledge: timeline plus explicit same-repository references."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Any

from reviewforge.tools.github_api import _valid_issue_snapshot, valid_repo, validate_pr_issue_context

SCHEMA = "review-v42-knowledge-v1"


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def _declared(body: str, repo: str, pr_number: int) -> list[int]:
    # Prose is only a retrieval lead. The fetched issue itself is validated independently.
    shorthand = re.findall(r"\b(?:fix(?:es|ed)?|clos(?:e|es|ed)|resolv(?:e|es|ed))\s*:?\s+#([1-9][0-9]*)\b", body, re.I)
    urls = re.findall(r"https://github\.com/" + re.escape(repo) + r"/issues/([1-9][0-9]*)(?![A-Za-z0-9_])", body, re.I)
    return sorted({int(number) for number in shorthand + urls if int(number) != pr_number})


def _failure(operation: str, exc: Exception, number: int | None = None) -> dict[str, Any]:
    response = getattr(exc, "response", None)
    item = {
        "operation": operation,
        "error_type": type(exc).__name__,
        "status_code": getattr(response, "status_code", None) or getattr(exc, "status_code", None),
    }
    if number is not None:
        item["issue_number"] = number
    return item


def _identity(state: Any) -> dict[str, Any]:
    repo, number, head = state.repo, state.pr_number, state.head_sha
    if not valid_repo(repo) or type(number) is not int or number < 1 or not isinstance(head, str) or not head:
        raise ValueError("invalid knowledge snapshot identity")
    body = getattr(state, "pr_body", "") or ""
    if not isinstance(body, str):
        raise ValueError("invalid PR description")
    return {
        "repo": repo,
        "pr_number": number,
        "head_sha": head,
        "pr_body_sha256": hashlib.sha256(body.encode()).hexdigest(),
    }


async def capture_knowledge(state: Any, github: Any) -> dict[str, Any]:
    identity = _identity(state)
    body = getattr(state, "pr_body", "") or ""
    original = getattr(state, "linked_issue_context", {}) or {}
    if original.get("schema") == SCHEMA:
        return validate_knowledge(original, state)
    excluded = original.get("status") == "excluded-historical-leakage"
    refs = [] if excluded else _declared(body, identity["repo"], identity["pr_number"])
    snapshot = {
        "schema": SCHEMA,
        **identity,
        "captured_at": datetime.now(UTC).isoformat(),
        "historical_head_time": False,
        "coverage": "partial",
        "declared_refs": refs,
        "timeline": None,
        "issues": [],
        "issue_captures": {},
        "failures": [],
        "status": "unavailable",
    }
    if excluded:
        snapshot["status"] = "excluded-historical-leakage"
    else:
        timeline = None
        if original.get("schema") == "github-pr-issue-context-v1":
            timeline = validate_pr_issue_context(
                original, repo=identity["repo"], pr_number=identity["pr_number"], head_sha=identity["head_sha"]
            )
            snapshot["timeline"] = timeline
        elif github is not None and callable(getattr(github, "get_pr_issue_context", None)):
            try:
                timeline = await github.get_pr_issue_context(
                    identity["repo"], identity["pr_number"], identity["head_sha"]
                )
                timeline = validate_pr_issue_context(
                    timeline, repo=identity["repo"], pr_number=identity["pr_number"], head_sha=identity["head_sha"]
                )
                snapshot["timeline"] = timeline
            except Exception as exc:
                snapshot["failures"].append(_failure("timeline", exc))
        else:
            snapshot["failures"].append({"operation": "timeline", "error_type": "Unavailable", "status_code": None})
        by_number = {issue["number"]: issue for issue in (timeline or {}).get("issues", [])}
        issue_captures = {str(number): timeline["captured_at"] for number in by_number}
        for number in refs:
            if number in by_number:
                continue
            try:
                if github is None or getattr(github, "_client", None) is None:
                    raise RuntimeError("issue API unavailable")
                response = await github._client.get(f"/repos/{identity['repo']}/issues/{number}")
                response.raise_for_status()
                issue = response.json()
                if not _valid_issue_snapshot(issue, identity["repo"], number):
                    raise ValueError("invalid issue snapshot")
                by_number[number] = issue
                issue_captures[str(number)] = snapshot["captured_at"]
            except Exception as exc:
                snapshot["failures"].append(_failure("declared_issue", exc, number))
        snapshot["issues"] = [by_number[n] for n in sorted(by_number)]
        snapshot["issue_captures"] = issue_captures
        timeline_failed = (
            timeline is None or bool(timeline.get("failures")) or timeline.get("status") in {"partial", "unavailable"}
        )
        if snapshot["failures"] or timeline_failed:
            snapshot["status"] = "partial" if snapshot["issues"] else "unavailable"
        else:
            snapshot["status"] = "available" if snapshot["issues"] else "no-linked-issues"
    snapshot["snapshot_sha256"] = _digest(snapshot)
    return snapshot


def validate_knowledge(snapshot: Any, state: Any) -> dict[str, Any]:
    identity = _identity(state)
    if (
        not isinstance(snapshot, dict)
        or snapshot.get("schema") != SCHEMA
        or any(snapshot.get(k) != v for k, v in identity.items())
    ):
        raise ValueError("knowledge snapshot identity mismatch")
    if snapshot.get("snapshot_sha256") != _digest({k: v for k, v in snapshot.items() if k != "snapshot_sha256"}):
        raise ValueError("knowledge snapshot digest mismatch")
    try:
        captured = datetime.fromisoformat(snapshot["captured_at"].replace("Z", "+00:00"))
    except (KeyError, AttributeError, ValueError, TypeError) as exc:
        raise ValueError("invalid capture timestamp") from exc
    if (
        captured.tzinfo is None
        or captured.utcoffset() is None
        or snapshot.get("historical_head_time") is not False
        or snapshot.get("coverage") != "partial"
    ):
        raise ValueError("invalid snapshot timing or coverage")
    status = snapshot.get("status")
    refs = snapshot.get("declared_refs")
    excluded = status == "excluded-historical-leakage"
    expected = (
        [] if excluded else _declared(getattr(state, "pr_body", "") or "", identity["repo"], identity["pr_number"])
    )
    if (
        status not in {"available", "partial", "unavailable", "no-linked-issues", "excluded-historical-leakage"}
        or refs != expected
    ):
        raise ValueError("invalid declared issue references")
    timeline = snapshot.get("timeline")
    if timeline is not None:
        timeline = validate_pr_issue_context(
            timeline, repo=identity["repo"], pr_number=identity["pr_number"], head_sha=identity["head_sha"]
        )
    issues = snapshot.get("issues")
    failures = snapshot.get("failures")
    if not isinstance(issues, list) or not isinstance(failures, list) or any(not isinstance(f, dict) for f in failures):
        raise ValueError("invalid snapshot issue list or failures")
    if excluded and (timeline is not None or issues or failures):
        raise ValueError("historical exclusion must not contain external knowledge")
    allowed = set(refs) | {issue["number"] for issue in (timeline or {}).get("issues", [])}
    numbers = []
    for issue in issues:
        if (
            not isinstance(issue, dict)
            or not _valid_issue_snapshot(issue, identity["repo"], issue.get("number"))
            or issue["number"] not in allowed
        ):
            raise ValueError("issue lacks a valid same-repository source")
        numbers.append(issue["number"])
    if numbers != sorted(set(numbers)):
        raise ValueError("duplicate or unordered issue snapshot")
    captures = snapshot.get("issue_captures")
    if not isinstance(captures, dict) or set(captures) != {str(number) for number in numbers}:
        raise ValueError("issue capture timestamps missing")
    for timestamp in captures.values():
        try:
            captured_issue = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValueError("invalid issue capture timestamp") from exc
        if captured_issue.tzinfo is None or captured_issue.utcoffset() is None:
            raise ValueError("issue capture timestamp requires timezone")
    by_number = {issue["number"]: issue for issue in issues}
    if any(by_number.get(issue["number"]) != issue for issue in (timeline or {}).get("issues", [])):
        raise ValueError("timeline issue snapshot differs from its original")
    if any(captures[str(issue["number"])] != timeline["captured_at"] for issue in (timeline or {}).get("issues", [])):
        raise ValueError("timeline issue capture time differs from its original")
    failed_refs = {
        failure.get("issue_number")
        for failure in failures
        if failure.get("operation") == "declared_issue" and type(failure.get("issue_number")) is int
    }
    if not set(refs) <= set(numbers) | failed_refs or not failed_refs <= set(refs):
        raise ValueError("declared issue lacks a snapshot or failure receipt")
    if not excluded:
        failed = (
            bool(failures)
            or timeline is None
            or bool(timeline.get("failures"))
            or timeline.get("status") in {"partial", "unavailable"}
        )
        expected_status = (
            ("partial" if issues else "unavailable") if failed else ("available" if issues else "no-linked-issues")
        )
        if status != expected_status:
            raise ValueError("snapshot status does not match its coverage")
    return json.loads(json.dumps(snapshot, ensure_ascii=False))
