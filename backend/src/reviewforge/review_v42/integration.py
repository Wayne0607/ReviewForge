"""Experimental v4.2 product entry, isolated from historical ledger protocols.

The entry produces durable local reports until the independent benchmark gates
are accepted. It never sends a GitHub review as a side effect of enabling it.
"""

from __future__ import annotations

import asyncio
import errno
import hashlib
import json
import os
from contextlib import ExitStack, contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import messages_to_dict
from pydantic import PrivateAttr

from reviewforge.core.state import StateStore
from reviewforge.engine.model_router import ModelRouter, _is_minimax
from reviewforge.engine.provider_retry import RetryChatLLM, RetryPolicy, current_retry_attempt
from reviewforge.engine.request_budget import _actual_usage
from reviewforge.review_v42.adapter import PinnedToolGateway, changed_regions
from reviewforge.review_v42.contracts import VERIFICATION_DELIVERY_POLICIES, Run
from reviewforge.review_v42.evaluation import summarize_run
from reviewforge.review_v42.integration_delivery import (
    INTEGRATION_DELIVERY_POLICY,
    INTEGRATION_JSON_CONSTRAINT,
    JSON_REQUEST_OPTIONS,
)
from reviewforge.review_v42.knowledge import SCHEMA as KNOWLEDGE_SCHEMA
from reviewforge.review_v42.knowledge import capture_knowledge, validate_knowledge
from reviewforge.review_v42.publication import PublicationOutbox
from reviewforge.review_v42.runtime import ReviewRuntime
from reviewforge.review_v42.snapshot import snapshot_identity
from reviewforge.review_v42.snapshot_diff import pinned_regions
from reviewforge.review_v42.store import ReviewStore
from reviewforge.review_v42.tools import EvidenceGateway
from reviewforge.review_v42.verifier import Verifier
from reviewforge.tools.github_api import validate_pr_issue_context
from reviewforge.tools.workspace import PRHeadWorkspace

_LOCKS: dict[str, asyncio.Lock] = {}
EXECUTION_POLICY = "v42-units-6"


@contextmanager
def _process_lock(path: Path):
    """Nonblocking OS ownership; process death releases the lock automatically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        if os.name == "nt":
            import msvcrt

            if stream.seek(0, 2) == 0:
                stream.write(b"\0")
                stream.flush()
            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLOCK}:
                    raise BlockingIOError("run is owned by another process") from exc
                raise
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _write_json(path: Path, value: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


class PhysicalMeter:
    """Append physical attempts and receipts before interpreting any response."""

    def __init__(self, path: Path, binding: dict | None = None):
        self.path = path
        self.binding = binding or {}
        # Preserve an interrupted append as an auditable unknown fragment, then
        # start the next event on a fresh line. Never overwrite prior receipts.
        if path.exists() and path.stat().st_size:
            with path.open("rb+") as stream:
                stream.seek(-1, 2)
                if stream.read(1) != b"\n":
                    stream.seek(0, 2)
                    stream.write(b"\n")
                    stream.flush()
                    os.fsync(stream.fileno())

    def append(self, row: dict) -> None:
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({**row, "binding": self.binding}, ensure_ascii=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def requests(self) -> list[dict]:
        requests: dict[tuple[str, int], dict] = {}
        if not self.path.exists():
            return []
        for index, line in enumerate(self.path.read_bytes().splitlines()):
            try:
                row = json.loads(line)
                if (
                    not isinstance(row, dict)
                    or not isinstance(row.get("logical_request_id"), str)
                    or type(row.get("attempt")) is not int
                ):
                    raise ValueError("invalid physical receipt")
            except (ValueError, UnicodeError):
                row = {
                    "logical_request_id": f"unreadable-fragment-{index}",
                    "attempt": 0,
                    "usage": None,
                    "meter_fragment_unknown": True,
                }
            if self.binding and not row.get("meter_fragment_unknown") and row.get("binding") != self.binding:
                raise ValueError("physical usage receipt belongs to another run")
            key = row["logical_request_id"], row["attempt"]
            requests.setdefault(key, {}).update(row)
        return list(requests.values())


class MeteredModel(BaseChatModel):
    """Thin native model wrapper; no prompts, quotas, or hidden reasoning replay."""

    _inner: Any = PrivateAttr()
    _meter: PhysicalMeter = PrivateAttr()
    _db: Any = PrivateAttr()
    _run_id: str = PrivateAttr()
    _agent: str = PrivateAttr()

    def __init__(self, inner: Any, meter: PhysicalMeter, db: Any, run_id: str, agent: str):
        super().__init__()
        self._inner, self._meter, self._db, self._run_id, self._agent = inner, meter, db, run_id, agent

    @property
    def _llm_type(self) -> str:
        return "review-v42-metered"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        raise NotImplementedError("v4.2 uses asynchronous model calls")

    def bind_tools(self, tools, **kwargs):
        bound = self._inner.bind_tools(tools, **kwargs)
        if not isinstance(getattr(bound, "kwargs", None), dict):
            raise TypeError("model must expose native tool binding arguments")
        return self.bind(**bound.kwargs)

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        identity = current_retry_attempt()
        if not identity:
            raise RuntimeError("physical request must be owned by retry wrapper")
        self._meter.append(
            {
                **identity,
                "agent": self._agent,
                "input_chars": len(json.dumps(messages_to_dict(messages), ensure_ascii=False)),
                "tool_schema_chars": len(json.dumps(kwargs.get("tools", []), ensure_ascii=False)),
                **(
                    {"response_constraint": INTEGRATION_JSON_CONSTRAINT}
                    if all(kwargs.get(key) == value for key, value in JSON_REQUEST_OPTIONS.items())
                    else {}
                ),
            }
        )
        result = await self._inner._agenerate(messages, stop, run_manager, **kwargs)
        message = result.generations[0].message if result.generations else None
        metadata = getattr(message, "usage_metadata", None) or {}
        wire = (result.llm_output or {}).get("token_usage") or {}
        prompt, output = (
            metadata.get("input_tokens", wire.get("prompt_tokens")),
            metadata.get("output_tokens", wire.get("completion_tokens")),
        )
        usage = None
        if type(prompt) is int and type(output) is int and min(prompt, output) >= 0:
            cache = (metadata.get("input_token_details") or {}).get(
                "cache_read", (wire.get("prompt_tokens_details") or {}).get("cached_tokens")
            )
            usage = {
                "input_tokens": prompt,
                "output_tokens": output,
                **({"cache_read_tokens": cache} if type(cache) is int and cache >= 0 else {}),
            }
        self._meter.append(
            {**identity, "agent": self._agent, "usage": usage, "reported_total_tokens": _actual_usage(result)}
        )
        if usage is not None and self._db:
            await self._db.record_token_usage(
                run_id=self._run_id,
                agent_name=self._agent,
                prompt_tokens=prompt,
                completion_tokens=output,
                total_tokens=prompt + output,
                model=(result.llm_output or {}).get("model_name", ""),
            )
        return result


def _raw_models(router, *, request_timeout=None):
    result = {}
    for name in ("v42_reviewer", "v42_verifier"):
        if isinstance(router, ModelRouter):
            effective = router.effective(name)
            # The existing MiniMax route uses Anthropic's native protocol.
            # OpenAI SSE assembly must only be selected for compatible routes.
            stream = not _is_minimax(effective["base_url"], effective["model"])
            options = {"request_timeout": request_timeout} if request_timeout is not None else {}
            result[name] = router.get_llm(name, max_retries=0, stream_transport=stream, **options)
        else:
            result[name] = router.get_llm(name)
    return result


def _model_signature(model):
    nodes, seen = [], set()
    while model is not None and id(model) not in seen:
        seen.add(id(model))
        nodes.append(
            {
                "type": type(model).__module__ + "." + type(model).__qualname__,
                **{
                    key: getattr(model, key)
                    for key in (
                        "model_name",
                        "model",
                        "temperature",
                        "max_tokens",
                        "top_p",
                        "reasoning_effort",
                        "thinking",
                        "extra_body",
                        "model_kwargs",
                        "seed",
                        "stop",
                        "timeout",
                        "request_timeout",
                        "streaming",
                        "stream_usage",
                        "v42_stream_transport",
                        "base_url",
                        "openai_api_base",
                    )
                    if hasattr(model, key)
                },
            }
        )
        model = getattr(model, "_inner", None)
    return hashlib.sha256(json.dumps(nodes, sort_keys=True, default=str).encode()).hexdigest()


def _models(orchestrator, run: Run, meter: PhysicalMeter, raw_models):
    config = orchestrator._pipeline_v4_config
    result = []
    for agent in ("v42_reviewer", "v42_verifier"):
        raw = raw_models[agent]

        async def attempt(row, name=agent):
            meter.append({**row, "agent": name})

        result.append(
            RetryChatLLM(
                MeteredModel(raw, meter, getattr(orchestrator, "_db", None), run.id, agent),
                policy=RetryPolicy(
                    max_attempts=config.v42_provider_max_attempts,
                    retry_404=config.provider_retry_404,
                    retry_after_max_seconds=config.v42_retry_after_max_seconds,
                    extended_attempts=True,
                ),
                attempt_sink=attempt,
            )
        )
    return result


async def _freeze_knowledge(directory: Path, state, github) -> dict:
    """Capture external context once; never silently refresh a resumed run."""
    identity = hashlib.sha256(
        json.dumps([state.repo, state.pr_number, state.base_sha, state.head_sha]).encode()
    ).hexdigest()
    path = directory / "knowledge" / (identity + ".json")
    lock = _LOCKS.setdefault(str(path.resolve()), asyncio.Lock())
    async with lock:
        supplied = state.linked_issue_context
        if path.exists():
            frozen = json.loads(path.read_text(encoding="utf-8"))
            supplied_is_exclusion = (
                supplied.get("schema") == "review-v42-no-external-context"
                and supplied.get("status") == frozen.get("status") == "excluded-historical-leakage"
            )
            supplied_is_timeline = supplied.get("schema") == "github-pr-issue-context-v1" and supplied == frozen.get(
                "timeline"
            )
            if supplied and supplied != frozen and not supplied_is_exclusion and not supplied_is_timeline:
                raise ValueError("prepared issue context differs from the frozen run; use a new state directory")
        else:
            frozen = supplied
            if github and frozen.get("schema") != KNOWLEDGE_SCHEMA:
                frozen = await capture_knowledge(state, github)
            if not frozen:
                frozen = {
                    "schema": "review-v42-no-external-context",
                    "repo": state.repo,
                    "pr_number": state.pr_number,
                    "head_sha": state.head_sha,
                    "status": "unavailable",
                    "issues": [],
                }
            path.parent.mkdir(parents=True, exist_ok=True)
            _validate_knowledge(frozen, state)
            _write_json(path, frozen)
        _validate_knowledge(frozen, state)
        state.linked_issue_context = frozen
        state.linked_issues = frozen.get("issues", [])
        return {
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "status": frozen["status"],
            "issue_count": len(state.linked_issues),
            "coverage": "partial",
            "historical_head_time": False,
        }


def _validate_knowledge(frozen: dict, state) -> None:
    if frozen.get("schema") == KNOWLEDGE_SCHEMA:
        validate_knowledge(frozen, state)
    elif frozen.get("schema") == "github-pr-issue-context-v1":
        validate_pr_issue_context(frozen, repo=state.repo, pr_number=state.pr_number, head_sha=state.head_sha)
    elif not (
        frozen.get("schema") == "review-v42-no-external-context"
        and frozen.get("repo") == state.repo
        and frozen.get("pr_number") == state.pr_number
        and frozen.get("head_sha") == state.head_sha
        and frozen.get("issues") == []
        and frozen.get("status") in {"unavailable", "excluded-historical-leakage"}
    ):
        raise ValueError("unbound external context")


def _state_directory(orchestrator) -> Path:
    database = getattr(orchestrator, "_db", None)
    return Path(
        os.environ.get("REVIEWFORGE_V42_STATE_DIR")
        or (Path(database._db_path).parent / "review-v42" if database else ".reviewforge/review-v42")
    )


async def _load_bound_diff(orchestrator, state) -> None:
    github = getattr(orchestrator._gateway, "_github", None)
    if github is None:
        # Explicit offline adapters supply already bound fixture input.
        await orchestrator._gateway.ensure_file_diffs(state)
        return

    def check(pr):
        if pr.get("head", {}).get("sha") != state.head_sha or pr.get("base", {}).get("sha") != state.base_sha:
            raise ValueError("PR base/head changed before fixed diff capture; zero model calls")

    before = await github.get_pr_info(state.repo, state.pr_number)
    check(before)
    files = await github.get_pr_files(state.repo, state.pr_number)
    after = await github.get_pr_info(state.repo, state.pr_number)
    check(after)
    if len({row["filename"] for row in files}) != len(files) or (
        type(after.get("changed_files")) is int and len(files) != after["changed_files"]
    ):
        raise ValueError("incomplete changed-file manifest; zero model calls")
    state.files_changed = [row["filename"] for row in files]
    state.file_diffs = {row["filename"]: row.get("patch") or "" for row in files}


async def run_review_v42(orchestrator, state) -> dict[str, Any]:
    identity = hashlib.sha256(
        json.dumps([state.repo, state.pr_number, state.base_sha, state.head_sha]).encode()
    ).hexdigest()
    with ExitStack() as stack:
        try:
            stack.enter_context(_process_lock(_state_directory(orchestrator) / "locks" / (identity + ".lock")))
        except BlockingIOError:
            return {"status": "duplicate_skipped", "mode": "review_v42"}
        return await _run_review_v42(orchestrator, state)


async def _run_review_v42(orchestrator, state) -> dict[str, Any]:
    """Run/recover the separate kernel and return its durable local report."""
    if state.ledger is not None:
        raise ValueError("historical hypothesis checkpoints cannot enter review_v42")
    if not state.repo or not state.base_sha or not state.head_sha:
        raise ValueError("v4.2 requires fixed repo/base/head")
    if orchestrator._model_router is None:
        raise ValueError("v4.2 requires an explicit model router")
    decision_cycle = os.environ.get("REVIEWFORGE_V42_DECISION_CYCLE", "").strip()
    if decision_cycle and (decision_cycle != "decision-cycle-1" or EXECUTION_POLICY != "v42-units-6"):
        raise ValueError("unsupported v4.2 decision cycle policy")
    verification_delivery = os.environ.get("REVIEWFORGE_V42_VERIFICATION_DELIVERY", "").strip()
    if verification_delivery and (
        verification_delivery not in VERIFICATION_DELIVERY_POLICIES or decision_cycle != "decision-cycle-1"
    ):
        raise ValueError("verification delivery requires decision-cycle-1 and a supported claim delivery policy")
    integration_delivery = os.environ.get("REVIEWFORGE_V42_INTEGRATION_DELIVERY", "").strip()
    if integration_delivery and (
        integration_delivery != INTEGRATION_DELIVERY_POLICY or decision_cycle != "decision-cycle-1"
    ):
        raise ValueError("integration delivery requires decision-cycle-1 and a supported integration delivery policy")

    def option(name: str, decision_default: str = "") -> str:
        value = os.environ.get(name, "").strip()
        if decision_cycle and decision_default:
            if value and value != decision_default:
                raise ValueError(f"incompatible decision cycle option: {name}")
            return decision_default
        return value

    unit_scheduler = os.environ.get("REVIEWFORGE_V42_UNIT_SCHEDULER", "").strip()
    if unit_scheduler and (unit_scheduler != "cooperative-1" or EXECUTION_POLICY != "v42-units-6"):
        raise ValueError("unsupported v4.2 unit scheduler")
    unit_plan = os.environ.get("REVIEWFORGE_V42_UNIT_PLAN", "").strip()
    if unit_plan and (unit_plan != "single-owner-1" or EXECUTION_POLICY != "v42-units-6"):
        raise ValueError("unsupported v4.2 unit plan")
    shared_investigation = option("REVIEWFORGE_V42_SHARED_INVESTIGATION", "shared-1")
    if shared_investigation and (shared_investigation != "shared-1" or EXECUTION_POLICY != "v42-units-6"):
        raise ValueError("unsupported v4.2 shared investigation")
    question_assistant = os.environ.get("REVIEWFORGE_V42_QUESTION_ASSISTANT", "").strip()
    if decision_cycle and question_assistant:
        raise ValueError("decision-cycle-1 does not enable delegated question assistants")
    if question_assistant and (
        question_assistant != "question-delivery-1"
        or EXECUTION_POLICY != "v42-units-6"
        or shared_investigation != "shared-1"
    ):
        raise ValueError("unsupported v4.2 question assistant")
    candidate_draft = option("REVIEWFORGE_V42_CANDIDATE_DRAFT", "candidate-draft-1")
    if candidate_draft and (
        candidate_draft != "candidate-draft-1"
        or EXECUTION_POLICY != "v42-units-6"
        or shared_investigation != "shared-1"
    ):
        raise ValueError("unsupported v4.2 candidate draft policy")
    line_policy = "git-lf-1"
    frontier_projection = option("REVIEWFORGE_V42_FRONTIER_PROJECTION", "source-line-union-1")
    if frontier_projection and (
        frontier_projection != "source-line-union-1"
        or EXECUTION_POLICY != "v42-units-6"
        or shared_investigation != "shared-1"
        or line_policy != "git-lf-1"
    ):
        raise ValueError("unsupported v4.2 frontier projection policy")
    line_run = SimpleNamespace(metadata={"source_line_policy": line_policy})
    database = getattr(orchestrator, "_db", None)
    directory = _state_directory(orchestrator)
    workspace = await orchestrator._gateway.workspace_for(state)
    base_workspace = None
    try:
        if workspace.source != "tarball" or workspace.truncated:
            raise ValueError("v4.2 requires a complete pinned workspace")
        github = getattr(orchestrator._gateway, "_github", None)
        review_base, base_identity = state.base_sha, ""
        if github is not None:
            review_base = await github.get_comparison_base(
                state.repo, state.base_sha, state.head_repo or state.repo, state.head_sha
            )
            base_workspace = await PRHeadWorkspace.build(
                StateStore(repo=state.repo, head_repo=state.repo, head_sha=review_base), github
            )
            if base_workspace.head_sha != review_base or workspace.head_sha != state.head_sha:
                raise ValueError("comparison workspace identity mismatch")
            fixed = await pinned_regions(base_workspace, workspace, run=line_run)
            regions, source_identity, base_identity = fixed["regions"], fixed["head_identity"], fixed["base_identity"]
            state.files_changed = list(dict.fromkeys(region["path"] for region in regions))
            state.file_diffs = {
                path: "\n".join(region["patch"] for region in regions if region["path"] == path)
                for path in state.files_changed
            }
        else:
            # Only explicitly injected offline adapters bypass GitHub snapshot capture.
            await _load_bound_diff(orchestrator, state)
            regions = changed_regions(state, run=line_run)
            source_identity = await snapshot_identity(workspace)
        knowledge = await _freeze_knowledge(directory, state, github)
        router = orchestrator._model_router
        models = (
            {
                name: {key: value for key, value in router.effective(name).items() if key in {"model", "base_url"}}
                for name in ("v42_reviewer", "v42_verifier")
            }
            if hasattr(router, "effective")
            else {}
        )
        # Endpoint identity affects binding but endpoint strings/credentials are not saved.
        raw_models = _raw_models(router, request_timeout=orchestrator._pipeline_v4_config.v42_request_timeout_seconds)
        retry = {
            key: getattr(orchestrator._pipeline_v4_config, key)
            for key in (
                "v42_provider_max_attempts",
                "provider_retry_404",
                "v42_retry_after_max_seconds",
                "v42_request_timeout_seconds",
            )
        }
        model_binding = hashlib.sha256(
            json.dumps(
                [models, {k: _model_signature(v) for k, v in raw_models.items()}, retry], sort_keys=True
            ).encode()
        ).hexdigest()
        kernel = hashlib.sha256(
            b"".join(p.name.encode() + p.read_bytes() for p in sorted(Path(__file__).parent.glob("*.py")))
        ).hexdigest()
        metadata = {
            "pr_number": state.pr_number,
            "workspace_digest": source_identity,
            "base_workspace_digest": base_identity,
            "comparison_base": review_base,
            "target_base": state.base_sha,
            "changed_regions": regions,
            "title": state.pr_title,
            "description": state.pr_body,
            "output_language": orchestrator._pipeline_v4_config.output_language,
            "model_binding": model_binding,
            "knowledge": knowledge,
            "kernel_digest": kernel,
            "execution_policy": EXECUTION_POLICY,
            "source_line_policy": line_policy,
        }
        if unit_scheduler:
            # Scheduler semantics are part of the immutable run identity.
            metadata["unit_scheduler"] = unit_scheduler
        if unit_plan:
            # Ownership semantics are also bound to the immutable run identity.
            metadata["unit_plan"] = unit_plan
        if shared_investigation:
            metadata["shared_investigation"] = shared_investigation
        if question_assistant:
            metadata["question_assistant_policy"] = question_assistant
            metadata["question_fencing_policy"] = "question-input-1"
        if candidate_draft:
            metadata["candidate_draft_policy"] = candidate_draft
        if frontier_projection:
            metadata["frontier_projection_policy"] = frontier_projection
        if decision_cycle:
            metadata["decision_cycle_policy"] = decision_cycle
        if verification_delivery:
            metadata["verification_delivery_policy"] = verification_delivery
        if integration_delivery:
            metadata["integration_delivery_policy"] = integration_delivery
            if getattr(raw_models["v42_reviewer"], "v42_stream_transport", False) is True:
                metadata["integration_response_constraint"] = INTEGRATION_JSON_CONSTRAINT
        identity = hashlib.sha256(
            json.dumps(
                [state.repo, state.pr_number, state.base_sha, state.head_sha, metadata],
                sort_keys=True,
                ensure_ascii=False,
            ).encode()
        ).hexdigest()[:24]
        run = Run(
            id="v42_" + identity,
            repo=state.repo,
            base=review_base,
            head=state.head_sha,
            environment=source_identity,
            policy=EXECUTION_POLICY,
            model=json.dumps({k: v.get("model", "") for k, v in models.items()}, sort_keys=True),
            metadata=metadata,
        )
        root = directory / run.id
        root.mkdir(parents=True, exist_ok=True)
        lock = _LOCKS.setdefault(str(root.resolve()), asyncio.Lock())
        if lock.locked():
            return {"status": "duplicate_skipped", "mode": "review_v42", "run_id": run.id}
        async with lock:
            orchestrator._events.set_run_id(run.id)
            store = ReviewStore(root / "board.sqlite", run)
            if database and not await database.get_run(run.id):
                await database.create_run(
                    run_id=run.id,
                    repo=state.repo,
                    pr_number=state.pr_number,
                    head_sha=state.head_sha,
                    base_sha=state.base_sha,
                )
            meter = PhysicalMeter(
                root / "requests.jsonl", {"run": run.id, "repo": run.repo, "base": run.base, "head": run.head}
            )
            meter.requests()  # Reject foreign receipts before any new model call.
            reviewer, verifier_model = _models(orchestrator, run, meter, raw_models)
            gateway = PinnedToolGateway(workspace, state, github, base_workspace=base_workspace, run=run)
            verifier = Verifier(store, verifier_model, EvidenceGateway(store, gateway))
            orchestrator._events.emit(
                "review.started", {"repo": state.repo, "pr": state.pr_number, "mode": "review_v42"}
            )
            try:
                runtime = await ReviewRuntime(store, reviewer, gateway, verifier).run(regions)
                batch = PublicationOutbox(store).freeze_batch()
                report = batch["report"]
                report["publication_batch_id"] = batch["id"]
                if runtime["status"] != "completed":
                    report["status"] = "partial"
                report["runtime"] = runtime
                report["metrics"] = summarize_run(report, meter.requests())
                _write_json(root / "report.json", report)
                summary = {
                    "status": "completed" if report["status"] == "complete" else "partial",
                    "mode": "review_v42",
                    "run_id": run.id,
                    "local_only": True,
                    "report_path": str((root / "report.json").resolve()),
                    "confirmed": sum(item["verdict"] == "accept" for item in report["verified"]),
                    "total_findings": len(runtime.get("cases", [])),
                    "comments_sent": 0,
                    "review_v42_report": report,
                }
                if database:
                    if summary["status"] == "completed":
                        await database.complete_run(
                            run.id, {k: v for k, v in summary.items() if k != "review_v42_report"}
                        )
                    else:
                        await database.fail_run(
                            run.id,
                            "review_v42 incomplete",
                            summary={k: v for k, v in summary.items() if k != "review_v42_report"},
                        )
                orchestrator._events.emit(
                    "review.completed" if summary["status"] == "completed" else "review.partial",
                    {k: v for k, v in summary.items() if k != "review_v42_report"},
                )
                return summary
            except (Exception, asyncio.CancelledError) as exc:
                if database:
                    await database.fail_run(run.id, "review_v42 interrupted: " + type(exc).__name__)
                raise
    finally:
        if base_workspace is not None:
            await asyncio.to_thread(base_workspace.cleanup)
        await orchestrator._gateway.cleanup_workspace(state)
