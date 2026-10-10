"""Capture deterministic v4 context at a public PR head without LLM calls."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
from dataclasses import asdict
from pathlib import Path

from martian_runner import REPO_ROOT, ReadOnlyGitHub, StateStore, _atomic_json, block_github_writes

from reviewforge.core.config import ReviewForgeConfig
from reviewforge.core.database import Database
from reviewforge.core.specs import build_registry
from reviewforge.engine.context_engine import ContextEngine
from reviewforge.engine.context_pack import ContextPack
from reviewforge.engine.semantic_diff import compile_semantic_changeset
from reviewforge.tools.gateway import ToolGateway
from reviewforge.tools.github_api import GitHubClient


async def build_context_runtime(root: Path):
    """Context collection requires repository access, not model credentials."""
    config = ReviewForgeConfig.load(REPO_ROOT / "reviewforge.yaml")
    github = GitHubClient(token=config.github.token)
    github._client.event_hooks["request"].append(block_github_writes)
    db = Database(root / f"reviewforge-context-{os.getpid()}.db")
    try:
        await db.connect()
        gateway = ToolGateway(
            build_registry(),
            ReadOnlyGitHub(github),
            pipeline_mode="hypothesis",
            workspace_max_bytes=config.pipeline_v4.workspace_max_bytes,
        )
        return gateway, config.pipeline_v4, db, github
    except BaseException:
        try:
            await db.close()
        finally:
            await github.close()
        raise


async def capture(args: argparse.Namespace) -> None:
    output = Path(args.output).resolve()
    gateway, config, db, github = await build_context_runtime(output.parent)
    state = None
    try:
        pr = await github.get_pr_info(args.repo, args.pr)
        files = await github.get_pr_files(args.repo, args.pr)
        state = StateStore(
            repo=args.repo,
            pr_number=args.pr,
            head_sha=pr["head"]["sha"],
            base_sha=pr["base"]["sha"],
            head_repo=(pr["head"].get("repo") or {}).get("full_name") or args.repo,
            pr_title=pr.get("title") or "",
            pr_body=pr.get("body") or "",
            files_changed=[file["filename"] for file in files],
            file_diffs={file["filename"]: file.get("patch") or "" for file in files},
            diff_summary="\n".join(
                f"--- {file['filename']} (+{file.get('additions', 0)} -{file.get('deletions', 0)})\n"
                f"{file.get('patch') or ''}"
                for file in files
            ),
        )
        workspace = await gateway.workspace_for(state)
        await ContextEngine(gateway, db).build(state)
        changeset = compile_semantic_changeset(state)
        pack = ContextPack.build(changeset, workspace, state, max_slices=config.context_pack_max_slices)
        rendered = pack.render_all(max_chars=config.context_pack_max_chars)
        assert rendered == pack.render_all(max_chars=config.context_pack_max_chars), "nondeterministic rendering"
        payload = {
            "source_revision": os.environ.get("REVIEWFORGE_SOURCE_REVISION", ""),
            "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "repo": state.repo,
            "pr_number": state.pr_number,
            "head_sha": state.head_sha,
            "workspace": {**asdict(workspace.info), "root": str(workspace.info.root)},
            "changeset": changeset.to_dict(),
            "units": {unit_id: asdict(context) for unit_id, context in pack.units.items()},
            "rendered": rendered,
            "rendered_chars": len(rendered),
            "max_chars": config.context_pack_max_chars,
            "github_writes": "blocked",
            "llm_calls": 0,
        }
        _atomic_json(output, payload)
        slice_count = sum(len(context.slices) for context in pack.units.values())
        print(
            f"Saved {state.repo}#{state.pr_number}@{state.head_sha}: "
            f"source={workspace.source}, files={workspace.info.file_count}, "
            f"units={len(pack.units)}, slices={slice_count}, chars={len(rendered)}"
        )
        if args.require_tarball and workspace.source != "tarball":
            raise RuntimeError("Context audit requires a repository snapshot; saved degraded diagnostic only")
    finally:
        if state is not None:
            await gateway.cleanup_workspace(state)
        await db.close()
        await github.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--pr", required=True, type=int)
    parser.add_argument("--output", required=True)
    parser.add_argument("--require-tarball", action="store_true", help="Reject degraded context in snapshot audits")
    asyncio.run(capture(parser.parse_args()))


if __name__ == "__main__":
    main()
