"""Hypothesis-pipeline orchestration entrypoint.

T4 intentionally stops after deterministic context construction.  Later task
cards extend this function; shadow mode never publishes from this path.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from reviewforge.engine.context_pack import ContextPack
from reviewforge.engine.hypothesis import HypothesisLedger
from reviewforge.engine.run_health import RunHealth
from reviewforge.engine.semantic_diff import compile_semantic_changeset
from reviewforge.tools.workspace import WorkspaceUnavailable


@dataclass(frozen=True)
class _UnavailableWorkspace:
    digest: str
    source: str = "api-fallback"


async def run_hypothesis_pipeline(orchestrator: Any, state: Any) -> RunHealth:
    """Build the immutable workspace, semantic units and deterministic pack."""

    started = time.perf_counter()
    try:
        workspace = await orchestrator._gateway.workspace_for(state)
        info = workspace.info
        workspace_payload = {
            "source": info.source,
            "file_count": info.file_count,
            "byte_size": info.byte_size,
            "truncated": info.truncated,
            "digest": info.digest,
        }
    except WorkspaceUnavailable:
        workspace = _UnavailableWorkspace(digest=str(state.head_sha or ""))
        workspace_payload = {
            "source": "unavailable",
            "file_count": 0,
            "byte_size": 0,
            "truncated": True,
            "digest": str(state.head_sha or ""),
        }
    workspace_payload["ms"] = int((time.perf_counter() - started) * 1000)
    workspace_event = orchestrator._events.emit("workspace.built", workspace_payload)

    changeset = compile_semantic_changeset(state)
    config = orchestrator._pipeline_v4_config
    pack = ContextPack.build(
        changeset,
        workspace,
        state,
        max_slices=config.context_pack_max_slices,
    )
    rendered = pack.render_all(max_chars=config.context_pack_max_chars)
    slices = sum(len(context.slices) for context in pack.units.values())
    truncated_units = sum(bool(context.truncated_kinds) for context in pack.units.values())
    orchestrator._events.emit(
        "context_pack.built",
        {
            "units": len(pack.units),
            "slices": slices,
            "truncated_units": truncated_units,
            "chars": len(rendered),
        },
    )
    if state.ledger is None:
        state.ledger = HypothesisLedger(
            run_id=workspace_event.run_id,
            head_sha=str(state.head_sha or ""),
            workspace_digest=pack.workspace_digest,
        )
    elif state.ledger.head_sha != str(state.head_sha or ""):
        raise ValueError("restored hypothesis ledger does not match PR head")
    return RunHealth.build()
