"""Separate source coverage identities from bounded, shareable investigation work.

Only exact candidate facts are shared. Semantic similarity, model confidence,
and an equal function name are insufficient to reuse evidence or a verdict.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections import deque
from dataclasses import asdict, dataclass
from itertools import groupby, zip_longest

from reviewforge.engine.context_pack import ContextPack
from reviewforge.engine.hypothesis import Hypothesis
from reviewforge.engine.semantic_diff import SemanticChangeSet, UnitKind


@dataclass(frozen=True)
class InvestigationTask:
    id: str
    hypothesis: Hypothesis
    members: tuple[str, ...]


def build_investigation_tasks(
    items: list[Hypothesis], *, head_sha: str, workspace_digest: str
) -> list[InvestigationTask]:
    groups: dict[str, list[Hypothesis]] = {}
    for item in items:
        anchor = item.identity.rsplit("::", 1)[-1]
        facts = {
            "head_sha": head_sha,
            "workspace_digest": workspace_digest,
            "anchor": anchor,
            "mechanism": item.mechanism.value,
            **{name: getattr(item, name) for name in ("claim", "trigger", "impact", "open_question", "refutation")},
            "sites": sorted(
                (asdict(site) for site in item.sites), key=lambda site: (site["path"], site["line"], site["excerpt"])
            ),
        }
        if not anchor or anchor == "<module>":
            facts["unit_id"] = item.unit_id
        task_id = (
            "it_" + hashlib.sha256(json.dumps(facts, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:24]
        )
        groups.setdefault(task_id, []).append(item)
    tasks = []
    severity_rank = {"info": 0, "warning": 1, "error": 2}
    for task_id, members in groups.items():
        representative = min(members, key=lambda item: (-severity_rank[item.severity], item.identity))
        tasks.append(
            InvestigationTask(task_id, copy.deepcopy(representative), tuple(sorted(item.identity for item in members)))
        )
    return tasks


def fair_investigation_tasks(tasks: list[InvestigationTask]) -> list[InvestigationTask]:
    """Interleave anchors as well as mechanisms without combining proof duties.

    A dozen wording variants at one function must not take every admission slot
    before another equally severe function gets its first investigation.
    """
    severity_rank = {"info": 0, "warning": 1, "error": 2}
    ranked = sorted(
        tasks,
        key=lambda task: (
            -severity_rank[task.hypothesis.severity],
            -len(task.hypothesis.sites),
            task.hypothesis.identity,
        ),
    )
    result = []
    for _, severity_tasks in groupby(ranked, key=lambda task: severity_rank[task.hypothesis.severity]):
        mechanisms: dict[str, dict[tuple[str, ...], deque[InvestigationTask]]] = {}
        for task in severity_tasks:
            item = task.hypothesis
            anchor = item.identity.rsplit("::", 1)[-1]
            key = (*sorted({site.path for site in item.sites}), anchor)
            if not anchor or anchor == "<module>":
                key += (item.unit_id,)
            mechanisms.setdefault(item.mechanism.value, {}).setdefault(key, deque()).append(task)
        queues = []
        for anchors in mechanisms.values():
            ordered = []
            while any(anchors.values()):
                ordered.extend(queue.popleft() for queue in anchors.values() if queue)
            queues.append(ordered)
        for batch in zip_longest(*queues):
            result.extend(task for task in batch if task is not None)
    return result


def context_units(item: Hypothesis, changeset: SemanticChangeSet | None, pack: ContextPack) -> list[str]:
    """Resolve the primary compiler site before using its declaration's context."""
    declared = item.unit_id
    if changeset is None:
        return [declared]
    unit = next((unit for unit in changeset.units if unit.id == declared), None)
    if unit is not None and unit.kind is UnitKind.RESOURCE:
        return [declared]
    primary = min(item.sites, key=lambda site: (unit is None or site.path != unit.path, site.path, site.line))
    enclosing = [
        unit
        for unit in changeset.units
        if unit.path == primary.path and unit.start_line <= primary.line <= unit.end_line and unit.id in pack.units
    ]
    if not enclosing:
        return [declared]
    anchor = min(enclosing, key=lambda unit: (unit.end_line - unit.start_line, unit.id)).id
    return list(dict.fromkeys([anchor, declared]))


def render_investigation_context(
    item: Hypothesis, changeset: SemanticChangeSet | None, pack: ContextPack, *, max_chars: int
) -> str:
    units = context_units(item, changeset, pack)
    if units == [item.unit_id]:
        return pack.render_for_unit(item.unit_id, max_chars=max_chars)
    pieces = [f"Investigation anchor: {units[0]}; declared coverage: {item.unit_id}. Navigation only."]
    for unit_id in units:
        remaining = max_chars - len("\n\n".join(pieces)) - 2
        if remaining <= 0:
            break
        rendered = pack.render_for_unit(unit_id, max_chars=remaining)
        if rendered:
            pieces.append(rendered)
    return "\n\n".join(pieces)[:max_chars]
