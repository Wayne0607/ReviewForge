"""Symbol Extractor — language-aware extraction of definitions, imports, and calls.

Extracts from diffs and full file content. Supports Python, JavaScript/TypeScript, Go.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


# ── Language detection ───────────────────────────────────────

LANG_MAP = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".vue": "typescript",
    ".svelte": "typescript",
    ".go": "go",
    ".java": "java",
    ".rs": "rust",
    ".rb": "ruby",
}


def detect_language(file_path: str) -> str:
    ext = Path(file_path).suffix.lower()
    return LANG_MAP.get(ext, "unknown")


# ── Import patterns per language ─────────────────────────────

IMPORT_PATTERNS: dict[str, list[tuple[str, str]]] = {
    "python": [
        # from x.y import *  (treat as module import) — checked before the named list
        (r"from\s+([\w.]+)\s+import\s+\*", "wildcard"),
        # from x.y.z import a, b as c, d  (comma list, optionally with trailing comment)
        (r"from\s+([\w.]+)\s+import\s+(\w[\w ,]*?)\s*(?:#.*)?$", "named"),
        # from x.y.z import (a, b, c) — multi-line joined
        (r"from\s+([\w.]+)\s+import\s+\(([^)]+)\)", "multi"),
        # import x.y.z (only at line start, not after 'from')
        (r"^import\s+([\w.]+)(?:\s+as\s+(\w+))?", "module"),
    ],
    "javascript": [
        # import * as moduleAlias from 'module'
        (r"import\s+\*\s+as\s+(\w+)\s+from\s*['\"]([^'\"]+)['\"]", "namespace"),
        # import { func } from 'module'
        (r"import\s*\{([^}]+)\}\s*from\s*['\"]([^'\"]+)['\"]", "destructured"),
        # import func from 'module'
        (r"import\s+(\w+)\s+from\s*['\"]([^'\"]+)['\"]", "default"),
        # import 'module'  (side-effect)
        (r"import\s*['\"]([^'\"]+)['\"]", "side_effect"),
        # const x = require('module')
        (r"require\s*\(\s*['\"]([^'\"]+)['\"]\s*\)", "require"),
    ],
    "typescript": [],  # Same as JS, will inherit
    "go": [
        # import alias "pkg"
        (r'import\s+(\w+)\s+"([^"]+)"', "aliased"),
        # import "pkg"
        (r'import\s+"([^"]+)"', "single"),
        # import ( "pkg1" "pkg2" )  — handled separately
    ],
    "java": [
        # import com.example.Class;
        (r"import\s+([\w.]+)\s*;", "single"),
    ],
    "ruby": [
        (r"require_relative\s+['\"]([^'\"]+)['\"]", "single"),
        (r"require\s+['\"]([^'\"]+)['\"]", "single"),
    ],
    "rust": [
        (r"use\s+([\w:]+)::(\w+)\s*;", "rust_use"),
        (r"use\s+([\w:]+)\s*;", "single"),
    ],
}

# TS inherits JS patterns
IMPORT_PATTERNS["typescript"] = IMPORT_PATTERNS["javascript"]


# ── Function/class definition patterns ──────────────────────

DEFINITION_PATTERNS: dict[str, list[tuple[str, str]]] = {
    "python": [
        (r"(?:async\s+)?def\s+(\w+)\s*\(", "function"),
        (r"class\s+(\w+)\s*[\(:]", "class"),
    ],
    "javascript": [
        (r"(?:export\s+(?:default\s+)?)?(?:async\s+)?function\s+(\w+)\s*\(", "function"),
        (r"(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s+)?\(", "function"),
        (r"(?:export\s+)?class\s+(\w+)", "class"),
        # Class/object methods (including Vue methods and Angular class methods).
        (
            r"^\s*(?:(?:public|private|protected|static|readonly|override|abstract|async)\s+)*"
            r"(?!if\b|for\b|while\b|switch\b|catch\b)(\w+)\s*\([^;]*\)\s*(?::[^={]+)?\s*\{",
            "function",
        ),
    ],
    "typescript": [],
    "go": [
        (r"func\s+(?:\(\w+\s+\*?\w+\)\s+)?(\w+)\s*\(", "function"),
        (r"type\s+(\w+)\s+struct", "class"),
    ],
    "java": [
        (
            r"^\s*(?:(?:public|private|protected|static|final|synchronized|native|abstract)\s+)*"
            r"(?:<[^>]+>\s+)?[\w<>\[\],.?]+\s+(\w+)\s*\([^;{}]*\)"
            r"\s*(?:throws\s+[^\{]+)?\{?",
            "function",
        ),
        (r"class\s+(\w+)", "class"),
    ],
    "ruby": [
        (r"def\s+(\w+[!?=]?)\s*(?:\(|$)", "function"),
        (r"class\s+(\w+)", "class"),
        (r"module\s+(\w+)", "class"),
    ],
    "rust": [
        (r"(?:pub\s+)?(?:unsafe\s+)?fn\s+(\w+)\s*\(", "function"),
        (r"(?:pub\s+)?struct\s+(\w+)", "class"),
        (r"(?:pub\s+)?enum\s+(\w+)", "class"),
    ],
}

DEFINITION_PATTERNS["typescript"] = DEFINITION_PATTERNS["javascript"]


# ── Function call patterns ──────────────────────────────────

CALL_PATTERNS: dict[str, list[str]] = {
    "python": [
        r"(\w+)\s*\(",
    ],
    "javascript": [
        r"(\w+)\s*\(",
        r"(\w+)\.\w+\s*\(",  # method calls
    ],
    "typescript": [],
    "go": [
        r"(\w+)\s*\(",
    ],
    "java": [
        r"(\w+)\s*\(",
    ],
    "ruby": [
        r"(\w+[!?=]?)\s*\(",
    ],
    "rust": [
        r"(\w+)\s*\(",
        r"(\w+)::\w+\s*\(",
    ],
}

CALL_PATTERNS["typescript"] = CALL_PATTERNS["javascript"]


# ── Data classes ─────────────────────────────────────────────


@dataclass
class SymbolInfo:
    name: str
    symbol_type: str  # 'function' / 'class'
    file_path: str
    line: int = 0


@dataclass
class ImportInfo:
    source: str  # module path
    name: str  # specific symbol imported
    file_path: str
    import_type: str = "named"  # named / wildcard / module / destructured
    line: int = 0
    # Name used by the importing file.  This differs from ``name`` for
    # aliases (``foo as f``), module aliases, and imported Java classes.
    local_name: str = ""


@dataclass
class CallInfo:
    caller: str  # function making the call
    callee: str  # function being called
    file_path: str
    line: int = 0
    # Receiver for a member call (``seed`` in ``seed.run()``).  ``receiver_type``
    # is populated when a local variable/constructor parameter has an imported
    # type, e.g. ``SeedJava seed`` or ``private admin: AdminComponent``.
    receiver: str = ""
    receiver_type: str = ""


# ── Extraction functions ────────────────────────────────────


def _split_import_alias(piece: str) -> tuple[str, str]:
    """Return the exported name and the binding visible in the consumer."""

    parts = re.split(r"\s+as\s+", piece.strip(), maxsplit=1)
    exported = parts[0].strip()
    local_name = parts[1].strip() if len(parts) == 2 else exported
    return exported, local_name


def extract_imports(content: str, file_path: str) -> list[ImportInfo]:
    """Extract import statements from file content."""
    lang = detect_language(file_path)
    patterns = IMPORT_PATTERNS.get(lang, [])
    imports = []

    for pattern, imp_type in patterns:
        for match in re.finditer(pattern, content, re.MULTILINE):
            line_no = content[: match.start()].count("\n") + 1
            if imp_type == "destructured":
                # import { a, b, c } from 'module'
                names = [n.strip() for n in match.group(1).split(",") if n.strip()]
                source = match.group(2)
                for name in names:
                    actual, local_name = _split_import_alias(name)
                    imports.append(
                        ImportInfo(
                            source=source,
                            name=actual,
                            file_path=file_path,
                            import_type=imp_type,
                            line=line_no,
                            local_name=local_name,
                        )
                    )
            elif imp_type == "multi":
                # from x.y import (a, b, c)
                source = match.group(1)
                names = [n.strip() for n in match.group(2).split(",") if n.strip()]
                for piece in names:
                    name, local_name = _split_import_alias(piece)
                    imports.append(
                        ImportInfo(
                            source=source,
                            name=name,
                            file_path=file_path,
                            import_type="named",
                            line=line_no,
                            local_name=local_name,
                        )
                    )
            elif imp_type == "wildcard":
                imports.append(
                    ImportInfo(
                        source=match.group(1),
                        name="*",
                        file_path=file_path,
                        import_type="wildcard",
                        line=line_no,
                    )
                )
            elif imp_type == "named":
                # group(2) may be a comma-separated list: "a, b as c, d"
                source = match.group(1)
                for piece in match.group(2).split(","):
                    piece = piece.strip()
                    if not piece:
                        continue
                    actual, local_name = _split_import_alias(piece)
                    imports.append(
                        ImportInfo(
                            source=source,
                            name=actual,
                            file_path=file_path,
                            import_type="named",
                            line=line_no,
                            local_name=local_name,
                        )
                    )
            elif imp_type == "module":
                source = match.group(1)
                alias = match.group(2) or source.split(".", 1)[0]
                imports.append(
                    ImportInfo(
                        source=source,
                        name="",
                        file_path=file_path,
                        import_type=imp_type,
                        line=line_no,
                        local_name=alias,
                    )
                )
            elif imp_type == "single":
                source = match.group(1)
                imported_name = source.rsplit(".", 1)[-1] if lang == "java" else ""
                local_name = imported_name or source.rstrip("/").rsplit("/", 1)[-1]
                imports.append(
                    ImportInfo(
                        source=source,
                        name=imported_name,
                        file_path=file_path,
                        import_type=imp_type,
                        line=line_no,
                        local_name=local_name,
                    )
                )
            elif imp_type == "aliased":
                imports.append(
                    ImportInfo(
                        source=match.group(2),
                        name="",
                        file_path=file_path,
                        import_type=imp_type,
                        line=line_no,
                        local_name=match.group(1),
                    )
                )
            elif imp_type == "namespace":
                imports.append(
                    ImportInfo(
                        source=match.group(2),
                        name="*",
                        file_path=file_path,
                        import_type=imp_type,
                        line=line_no,
                        local_name=match.group(1),
                    )
                )
            elif imp_type == "rust_use":
                imports.append(
                    ImportInfo(
                        source=match.group(1).replace("::", "."),
                        name=match.group(2),
                        file_path=file_path,
                        import_type=imp_type,
                        line=line_no,
                        local_name=match.group(2),
                    )
                )
            elif imp_type == "default":
                imports.append(
                    ImportInfo(
                        source=match.group(2),
                        name=match.group(1),
                        file_path=file_path,
                        import_type=imp_type,
                        line=line_no,
                        local_name=match.group(1),
                    )
                )
            elif imp_type == "require":
                imports.append(
                    ImportInfo(source=match.group(1), name="", file_path=file_path, import_type=imp_type, line=line_no)
                )
            elif imp_type == "side_effect":
                imports.append(
                    ImportInfo(source=match.group(1), name="", file_path=file_path, import_type=imp_type, line=line_no)
                )

    return imports


def extract_definitions(content: str, file_path: str) -> list[SymbolInfo]:
    """Extract function and class definitions from file content."""
    lang = detect_language(file_path)
    patterns = DEFINITION_PATTERNS.get(lang, [])
    symbols = []
    lines = content.split("\n")

    for i, line in enumerate(lines):
        for pattern, sym_type in patterns:
            match = re.search(pattern, line)
            if match:
                symbols.append(
                    SymbolInfo(
                        name=match.group(1),
                        symbol_type=sym_type,
                        file_path=file_path,
                        line=i + 1,
                    )
                )
                break  # One match per line

    return symbols


def extract_calls(content: str, file_path: str) -> list[CallInfo]:
    """Extract function calls from file content."""
    lang = detect_language(file_path)
    if not CALL_PATTERNS.get(lang, []):
        return []

    calls: list[CallInfo] = []
    lines = content.split("\n")

    # Find current function context
    current_func = _find_enclosing_function(content, lines)
    definitions_by_line: dict[int, set[str]] = {}
    for definition in extract_definitions(content, file_path):
        definitions_by_line.setdefault(definition.line, set()).add(definition.name)
    receiver_types = _extract_receiver_types(content, lang)

    ignored = {
        "if",
        "for",
        "while",
        "return",
        "print",
        "len",
        "range",
        "int",
        "str",
        "float",
        "list",
        "dict",
        "set",
        "tuple",
        "True",
        "False",
        "None",
        "self",
        "cls",
        "super",
        "import",
        "from",
        "class",
        "def",
        "func",
        "function",
        "async",
        "await",
        "try",
        "except",
        "finally",
        "with",
        "as",
        "yield",
        "switch",
        "catch",
        "new",
    }

    member_pattern = re.compile(
        r"(?P<receiver>[A-Za-z_$]\w*(?:\s*\.\s*[A-Za-z_$]\w*)*)"
        r"\s*\.\s*(?P<callee>[A-Za-z_$]\w*)\s*\("
    )
    direct_pattern = re.compile(r"(?<![.\w$])(?P<callee>[A-Za-z_$]\w*)\s*\(")
    jsx_pattern = re.compile(r"<(?P<callee>[A-Z][A-Za-z0-9_$]*)\b")

    for i, line in enumerate(lines):
        stripped = line.strip()
        # Skip comments/imports. Definitions are handled per call below so an
        # inline function body can still contribute a real call.
        if stripped.startswith("#") or stripped.startswith("//") or stripped.startswith("*"):
            continue
        if re.match(r"(?:from|import|use)\s+", stripped):
            continue

        caller = current_func.get(i, "<module>")
        occupied: list[tuple[int, int]] = []

        for match in member_pattern.finditer(line):
            receiver = re.sub(r"\s+", "", match.group("receiver"))
            callee = match.group("callee")
            if callee in ignored:
                continue
            receiver_name = receiver.rsplit(".", 1)[-1]
            calls.append(
                CallInfo(
                    caller=caller,
                    callee=callee,
                    file_path=file_path,
                    line=i + 1,
                    receiver=receiver,
                    receiver_type=receiver_types.get(receiver_name, ""),
                )
            )
            occupied.append(match.span())

        for match in direct_pattern.finditer(line):
            callee = match.group("callee")
            if callee in ignored or callee in definitions_by_line.get(i + 1, set()):
                continue
            if any(start <= match.start() < end for start, end in occupied):
                continue
            calls.append(CallInfo(caller=caller, callee=callee, file_path=file_path, line=i + 1))

        if lang in {"javascript", "typescript"}:
            for match in jsx_pattern.finditer(line):
                callee = match.group("callee")
                calls.append(CallInfo(caller=caller, callee=callee, file_path=file_path, line=i + 1))

    return calls


def _extract_receiver_types(content: str, lang: str) -> dict[str, str]:
    """Infer local receiver names whose declared type can be tied to an import."""

    receiver_types: dict[str, str] = {}
    if lang in {"javascript", "typescript"}:
        # TypeScript parameter properties and typed fields/parameters.
        for match in re.finditer(
            r"(?:(?:public|private|protected|readonly)\s+)*(\w+)\s*:\s*([A-Za-z_$]\w*)",
            content,
        ):
            receiver_types[match.group(1)] = match.group(2)
    elif lang == "java":
        for match in re.finditer(
            r"\b([A-Z][A-Za-z0-9_$]*(?:<[^;=()]+>)?)\s+(\w+)\s*(?=[=;,)])",
            content,
        ):
            receiver_types[match.group(2)] = match.group(1).split("<", 1)[0]

    # Constructor assignment is shared by JavaScript/TypeScript and Java.
    for match in re.finditer(r"\b(?:const|let|var)?\s*(\w+)\s*=\s*new\s+([A-Za-z_$]\w*)\s*\(", content):
        receiver_types[match.group(1)] = match.group(2)
    return receiver_types


def _find_enclosing_function(content: str, lines: list[str]) -> dict[int, str]:
    """Map each line number to its enclosing function name."""
    result = {}
    current = "<module>"
    func_indent = -1

    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            result[i] = current
            continue

        indent = len(line) - len(line.lstrip())

        # Check for function/class definition
        def_match = re.match(r"(?:async\s+)?def\s+(\w+)\s*\(", stripped)
        class_match = re.match(r"class\s+(\w+)", stripped)

        if def_match:
            current = def_match.group(1)
            func_indent = indent
        elif class_match:
            current = class_match.group(1)
            func_indent = indent
        elif indent <= func_indent and func_indent >= 0:
            # Back to outer scope
            current = "<module>"
            func_indent = -1

        result[i] = current

    return result


def extract_diff_symbols(diff_content: str, file_path: str) -> tuple[list[SymbolInfo], list[ImportInfo]]:
    """Extract added definitions/imports with GitHub RIGHT-side line numbers."""

    added_lines = _mapped_added_lines(diff_content)
    if not added_lines:
        return [], []

    # Join multi-line imports (Python: from x import (\n  a,\n  b,\n)) while
    # retaining the first source line as the import's review-comment anchor.
    joined_lines = _join_multiline_import_lines(added_lines)
    added_content = "\n".join(line for _line_no, line in joined_lines)

    symbols = extract_definitions(added_content, file_path)
    imports = extract_imports(added_content, file_path)
    symbols = [item for item in symbols if _remap_item_line(item, joined_lines)]
    imports = [item for item in imports if _remap_item_line(item, joined_lines)]
    return symbols, imports


def extract_diff_calls(diff_content: str, file_path: str) -> list[CallInfo]:
    """Extract calls added by a diff with GitHub RIGHT-side line numbers."""

    added_lines = _mapped_added_lines(diff_content)
    if not added_lines:
        return []

    added_content = "\n".join(line for _line_no, line in added_lines)
    calls = extract_calls(added_content, file_path)
    return [item for item in calls if _remap_item_line(item, added_lines)]


def _mapped_added_lines(diff_content: str) -> list[tuple[int, str]]:
    # Lazy import avoids a symbol_extractor ↔ detectors package import cycle:
    # security detectors already import detect_language from this module.
    from reviewforge.engine.detectors.unified_diff import iter_added_lines

    return iter_added_lines(diff_content)


def _remap_item_line(item: SymbolInfo | ImportInfo | CallInfo, mapped_lines: list[tuple[int, str]]) -> bool:
    relative_line = item.line
    if relative_line <= 0 or relative_line > len(mapped_lines):
        return False
    item.line = mapped_lines[relative_line - 1][0]
    return True


def _join_multiline_imports(content: str) -> str:
    """Join multi-line import statements into single lines."""
    lines = [(idx, line) for idx, line in enumerate(content.split("\n"), start=1)]
    return "\n".join(line for _line_no, line in _join_multiline_import_lines(lines))


def _join_multiline_import_lines(lines: list[tuple[int, str]]) -> list[tuple[int, str]]:
    """Join multi-line imports without losing their mapped source line."""

    result: list[tuple[int, str]] = []
    buffer = ""
    buffer_line = 0
    paren_depth = 0

    for line_no, line in lines:
        if buffer:
            buffer += " " + line.strip()
            paren_depth += line.count("(") - line.count(")")
            if paren_depth <= 0:
                result.append((buffer_line, buffer))
                buffer = ""
                buffer_line = 0
                paren_depth = 0
        elif re.match(r"(?:from|import)\s+", line.strip()) and "(" in line and ")" not in line:
            buffer = line.strip()
            buffer_line = line_no
            paren_depth = line.count("(") - line.count(")")
        else:
            result.append((line_no, line))

    if buffer:
        result.append((buffer_line, buffer))

    return result
