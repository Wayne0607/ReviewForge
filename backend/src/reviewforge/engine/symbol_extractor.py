"""Symbol Extractor — language-aware extraction of definitions, imports, and calls.

Extracts from diffs and full file content. Supports Python, JavaScript/TypeScript, Go.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ── Language detection ───────────────────────────────────────

LANG_MAP = {
    ".py": "python",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript",
    ".ts": "typescript", ".tsx": "typescript",
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
        # from x.y.z import func_name
        (r"from\s+([\w.]+)\s+import\s+(\w+)", "named"),
        # from x.y import *  (treat as module import)
        (r"from\s+([\w.]+)\s+import\s+\*", "wildcard"),
        # from x.y.z import (a, b, c) — multi-line joined
        (r"from\s+([\w.]+)\s+import\s+\(([^)]+)\)", "multi"),
        # import x.y.z (only at line start, not after 'from')
        (r"^import\s+([\w.]+)", "module"),
    ],
    "javascript": [
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
        # import "pkg"
        (r'import\s+"([^"]+)"', "single"),
        # import alias "pkg"
        (r'import\s+\w+\s+"([^"]+)"', "aliased"),
        # import ( "pkg1" "pkg2" )  — handled separately
    ],
    "java": [
        # import com.example.Class;
        (r"import\s+([\w.]+)\s*;", "single"),
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
    ],
    "typescript": [],
    "go": [
        (r"func\s+(?:\(\w+\s+\*?\w+\)\s+)?(\w+)\s*\(", "function"),
        (r"type\s+(\w+)\s+struct", "class"),
    ],
    "java": [
        (r"(?:public|private|protected)?\s*(?:static\s+)?(?:\w+\s+)+(\w+)\s*\(", "function"),
        (r"class\s+(\w+)", "class"),
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
    name: str    # specific symbol imported
    file_path: str
    import_type: str = "named"  # named / wildcard / module / destructured


@dataclass
class CallInfo:
    caller: str  # function making the call
    callee: str  # function being called
    file_path: str
    line: int = 0


# ── Extraction functions ────────────────────────────────────

def extract_imports(content: str, file_path: str) -> list[ImportInfo]:
    """Extract import statements from file content."""
    lang = detect_language(file_path)
    patterns = IMPORT_PATTERNS.get(lang, [])
    imports = []

    for pattern, imp_type in patterns:
        for match in re.finditer(pattern, content, re.MULTILINE):
            if imp_type == "destructured":
                # import { a, b, c } from 'module'
                names = [n.strip() for n in match.group(1).split(",")]
                source = match.group(2)
                for name in names:
                    actual = name.split(" as ")[0].strip() if " as " in name else name
                    imports.append(ImportInfo(source=source, name=actual, file_path=file_path, import_type=imp_type))
            elif imp_type == "multi":
                # from x.y import (a, b, c)
                source = match.group(1)
                names = [n.strip() for n in match.group(2).split(",") if n.strip()]
                for name in names:
                    name = name.split(" as ")[0].strip() if " as " in name else name
                    imports.append(ImportInfo(source=source, name=name, file_path=file_path, import_type="named"))
            elif imp_type in ("named", "wildcard"):
                imports.append(ImportInfo(source=match.group(1), name=match.group(2) if imp_type == "named" else "*", file_path=file_path, import_type=imp_type))
            elif imp_type in ("module", "single"):
                imports.append(ImportInfo(source=match.group(1), name="", file_path=file_path, import_type=imp_type))
            elif imp_type == "default":
                imports.append(ImportInfo(source=match.group(2), name=match.group(1), file_path=file_path, import_type=imp_type))
            elif imp_type == "require":
                imports.append(ImportInfo(source=match.group(1), name="", file_path=file_path, import_type=imp_type))
            elif imp_type == "side_effect":
                imports.append(ImportInfo(source=match.group(1), name="", file_path=file_path, import_type=imp_type))

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
                symbols.append(SymbolInfo(
                    name=match.group(1),
                    symbol_type=sym_type,
                    file_path=file_path,
                    line=i + 1,
                ))
                break  # One match per line

    return symbols


def extract_calls(content: str, file_path: str) -> list[CallInfo]:
    """Extract function calls from file content."""
    lang = detect_language(file_path)
    patterns = CALL_PATTERNS.get(lang, [])
    calls = []
    lines = content.split("\n")

    # Find current function context
    current_func = _find_enclosing_function(content, lines)

    for i, line in enumerate(lines):
        stripped = line.strip()
        # Skip comments and definitions
        if stripped.startswith("#") or stripped.startswith("//") or stripped.startswith("*"):
            continue
        if re.match(r"(?:async\s+)?def\s+|function\s+|class\s+", stripped):
            continue

        for pattern in patterns:
            for match in re.finditer(pattern, line):
                callee = match.group(1)
                # Skip common keywords/builtins
                if callee in ("if", "for", "while", "return", "print", "len", "range",
                              "int", "str", "float", "list", "dict", "set", "tuple",
                              "True", "False", "None", "self", "cls", "super",
                              "import", "from", "class", "def", "async", "await",
                              "try", "except", "finally", "with", "as", "yield"):
                    continue
                caller = current_func.get(i, "<module>")
                calls.append(CallInfo(caller=caller, callee=callee, file_path=file_path, line=i + 1))

    return calls


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
    """Extract new definitions and imports from a diff (only added lines)."""
    added_lines = []
    for line in diff_content.split("\n"):
        if line.startswith("+") and not line.startswith("+++"):
            added_lines.append(line[1:])

    added_content = "\n".join(added_lines)

    # Join multi-line imports (Python: from x import (\n  a,\n  b,\n))
    added_content = _join_multiline_imports(added_content)

    symbols = extract_definitions(added_content, file_path)
    imports = extract_imports(added_content, file_path)
    return symbols, imports


def _join_multiline_imports(content: str) -> str:
    """Join multi-line import statements into single lines."""
    lines = content.split("\n")
    result = []
    buffer = ""
    paren_depth = 0

    for line in lines:
        if buffer:
            buffer += " " + line.strip()
            paren_depth += line.count("(") - line.count(")")
            if paren_depth <= 0:
                result.append(buffer)
                buffer = ""
                paren_depth = 0
        elif re.match(r"(?:from|import)\s+", line.strip()) and "(" in line and ")" not in line:
            buffer = line.strip()
            paren_depth = line.count("(") - line.count(")")
        else:
            result.append(line)

    if buffer:
        result.append(buffer)

    return "\n".join(result)
