"""Concrete saved search locations, never resolved dataflow or proof.

A path-only read used to select the first literal hit, often an import before
the actual use in the same file. Prefer unread uses and expose every retained
location so the model can request explicit windows when the default is wrong.
"""

from __future__ import annotations

import hashlib
import re

from reviewforge.engine.hypothesis import Observation

SOURCE_CHARS = 6_000
TOOL_CHARS = 12_000
BEFORE_LINES = 48
AFTER_LINES = 16


def _pieces(text, first, *, limit=1_200):
    """Keep the original Observation bound and exact bytes in named spans."""
    pieces = []
    pending = ""
    start = line = first
    for source in text.splitlines(keepends=True):
        if pending and len(pending) + len(source) > limit:
            pieces.append((pending, (start, line - 1) if first is not None else None))
            pending = ""
        if not pending:
            start = line
        # A long line is retained as exact fragments, never described as a
        # complete function/body. All fragments carry the same source line.
        while len(source) > limit:
            pieces.append((source[:limit], (line, line) if first is not None else None))
            source = source[limit:]
        pending += source
        if line is not None:
            line += 1
    if pending or not pieces:
        pieces.append((pending, (start, line - 1) if first is not None else None))
    return pieces


async def save_window(
    worker, name, args, query, text, status, original_chars, shared_result_id, reused, diff_navigation
):
    """One bounded read, several immutable 1200-character observations.

    Keeping the complete delivered window avoids silently losing its argument
    construction. No old Observation schema/checkpoint changes are necessary.
    """
    excerpt, line_range = text[:SOURCE_CHARS], None
    if name == "read_file" and status == "success":
        excerpt, line_range = worker._saved_read_excerpt(text, args)
    parts = _pieces(excerpt, line_range[0] if line_range else None)
    replies = []
    saved_ids = []
    for part, span in parts:
        requested = (
            (int(args.get("start") or 0), int(args.get("end") or 0)) if "start" in args or "end" in args else None
        )
        observation = Observation(
            id=f"obs_{worker._obs_counter}",
            tool=name,
            query=query,
            path="" if name == "find_references" else str(args.get("path") or ""),
            line_range=requested,
            sha=worker._state.head_sha if worker._state else "",
            result_digest=hashlib.sha256(text.encode()).hexdigest()[:16],
            excerpt=part,
            status=status,
            excerpt_line_range=span,
            shared_result_id=shared_result_id,
        )
        worker._obs_counter += 1
        worker._observations.append(observation)
        saved_ids.append(observation.id)
        if worker._evidence_store is not None:
            worker._evidence_store.observation(observation)
        worker._progress.append(
            {
                "phase": "evidence",
                "query": query,
                "observation_id": observation.id,
                "status": status,
                "shared_result_id": shared_result_id,
                "reused": reused,
            }
        )
        if diff_navigation:
            worker._progress.append({"phase": "diff-navigation", "observation_id": observation.id, **diff_navigation})
        if name == "find_references":
            lookup = getattr(worker._executor, "reference_lookups", {}).get(query)
            worker._progress.append(
                {
                    "phase": "reference-navigation",
                    "observation_id": observation.id,
                    "lookup_scope": lookup
                    or {
                        "kind": "literal-identifier-reference",
                        "navigation_only": True,
                        "execution_resolved": False,
                        "aliases_resolved": False,
                        "metadata_available": False,
                    },
                }
            )
        worker._progress.append(
            {
                "phase": "observation-window",
                "observation_id": observation.id,
                "original_chars": original_chars,
                "saved_chars": len(part),
                "group_saved_chars": len(excerpt),
                "truncated": original_chars > len(excerpt),
                "next_action": "This window is split into separately cited observations with the same query. "
                "Read an adjacent explicit line range for missing argument definitions or guards.",
            }
        )
        replies.append(worker._observation_reply(observation))
    worker._progress.append(
        {
            "phase": "source-window-v9",
            "query": query,
            "observation_ids": saved_ids,
            "saved_range": line_range,
            "saved_chars": len(excerpt),
            "truncated": original_chars > len(excerpt),
        }
    )
    if worker._on_progress is not None:
        await worker._on_progress(worker._observations, worker._progress)
    return "\n\n".join(replies)[:TOOL_CHARS]


def targets(observations):
    result = []
    seen = set()
    for observation in reversed(observations):
        if observation.status != "success" or observation.tool not in {
            "grep",
            "find_callers",
            "find_references",
            "find_definition",
        }:
            continue
        for text in observation.excerpt.splitlines():
            if observation.tool == "find_definition":
                hit = re.match(r"^- .* \[[^]]+\] (.+):(\d+)$", text)
                role = "declaration"
            else:
                hit = re.match(r"^- (.+?):(\d+):\s*(.*)$", text)
                if not hit:
                    continue
                source = hit[3].lstrip()
                role = (
                    "import"
                    if re.match(r"(?:import\b|from\b|export\s+(?:\*|\{)|#include\b)", source)
                    else "comment"
                    if source.startswith(("//", "#", "/*", "*"))
                    else "use"
                )
            if not hit or int(hit[2]) <= 0:
                continue
            key = (hit[1], int(hit[2]))
            if key in seen:
                continue
            seen.add(key)
            result.append(
                {
                    "path": key[0],
                    "line": key[1],
                    "role": role,
                    "from_observation": observation.id,
                    "already_read": any(
                        item.tool == "read_file"
                        and item.status == "success"
                        and item.path == key[0]
                        and item.excerpt_line_range is not None
                        and item.excerpt_line_range[0] <= key[1] <= item.excerpt_line_range[1]
                        for item in observations
                    ),
                }
            )
    return result


def focus(observations, path):
    locations = [item for item in targets(observations) if item["path"] == path]
    if not locations:
        return None
    # Stable ties preserve the latest positive search. Imports are still
    # available explicitly; choosing a use does not claim runtime resolution.
    role_rank = {"use": 0, "declaration": 1, "import": 2, "comment": 3}
    return min(locations, key=lambda item: (role_rank[item["role"]], item["already_read"]))["line"]


def packet(observations, *, limit=24):
    locations = targets(observations)
    locations.sort(key=lambda item: (item["already_read"], item["role"] != "use"))
    continuations = []
    groups = {}
    for observation in observations:
        if observation.tool != "read_file" or observation.status != "success" or not observation.excerpt_line_range:
            continue
        key = (observation.path, observation.query)
        groups.setdefault(key, []).append(observation)
    for (path, _query), group in groups.items():
        first = min(item.excerpt_line_range[0] for item in group)
        last = max(item.excerpt_line_range[1] for item in group)
        observation = group[0]
        candidates = [(max(1, first - BEFORE_LINES), first - 1), (last + 1, last + BEFORE_LINES)]
        for start, end in candidates:
            if end < start or any(
                item.tool == "read_file"
                and item.status == "success"
                and item.path == observation.path
                and item.excerpt_line_range is not None
                and item.excerpt_line_range[0] <= start
                and end <= item.excerpt_line_range[1]
                for item in observations
            ):
                continue
            continuations.append(
                {
                    "tool": "read_file",
                    "args": {"path": observation.path, "start": start, "end": end},
                    "from_observation": observation.id,
                }
            )
    return {
        "navigation_only": True,
        "execution_resolved": False,
        "locations": locations[:limit],
        "omitted_locations": max(0, len(locations) - limit),
        "adjacent_windows": continuations[-limit:],
        "instruction": "A hit is a location, not proof. Read the use and follow the actual argument/guard definitions. "
        "Adjacent windows may be outside the file; an empty read proves no source there, not no consumer. "
        "Supply explicit start/end to override the automatic use-first window.",
    }
