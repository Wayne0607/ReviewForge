"""Bounded, predeclared protocol diagnostics; never a PR quality score.

Inputs contain immutable benchmark prompts and candidate facts, never goldens.
Closure replay requires every saved source excerpt to match a freshly fetched
pinned head file. Counterevidence investigation uses an explicitly partial
source subset; absence from that subset cannot prove repository-wide absence.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from dataclasses import asdict
from pathlib import Path

from langchain_core.messages import HumanMessage, SystemMessage
from martian_runner import StateStore, _atomic_json, _build_runtime

from reviewforge.engine.context_pack import ContextPack, ContextSlice, UnitContext
from reviewforge.engine.hypothesis import Hypothesis, Observation
from reviewforge.engine.hypothesis_generator import HypothesisGenerator
from reviewforge.engine.investigator import Investigator, build_workspace_executor
from reviewforge.engine.llm_protocol_v4 import request_options
from reviewforge.engine.semantic_diff import SemanticChangeSet, SemanticUnit, UnitKind
from reviewforge.engine.token_tracker import RunContext, TrackedChatLLM
from reviewforge.tools.workspace import GrepHit


class SourceSubset:
    source = "pinned-diagnostic-subset"

    def __init__(self, files):
        self.files = files

    def read(self, path, *args):
        return self.files.get(path)

    def grep(self, pattern, *, max_hits=20, **kwargs):
        import re

        hits = []
        for path, source in self.files.items():
            for line, text in enumerate(source.splitlines(), 1):
                if re.search(pattern, text):
                    hits.append(GrepHit(path, line, text))
        return hits[:max_hits]

    def find_symbol_definitions(self, symbol, **kwargs):
        return []

    def find_callers(self, symbol, **kwargs):
        return []


async def run(args):
    root = Path(args.output).resolve()
    root.mkdir(parents=True, exist_ok=True)
    if (root / "report.json").exists():
        raise RuntimeError("Use a fresh diagnostic output directory")
    payload = json.loads(Path(args.input).read_text(encoding="utf-8"))
    orchestrator, db, github = await _build_runtime(
        root,
        model_override=args.model,
        output_language="en",
        pipeline="hypothesis",
        thinking="disabled",
        investigator_thinking="enabled",
        reasoning_effort="low",
        llm_min_interval=30,
    )
    usage = []
    results = []
    report = {
        "source_revision": os.environ["REVIEWFORGE_SOURCE_REVISION"],
        "protocol_version": 2,
        "driver_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "input_sha256": hashlib.sha256(Path(args.input).read_bytes()).hexdigest(),
        "model": args.model,
        "recorded_token_limit": args.token_limit,
        "quality": None,
        "full_pr_review": False,
        "semantic_retries": 0,
    }

    async def record(value):
        usage.append(value)
        if sum(item["total_tokens"] for item in usage) > args.token_limit:
            raise RuntimeError("predeclared diagnostic recorded-token budget exceeded")

    def model(agent, closing=False):
        return TrackedChatLLM(
            orchestrator._model_router.get_llm(agent),
            RunContext(),
            agent,
            usage_sink=record,
            request_options=request_options(args.model, agent, closing=closing),
        )

    try:
        # Validate all source-bound inputs before spending any model tokens.
        prepared = []
        for case in payload["candidates"]:
            info = await github.get_pr_info(case["repo"], case["pr_number"])
            if info["head"]["sha"] != case["head_sha"]:
                raise RuntimeError("PR head changed; no model calls")
            head_repo = (info["head"].get("repo") or {}).get("full_name") or case["repo"]
            paths = {site["path"] for site in case["hypothesis"]["sites"]}
            paths.update(obs["path"] for obs in case.get("closure_observations", []))
            files = {path: await github.get_file_content(head_repo, case["head_sha"], path) for path in sorted(paths)}
            for obs in case.get("closure_observations", []):
                if obs["status"] != "success" or obs["tool"] != "read_file" or obs["sha"] != case["head_sha"]:
                    raise RuntimeError("Closure replay needs positive, head-bound file observations")
                if obs["excerpt"] not in files[obs["path"]]:
                    raise RuntimeError("Saved excerpt differs from pinned source; no model calls")
            pr_files = await github.get_pr_files(case["repo"], case["pr_number"])
            state = StateStore(
                repo=case["repo"],
                pr_number=case["pr_number"],
                head_sha=case["head_sha"],
                head_repo=head_repo,
                file_diffs={file["filename"]: file.get("patch") or "" for file in pr_files},
            )
            hypothesis = Hypothesis.from_dict(case["hypothesis"])
            if hypothesis.status.value != "open" or hypothesis.observations:
                raise RuntimeError("Diagnostics need fresh OPEN candidates, not restored verdicts")
            for site in hypothesis.sites:
                if site.excerpt not in files[site.path]:
                    raise RuntimeError("Candidate anchor differs from pinned source")
            source_hashes = {path: hashlib.sha256(source.encode()).hexdigest() for path, source in files.items()}
            _atomic_json(
                root / (case["name"] + "-source.json"),
                {
                    "head_sha": case["head_sha"],
                    "head_repo": head_repo,
                    "source_sha256": source_hashes,
                    "source_kind": "pinned-diagnostic-subset",
                    "complete_repository": False,
                },
            )
            prepared.append((case, state, hypothesis, files))

        capability = await model("investigator", closing=True).ainvoke(
            [HumanMessage(content='Return exactly this JSON object: {"ready":true}.')],
            max_tokens=128,
        )
        if json.loads(capability.content).get("ready") is not True:
            raise RuntimeError("Relay JSON capability diagnostic failed")
        results.append(
            {
                "case": "relay-output-options",
                "valid_json": True,
                "body_chars": len(capability.content),
            }
        )

        generator_messages = [
            SystemMessage(content=message["content"])
            if message["role"] == "system"
            else HumanMessage(content=message["content"])
            for message in payload["generator"]["messages"]
        ]
        generated = await model("hypothesis_generator").ainvoke(generator_messages, max_tokens=8192)
        parsed = HypothesisGenerator._parse_response(generated.content)
        if parsed is None:
            raise RuntimeError("Generator JSON still unparseable; no discovery retry")
        expected = set(payload["generator"]["expected_units"])
        reported = HypothesisGenerator._reported_units(parsed)
        results.append(
            {
                "case": "discourse10-generation",
                "valid_json": True,
                "body_chars": len(generated.content),
                "expected_units": len(expected),
                "reported_units": len(reported & expected),
                "missing_units": sorted(expected - reported),
                "unexpected_units": sorted(reported - expected),
                "candidate_count": len(parsed["hypotheses"]),
            }
        )

        for case, state, hypothesis, files in prepared:
            site = hypothesis.sites[0]
            unit = SemanticUnit(
                id=hypothesis.unit_id,
                path=site.path,
                language="typescript" if site.path.endswith(".ts") else "python",
                kind=UnitKind.SYMBOL,
                symbol=hypothesis.identity.rsplit("::", 1)[-1],
                start_line=max(1, site.line - 3),
                end_line=site.line + 8,
            )
            changeset = SemanticChangeSet(state.repo, state.pr_number, state.head_sha, [unit])
            lines = files[site.path].splitlines(keepends=True)
            pack = ContextPack(
                units={
                    unit.id: UnitContext(
                        unit.id,
                        [
                            ContextSlice(
                                "callee",
                                site.path,
                                unit.start_line,
                                unit.end_line,
                                unit.symbol,
                                "".join(lines[unit.start_line - 1 : unit.end_line]),
                                "fixed source anchor, not evidence",
                                state.head_sha,
                            )
                        ],
                    )
                },
                pr_intent=(
                    "Protocol diagnostic with a partial source subset. Caller absence is not repository-wide absence."
                ),
            )
            investigator = Investigator(
                model("investigator"),
                build_workspace_executor(SourceSubset(files), state),
                changeset=changeset,
                protocol_version=2,
                closing_llm=model("investigator", closing=True),
            )
            if case.get("closure_observations"):
                investigator._state = state
                investigator._observations = [Observation(**obs) for obs in case["closure_observations"]]
                initial = [
                    SystemMessage(content=investigator._system_prompt()),
                    HumanMessage(content=investigator._render_user(hypothesis, state, pack)),
                ]
                result = await investigator._close(
                    investigator._closing_chat(initial, 24_000),
                    hypothesis,
                    set(state.file_diffs),
                    token_limit=24_000,
                    steps=0,
                )
            else:
                result = await investigator.investigate(hypothesis, state, pack)
            results.append({"case": case["name"], **asdict(result)})
            _atomic_json(root / (case["name"] + "-result.json"), asdict(result))
            if result.outcome in {
                "provider-error",
                "output-error",
                "budget-exhausted",
                "tool-error",
            }:
                raise RuntimeError("Protocol diagnostic still has an operational failure; no semantic retry")
        report["status"] = "completed"
    except Exception as exc:
        report.update(status="failed", error_type=type(exc).__name__)
        raise
    finally:
        report.update(
            results=results,
            recorded_tokens=sum(item["total_tokens"] for item in usage),
            requests=len(usage),
        )
        _atomic_json(root / "report.json", report)
        print(
            json.dumps(
                {key: report[key] for key in ("status", "recorded_tokens", "requests", "quality")},
                ensure_ascii=False,
            )
        )
        await db.close()
        await github.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--model", default="deepseek-v4-flash-0731")
    parser.add_argument("--token-limit", type=int, default=100_000)
    asyncio.run(run(parser.parse_args()))
