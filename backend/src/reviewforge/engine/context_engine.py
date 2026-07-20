"""Build bounded, repository-aware context for a pull-request review.

The existing cross-PR graph is intentionally conservative and is populated at
the end of a review.  This module complements it by producing an Impact
Manifest *before* planning, so both the Planner and Reviewers can reason about
changed symbols, callers, imports and likely tests while decisions are made.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import PurePosixPath
from typing import Any

from reviewforge.core.database import Database
from reviewforge.core.state import StateStore
from reviewforge.engine.detectors.unified_diff import iter_added_lines
from reviewforge.engine.symbol_extractor import (
    CallInfo,
    ImportInfo,
    SymbolInfo,
    detect_language,
    extract_calls,
    extract_definitions,
    extract_diff_calls,
    extract_diff_symbols,
    extract_imports,
)
from reviewforge.tools.gateway import ToolGateway

_MAX_FILES = 16
_MAX_SYMBOLS_PER_FILE = 12
_MAX_SEARCH_SYMBOLS = 6
_MAX_REFERENCE_PATHS = 8
_MAX_GRAPH_ROWS = 12
_MAX_FILE_CHARS = 300_000
_SUPPORTED_LANGUAGES = {"python", "go", "java", "rust", "ruby", "javascript", "typescript"}
_LOW_SIGNAL_NAMES = {
    "append",
    "close",
    "error",
    "get",
    "len",
    "log",
    "main",
    "map",
    "new",
    "open",
    "print",
    "read",
    "run",
    "set",
    "string",
    "write",
}
_SENSITIVE_NAME = re.compile(
    r"(?:auth|admin|permission|token|secret|password|encrypt|decrypt|query|execute|upload|download|redirect|deserialize)",
    re.IGNORECASE,
)


@dataclass
class ImpactFile:
    path: str
    language: str
    added_lines: list[int] = field(default_factory=list)
    changed_symbols: list[dict[str, Any]] = field(default_factory=list)
    imports: list[dict[str, Any]] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)
    content_available: bool = False


class ContextEngine:
    """Create a small, evidence-oriented Impact Manifest for one PR."""

    def __init__(self, gateway: ToolGateway, db: Database | None = None) -> None:
        self._gateway = gateway
        self._db = db

    async def build(self, state: StateStore) -> dict[str, Any]:
        await self._gateway.ensure_file_diffs(state)
        selected = [path for path in state.files_changed if detect_language(path) in _SUPPORTED_LANGUAGES][:_MAX_FILES]

        semaphore = asyncio.Semaphore(4)

        async def inspect(path: str) -> ImpactFile:
            async with semaphore:
                return await self._inspect_file(path, state)

        files = await asyncio.gather(*(inspect(path) for path in selected))
        symbols = self._rank_search_symbols(files)
        references = await self._find_references(symbols, state)
        known_paths = {
            *state.files_changed,
            *(path for item in references for path in item["paths"]),
        }
        graph_context = await self._load_graph_context(symbols, known_paths)

        test_paths = sorted({path for item in references for path in item["paths"] if _is_test_path(path)})[
            :_MAX_REFERENCE_PATHS
        ]
        risk_signals = self._risk_signals(files, references, test_paths)

        manifest: dict[str, Any] = {
            "version": 1,
            "files": [asdict(item) for item in files],
            "references": references,
            "candidate_tests": test_paths,
            "historical_graph": graph_context,
            "risk_signals": risk_signals,
            "coverage": {
                "changed_files": len(state.files_changed),
                "indexed_files": len(files),
                "truncated": len(selected)
                < len([path for path in state.files_changed if detect_language(path) in _SUPPORTED_LANGUAGES]),
            },
        }
        state.impact_manifest = manifest
        return manifest

    async def _inspect_file(self, path: str, state: StateStore) -> ImpactFile:
        diff = (state.file_diffs or {}).get(path, "")
        added_lines = sorted({line for line, _content in iter_added_lines(diff)})
        content = ""
        try:
            content = await self._gateway.invoke("read_file", {"file_path": path}, state)
            if len(content) > _MAX_FILE_CHARS:
                content = ""
        except Exception:
            # Deleted, binary and inaccessible files remain represented by their diff.
            content = ""

        definitions: list[SymbolInfo]
        imports: list[ImportInfo]
        calls: list[CallInfo]
        if content:
            definitions = extract_definitions(content, path)
            imports = extract_imports(content, path)
            calls = extract_calls(content, path)
            changed = [symbol for symbol in definitions if _touches_changed_line(symbol, added_lines)]
        else:
            changed, imports = extract_diff_symbols(diff, path)
            calls = extract_diff_calls(diff, path)

        # A hunk may modify module-level code without touching a declaration.
        # Keep diff-level definitions as a fallback for incomplete/truncated file reads.
        if not changed:
            changed, _diff_imports = extract_diff_symbols(diff, path)

        changed_names = {item.name for item in changed}
        relevant_calls = [
            call
            for call in calls
            if call.caller in changed_names or call.line in added_lines or call.caller == "<module>"
        ]
        return ImpactFile(
            path=path,
            language=detect_language(path),
            added_lines=added_lines[:40],
            changed_symbols=[
                {
                    "name": item.name,
                    "type": item.symbol_type,
                    "line": item.line,
                    "start_line": item.start_line or item.line,
                    "end_line": item.end_line,
                }
                for item in changed[:_MAX_SYMBOLS_PER_FILE]
            ],
            imports=[
                {"source": item.source, "name": item.name, "local_name": item.local_name, "line": item.line}
                for item in imports[:_MAX_SYMBOLS_PER_FILE]
            ],
            calls=[
                {"caller": item.caller, "callee": item.callee, "line": item.line}
                for item in relevant_calls[:_MAX_SYMBOLS_PER_FILE]
            ],
            content_available=bool(content),
        )

    @staticmethod
    def _rank_search_symbols(files: list[ImpactFile]) -> list[str]:
        ranked: list[tuple[int, str]] = []
        for item in files:
            for symbol in item.changed_symbols:
                name = str(symbol["name"])
                score = 4 + int(not name.startswith("_")) + int(bool(_SENSITIVE_NAME.search(name)))
                ranked.append((score, name))
            for call in item.calls:
                name = str(call["callee"])
                ranked.append((2 + int(bool(_SENSITIVE_NAME.search(name))), name))
        result: list[str] = []
        for _score, name in sorted(ranked, key=lambda pair: (-pair[0], pair[1])):
            if len(name) < 3 or name.lower() in _LOW_SIGNAL_NAMES or name in result:
                continue
            result.append(name)
            if len(result) >= _MAX_SEARCH_SYMBOLS:
                break
        return result

    async def _find_references(self, symbols: list[str], state: StateStore) -> list[dict[str, Any]]:
        semaphore = asyncio.Semaphore(3)

        async def search(symbol: str) -> dict[str, Any]:
            async with semaphore:
                try:
                    output = await self._gateway.invoke("search_code", {"pattern": symbol}, state)
                except Exception as exc:
                    return {"symbol": symbol, "paths": [], "status": "unavailable", "error": type(exc).__name__}
                paths = _parse_search_paths(str(output), state.files_changed)
                return {"symbol": symbol, "paths": paths[:_MAX_REFERENCE_PATHS], "status": "ok"}

        return list(await asyncio.gather(*(search(symbol) for symbol in symbols)))

    async def _load_graph_context(self, symbols: list[str], known_paths: set[str]) -> list[dict[str, Any]]:
        if self._db is None:
            return []
        rows: list[dict[str, Any]] = []
        for symbol in symbols:
            try:
                definitions = await self._db.find_symbols_by_name(symbol)
                relations = await self._db.find_relations_to_symbol(symbol)
            except Exception:
                continue
            applicable_relations = [
                row
                for row in relations
                if str(row.get("source_file", "")) in known_paths or str(row.get("target_file", "")) in known_paths
            ][:3]
            relation_targets = {str(row.get("target_file", "")) for row in applicable_relations}
            for row in [
                row
                for row in definitions
                if str(row.get("file_path", "")) in known_paths or str(row.get("file_path", "")) in relation_targets
            ][:3]:
                rows.append(
                    {
                        "kind": "definition",
                        "symbol": symbol,
                        "file": str(row.get("file_path", "")),
                        "risk": str(row.get("risk_level", "safe")),
                    }
                )
            for row in applicable_relations:
                rows.append(
                    {
                        "kind": str(row.get("relation_type", "relation")),
                        "symbol": symbol,
                        "source_file": str(row.get("source_file", "")),
                        "source_symbol": str(row.get("source_symbol", "")),
                        "target_file": str(row.get("target_file", "")),
                    }
                )
            if len(rows) >= _MAX_GRAPH_ROWS:
                break
        return rows[:_MAX_GRAPH_ROWS]

    @staticmethod
    def _risk_signals(
        files: list[ImpactFile], references: list[dict[str, Any]], test_paths: list[str]
    ) -> list[dict[str, Any]]:
        signals: list[dict[str, Any]] = []
        refs_by_symbol = {item["symbol"]: item["paths"] for item in references}
        for item in files:
            for symbol in item.changed_symbols:
                name = str(symbol["name"])
                paths = refs_by_symbol.get(name, [])
                if paths:
                    signals.append(
                        {
                            "type": "blast-radius",
                            "file": item.path,
                            "symbol": name,
                            "reference_count": len(paths),
                        }
                    )
                if _SENSITIVE_NAME.search(name):
                    signals.append({"type": "security-sensitive-symbol", "file": item.path, "symbol": name})
        if signals and not test_paths:
            signals.append(
                {
                    "type": "test-evidence-not-discovered",
                    "note": (
                        "Repository search found no likely test file; this is a retrieval hint, "
                        "not proof of missing tests."
                    ),
                }
            )
        return signals[:16]


def render_impact_manifest(
    manifest: dict[str, Any] | None,
    *,
    files: list[str] | None = None,
    symbol: str = "",
    max_chars: int = 8_000,
) -> str:
    """Render a filtered, bounded JSON view suitable for prompts and tools."""

    if not manifest:
        return "No impact context is available."
    selected_files = set(files or [])
    needle = symbol.strip().lower()
    payload = dict(manifest)
    payload["files"] = [
        item for item in manifest.get("files", []) if not selected_files or str(item.get("path", "")) in selected_files
    ]
    if needle:
        payload["files"] = [item for item in payload["files"] if needle in json.dumps(item, ensure_ascii=False).lower()]
        payload["references"] = [
            item for item in manifest.get("references", []) if needle in str(item.get("symbol", "")).lower()
        ]
        payload["historical_graph"] = [
            item
            for item in manifest.get("historical_graph", [])
            if needle in json.dumps(item, ensure_ascii=False).lower()
        ]
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if len(text) <= max_chars:
        return text

    # Keep tool output valid JSON while progressively dropping lowest-value
    # tail data. A raw string slice can leave the model with malformed evidence.
    payload["truncated"] = True
    for key in ("historical_graph", "risk_signals", "references", "files"):
        items = payload.get(key)
        while (
            isinstance(items, list)
            and items
            and len(json.dumps(payload, ensure_ascii=False, separators=(",", ":"))) > max_chars
        ):
            items.pop()
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if len(text) <= max_chars:
        return text
    return json.dumps({"version": manifest.get("version", 1), "truncated": True}, separators=(",", ":"))


def _touches_changed_line(symbol: SymbolInfo, added_lines: list[int]) -> bool:
    start = symbol.start_line or symbol.line
    end = symbol.end_line or symbol.line
    return any(start <= line <= end for line in added_lines)


def _parse_search_paths(output: str, changed_files: list[str]) -> list[str]:
    changed = set(changed_files)
    paths: list[str] = []
    for raw in output.splitlines():
        candidate = raw.strip().removeprefix("- ").strip()
        if not candidate or candidate == "No results" or candidate in changed:
            continue
        # Mock/plugin clients may include a short ``path:line`` suffix.
        candidate = re.sub(r":\d+(?::\d+)?$", "", candidate)
        if candidate not in paths:
            paths.append(candidate)
    return paths


def _is_test_path(path: str) -> bool:
    normalized = str(PurePosixPath(path)).lower()
    name = PurePosixPath(normalized).name
    return (
        "/test/" in f"/{normalized}/"
        or "/tests/" in f"/{normalized}/"
        or name.startswith("test_")
        or ".test." in name
        or ".spec." in name
        or name.endswith("_test.go")
    )
