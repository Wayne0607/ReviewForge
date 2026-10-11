"""Bounded reverse-call pointers for shared state, never execution evidence.

The original state index stops at same-class references. Assignment-shaped
references select navigation seeds here; they do not establish a writer set.
Two sampled, declared caller paths per seed are followed, with explicit bounds. No
method name, directory named test, or framework registration decides a path.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from reviewforge.engine.java_navigation import JavaSource

MAX_PATH_DEPTH = 5
MAX_UNIT_QUERIES = 20
MAX_PACK_QUERIES = 40
MAX_ROOTS = 4
MAX_PATH_CHARS = 2400
PATH_WIDTH = 2


@dataclass(frozen=True)
class _Edge:
    path: str
    line: int
    caller: str


def _seeds(source: JavaSource, definition: Any) -> list[str]:
    """Select code tokens, conservatively excluding local shadowing.

    Unsupported mutation through aliases/objects is not indexed. A seed is
    only a place to inspect, not proof the assignment executes on any input.
    """
    roots = []
    rows = source.mask.splitlines()
    for field, references in source.state_references(definition)[:4]:
        for method, _ in references[:6]:
            owner = source.owner(method.line)
            matcher = re.compile(
                rf"(?<![\w$.])(?P<receiver>this\.|{re.escape(owner.name)}\.)?"
                rf"{re.escape(field.name)}\s*(?:=(?!=)|[+*/%&|^-]=|\+\+|--)"
            )
            shadowed = any(
                name == field.name and source._function(line) == method for name, _type, line in source.bindings
            )
            for number in range(method.line, method.end_line + 1):
                if source.owner(number) != field.owner:
                    continue
                match = matcher.search(rows[number - 1])
                if match and (match.group("receiver") or not shadowed):
                    root = source.qualified_definition(method)
                    if root not in roots:
                        roots.append(root)
                    break
    return roots


class StatePaths:
    """Per-pack cache; no shared mutable investigator/executor metadata."""

    def __init__(self, workspace: Any, read_java: Callable[[str], JavaSource | None], max_hits: int):
        self.workspace = workspace
        self.read_java = read_java
        self.max_hits = 12 if max_hits > 0 else 0
        self._cache: dict[str, list[_Edge]] = {}
        self.begin_unit()

    def begin_unit(self) -> None:
        self._unit_queries = 0
        self._unit_results: dict[tuple[tuple[str, ...], str], tuple[str, bool]] = {}

    def _callers(self, target: str) -> list[_Edge] | None:
        if target in self._cache:
            return self._cache[target]
        if len(self._cache) >= MAX_PACK_QUERIES:
            return None
        self._cache[target] = []
        try:
            hits = self.workspace.find_callers(target, language="java", max_hits=self.max_hits, diverse=True)
        except (AttributeError, TypeError, ValueError, OSError):
            return []
        edges = []
        for hit in hits[: self.max_hits]:
            path = str(hit.get("path", "") if isinstance(hit, dict) else hit.path)
            line = int(hit.get("line", 0) if isinstance(hit, dict) else hit.line)
            source = self.read_java(path)
            method = source._function(line) if source and line > 0 else None
            if method is None:
                continue
            # Recheck the saved source, including the declared nested owner.
            # A callback callsite remains navigation, never callback ordering.
            if not any(call.line == line and source.call_target(call) == target for call in source.calls):
                continue
            edge = _Edge(path, line, source.qualified_definition(method))
            if not any(item.path == edge.path and item.caller == edge.caller for item in edges):
                edges.append(edge)
        self._cache[target] = edges
        return edges

    def navigation(self, source: JavaSource, definition: Any, anchor_path: str) -> tuple[str, bool]:
        roots = _seeds(source, definition)
        if not roots or not self.max_hits:
            return "", bool(roots)
        key = (tuple(roots), anchor_path)
        if key in self._unit_results:
            return self._unit_results[key]
        active: dict[str, list[tuple[_Edge, ...]]] = {root: [()] for root in roots[:MAX_ROOTS]}
        completed: dict[str, list[tuple[_Edge, ...]]] = {root: [] for root in active}
        anchor = PurePosixPath(anchor_path).parts[:-1]

        def proximity(edge: _Edge):
            parts = PurePosixPath(edge.path).parts[:-1]
            shared = 0
            for left, right in zip(anchor, parts, strict=False):
                if left != right:
                    break
                shared += 1
            return (-shared, len(parts) + len(anchor) - 2 * shared, edge.path, edge.line, edge.caller)

        def rank(chain: tuple[_Edge, ...]):
            return (proximity(chain[-1]), -len(chain), tuple((e.path, e.line, e.caller) for e in chain))

        # Keep a small beam: a nearer intermediate file may lead away from the
        # changed command. Re-rank after expansion rather than committing to
        # that first branch. Round-robin seeds still preserves alternatives.
        for _depth in range(MAX_PATH_DEPTH):
            next_active = {}
            for root, chains in active.items():
                expanded = []
                for chain in chains:
                    target = chain[-1].caller if chain else root
                    if self._unit_queries >= MAX_UNIT_QUERIES:
                        if chain:
                            completed[root].append(chain)
                        continue
                    self._unit_queries += 1
                    edges = self._callers(target)
                    visited = {root, *(edge.caller for edge in chain)}
                    choices = sorted((edge for edge in edges or [] if edge.caller not in visited), key=proximity)
                    if choices:
                        expanded.extend((*chain, edge) for edge in choices[:PATH_WIDTH])
                    elif chain:
                        completed[root].append(chain)
                if expanded:
                    next_active[root] = sorted(set(expanded), key=rank)[:PATH_WIDTH]
            active = next_active
            if not active or self._unit_queries >= MAX_UNIT_QUERIES:
                break
        for root, chains in active.items():
            completed[root].extend(chains)
        entries = [
            root + " <- " + " <- ".join(f"{edge.path}:{edge.line} ({edge.caller})" for edge in chain)
            for root, chains in completed.items()
            for chain in sorted(set(chains), key=rank)[:PATH_WIDTH]
        ]
        if not entries:
            self._unit_results[key] = ("", True)
            return "", True
        prefix = (
            "State caller navigation (not evidence; sampled paths, other callers/mutations unchecked; "
            "dispatch and order unproved; read callsite guards and binding): "
        )
        text = prefix + " | ".join(entries)
        marker = " ... paths omitted"
        if len(text) > MAX_PATH_CHARS:
            text = text[: MAX_PATH_CHARS - len(marker)] + marker
        # Always unchecked: neither missing callers nor an endpoint is proof.
        self._unit_results[key] = (text, True)
        return text, True
