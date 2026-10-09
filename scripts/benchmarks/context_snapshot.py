"""Capture deterministic v4 context at a public PR head without LLM calls."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
from dataclasses import asdict
from pathlib import Path

from martian_runner import StateStore, _atomic_json, _build_runtime

from reviewforge.engine.context_engine import ContextEngine
from reviewforge.engine.context_pack import ContextPack
from reviewforge.engine.semantic_diff import compile_semantic_changeset


async def capture(args: argparse.Namespace) -> None:
    output = Path(args.output).resolve()
    orchestrator, db, github = await _build_runtime(output.parent, "", "en", "hypothesis")
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
        workspace = await orchestrator._gateway.workspace_for(state)
        await ContextEngine(orchestrator._gateway, db).build(state)
        changeset = compile_semantic_changeset(state)
        config = orchestrator._pipeline_v4_config
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
        print(f"Saved {state.repo}#{state.pr_number}@{state.head_sha}: {len(pack.units)} units, {len(rendered)} chars")
    finally:
        if state is not None:
            await orchestrator._gateway.cleanup_workspace(state)
        await db.close()
        await github.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--pr", required=True, type=int)
    parser.add_argument("--output", required=True)
    asyncio.run(capture(parser.parse_args()))


if __name__ == "__main__":
    main()
