"""Protocol-v4 presentation only: share identical delivered source bodies.

These helpers do not select source, change provenance or complete coverage.
The caller must retain the original DiscoveryBlock for reference validation.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from reviewforge.engine.discovery import DiscoveryBlock, compact_source_coordinates

_DIFF_HEADER = "### Shared before/after diff: "
_RIGHT = re.compile(r"^\+(\d+)( \[[^\]]+\])? \| (.*)$")
_BOTH = re.compile(r"^ before=(\d+)( \[[^\]]+\])? RIGHT=(\d+)( \[[^\]]+\])? \| (.*)$")
_CONTEXT = re.compile(r"^(\d+)( \[[^\]]+\])? \| (.*)$")
_NAVIGATION = re.compile(r"^- (.+?) -> ([a-z_]+) (.+):(\d+)-(\d+) (.*?): (.*)$")
_UNIT_ID = re.compile(r"su_[0-9a-f]{16}")
_ALIASES = "Context aliases reuse complete source lines; IDs stay context, never RIGHT sites."


@dataclass(frozen=True)
class DiscoveryPresentation:
    diff_text: str
    context_text: str
    navigation: str


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _context_path(line: str, block: DiscoveryBlock) -> tuple[str, str] | None:
    # Unit IDs and repository paths can contain spaces. Match known IDs before
    # reading the collector's kind/path/range structure.
    for unit in sorted(block.units, key=lambda item: -len(item.id)):
        prefix = f"### {unit.id} "
        if line.startswith(prefix):
            match = re.fullmatch(r"\S+ (.+):\d+-\d+", line[len(prefix) :])
            return (unit.id, match[1]) if match is not None else None
    return None


def _shared_context(block: DiscoveryBlock, diff_text: str) -> str:
    original = compact_source_coordinates(block.context_text)
    facts = {(item.side, item.path, item.line, item.text, item.unit_id) for item in block.facts}
    right_facts = {(item.path, item.line, item.text) for item in block.facts if item.side == "after"}
    delivered_right: dict[tuple[str, int, str], str] = {}
    path = ""
    for line in diff_text.split("\n"):
        if line.startswith(_DIFF_HEADER):
            path = line.removeprefix(_DIFF_HEADER)
        elif match := _RIGHT.fullmatch(line):
            key = (path, int(match[1]), match[3])
            if key in right_facts and match[2] and match[2].strip() == block.reference_labels("after", path, key[1]):
                delivered_right[key] = match[2].strip()
        elif match := _BOTH.fullmatch(line):
            key = (path, int(match[3]), match[5])
            if key in right_facts and match[4] and match[4].strip() == block.reference_labels("after", path, key[1]):
                delivered_right[key] = match[4].strip()

    rendered: list[str] = []
    seen: set[tuple[str, int, str]] = set()
    owner: tuple[str, str] | None = None
    shared = False
    for line in original.split("\n"):
        if line.startswith("### "):
            owner = _context_path(line, block)
        match = _CONTEXT.fullmatch(line)
        if owner is not None and match is not None and match[2]:
            unit_id, path = owner
            number, body = int(match[1]), match[3]
            key = (path, number, body)
            if ("context", path, number, body, unit_id) in facts and match[2].strip() == block.reference_labels(
                "context", path, number
            ):
                target = (
                    f"RIGHT {delivered_right[key]}"
                    if key in delivered_right
                    else "earlier Context"
                    if key in seen
                    else ""
                )
                alias = f"{match[1]}{match[2]} = {target}"
                if target and len(alias.encode()) < len(line.encode()):
                    line, shared = alias, True
                seen.add(key)
        rendered.append(line)
    candidate = _ALIASES + "\n" + "\n".join(rendered)
    return candidate if shared and len(candidate.encode()) < len(original.encode()) else original


def _project_navigation(text: str, block: DiscoveryBlock) -> str:
    ids = {unit.id for unit in block.units}
    rendered = []
    for line in text.split("\n"):
        match = _NAVIGATION.fullmatch(line)
        # A collector-shaped row explicitly attributes the relation to this
        # canonical semantic unit. Unknown syntax/ownership and omission notices
        # stay verbatim; do not infer an owner from a path or symbol substring.
        if match is not None and _UNIT_ID.fullmatch(match[1]) and match[1] not in ids:
            continue
        rendered.append(line)
    return "\n".join(rendered)


def compact_navigation(text: str) -> str:
    """Losslessly table contiguous collector rows; preserve every field/order."""
    lines = text.split("\n")
    rendered: list[str] = []
    index = 0
    while index < len(lines):
        rows = []
        end = index
        while end < len(lines) and (match := _NAVIGATION.fullmatch(lines[end])) is not None:
            rows.append(match.groups())
            end += 1
        if not rows:
            rendered.append(lines[index])
            index += 1
            continue
        paths = {path: f"p{number}" for number, path in enumerate(dict.fromkeys(row[2] for row in rows))}
        table = ["Navigation paths " + _json({name: path for path, name in paths.items()})]
        table.append("Navigation rows [unit_id,kind,path_key,start,end,symbol,reason] (not evidence):")
        table.extend(_json([row[0], row[1], paths[row[2]], *row[3:]]) for row in rows)
        original, compact = "\n".join(lines[index:end]), "\n".join(table)
        rendered.append(compact if len(compact.encode()) < len(original.encode()) else original)
        index = end
    return "\n".join(rendered)


def present_discovery_block(block: DiscoveryBlock) -> DiscoveryPresentation:
    """Render v4 source sharing without mutating the original evidence block.

    Call only for protocol >=4. Metadata aliases contain all original context
    reference labels and point to a complete, already delivered source line at
    the same path/coordinate. Source bodies and all validators remain original.
    """
    diff_text = compact_source_coordinates(block.diff_text)
    return DiscoveryPresentation(
        diff_text=diff_text,
        context_text=_shared_context(block, diff_text),
        navigation=compact_navigation(_project_navigation(block.navigation, block)),
    )
