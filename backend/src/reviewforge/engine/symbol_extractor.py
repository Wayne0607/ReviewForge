"""Symbol Extractor — language-aware extraction of definitions, imports, and calls.

Extracts from diffs and full file content. Supports Python, JavaScript/TypeScript, Go.
"""

from __future__ import annotations

import ast
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


# Bounded approximations of balanced generic argument lists.  The alternatives
# are delimiter-disjoint, and malformed input cannot wander into the next
# declaration. Eight levels comfortably cover generated nested collection types.
def _bounded_delimited_group(open_char: str, close_char: str, *, max_depth: int = 8) -> str:
    escaped_open = re.escape(open_char)
    escaped_close = re.escape(close_char)
    plain = rf"[^{escaped_open}{escaped_close}]"
    group = rf"{escaped_open}{plain}*{escaped_close}"
    for _ in range(max_depth - 1):
        group = rf"{escaped_open}(?:{plain}|{group})*{escaped_close}"
    return group


_ANGLE_GROUP = _bounded_delimited_group("<", ">")
_SQUARE_GROUP = _bounded_delimited_group("[", "]")
_JAVA_TYPE = rf"[\w$?.]+(?:\s*{_ANGLE_GROUP})?(?:\s*\[\s*\])*"


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
            r"(?!if\b|for\b|while\b|switch\b|catch\b)(\w+)\s*\([^;{}]*\)\s*(?::[^={]+)?\s*\{",
            "function",
        ),
    ],
    "typescript": [],
    "go": [
        (
            rf"func\s+(?:\(\s*\w+\s+\*?\w+(?:{_SQUARE_GROUP})?\s*\)\s+)?"
            rf"(\w+)\s*(?:{_SQUARE_GROUP})?\s*\(",
            "function",
        ),
        (rf"type\s+(\w+)\s*(?:{_SQUARE_GROUP})?\s+struct", "class"),
    ],
    "java": [
        (
            r"^\s*(?!new\b|return\b|throw\b)"
            r"(?:(?:public|private|protected|static|final|synchronized|native|abstract|default|strictfp)\s+)*"
            rf"(?:{_ANGLE_GROUP}\s+)?{_JAVA_TYPE}\s+(\w+)\s*\(",
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
        (
            rf"(?:pub(?:\([^)]*\))?\s+)?(?:const\s+)?(?:async\s+)?(?:unsafe\s+)?"
            rf"(?:extern\s+\"[^\"]+\"\s+)?fn\s+(\w+)\s*(?:{_ANGLE_GROUP})?\s*\(",
            "function",
        ),
        (r"(?:pub\s+)?struct\s+(\w+)", "class"),
        (r"(?:pub\s+)?enum\s+(\w+)", "class"),
    ],
}

DEFINITION_PATTERNS["typescript"] = [
    (
        rf"(?:export\s+(?:default\s+)?)?(?:async\s+)?function\s+(\w+)\s*{_ANGLE_GROUP}\s*\(",
        "function",
    ),
    (
        rf"(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s+)?{_ANGLE_GROUP}\s*\(",
        "function",
    ),
    *DEFINITION_PATTERNS["javascript"],
]


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
    # Inclusive source range owned by this declaration. ``start_line`` can
    # precede ``line`` for decorators/annotations/doc comments. A zero
    # ``end_line`` means the extractor could not prove a safe boundary.
    start_line: int = 0
    end_line: int = 0


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
    # One-based column of the complete call expression (the receiver for a
    # member call, otherwise the callee).  Together with ``line`` this keeps
    # distinct calls to the same historical sink from being collapsed before
    # semantic confirmation and lets the confirmer recover a balanced,
    # multi-line argument list from the RIGHT side of the diff.
    column: int = 0
    # Receiver for a member call (``seed`` in ``seed.run()``).  ``receiver_type``
    # is populated when a local variable/constructor parameter has an imported
    # type, e.g. ``SeedJava seed`` or ``private admin: AdminComponent``.
    receiver: str = ""
    receiver_type: str = ""
    # True only when a language-aware scope analysis proves that the binding
    # used by this call is an unshadowed import in the same lexical scope.
    binding_proven: bool = False


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
    code_mask = mask_non_code(content, lang)

    for pattern, imp_type in patterns:
        for match in re.finditer(pattern, content, re.MULTILINE):
            first_token = re.search(r"[A-Za-z_]", match.group(0))
            if first_token is None or code_mask[match.start() + first_token.start()].isspace():
                continue
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

    # Go commonly groups imports in a parenthesized block.  The line-oriented
    # ``import alias \"pkg\"`` pattern above cannot see entries after the opening
    # parenthesis, which previously reduced exact namespace calls to same-name
    # fuzzy matches in cross-PR analysis.
    if lang == "go":
        comment_mask = _mask_c_style_comments(content)
        for block in re.finditer(r"\bimport\s*\((?P<body>.*?)\)", comment_mask, re.DOTALL):
            if code_mask[block.start()].isspace():
                continue
            body = block.group("body")
            for match in re.finditer(
                r'^\s*(?:(?P<alias>[A-Za-z_]\w*|\.)\s+)?"(?P<source>[^"]+)"',
                body,
                re.MULTILINE,
            ):
                alias = match.group("alias") or ""
                # Blank and dot imports do not expose a stable namespace that
                # can prove a member call; underscore imports expose no binding.
                if alias in {"_", "."}:
                    continue
                source = match.group("source")
                imports.append(
                    ImportInfo(
                        source=source,
                        name="",
                        file_path=file_path,
                        import_type="aliased" if alias else "single",
                        line=content[: block.start("body") + match.start()].count("\n") + 1,
                        local_name=alias or source.rstrip("/").rsplit("/", 1)[-1],
                    )
                )

    return imports


def extract_definitions(content: str, file_path: str) -> list[SymbolInfo]:
    """Extract function/class definitions and their reliable source ranges."""
    lang = detect_language(file_path)
    patterns = DEFINITION_PATTERNS.get(lang, [])
    symbols: list[SymbolInfo] = []
    seen: set[tuple[str, str, int]] = set()

    # Search the complete source instead of one physical line at a time. This
    # keeps Java/TypeScript method declarations with multi-line signatures
    # visible to patterns that intentionally span whitespace/newlines.
    for pattern, sym_type in patterns:
        for match in re.finditer(pattern, content, re.MULTILINE):
            line = content[: match.start(1)].count("\n") + 1
            key = (match.group(1), sym_type, line)
            if key in seen:
                continue
            seen.add(key)
            symbols.append(
                SymbolInfo(
                    name=match.group(1),
                    symbol_type=sym_type,
                    file_path=file_path,
                    line=line,
                    start_line=_match_declaration_start(content, match, line),
                )
            )

    symbols.sort(key=lambda item: (item.line, item.symbol_type != "class", item.name))
    _populate_symbol_ranges(content, lang, symbols)
    return symbols


def _match_declaration_start(content: str, match: re.Match[str], fallback: int) -> int:
    """Return the first non-whitespace line in a possibly multi-line signature."""

    prefix = content[match.start() : match.start(1)]
    first_token = re.search(r"\S", prefix)
    if first_token is None:
        return fallback
    return content[: match.start() + first_token.start()].count("\n") + 1


def _populate_symbol_ranges(content: str, lang: str, symbols: list[SymbolInfo]) -> None:
    """Populate inclusive declaration ranges without guessing past unknown syntax."""

    if not symbols:
        return
    lines = content.split("\n")

    if lang == "python" and _populate_python_ranges(content, symbols):
        return

    for symbol_index, symbol in enumerate(symbols):
        symbol.start_line = _leading_declaration_start(lines, symbol.start_line or symbol.line, lang)
        if lang in {"go", "java", "javascript", "typescript", "rust"}:
            # A body-less/unsupported declaration must never borrow the opening
            # brace of the following declaration.  Classes and outer functions
            # may legitimately contain later symbols, so the boundary only
            # applies while the scanner is still looking for this symbol's body.
            next_declaration_line = next(
                (
                    candidate.start_line or candidate.line
                    for candidate in symbols[symbol_index + 1 :]
                    if candidate.line > symbol.line
                ),
                0,
            )
            symbol.end_line = _find_braced_symbol_end(
                lines,
                symbol.line,
                declaration_start_line=symbol.start_line,
                next_declaration_line=next_declaration_line,
                lang=lang,
                symbol_name=symbol.name,
            )
        elif lang == "ruby":
            symbol.end_line = _find_ruby_symbol_end(lines, symbol.line)


def _populate_python_ranges(content: str, symbols: list[SymbolInfo]) -> bool:
    """Use Python's parser for decorator, multi-line signature and body bounds."""

    try:
        tree = ast.parse(content)
    except SyntaxError:
        return False

    by_key = {(symbol.name, symbol.line, symbol.symbol_type): symbol for symbol in symbols}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            symbol_type = "function"
        elif isinstance(node, ast.ClassDef):
            symbol_type = "class"
        else:
            continue

        symbol = by_key.get((node.name, node.lineno, symbol_type))
        if symbol is None:
            continue
        decorators = [item.lineno for item in node.decorator_list]
        symbol.start_line = min([node.lineno, *decorators])
        symbol.end_line = node.end_lineno or 0
    return True


def _leading_declaration_start(lines: list[str], definition_line: int, lang: str) -> int:
    """Include contiguous comments/annotations that document a declaration."""

    start = definition_line
    index = definition_line - 2

    while index >= 0:
        stripped = lines[index].strip()
        if not stripped:
            break

        is_comment = stripped.startswith(("//", "#", "/*", "*", "*/"))
        is_annotation = lang in {"java", "javascript", "typescript"} and stripped.startswith("@")

        annotation_start = None
        if lang in {"java", "javascript", "typescript"}:
            annotation_start = _multiline_annotation_start(lines, index)
        if annotation_start is not None:
            start = annotation_start
            index = annotation_start - 2
            continue

        if not (is_comment or is_annotation):
            break
        start = index + 1
        index -= 1

    return start


def _multiline_annotation_start(lines: list[str], end_index: int) -> int | None:
    """Return a bounded ``@Annotation(...)`` start ending at ``end_index``."""

    stripped = lines[end_index].strip()
    if not stripped.endswith(")"):
        return None

    depth = 0
    for index in range(end_index, max(-1, end_index - 20), -1):
        candidate = lines[index].strip()
        if not candidate:
            return None
        depth += candidate.count(")") - candidate.count("(")
        if candidate.startswith("@"):
            return index + 1 if depth == 0 else None
        # ``foo()`` or another complete expression immediately before a
        # declaration is not an annotation continuation.
        if depth <= 0:
            return None
    return None


def _find_braced_symbol_end(
    lines: list[str],
    definition_line: int,
    *,
    declaration_start_line: int = 0,
    next_declaration_line: int = 0,
    lang: str = "",
    symbol_name: str = "",
) -> int:
    """Find a balanced brace body's inclusive end, ignoring strings/comments."""

    depth = 0
    started = False
    # Braces inside a function signature are not the function body.  This is
    # common in TypeScript destructured parameters and inline object types, e.g.
    # ``function Card({ html }: { html: string }) {``.  Treating ``{ html }`` as
    # the body made the range end on the declaration line, so a finding on the
    # first real statement could not be attributed to the symbol.
    paren_depth = 0
    bracket_depth = 0
    angle_depth = 0
    declaration_start_line = declaration_start_line or definition_line
    declaration_end_line = next_declaration_line - 1 if next_declaration_line else definition_line + 12
    declaration = "\n".join(lines[declaration_start_line - 1 : declaration_end_line])
    arrow_declaration = bool(
        symbol_name
        and lang in {"javascript", "typescript"}
        and re.search(
            rf"\b(?:const|let|var)\s+{re.escape(symbol_name)}\s*=",
            declaration,
            re.MULTILINE,
        )
    )
    arrow_seen = False
    arrow_pending = False
    arrow_expression = False
    expression_brace_depth = 0
    expression_last_line = 0
    type_brace_depth = 0
    saw_parameters = False
    parameters_closed = False
    return_type_start = -1
    signature: list[str] = []
    quote = ""
    escaped = False
    block_comment = False

    for line_index in range(definition_line - 1, len(lines)):
        source_line = line_index + 1
        if next_declaration_line and source_line >= next_declaration_line and not started:
            return expression_last_line if arrow_expression else 0

        line = lines[line_index]
        index = 0
        while index < len(line):
            char = line[index]
            pair = line[index : index + 2]

            if block_comment:
                if pair == "*/":
                    block_comment = False
                    index += 2
                else:
                    index += 1
                continue
            if quote:
                if arrow_expression and not char.isspace():
                    expression_last_line = source_line
                if escaped:
                    escaped = False
                elif char == "\\" and quote != "`":
                    escaped = True
                elif char == quote:
                    quote = ""
                index += 1
                continue
            if pair == "//":
                break
            if pair == "/*":
                block_comment = True
                index += 2
                continue
            if arrow_pending and not char.isspace():
                if char == "{" and paren_depth == 0 and bracket_depth == 0 and angle_depth == 0:
                    depth = 1
                    started = True
                    arrow_pending = False
                    signature.append(char)
                    index += 1
                    continue
                arrow_pending = False
                arrow_expression = True
            if char in {'"', "'", "`"}:
                if arrow_expression:
                    expression_last_line = source_line
                quote = char
                signature.append(char)
                index += 1
                continue
            if not started and type_brace_depth:
                signature.append(char)
                if char == "{":
                    type_brace_depth += 1
                elif char == "}":
                    type_brace_depth -= 1
                index += 1
                continue

            if arrow_expression:
                if not char.isspace():
                    expression_last_line = source_line
                if char == "(":
                    paren_depth += 1
                elif char == ")":
                    paren_depth = max(0, paren_depth - 1)
                elif char == "[":
                    bracket_depth += 1
                elif char == "]":
                    bracket_depth = max(0, bracket_depth - 1)
                elif char == "{":
                    expression_brace_depth += 1
                elif char == "}":
                    expression_brace_depth = max(0, expression_brace_depth - 1)
                elif char == ";" and paren_depth == 0 and bracket_depth == 0 and expression_brace_depth == 0:
                    return source_line
                index += 1
                continue

            if (
                not started
                and arrow_declaration
                and pair == "=>"
                and paren_depth == 0
                and bracket_depth == 0
                and angle_depth == 0
            ):
                arrow_seen = True
                arrow_pending = True
                signature.extend(pair)
                index += 2
                continue
            if not started and char == "(":
                paren_depth += 1
                saw_parameters = True
            elif not started and char == ")":
                paren_depth = max(0, paren_depth - 1)
                if saw_parameters and paren_depth == 0:
                    parameters_closed = True
            elif not started and char == "[":
                bracket_depth += 1
            elif not started and char == "]":
                bracket_depth = max(0, bracket_depth - 1)
            elif (
                not started
                and char == "<"
                and paren_depth == 0
                and bracket_depth == 0
                and lang in {"java", "javascript", "typescript", "rust"}
            ):
                angle_depth += 1
            elif not started and char == ">" and angle_depth:
                angle_depth -= 1
            elif (
                not started
                and char == ":"
                and lang == "typescript"
                and parameters_closed
                and paren_depth == 0
                and bracket_depth == 0
                and angle_depth == 0
                and return_type_start < 0
            ):
                return_type_start = len(signature)
            elif char == "{":
                top_level = paren_depth == 0 and bracket_depth == 0 and angle_depth == 0
                type_literal = top_level and (
                    (lang == "go" and re.search(r"\b(?:struct|interface)\s*$", "".join(signature)))
                    or (
                        lang == "typescript"
                        and return_type_start >= 0
                        and _typescript_type_literal_brace("".join(signature[return_type_start:]))
                    )
                    # Before an arrow's top-level ``=>``, any brace belongs to
                    # a parameter/generic/return type rather than its body.
                    or (arrow_declaration and not arrow_seen)
                )
                if not started and type_literal:
                    type_brace_depth = 1
                elif started or top_level:
                    depth += 1
                    started = True
            elif char == "}" and started:
                depth -= 1
                if depth == 0:
                    return source_line
            signature.append(char)
            index += 1

        signature.append("\n")

        # A declaration without an opening brace should not scan arbitrarily
        # far into another symbol and manufacture a range.
        if not started and line_index + 1 - definition_line >= 12:
            return expression_last_line if arrow_expression else 0

    return expression_last_line if arrow_expression else 0


def _typescript_type_literal_brace(return_type_prefix: str) -> bool:
    """Whether a top-level brace continues a TypeScript return type."""

    prefix = return_type_prefix.strip()
    if not prefix.startswith(":"):
        return False
    type_prefix = prefix[1:].rstrip()
    if not type_prefix:
        return True
    return bool(
        re.search(r"(?:=>|[<|&?:,(=])\s*$", type_prefix)
        or re.search(r"\b(?:extends|infer|keyof|typeof)\s*$", type_prefix)
    )


def _find_ruby_symbol_end(lines: list[str], definition_line: int) -> int:
    """Find a Ruby declaration's matching ``end`` for common block syntax."""

    depth = 0
    opener = re.compile(r"^(?:def|class|module|if|unless|case|begin|while|until|for)\b|\bdo\s*(?:\|.*\|)?\s*$")
    for index in range(definition_line - 1, len(lines)):
        stripped = lines[index].strip()
        if not stripped or stripped.startswith("#"):
            continue
        if opener.search(stripped):
            depth += 1
        if re.match(r"^end\b", stripped):
            depth -= 1
            if depth == 0:
                return index + 1
    return 0


def _blank_span(characters: list[str], start: int, end: int) -> None:
    for index in range(start, min(end, len(characters))):
        if characters[index] not in {"\n", "\r"}:
            characters[index] = " "


def _mask_c_style_comments(content: str) -> str:
    """Blank // and /* */ comments while preserving quoted import paths."""

    characters = list(content)
    index = 0
    quote = ""
    escaped = False
    while index < len(content):
        if quote:
            if escaped:
                escaped = False
            elif content[index] == "\\" and quote != "`":
                escaped = True
            elif content[index] == quote:
                quote = ""
            index += 1
            continue
        if content[index] in {'"', "'", "`"}:
            quote = content[index]
            index += 1
            continue
        if content.startswith("//", index):
            end = content.find("\n", index + 2)
            end = len(content) if end < 0 else end
            _blank_span(characters, index, end)
            index = end
            continue
        if content.startswith("/*", index):
            end = content.find("*/", index + 2)
            end = len(content) if end < 0 else end + 2
            _blank_span(characters, index, end)
            index = end
            continue
        index += 1
    return "".join(characters)


def _javascript_regex_end(content: str, start: int) -> int:
    """Return the end of a likely JS regex literal, or the original offset."""

    line_start = content.rfind("\n", 0, start) + 1
    prefix = content[line_start:start].rstrip()
    if (
        prefix
        and prefix[-1] not in "=(:,[!&|?{};"
        and not prefix.endswith("=>")
        and not re.search(r"\b(?:return|case|throw|yield)\s*$", prefix)
    ):
        return start
    escaped = False
    in_class = False
    index = start + 1
    while index < len(content) and content[index] not in "\r\n":
        char = content[index]
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == "[":
            in_class = True
        elif char == "]":
            in_class = False
        elif char == "/" and not in_class:
            index += 1
            while index < len(content) and content[index].isalpha():
                index += 1
            return index
        index += 1
    return start


def mask_non_code(
    content: str,
    lang: str,
    *,
    mask_strings: bool = True,
    preserve_ruby_commands: bool = False,
) -> str:
    """Blank lexical non-code while preserving every line and column.

    ``mask_strings=False`` masks comments only while treating strings, regex
    literals and heredocs as opaque. This is useful for secret detectors that
    intentionally inspect literal contents without accepting commented prose.
    """

    characters = list(content)
    opaque_spans: list[tuple[int, int]] = []

    # Ruby heredocs are line-oriented and can contain arbitrary call-shaped
    # prose. Mask their bodies before the character scanner handles quotes.
    if lang == "ruby":
        for match in list(re.finditer(r"<<[-~]?\s*['\"]?(?P<tag>[A-Za-z_]\w*)['\"]?", content)):
            body_start = content.find("\n", match.end())
            if body_start < 0:
                continue
            terminator = re.search(
                rf"^\s*{re.escape(match.group('tag'))}\s*$",
                content[body_start + 1 :],
                re.MULTILINE,
            )
            if terminator is not None:
                end = body_start + 1 + terminator.end()
                opaque_spans.append((body_start + 1, end))
                if mask_strings:
                    _blank_span(characters, body_start + 1, end)

        for match in re.finditer(r"^=begin\b.*?^=end\b[^\n]*", content, re.MULTILINE | re.DOTALL):
            opaque_spans.append((match.start(), match.end()))
            _blank_span(characters, match.start(), match.end())

    if lang in {"javascript", "typescript"}:
        for match in re.finditer(r"<!--.*?-->", content, re.DOTALL):
            opaque_spans.append((match.start(), match.end()))
            _blank_span(characters, match.start(), match.end())

    opaque_by_start = {start: end for start, end in sorted(opaque_spans)}
    scan = content
    slash_comments = lang in {"javascript", "typescript", "go", "java", "rust"}
    hash_comments = lang in {"python", "ruby"}
    index = 0
    while index < len(scan):
        if index in opaque_by_start:
            index = opaque_by_start[index]
            continue
        if slash_comments and scan.startswith("//", index):
            end = scan.find("\n", index + 2)
            end = len(scan) if end < 0 else end
            _blank_span(characters, index, end)
            index = end
            continue
        if slash_comments and scan.startswith("/*", index):
            if lang == "rust":
                depth = 1
                end = index + 2
                while end < len(scan) and depth:
                    if scan.startswith("/*", end):
                        depth += 1
                        end += 2
                    elif scan.startswith("*/", end):
                        depth -= 1
                        end += 2
                    else:
                        end += 1
            else:
                end = scan.find("*/", index + 2)
                end = len(scan) if end < 0 else end + 2
            _blank_span(characters, index, end)
            index = end
            continue
        if hash_comments and scan[index] == "#":
            end = scan.find("\n", index + 1)
            end = len(scan) if end < 0 else end
            _blank_span(characters, index, end)
            index = end
            continue

        if lang == "rust":
            raw = re.match(r"(?:br|r)(?P<hashes>#{0,8})\"", scan[index:])
            if raw:
                terminator = '"' + raw.group("hashes")
                end = scan.find(terminator, index + raw.end())
                end = len(scan) if end < 0 else end + len(terminator)
                if mask_strings:
                    _blank_span(characters, index, end)
                index = end
                continue

        if lang == "ruby" and scan[index] == "%":
            percent = re.match(r"%(?:[qQwWxrsiI])?(?P<opening>[({\[<!/|])", scan[index:])
            if percent:
                opening = percent.group("opening")
                closing = {"(": ")", "[": "]", "{": "}", "<": ">"}.get(opening, opening)
                paired = opening != closing
                depth = 1
                cursor = index + percent.end()
                escaped = False
                while cursor < len(scan) and depth:
                    char = scan[cursor]
                    if escaped:
                        escaped = False
                    elif char == "\\":
                        escaped = True
                    elif paired and char == opening:
                        depth += 1
                    elif char == closing:
                        depth -= 1
                    cursor += 1
                if mask_strings and not (preserve_ruby_commands and percent.group(0).startswith("%x")):
                    _blank_span(characters, index, cursor)
                index = cursor
                continue

        if lang == "rust" and scan[index] == "'":
            lifetime = re.match(r"'[A-Za-z_]\w*", scan[index:])
            if lifetime and (index + lifetime.end() >= len(scan) or scan[index + lifetime.end()] != "'"):
                index += lifetime.end()
                continue

        delimiter = ""
        raw_string = False
        for candidate in ('"""', "'''", '"', "'", "`"):
            if scan.startswith(candidate, index):
                delimiter = candidate
                break
        if delimiter:
            cursor = index + len(delimiter)
            while cursor < len(scan):
                if scan.startswith(delimiter, cursor):
                    cursor += len(delimiter)
                    break
                if not raw_string and scan[cursor] == "\\":
                    cursor += 2
                else:
                    cursor += 1
            if mask_strings and not (lang == "ruby" and delimiter == "`" and preserve_ruby_commands):
                _blank_span(characters, index, cursor)
            index = cursor
            continue

        if lang in {"javascript", "typescript", "ruby"} and scan[index] == "/":
            end = _javascript_regex_end(scan, index)
            if end > index:
                if mask_strings:
                    _blank_span(characters, index, end)
                index = end
                continue
        index += 1
    return "".join(characters)


def mask_comments(content: str, lang: str) -> str:
    """Blank comments but preserve literal contents and coordinates."""

    return mask_non_code(content, lang, mask_strings=False)


def _mask_non_executable_declarations(content: str, lang: str) -> str:
    """Blank declaration-only blocks that contain call-shaped signatures."""

    patterns: list[re.Pattern[str]] = []
    if lang == "go":
        patterns.append(re.compile(r"\btype\s+[A-Za-z_]\w*(?:\s*\[[^]]*\])?\s+(?:interface|struct)\s*\{"))
    elif lang == "typescript":
        patterns.extend(
            [
                re.compile(r"\binterface\s+[A-Za-z_$][\w$]*(?:\s*<[^>{}]*>)?[^{}]*\{"),
                re.compile(r"\btype\s+[A-Za-z_$][\w$]*(?:\s*<[^>{}]*>)?\s*=\s*\{"),
                re.compile(r"\bdeclare\s+(?:class|namespace|module)\s+[A-Za-z_$][\w$]*[^{}]*\{"),
            ]
        )
    if not patterns:
        return content
    characters = list(content)
    for pattern in patterns:
        for match in pattern.finditer(content):
            opening = content.find("{", match.start(), match.end())
            depth = 0
            end = len(content)
            for index in range(opening, len(content)):
                if content[index] == "{":
                    depth += 1
                elif content[index] == "}":
                    depth -= 1
                    if depth == 0:
                        end = index + 1
                        break
            _blank_span(characters, match.start(), end)

    if lang in {"javascript", "typescript"}:
        masked = "".join(characters)
        body_method = re.compile(
            r"(?:^|[;{}])\s*"
            r"(?:(?:public|private|protected|static|readonly|override|abstract|async|get|set)\s+)*"
            r"(?!if\b|for\b|while\b|switch\b|catch\b|with\b)"
            r"[A-Za-z_$][\w$]*\s*\((?:[^()]|\([^()]*\))*\)\s*"
            r"(?:\??\s*:\s*[^;{=]+)?\{",
            re.MULTILINE,
        )
        abstract_method = re.compile(
            r"(?:^|[;{}])\s*(?:abstract|declare)\s+[A-Za-z_$][\w$]*\s*"
            r"\((?:[^()]|\([^()]*\))*\)\s*(?:\??\s*:\s*[^;{=]+)?;",
            re.MULTILINE,
        )
        for pattern in (body_method, abstract_method):
            for match in pattern.finditer(masked):
                end = match.end() - 1 if masked[match.end() - 1] == "{" else match.end()
                _blank_span(characters, match.start(), end)
    return "".join(characters)


def _call_shape_is_declaration(line: str, call_start: int, open_paren: int, lang: str) -> bool:
    """Reject method/type signatures that share the ``name(...)`` shape."""

    depth = 0
    close_paren = -1
    for index in range(open_paren, len(line)):
        if line[index] == "(":
            depth += 1
        elif line[index] == ")":
            depth -= 1
            if depth == 0:
                close_paren = index
                break
    if close_paren < 0:
        return False
    tail = line[close_paren + 1 :]
    if lang not in {"javascript", "typescript"}:
        return False
    prefix = line[:call_start]
    declaration_prefix = bool(
        re.search(
            r"(?:^|[;{}])\s*(?:(?:public|private|protected|static|readonly|override|abstract|async)\s+)*$",
            prefix,
        )
    )
    if declaration_prefix and re.match(r"^\s*\{", tail):
        return True
    if declaration_prefix and re.match(r"^\s*(?:\??\s*)?:\s*[^;{=]+\s*(?:;|\{)", tail):
        return True
    return False


def extract_calls(content: str, file_path: str) -> list[CallInfo]:
    """Extract function calls from file content."""
    lang = detect_language(file_path)
    if not CALL_PATTERNS.get(lang, []):
        return []

    calls: list[CallInfo] = []
    lexical_code = mask_non_code(content, lang)
    definitions = extract_definitions(lexical_code, file_path)
    code = _mask_non_executable_declarations(lexical_code, lang)
    lines = code.split("\n")

    # Find current function context
    current_func = _find_enclosing_function(lines, definitions)
    definitions_by_line: dict[int, set[str]] = {}
    for definition in definitions:
        definitions_by_line.setdefault(definition.line, set()).add(definition.name)
    receiver_types = _extract_receiver_types(code, lang)

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
                    column=match.start() + 1,
                    receiver=receiver,
                    receiver_type=receiver_types.get(receiver_name, ""),
                )
            )
            occupied.append(match.span())

        for match in direct_pattern.finditer(line):
            callee = match.group("callee")
            if callee in ignored or callee in definitions_by_line.get(i + 1, set()):
                continue
            if _call_shape_is_declaration(line, match.start(), line.find("(", match.start()), lang):
                continue
            if any(start <= match.start() < end for start, end in occupied):
                continue
            calls.append(
                CallInfo(
                    caller=caller,
                    callee=callee,
                    file_path=file_path,
                    line=i + 1,
                    column=match.start() + 1,
                )
            )

        if Path(file_path).suffix.lower() in {".jsx", ".tsx", ".vue", ".svelte"}:
            for match in jsx_pattern.finditer(line):
                callee = match.group("callee")
                calls.append(
                    CallInfo(
                        caller=caller,
                        callee=callee,
                        file_path=file_path,
                        line=i + 1,
                        column=match.start() + 1,
                    )
                )

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


def _find_enclosing_function(lines: list[str], definitions: list[SymbolInfo]) -> dict[int, str]:
    """Map physical lines to the narrowest proven function body.

    The former indentation-only implementation understood Python but silently
    labelled JavaScript, TypeScript, Go, Java, Rust and Ruby calls as module
    scope.  Definition ranges are already language-aware, so reuse those proven
    boundaries instead of maintaining a second parser here.
    """

    functions = [
        item
        for item in definitions
        if item.symbol_type == "function"
        and (item.start_line or item.line) > 0
        and item.end_line >= (item.start_line or item.line)
    ]
    result: dict[int, str] = {}
    for index in range(len(lines)):
        line_no = index + 1
        containing = [item for item in functions if (item.start_line or item.line) <= line_no <= item.end_line]
        if not containing:
            result[index] = "<module>"
            continue
        owner = min(
            containing,
            key=lambda item: item.end_line - (item.start_line or item.line),
        )
        result[index] = owner.name
    return result


def extract_diff_symbols(diff_content: str, file_path: str) -> tuple[list[SymbolInfo], list[ImportInfo]]:
    """Extract added definitions/imports with GitHub RIGHT-side line numbers."""

    added_lines = _mapped_added_lines(diff_content)
    if not added_lines:
        return [], []

    # Definitions retain every physical line so their source ranges stay
    # meaningful. Imports use a joined view while retaining the first source
    # line as their review-comment anchor.
    added_content = "\n".join(line for _line_no, line in added_lines)
    joined_lines = _join_multiline_import_lines(added_lines)

    symbols = extract_definitions(added_content, file_path)
    import_lines = added_lines if detect_language(file_path) == "go" else joined_lines
    imports = extract_imports("\n".join(line for _line_no, line in import_lines), file_path)
    symbols = [item for item in symbols if _remap_symbol_range(item, added_lines)]
    imports = [item for item in imports if _remap_item_line(item, import_lines)]
    return symbols, imports


def extract_diff_calls(diff_content: str, file_path: str) -> list[CallInfo]:
    """Extract calls added by a diff with GitHub RIGHT-side line numbers."""

    added_lines = _mapped_added_lines(diff_content)
    if not added_lines:
        return []

    added_content = "\n".join(line for _line_no, line in added_lines)
    calls = extract_calls(added_content, file_path)
    if detect_language(file_path) == "python" and _is_complete_new_file_diff(diff_content, added_lines):
        proven = _python_proven_import_calls(added_content)
        for call in calls:
            binding = call.receiver.split(".", 1)[0] if call.receiver else call.callee
            call.binding_proven = (call.line, binding) in proven
    return [item for item in calls if _remap_item_line(item, added_lines)]


def _is_complete_new_file_diff(diff_content: str, added_lines: list[tuple[int, str]]) -> bool:
    """Prove that a diff contains the advertised complete contents of one new file."""

    headers = list(re.finditer(r"^@@ .* @@", diff_content, re.MULTILINE))
    new_file = re.search(r"^@@ -0,0 \+1(?:,(?P<count>\d+))? @@", diff_content, re.MULTILINE)
    if new_file is None or len(headers) != 1 or headers[0].start() != new_file.start():
        return False
    expected = int(new_file.group("count") or 1)
    return len(added_lines) == expected and [line for line, _content in added_lines] == list(range(1, expected + 1))


def _python_proven_import_calls(content: str) -> set[tuple[int, str]]:
    """Prove calls through one unconditional, unshadowed module import.

    This deliberately supports a narrow Python subset. Imports nested under a
    branch/try/loop, function-local imports, rebinding, deletion, class-body
    lookup and comprehension shadowing all remain contextual for the LLM.
    """

    try:
        tree = ast.parse(content + "\n")
    except SyntaxError:
        return set()

    if any(
        isinstance(node, ast.ImportFrom) and any(alias.name == "*" for alias in node.names) for node in ast.walk(tree)
    ):
        return set()
    if any(
        isinstance(node, ast.Call) and _python_call_root(node.func) in {"eval", "exec", "globals", "locals", "vars"}
        for node in ast.walk(tree)
    ):
        # Star imports and dynamic namespace access can replace a lexical
        # binding without leaving an assignment node that static scope
        # analysis can prove. Keep every edge in such a file LLM-gated.
        return set()

    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent

    function_scopes: list[ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda] = [
        node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
    ]
    module_bindings = _python_scope_bindings(tree)
    binding_cache = {scope: _python_scope_bindings(scope) for scope in function_scopes}
    direct_imports = _python_direct_import_bindings(tree)
    proven: set[tuple[int, str]] = set()

    for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
        binding = _python_call_root(call.func)
        if not binding:
            continue
        ancestors: list[ast.AST] = []
        current: ast.AST | None = call
        while current in parents:
            current = parents[current]
            ancestors.append(current)
        nearest_function_index = next(
            (
                index
                for index, node in enumerate(ancestors)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
            ),
            -1,
        )
        nearest_class_index = next(
            (index for index, node in enumerate(ancestors) if isinstance(node, ast.ClassDef)),
            -1,
        )
        if nearest_class_index >= 0 and (nearest_function_index < 0 or nearest_class_index < nearest_function_index):
            # Class-body name lookup includes a transient namespace that this
            # compact scope model intentionally does not attempt to resolve.
            continue

        if any(
            isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp))
            and any(binding in _python_target_names(generator.target) for generator in node.generators)
            for node in ancestors
        ):
            continue

        candidate_scopes = [
            node for node in ancestors if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
        ]
        shadowed = False
        for scope in candidate_scopes:
            imports, other, globals_, nonlocals = binding_cache[scope]
            if binding in imports or binding in other or binding in globals_ or binding in nonlocals:
                shadowed = True
                break
        if shadowed:
            continue

        imports, other, _globals, _nonlocals = module_bindings
        if direct_imports.count(binding) == 1 and imports.count(binding) == 1 and binding not in other:
            proven.add((call.lineno, binding))
    return proven


def _python_direct_import_bindings(tree: ast.Module) -> list[str]:
    """Return bindings imported by unconditional top-level statements only."""

    bindings: list[str] = []
    for statement in tree.body:
        if isinstance(statement, ast.Import):
            bindings.extend(alias.asname or alias.name.split(".", 1)[0] for alias in statement.names)
        elif isinstance(statement, ast.ImportFrom):
            bindings.extend(alias.asname or alias.name for alias in statement.names if alias.name != "*")
    return bindings


def _python_target_names(target: ast.AST) -> set[str]:
    """Return lexical names introduced by an assignment-like AST target."""

    if isinstance(target, ast.Name):
        return {target.id}
    if isinstance(target, (ast.Tuple, ast.List)):
        return set().union(*(_python_target_names(element) for element in target.elts), set())
    if isinstance(target, ast.Starred):
        return _python_target_names(target.value)
    return set()


def _python_call_root(function: ast.expr) -> str:
    node = function
    while isinstance(node, ast.Attribute):
        node = node.value
    return node.id if isinstance(node, ast.Name) else ""


def _python_scope_bindings(
    scope: ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda,
) -> tuple[list[str], set[str], set[str], set[str]]:
    imports: list[str] = []
    other: set[str] = set()
    globals_: set[str] = set()
    nonlocals: set[str] = set()

    def add_target(target: ast.AST) -> None:
        if isinstance(target, ast.Name):
            other.add(target.id)
        elif isinstance(target, (ast.Tuple, ast.List)):
            for element in target.elts:
                add_target(element)

    class Visitor(ast.NodeVisitor):
        def visit_Name(self, node: ast.Name) -> None:
            if isinstance(node.ctx, (ast.Store, ast.Del)):
                other.add(node.id)

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            if node is scope:
                for argument in [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]:
                    other.add(argument.arg)
                if node.args.vararg:
                    other.add(node.args.vararg.arg)
                if node.args.kwarg:
                    other.add(node.args.kwarg.arg)
                for statement in node.body:
                    self.visit(statement)
            else:
                other.add(node.name)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self.visit_FunctionDef(node)

        def visit_Lambda(self, node: ast.Lambda) -> None:
            if node is scope:
                for argument in [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]:
                    other.add(argument.arg)
                if node.args.vararg:
                    other.add(node.args.vararg.arg)
                if node.args.kwarg:
                    other.add(node.args.kwarg.arg)
                self.visit(node.body)

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            other.add(node.name)

        def visit_Import(self, node: ast.Import) -> None:
            imports.extend(alias.asname or alias.name.split(".", 1)[0] for alias in node.names)

        def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
            imports.extend(alias.asname or alias.name for alias in node.names if alias.name != "*")

        def visit_Assign(self, node: ast.Assign) -> None:
            for target in node.targets:
                add_target(target)
            self.visit(node.value)

        def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
            add_target(node.target)
            if node.value:
                self.visit(node.value)

        def visit_AugAssign(self, node: ast.AugAssign) -> None:
            add_target(node.target)
            self.visit(node.value)

        def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
            add_target(node.target)
            self.visit(node.value)

        def visit_For(self, node: ast.For) -> None:
            add_target(node.target)
            self.generic_visit(node)

        def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
            self.visit_For(node)

        def visit_With(self, node: ast.With) -> None:
            for item in node.items:
                if item.optional_vars:
                    add_target(item.optional_vars)
            self.generic_visit(node)

        def visit_AsyncWith(self, node: ast.AsyncWith) -> None:
            self.visit_With(node)

        def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
            if node.name:
                other.add(node.name)
            self.generic_visit(node)

        def visit_Global(self, node: ast.Global) -> None:
            globals_.update(node.names)

        def visit_Nonlocal(self, node: ast.Nonlocal) -> None:
            nonlocals.update(node.names)

        def visit_MatchAs(self, node: ast.MatchAs) -> None:
            if node.name:
                other.add(node.name)
            self.generic_visit(node)

        def visit_MatchStar(self, node: ast.MatchStar) -> None:
            if node.name:
                other.add(node.name)

        def visit_MatchMapping(self, node: ast.MatchMapping) -> None:
            if node.rest:
                other.add(node.rest)
            self.generic_visit(node)

    visitor = Visitor()
    if isinstance(scope, ast.Module):
        for statement in scope.body:
            visitor.visit(statement)
    else:
        visitor.visit(scope)
    other.difference_update(globals_)
    return imports, other, globals_, nonlocals


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


def _remap_symbol_range(item: SymbolInfo, mapped_lines: list[tuple[int, str]]) -> bool:
    """Remap a relative range only when every coordinate remains contiguous."""

    relative_line = item.line
    if relative_line <= 0 or relative_line > len(mapped_lines):
        return False

    item.line = mapped_lines[relative_line - 1][0]
    relative_start = item.start_line or relative_line
    relative_end = item.end_line
    if relative_start <= 0 or relative_start > len(mapped_lines) or relative_end < relative_start:
        item.start_line = item.line
        item.end_line = 0
        return True
    if relative_end > len(mapped_lines):
        item.start_line = item.line
        item.end_line = 0
        return True

    mapped_range = [line for line, _content in mapped_lines[relative_start - 1 : relative_end]]
    if any(right != left + 1 for left, right in zip(mapped_range, mapped_range[1:], strict=False)):
        item.start_line = item.line
        item.end_line = 0
        return True

    item.start_line = mapped_range[0]
    item.end_line = mapped_range[-1]
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
