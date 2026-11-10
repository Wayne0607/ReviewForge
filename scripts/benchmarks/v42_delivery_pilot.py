"""Run one frozen small-scope episode through the real v4.2 kernel.

The input contains only task/source material, never scoring answers. Invoke this
same harness with each frozen checkout on PYTHONPATH. There are no review-step,
token, evidence or output quotas. Physical attempts retain the production meter.
Scoring is a separate human/evaluator operation; completion is not a quality score.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import json
import tarfile
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import reviewforge.review_v42.store as store_module
from reviewforge.core.config import PipelineV4Config, ReviewForgeConfig
from reviewforge.core.llm_settings import EncryptedLLMSettingsStore, apply_override
from reviewforge.core.state import StateStore
from reviewforge.engine.model_router import ModelRouter
from reviewforge.review_v42.adapter import PinnedToolGateway
from reviewforge.review_v42.contracts import VERIFICATION_DELIVERY_POLICIES, Question, Run
from reviewforge.review_v42.evaluation import summarize_run
from reviewforge.review_v42.integration import PhysicalMeter, _model_signature, _models, _raw_models, _write_json
from reviewforge.review_v42.publication import PublicationOutbox
from reviewforge.review_v42.runtime import ReviewRuntime
from reviewforge.review_v42.snapshot_diff import pinned_regions
from reviewforge.review_v42.store import ReviewStore
from reviewforge.review_v42.tools import EvidenceGateway
from reviewforge.review_v42.verifier import Verifier
from reviewforge.tools.workspace import PRHeadWorkspace


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


class PilotStopRequested(asyncio.CancelledError):
    """Operator interruption, never a quality result or a provider failure."""


class RequestBoundary:
    """Drain active physical calls before honoring an explicit operator stop."""

    def __init__(self, stop_file):
        self.stop_file = stop_file
        self.active = 0
        self.idle = asyncio.Event()
        self.idle.set()

    async def invoke(self, model, *args, **kwargs):
        if self.stop_file.exists():
            await self.idle.wait()
            raise PilotStopRequested("operator requested stop at request boundary")
        self.active += 1
        self.idle.clear()
        try:
            return await model.ainvoke(*args, **kwargs)
        finally:
            self.active -= 1
            if not self.active:
                self.idle.set()


class ControlledModel:
    def __init__(self, model, boundary):
        self.model, self.boundary = model, boundary

    def bind_tools(self, *args, **kwargs):
        return ControlledModel(self.model.bind_tools(*args, **kwargs), self.boundary)

    async def ainvoke(self, *args, **kwargs):
        return await self.boundary.invoke(self.model, *args, **kwargs)


class FixtureArchives:
    """Use the normal tarball workspace path without external source access."""

    def __init__(self, snapshots):
        self.snapshots = snapshots

    async def get_repo_tarball(self, repo, sha):
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as archive:
            for path, content in sorted(self.snapshots[sha].items()):
                raw = content.encode("utf-8")
                member = tarfile.TarInfo("fixture/" + path)
                member.size = len(raw)
                member.mtime = 0
                archive.addfile(member, io.BytesIO(raw))
        return buffer.getvalue()


def input_material(path):
    record = json.loads(path.read_text(encoding="utf-8"))
    allowed = {"id", "title", "description", "base_files", "head_files"}
    if set(record) != allowed or any(not isinstance(record[k], str) for k in ("id", "title", "description")):
        raise ValueError("pilot input must contain only the declared task and source fields")
    for key in ("base_files", "head_files"):
        if not isinstance(record[key], dict) or not record[key]:
            raise ValueError("complete source snapshots required")
        for name, content in record[key].items():
            if not isinstance(name, str) or not isinstance(content, str):
                raise ValueError("source paths and UTF-8 text required")
            if name.startswith("/") or "\\" in name or any(part in {"", ".", ".."} for part in name.split("/")):
                raise ValueError("invalid source path")
    return record


async def episode(args):
    if args.verification_delivery and args.arm != "decision":
        raise ValueError("verification delivery requires the decision arm")
    record = input_material(args.input)
    settings = EncryptedLLMSettingsStore(args.settings).load()
    if settings is None:
        raise ValueError("frozen console settings unavailable")
    config = ReviewForgeConfig.load(args.config)
    router = ModelRouter(apply_override(config.llm, settings), max_retries=0)
    policy = PipelineV4Config(mode="review_v42", output_language="en")
    raw = _raw_models(router, request_timeout=policy.v42_request_timeout_seconds)
    model_binding = digest({name: _model_signature(model) for name, model in raw.items()})
    base, head = digest(record["base_files"]), digest(record["head_files"])
    repo = "delivery-pilot/" + record["id"]
    archives = FixtureArchives({base: record["base_files"], head: record["head_files"]})
    state = StateStore(
        repo=repo,
        head_repo=repo,
        pr_number=1,
        base_sha=base,
        head_sha=head,
        pr_title=record["title"],
        pr_body=record["description"],
    )
    state.linked_issue_context = {
        "schema": "review-v42-no-external-context",
        "repo": repo,
        "pr_number": 1,
        "head_sha": head,
        "status": "unavailable",
        "issues": [],
    }
    head_workspace = await PRHeadWorkspace.build(state, archives)
    base_workspace = await PRHeadWorkspace.build(StateStore(repo=repo, head_repo=repo, head_sha=base), archives)
    try:
        fixed = await pinned_regions(
            base_workspace, head_workspace, run=SimpleNamespace(metadata={"source_line_policy": "git-lf-1"})
        )
        regions = fixed["regions"]
        state.files_changed = list(dict.fromkeys(region["path"] for region in regions))
        state.file_diffs = {
            path: "\n".join(r["patch"] for r in regions if r["path"] == path) for path in state.files_changed
        }
        metadata = {
            "title": record["title"],
            "description": record["description"],
            "output_language": "en",
            "changed_regions": regions,
            "workspace_digest": fixed["head_identity"],
            "base_workspace_digest": fixed["base_identity"],
            "comparison_base": base,
            "shared_investigation": "shared-1",
            "source_line_policy": "git-lf-1",
            "candidate_draft_policy": "candidate-draft-1",
            "frontier_projection_policy": "source-line-union-1",
            "model_binding": model_binding,
            "kernel_revision": args.revision,
            "kernel_digest": hashlib.sha256(
                b"".join(
                    p.name.encode() + p.read_bytes() for p in sorted(Path(store_module.__file__).parent.glob("*.py"))
                )
            ).hexdigest(),
            "pilot_input_hash": digest(record),
            "replicate": args.replicate,
            "retry_policy": {
                "attempts": policy.v42_provider_max_attempts,
                "idle_seconds": policy.v42_request_timeout_seconds,
                "retry_after_seconds": policy.v42_retry_after_max_seconds,
            },
        }
        if args.arm == "decision":
            metadata["decision_cycle_policy"] = "decision-cycle-1"
        if args.verification_delivery:
            metadata["verification_delivery_policy"] = args.verification_delivery
        identity = "pilot_" + digest([repo, base, head, metadata])[:24]
        run = Run(
            identity,
            repo,
            base,
            head,
            environment=fixed["head_identity"],
            model=router.effective("v42_reviewer")["model"],
            policy="v42-units-6",
            metadata=metadata,
        )
        args.output.mkdir(parents=True, exist_ok=True)
        binding = asdict(run)
        binding_path = args.output / "binding.json"
        if binding_path.exists() and json.loads(binding_path.read_text(encoding="utf-8")) != binding:
            raise ValueError("existing episode binding differs; use a new directory")
        _write_json(binding_path, binding)
        store = ReviewStore(args.output / "board.sqlite", run)
        meter = PhysicalMeter(args.output / "requests.jsonl", {"run": identity, "input": digest(record)})
        meter.requests()
        owner = SimpleNamespace(_pipeline_v4_config=policy, _db=None)
        reviewer, verifier_model = _models(owner, run, meter, raw)
        boundary = RequestBoundary(args.output / "operator-stop.json")
        reviewer, verifier_model = (ControlledModel(model, boundary) for model in (reviewer, verifier_model))
        gateway = PinnedToolGateway(head_workspace, state, base_workspace=base_workspace, run=run)
        verifier = Verifier(store, verifier_model, EvidenceGateway(store, gateway))
        error = None
        runtime = None
        print(
            json.dumps(
                {
                    "event": "episode_started",
                    "run": identity,
                    "arm": args.arm,
                    "sample": record["id"],
                    "replicate": args.replicate,
                }
            ),
            flush=True,
        )
        try:
            runtime = await ReviewRuntime(store, reviewer, gateway, verifier).run(regions)
        except BaseException as exc:
            error = type(exc).__name__
            raise
        finally:
            report = PublicationOutbox(store).freeze_batch()["report"]
            if error or runtime is None or runtime["status"] != "completed":
                report["status"] = "partial"
            report["runtime"] = runtime
            report["interruption"] = error
            report["questions"] = [asdict(q) for q in store.list(Question)]
            report["metrics"] = summarize_run(report, meter.requests())
            _write_json(args.output / "report.json", report)
            print(
                json.dumps(
                    {
                        "event": "episode_finished",
                        "run": identity,
                        "status": report["status"],
                        "metrics": report["metrics"],
                    }
                ),
                flush=True,
            )
    finally:
        head_workspace.cleanup()
        base_workspace.cleanup()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--settings", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--arm", choices=["baseline", "decision"], required=True)
    parser.add_argument("--verification-delivery", choices=sorted(VERIFICATION_DELIVERY_POLICIES))
    parser.add_argument("--replicate", type=int, choices=[1, 2, 3], required=True)
    asyncio.run(episode(parser.parse_args()))


if __name__ == "__main__":
    main()
