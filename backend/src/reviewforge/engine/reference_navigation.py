"""Bounded literal-reference navigation, never a resolved call/data-flow graph.

Direct-call lookup deliberately excludes lazy bindings and JSX. This separate
v4 lookup preserves exact text and makes those references discoverable without
changing what existing caller results mean. Hits may be comments or strings;
read their pinned source before establishing any actual binding or contract.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from reviewforge.engine.symbol_extractor import LANG_MAP

if TYPE_CHECKING:
    from reviewforge.tools.workspace import GrepHit

MAX_REFERENCE_HITS = 20
REFERENCE_HITS_PER_FILE = 2
MAX_REFERENCE_FILE_BYTES = 2 * 1024 * 1024
_IDENTIFIER = r"(?:[^\W\d]|\$)[\w$]*"
_SYMBOL = re.compile(rf"{_IDENTIFIER}(?:\.{_IDENTIFIER})*")
_TEXT_SUFFIXES = frozenset(
    {
        *LANG_MAP,
        ".c",
        ".h",
        ".cc",
        ".cpp",
        ".cxx",
        ".hpp",
        ".cs",
        ".fs",
        ".fsx",
        ".kt",
        ".kts",
        ".scala",
        ".swift",
        ".m",
        ".mm",
        ".php",
        ".phtml",
        ".pl",
        ".pm",
        ".lua",
        ".r",
        ".dart",
        ".ex",
        ".exs",
        ".erl",
        ".hrl",
        ".clj",
        ".cljs",
        ".cljc",
        ".edn",
        ".hs",
        ".elm",
        ".cjs",
        ".mts",
        ".cts",
        ".pyi",
        ".sh",
        ".bash",
        ".zsh",
        ".ps1",
        ".psm1",
        ".bat",
        ".cmd",
        ".json",
        ".jsonc",
        ".json5",
        ".yaml",
        ".yml",
        ".toml",
        ".ini",
        ".cfg",
        ".conf",
        ".xml",
        ".html",
        ".htm",
        ".xhtml",
        ".css",
        ".scss",
        ".sass",
        ".less",
        ".md",
        ".mdx",
        ".rst",
        ".txt",
        ".adoc",
        ".tex",
        ".graphql",
        ".gql",
        ".proto",
        ".sql",
        ".properties",
        ".po",
        ".pot",
        ".arb",
        ".strings",
        ".resx",
        ".ftl",
        ".j2",
        ".jinja",
        ".jinja2",
        ".ejs",
        ".hbs",
        ".mustache",
        ".vue",
        ".svelte",
        ".tf",
        ".tfvars",
        ".hcl",
        ".cmake",
        ".gradle",
        ".lock",
        ".env",
    }
)
_TEXT_NAMES = frozenset(
    {
        "readme",
        "license",
        "copying",
        "makefile",
        "dockerfile",
        "containerfile",
        "gemfile",
        "rakefile",
        "procfile",
        "justfile",
        ".gitignore",
        ".gitattributes",
        ".editorconfig",
        ".npmrc",
        ".nvmrc",
        ".env",
    }
)


@dataclass(frozen=True, slots=True)
class ReferenceSearchResult:
    hits: tuple[GrepHit, ...] = ()
    truncated: bool = False  # Actual matching rows omitted by hit/per-file caps.
    scanned_files: int = 0
    max_hits: int = 10
    per_file_limit: int = REFERENCE_HITS_PER_FILE
    skipped_files: int = 0  # Type/size/binary limits are distinct from hit truncation.
    skipped_large_files: int = 0
    source_truncated: bool = False
    unavailable: bool = False


def reference_pattern(symbol: str) -> re.Pattern[str]:
    """Accept an exact identifier/dotted identifier, never user regex syntax."""
    if not isinstance(symbol, str) or not 0 < len(symbol) <= 256 or not _SYMBOL.fullmatch(symbol):
        raise ValueError("find_references requires an exact identifier or dotted identifier (max 256 characters)")
    return re.compile(rf"(?<![\w$]){re.escape(symbol)}(?![\w$])")


def reference_hit_limit(max_hits: int) -> int:
    if isinstance(max_hits, bool) or not isinstance(max_hits, int) or max_hits <= 0:
        raise ValueError("find_references max_hits must be a positive integer")
    return min(max_hits, MAX_REFERENCE_HITS)


def is_reference_text_path(path: str) -> bool:
    """Gate decoding as definition search does, with explicit text formats too.

    A glob cannot override this resource boundary. Unknown formats remain
    directly readable but are not included in this bounded navigation scan.
    """
    candidate = PurePosixPath(path)
    return candidate.suffix.lower() in _TEXT_SUFFIXES or candidate.name.lower() in _TEXT_NAMES


def select_reference_hits(hits: Iterable[GrepHit], *, path: str, max_hits: int) -> ReferenceSearchResult:
    """Keep bounded rows: foreign files first, then round-robin path/line.

    Input is path/line ordered, so only the current file's count is needed.
    No production/test guesses or semantic caller claims affect this order.
    """
    limit = reference_hit_limit(max_hits)
    selected: list[tuple[bool, int, str, int, GrepHit]] = []
    current_path = None
    ordinal = 0
    total = 0
    for hit in hits:
        if hit.path != current_path:
            current_path, ordinal = hit.path, 0
        total += 1
        if ordinal < REFERENCE_HITS_PER_FILE:
            selected.append((hit.path == path, ordinal, hit.path, hit.line, hit))
            selected.sort(key=lambda item: item[:4])
            del selected[limit:]
        ordinal += 1
    return ReferenceSearchResult(
        hits=tuple(item[4] for item in selected), truncated=total > len(selected), max_hits=limit
    )
