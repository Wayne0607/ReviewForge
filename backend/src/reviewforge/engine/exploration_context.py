"""Bounded navigation to immutable Context sources, never additional evidence."""

from __future__ import annotations

import json

from reviewforge.engine.investigation_tasks import context_units


def context_navigation(item, changeset, pack, *, head_sha: str, max_chars: int = 4096) -> dict:
    """Keep complete location records while omitting Context's duplicate bodies.

    Observations are serialized separately and are never truncated by this
    navigation limit. A missing location is reported, not treated as no caller.
    """
    if max_chars < 256:
        raise ValueError("navigation requires room for its bounded envelope")
    locations = []
    seen = set()
    stale = 0
    kinds = set()
    for unit_id in context_units(item, changeset, pack):
        context = pack.units.get(unit_id)
        if context is None:
            continue
        kinds.update(context.truncated_kinds)
        for source in context.slices:
            key = (source.kind, source.path, source.start_line, source.end_line, source.symbol, source.sha)
            if key in seen:
                continue
            seen.add(key)
            if source.sha != head_sha:
                stale += 1
                continue
            locations.append(
                {
                    "kind": source.kind,
                    "path": source.path,
                    "start": source.start_line,
                    "end": source.end_line,
                    "symbol": source.symbol,
                }
            )
    result = {
        "evidence": False,
        "locations": [],
        "omitted_locations": len(locations),
        "stale_locations": stale,
        "truncated_kinds": [],
    }

    def fits(trial):
        return len(json.dumps(trial, ensure_ascii=False, separators=(",", ":"))) <= max_chars

    for kind in sorted(kinds):
        trial = {**result, "truncated_kinds": [*result["truncated_kinds"], kind]}
        if fits(trial):
            result = trial
    for location in locations:
        trial = {
            **result,
            "locations": [*result["locations"], location],
            "omitted_locations": result["omitted_locations"] - 1,
        }
        if fits(trial):
            result = trial
    return result
