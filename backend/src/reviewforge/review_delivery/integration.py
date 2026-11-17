"""Product entry for one independently judged finding per PR comment.

Only fixed-source and transport utilities are reused from v4.2. Its work-item,
coverage, integration and publication state machines do not run here.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from reviewforge.core.state import StateStore
from reviewforge.review_v42.adapter import PinnedToolGateway
from reviewforge.review_v42.integration import (
    PhysicalMeter,
    _freeze_knowledge,
    _model_signature,
    _models,
    _process_lock,
    _write_json,
)
from reviewforge.review_v42.snapshot_diff import pinned_regions
from reviewforge.tools.workspace import PRHeadWorkspace

from .contracts import Binding
from .providers import conversation_protocols
from .providers import raw_models as _raw_models
from .publication import FindingPublisher
from .runtime import DeliveryRuntime
from .store import DeliveryStore

_LOCKS: dict[str, asyncio.Lock] = {}


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _directory(orchestrator: Any) -> Path:
    database = getattr(orchestrator, "_db", None)
    return Path(
        os.environ.get("REVIEWFORGE_DELIVERY_STATE_DIR")
        or (Path(database._db_path).parent / "review-delivery" if database else ".reviewforge/review-delivery")
    )


def _check_pr(pr: Any, state: Any) -> None:
    head = pr.get("head") if isinstance(pr, dict) else None
    base = pr.get("base") if isinstance(pr, dict) else None
    if (
        not isinstance(pr, dict)
        or pr.get("number") != state.pr_number
        or not isinstance(head, dict)
        or not isinstance(base, dict)
        or head.get("sha") != state.head_sha
        or base.get("sha") != state.base_sha
        or not isinstance(base.get("repo"), dict)
        or str(base["repo"].get("full_name", "")).casefold() != state.repo.casefold()
        or pr.get("state") != "open"
    ):
        raise ValueError("PR is closed or its fixed base/head changed")


def _usage(rows: list[dict[str, Any]]) -> dict[str, Any]:
    known = [row["usage"] for row in rows if isinstance(row.get("usage"), dict)]
    return {
        "physical_requests": len(rows),
        "input_tokens": sum(item.get("input_tokens", 0) for item in known),
        "output_tokens": sum(item.get("output_tokens", 0) for item in known),
        "unknown_usage_requests": len(rows) - len(known),
        "billing": False,
    }


def _external_index(gateway: PinnedToolGateway, state: Any) -> dict[str, Any]:
    # The index uses the exact same frozen representations as read_knowledge.
    frozen = state.linked_issue_context
    return {
        path: {
            "revision": item["revision"],
            "sha256": hashlib.sha256(item["content"].encode()).hexdigest(),
            "source_url": item["url"],
            "captured_at": frozen.get("issue_captures", {}).get(
                item["url"].rsplit("/", 1)[-1], frozen.get("captured_at", "")
            ),
            "historical_head_time": False,
        }
        for path, item in gateway.context._issues.items()
    }


async def run_review_delivery(orchestrator: Any, state: StateStore) -> dict[str, Any]:
    """Run/resume the isolated finding kernel; mode selection authorizes delivery.

    Tests inject a GitHub HTTP substitute or no client. This function never
    treats absence of a publication client as successful remote delivery.
    """
    if state.ledger is not None:
        raise ValueError("historical checkpoints cannot enter review_delivery")
    if (
        not state.repo
        or type(state.pr_number) is not int
        or state.pr_number < 1
        or not state.base_sha
        or not state.head_sha
    ):
        raise ValueError("review_delivery requires fixed repo/PR/base/head")
    if getattr(orchestrator, "_model_router", None) is None:
        raise ValueError("review_delivery requires an explicit model router")
    # Keep one ledger for this PR head. A changed configuration must be an
    # explicit new state directory, not an unnoticed second set of comments.
    run_id = "fd_" + _digest([state.repo, state.pr_number, state.base_sha, state.head_sha])[:24]
    directory = _directory(orchestrator)
    lock_path = directory / "locks" / (run_id + ".lock")
    lock = _LOCKS.setdefault(str(lock_path.resolve()), asyncio.Lock())
    if lock.locked():
        return {"status": "duplicate_skipped", "mode": "review_delivery", "run_id": run_id}
    async with lock:
        with ExitStack() as stack:
            try:
                stack.enter_context(_process_lock(lock_path))
            except BlockingIOError:
                return {"status": "duplicate_skipped", "mode": "review_delivery", "run_id": run_id}
            return await _run(orchestrator, state, directory, run_id)


async def _recover_corrections(
    root: Path, github: Any, actor_id: int, state: StateStore, run_id: str
) -> list[dict[str, Any]]:
    """An existing comment's correction does not require a new review snapshot."""
    if not (root / "review.sqlite").is_file():
        return []
    existing = DeliveryStore.reopen(root / "review.sqlite")
    try:
        binding = existing.binding
        if (
            binding.run_id != run_id
            or binding.repo != state.repo
            or binding.pr_number != state.pr_number
            or binding.head_sha != state.head_sha
            or binding.source_environment.get("target_base") != state.base_sha
        ):
            raise ValueError("correction ledger differs from the frozen run identity")
        expected_actor = existing.binding.model_config.get("publication_actor")
        if expected_actor != actor_id:
            raise ValueError("publication actor differs from the frozen run")
        publisher = FindingPublisher(existing, github, actor_id)
        results = []
        for item in existing.list_outbox():
            verdict = existing.get_verdict(item["verdict_id"])
            if not item["remote_id"] or verdict["verdict"] not in {"reject", "needs_evidence"}:
                continue
            try:
                results.append(await publisher.publish(item["finding_id"]))
            except Exception as exc:
                results.append({"finding_id": item["finding_id"], "error": str(exc)})
        return results
    finally:
        existing.close()


async def _run(orchestrator: Any, state: StateStore, directory: Path, run_id: str) -> dict[str, Any]:
    database = getattr(orchestrator, "_db", None)
    github = getattr(orchestrator._gateway, "_github", None)
    base_workspace, store = None, None
    corrections: list[dict[str, Any]] = []
    root = directory / run_id
    root.mkdir(parents=True, exist_ok=True)
    try:
        actor_id = None
        if github is not None:
            # Identity and source checks precede every model call.
            actor_id = await github.get_authenticated_user_id()
            if type(actor_id) is not int or actor_id <= 0:
                raise ValueError("publication actor identity is unavailable")
            corrections = await _recover_corrections(root, github, actor_id, state, run_id)
            _check_pr(await github.get_pr_info(state.repo, state.pr_number), state)
        workspace = await orchestrator._gateway.workspace_for(state)
        if github is not None:
            comparison_base = await github.get_comparison_base(
                state.repo, state.base_sha, state.head_repo or state.repo, state.head_sha
            )
            base_workspace = await PRHeadWorkspace.build(
                StateStore(repo=state.repo, head_repo=state.repo, head_sha=comparison_base), github
            )
        else:
            # Explicit offline adapters supply both complete fixed snapshots.
            base_workspace = await orchestrator._gateway.base_workspace_for(state)
            comparison_base = base_workspace.head_sha
        if workspace.head_sha != state.head_sha or base_workspace.head_sha != comparison_base:
            raise ValueError("comparison workspace identity mismatch")
        source_run = SimpleNamespace(metadata={"source_line_policy": "git-lf-1"})
        fixed = await pinned_regions(base_workspace, workspace, run=source_run)
        if github is not None:
            _check_pr(await github.get_pr_info(state.repo, state.pr_number), state)
        regions = fixed["regions"]
        state.files_changed = list(dict.fromkeys(region["path"] for region in regions))
        state.file_diffs = {
            path: "\n".join(region["patch"] for region in regions if region["path"] == path)
            for path in state.files_changed
        }
        knowledge = await _freeze_knowledge(directory, state, github)
        gateway = PinnedToolGateway(workspace, state, github, base_workspace=base_workspace, run=source_run)
        config = orchestrator._pipeline_v4_config
        raw_models = _raw_models(orchestrator._model_router, request_timeout=config.v42_request_timeout_seconds)
        protocols = conversation_protocols(orchestrator._model_router)
        retry = {
            key: getattr(config, key)
            for key in (
                "v42_provider_max_attempts",
                "provider_retry_404",
                "v42_retry_after_max_seconds",
                "v42_request_timeout_seconds",
            )
        }
        binding = Binding(
            run_id=run_id,
            repo=state.repo,
            pr_number=state.pr_number,
            base_sha=comparison_base,
            head_sha=state.head_sha,
            source_environment={
                "head": fixed["head_identity"],
                "base": fixed["base_identity"],
                "external": _external_index(gateway, state),
                "knowledge": knowledge,
                "head_repo": state.head_repo or state.repo,
                "target_base": state.base_sha,
                "task_digest": _digest([state.pr_title, state.pr_body]),
            },
            model_config={
                "models": {key: _model_signature(value) for key, value in raw_models.items()},
                "conversation_protocols": protocols,
                "retry": retry,
                "output_language": config.output_language,
                "publication_actor": actor_id,
                "schema": 1,
                "kernel_digest": _digest(
                    {
                        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                        for path in sorted(Path(__file__).parent.glob("*.py"))
                    }
                ),
            },
        )
        store = DeliveryStore(root / "review.sqlite", binding)
        meter = PhysicalMeter(root / "requests.jsonl", binding.as_dict())
        meter.requests()  # A foreign usage ledger must fail before sampling.
        if database and not await database.get_run(run_id):
            await database.create_run(
                run_id=run_id,
                repo=state.repo,
                pr_number=state.pr_number,
                head_sha=state.head_sha,
                base_sha=state.base_sha,
            )
        orchestrator._events.set_run_id(run_id)
        orchestrator._events.emit(
            "review.started", {"repo": state.repo, "pr": state.pr_number, "mode": "review_delivery"}
        )
        lead, verifier = _models(orchestrator, SimpleNamespace(id=run_id), meter, raw_models)
        publisher = FindingPublisher(store, github, actor_id) if github is not None else None
        task = json.dumps(
            {
                "request": "Find defects introduced by this PR. Each confirmed distinct defect gets its own comment.",
                "repo": state.repo,
                "pr_number": state.pr_number,
                "base": comparison_base,
                "head": state.head_sha,
                "output_language": config.output_language,
                "untrusted_pr_title": state.pr_title,
                "untrusted_pr_description": state.pr_body,
                "changed_regions": [
                    {key: value for key, value in region.items() if key != "patch"} for region in regions
                ],
                "navigation": "Use read_diff to inspect changes and read_source for original evidence.",
            },
            ensure_ascii=False,
        )
        try:
            runtime = await DeliveryRuntime(
                store,
                gateway,
                lead,
                verifier,
                publisher=publisher.drain if publisher else None,
                conversation_protocols=protocols,
                changed_regions=regions,
            ).run(task)
        except Exception as exc:
            # Sessions and requests are already durable. Preserve accepted
            # findings and report the exact failed boundary instead of losing
            # all usable results behind a top-level run exception.
            runtime = {
                "status": "partial",
                "error": {"type": type(exc).__name__, "message": str(exc)},
                "recovery": "Resume this same state directory and frozen configuration; pending work is persisted.",
            }
        deliveries = store.list_outbox()
        findings = store.list_findings()
        undelivered = [item for item in deliveries if item["status"] != "delivered"]
        remote_incomplete = github is not None and bool(undelivered)
        summary = {
            "status": "completed"
            if runtime.get("status") in {"complete", "completed"} and not remote_incomplete
            else "partial",
            "mode": "review_delivery",
            "run_id": run_id,
            "local_only": github is None,
            "confirmed": sum(item["state"] == "accept" for item in findings),
            "total_findings": len(findings),
            "comments_sent": sum(item["status"] == "delivered" for item in deliveries),
            "pending_comments": len(undelivered),
            "recovered_corrections": corrections,
            "runtime": runtime,
            "findings": findings,
            "verdicts": store.list_verdicts(),
            "deliveries": deliveries,
            "metrics": {"usage": _usage(meter.requests()), "quality": None},
            "report_path": str((root / "report.json").resolve()),
        }
        _write_json(root / "report.json", summary)
        public = {
            key: value for key, value in summary.items() if key not in {"findings", "verdicts", "deliveries", "runtime"}
        }
        if database:
            if summary["status"] == "completed":
                await database.complete_run(run_id, public)
            else:
                await database.fail_run(run_id, "review_delivery incomplete", summary=public)
        orchestrator._events.emit("review.completed" if summary["status"] == "completed" else "review.partial", public)
        return summary
    except (Exception, asyncio.CancelledError) as exc:
        # Even a preflight/network failure must leave a current diagnostic,
        # without presenting a previous success report as this attempt's result.
        failure = {
            "status": "partial",
            "mode": "review_delivery",
            "run_id": run_id,
            "local_only": github is None,
            "confirmed": 0,
            "total_findings": 0,
            "comments_sent": 0,
            "findings": [],
            "verdicts": [],
            "deliveries": [],
            "recovered_corrections": corrections,
            "runtime": {
                "status": "partial",
                "error": {"type": type(exc).__name__, "message": str(exc)},
                "recovery": "Resume the original frozen run after restoring the reported unavailable boundary.",
            },
            "report_path": str((root / "report.json").resolve()),
        }
        try:
            if store is None and (root / "review.sqlite").is_file():
                store = DeliveryStore.reopen(root / "review.sqlite")
            if store is not None:
                failure["findings"] = store.list_findings()
                failure["verdicts"] = store.list_verdicts()
                failure["deliveries"] = store.list_outbox()
                failure["confirmed"] = sum(item["state"] == "accept" for item in failure["findings"])
                failure["total_findings"] = len(failure["findings"])
                failure["comments_sent"] = sum(item["status"] == "delivered" for item in failure["deliveries"])
                rows = PhysicalMeter(root / "requests.jsonl", store.binding.as_dict()).requests()
            else:
                rows = []
            failure["metrics"] = {"usage": _usage(rows), "quality": None}
        except Exception as recovery_exc:
            failure["runtime"]["state_recovery_error"] = str(recovery_exc)
        _write_json(root / "report.json", failure)
        if database and await database.get_run(run_id):
            await database.fail_run(run_id, "review_delivery interrupted: " + type(exc).__name__, summary=failure)
        orchestrator._events.emit(
            "review.partial", {key: failure[key] for key in ("run_id", "mode", "status", "runtime")}
        )
        if isinstance(exc, asyncio.CancelledError):
            raise
        return failure
    finally:
        if store is not None:
            store.close()
        if base_workspace is not None:
            cleanup = getattr(base_workspace, "cleanup", None)
            if callable(cleanup):
                await asyncio.to_thread(cleanup)
        await orchestrator._gateway.cleanup_workspace(state)
