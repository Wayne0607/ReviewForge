"""Code-backed declarations and distinct semantic identities for v4 only.

The legacy extractor deliberately exposes its historical regex behavior.
Reuse its language/range support, but require declaration tokens to be code
and preserve separate overloads before the context pack indexes units by ID.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from typing import Any

from reviewforge.engine import symbol_extractor
from reviewforge.engine.semantic_diff import SemanticChangeSet, compile_semantic_changeset

_JAVA_EXPRESSION_STARTS = frozenset(
    {
        "return",
        "new",
        "throw",
        "case",
        "yield",
        "else",
        "if",
        "while",
        "switch",
        "assert",
        "try",
        "catch",
        "for",
        "do",
        "break",
        "continue",
        "this",
        "super",
        "instanceof",
        "package",
        "import",
    }
)


def extract_code_definitions(content: str, path: str) -> list[symbol_extractor.SymbolInfo]:
    language = symbol_extractor.detect_language(path)
    mask = symbol_extractor.mask_non_code(content, language)
    accepted = set()
    for pattern, kind in symbol_extractor.DEFINITION_PATTERNS.get(language, []):
        for match in re.finditer(pattern, content, re.MULTILINE):
            name = match.group(1)
            if mask[match.start(1) : match.end(1)] != name:
                continue
            prefix = re.search(r"[A-Za-z_$][\w$]*", mask[match.start() : match.start(1)])
            if language == "java" and kind == "function" and prefix and prefix.group() in _JAVA_EXPRESSION_STARTS:
                continue
            accepted.add((name, kind, content[: match.start(1)].count("\n") + 1))
    declarations = [
        item
        for item in symbol_extractor.extract_definitions(content, path)
        if (item.name, item.symbol_type, item.line) in accepted
    ]
    if language == "java":
        # The historical regex indexes classes only. Interface declarations
        # need the same ranges/owner identity for v4's bounded member lookup.
        for match in re.finditer(r"\binterface\s+([A-Za-z_$][\w$]*)", mask):
            line = mask.count("\n", 0, match.start(1)) + 1
            declarations.append(
                symbol_extractor.SymbolInfo(
                    name=match.group(1),
                    symbol_type="class",
                    file_path=path,
                    line=line,
                    start_line=mask.count("\n", 0, match.start()) + 1,
                )
            )
        declarations.sort(key=lambda item: (item.line, item.symbol_type != "class", item.name))
        symbol_extractor._populate_symbol_ranges(content, language, declarations)
    return declarations


def _union(first: list[Any], second: list[Any]) -> list[Any]:
    values = {json.dumps(item, sort_keys=True, ensure_ascii=False): item for item in first}
    for item in second:
        values.setdefault(json.dumps(item, sort_keys=True, ensure_ascii=False), item)
    return list(values.values())


def compile_changeset_v4(state: Any) -> SemanticChangeSet:
    """Keep ordinary IDs; qualify collisions by their actual declaration range.

    Overloads/classes/constructors can share a name in one file. The old ID
    assumes uniqueness of that name; silently assigning those units to a dict
    loses context. Identical manifest rows merge, retaining every changed line.
    """
    changeset = compile_semantic_changeset(state)
    counts = Counter(unit.id for unit in changeset.units)
    unique = {}
    for unit in changeset.units:
        if counts[unit.id] > 1:
            declaration = [unit.id, unit.provenance.note, unit.start_line, unit.end_line]
            if unit.start_line <= 0 or unit.end_line < unit.start_line:
                declaration.append(sorted(unit.added_lines))
            unit.id = "su_" + hashlib.sha256(json.dumps(declaration).encode()).hexdigest()[:16]
        existing = unique.get(unit.id)
        if existing is None:
            unique[unit.id] = unit
            continue
        existing.added_lines = sorted(set(existing.added_lines + unit.added_lines))
        existing.risk_score = max(existing.risk_score, unit.risk_score)
        for field in (
            "calls",
            "imports",
            "references",
            "candidate_tests",
            "risk_signals",
            "wiki_facts",
            "risk_reasons",
        ):
            setattr(existing, field, _union(getattr(existing, field), getattr(unit, field)))
    changeset.units = list(unique.values())
    return changeset
