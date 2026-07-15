"""Conservative deterministic checks for concrete quality defects.

The rules in this module require an observable syntax shape on a changed line.
They intentionally avoid broad resource, naming, or design heuristics that need
repository context and therefore belong in an LLM reviewer.
"""

from __future__ import annotations

import ast
import re
from pathlib import PurePosixPath

from reviewforge.engine.detectors.base import DetectorFinding, dedupe_findings
from reviewforge.engine.detectors.unified_diff import iter_added_lines, iter_right_lines
from reviewforge.engine.symbol_extractor import mask_comments, mask_non_code

_LOW_SIGNAL_PATH_PARTS = {
    "__tests__",
    "acceptance-tests",
    "contract-tests",
    "e2e-tests",
    "example",
    "examples",
    "fixture",
    "fixtures",
    "integration-test",
    "integration-tests",
    "spec",
    "specs",
    "test",
    "test-data",
    "test_data",
    "testdata",
    "testfixtures",
    "tests",
    "unit-tests",
}
_NEW_FILE_HUNK = re.compile(r"^@@ -0,0 \+1(?:,(?P<count>\d+))? @@", re.MULTILINE)
_QUOTED_TEXT = re.compile(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|`(?:\\.|[^`\\])*`')
_EMPTY_CATCH = re.compile(r"\bcatch\s*(?:\([^)]*\))?\s*\{")
_GO_CALL_TAIL = r"\s*;?\s*(?://.*)?$"
_LITERAL_REGEX_UNWRAP = re.compile(
    r"\b(?:regex::)?Regex::new\s*\(\s*"
    r"(?:\"(?:\\.|[^\"\\])*\"|r(?P<hashes>#{0,8})\".*?\"(?P=hashes))"
    r"\s*\)\s*\.unwrap\s*\(\s*\)"
)


def _is_low_signal_path(file_path: str) -> bool:
    """Skip non-production examples, fixtures, specs, and tests."""

    normalized = file_path.replace("\\", "/").lower()
    path = PurePosixPath(normalized)
    name = path.name
    return (
        any(
            part in _LOW_SIGNAL_PATH_PARTS
            or part.strip("_-") in {"fixture", "fixtures", "testing"}
            or re.fullmatch(
                r"(?:unit|integration|e2e|acceptance|contract)[_-]?tests?",
                part.strip("_-"),
            )
            for part in path.parts[:-1]
        )
        or name.startswith(("test_", "spec_"))
        or bool(re.search(r"(?:^|[._-])(?:test|spec)(?:[._-]|$)", name))
        or name.endswith(("_test.go", "test.java"))
    )


def _is_complete_new_file_patch(patch: str) -> bool:
    """Return true only when one hunk contains the advertised whole new file."""

    headers = list(re.finditer(r"^@@ .* @@", patch, re.MULTILINE))
    match = _NEW_FILE_HUNK.search(patch)
    if match is None or len(headers) != 1 or headers[0].start() != match.start():
        return False
    expected = int(match.group("count") or 1)
    additions = iter_added_lines(patch)
    return len(additions) == expected and [line for line, _content in additions] == list(range(1, expected + 1))


def _masked_rows(
    rows: list[tuple[int, str]],
    language: str,
    *,
    comments_only: bool = False,
) -> list[tuple[int, str]]:
    """Mask each contiguous source group without inventing cross-hunk state."""

    masked_rows: list[tuple[int, str]] = []
    group: list[tuple[int, str]] = []

    def flush() -> None:
        if not group:
            return
        source = "\n".join(content for _line, content in group)
        masked = mask_comments(source, language) if comments_only else mask_non_code(source, language)
        masked_lines = masked.split("\n")
        masked_rows.extend((row[0], masked_lines[index]) for index, row in enumerate(group))
        group.clear()

    for row in rows:
        if group and row[0] != group[-1][0] + 1:
            flush()
        group.append(row)
    flush()
    return masked_rows


def _structural_text(line: str) -> str:
    """Remove strings and line comments before counting braces."""

    return _QUOTED_TEXT.sub('""', line).split("//", 1)[0]


def _brace_block(rows: list[tuple[int, str]], start: int) -> list[tuple[int, str]]:
    """Return a complete contiguous brace block beginning at ``start``."""

    depth = 0
    saw_open = False
    block: list[tuple[int, str]] = []
    previous_line = 0
    for line_no, content in rows[start:]:
        if previous_line and line_no != previous_line + 1:
            return []
        previous_line = line_no
        block.append((line_no, content))
        structural = _structural_text(content)
        opens = structural.count("{")
        closes = structural.count("}")
        saw_open = saw_open or opens > 0
        depth += opens - closes
        if saw_open and depth <= 0:
            return block
    return []


def _go_function_literal_contains_line(block: list[tuple[int, str]], target_line: int) -> bool:
    """Prove that ``target_line`` is inside an anonymous Go function body."""

    target_indexes = [index for index, (line_no, _text) in enumerate(block) if line_no == target_line]
    if not target_indexes:
        return False
    target_index = target_indexes[0]
    structural_rows = [_structural_text(text) for _line_no, text in block]
    source = "\n".join(structural_rows)
    row_offsets: list[int] = []
    offset = 0
    for text in structural_rows:
        row_offsets.append(offset)
        offset += len(text) + 1
    defer_match = re.search(r"\bdefer\b", structural_rows[target_index])
    if defer_match is None:
        return False
    target_offset = row_offsets[target_index] + defer_match.start()

    for body_start in _go_function_body_offsets(source):
        if body_start >= target_offset:
            continue
        depth = 0
        for character in source[body_start:target_offset]:
            if character == "{":
                depth += 1
            elif character == "}":
                depth -= 1
                if depth <= 0:
                    break
        else:
            if depth > 0:
                return True
    return False


def _go_function_body_offsets(source: str) -> list[int]:
    """Locate anonymous Go function bodies, skipping struct/interface result types."""

    source = re.sub(
        r"/\*.*?\*/",
        lambda match: "".join("\n" if char == "\n" else " " for char in match.group(0)),
        source,
        flags=re.DOTALL,
    )
    offsets: list[int] = []
    for match in re.finditer(r"\bfunc\s*\(", source):
        params_open = source.find("(", match.start())
        params_close = _matching_delimiter(source, params_open, "(", ")")
        if params_close < 0:
            continue
        cursor = params_close + 1
        while cursor < len(source):
            brace = source.find("{", cursor)
            if brace < 0:
                break
            result_prefix = source[cursor:brace].rstrip()
            if re.search(r"\b(?:struct|interface)\s*$", result_prefix):
                type_end = _matching_delimiter(source, brace, "{", "}")
                if type_end < 0:
                    break
                cursor = type_end + 1
                continue
            offsets.append(brace)
            break
    return offsets


def _matching_delimiter(source: str, start: int, opening: str, closing: str) -> int:
    """Return the matching delimiter in already string/comment-masked source."""

    if start < 0 or start >= len(source) or source[start] != opening:
        return -1
    depth = 0
    for index in range(start, len(source)):
        if source[index] == opening:
            depth += 1
        elif source[index] == closing:
            depth -= 1
            if depth == 0:
                return index
    return -1


def _empty_catch_lines(rows: list[tuple[int, str]], added: set[int]) -> list[int]:
    """Find catch clauses whose newly added body contains no statement."""

    findings: list[int] = []
    for index, (line_no, content) in enumerate(rows):
        if line_no not in added:
            continue
        match = _EMPTY_CATCH.search(_structural_text(content))
        if not match:
            continue
        if re.match(r"\s*}", _structural_text(content)[match.end() :]):
            findings.append(line_no)
            continue

        previous = line_no
        for next_line, next_content in rows[index + 1 :]:
            if next_line != previous + 1:
                break
            previous = next_line
            stripped = _structural_text(next_content).strip()
            if not stripped:
                continue
            if re.match(r"^}\s*(?:finally\b|$)", stripped):
                findings.append(line_no)
            break
    return findings


def _finding(
    file_path: str,
    line: int,
    category: str,
    message: str,
    suggestion: str,
    *,
    severity: str = "warning",
    confidence: float = 0.99,
) -> DetectorFinding:
    return DetectorFinding(
        file=file_path,
        line=line,
        severity=severity,
        category=category,
        message=message,
        suggestion=suggestion,
        confidence=confidence,
    )


def _java_findings(file_path: str, rows: list[tuple[int, str]], added: set[int]) -> list[DetectorFinding]:
    findings = [
        _finding(
            file_path,
            line,
            "exception-handling",
            "The added catch block silently swallows every exception.",
            "Handle the failure, propagate it, or record enough context for recovery.",
        )
        for line in _empty_catch_lines(rows, added)
    ]

    source = "\n".join(content for _line, content in rows)
    optional_names = set(re.findall(r"\bOptional\s*<[^;\n]+?>\s+([A-Za-z_]\w*)", source))
    for name in optional_names:
        if re.search(rf"\b{re.escape(name)}\s*\.\s*(?:isPresent|isEmpty)\s*\(", source):
            # Dominance is not provable from a partial hunk. Conservatively let
            # contextual review handle any Optional guarded somewhere nearby.
            continue
        for line_no, content in rows:
            if line_no in added and re.search(rf"\b{re.escape(name)}\s*\.\s*get\s*\(\s*\)", content):
                findings.append(
                    _finding(
                        file_path,
                        line_no,
                        "null-safety",
                        f"Optional `{name}` is dereferenced with get() without a visible presence guard.",
                        "Use orElse/orElseThrow, or prove presence before dereferencing the Optional.",
                        confidence=0.9,
                    )
                )
    return findings


def _vue_findings(file_path: str, rows: list[tuple[int, str]], added: set[int]) -> list[DetectorFinding]:
    findings = [
        _finding(
            file_path,
            line,
            "exception-handling",
            "The Vue component adds an empty catch block that hides a failed operation.",
            "Handle the error or surface it to the component's error state.",
        )
        for line in _empty_catch_lines(rows, added)
    ]

    for index, (line_no, content) in enumerate(rows):
        if line_no not in added or not re.search(r"\bcomputed\s*\(", content) or "{" not in content:
            continue
        block = _brace_block(rows, index)
        for effect_line, effect in block:
            if effect_line in added and re.search(r"\b[A-Za-z_]*fetch\w*\s*\(", effect, re.IGNORECASE):
                findings.append(
                    _finding(
                        file_path,
                        effect_line,
                        "computed-side-effect",
                        "A computed getter starts a fetch operation while it is being evaluated.",
                        "Move the fetch into an explicit lifecycle hook, watcher, or action.",
                        # A lexical name such as `fetch` can be a local pure
                        # helper. Without scope/module resolution the side
                        # effect is useful review evidence, not auto-proof.
                        confidence=0.9,
                    )
                )

    source = "\n".join(content for _line, content in rows)
    prop_bindings = set(re.findall(r"\b(?:const|let)\s+([A-Za-z_]\w*)\s*=\s*defineProps\b", source))
    for line_no, content in rows:
        if line_no not in added:
            continue
        for binding in prop_bindings:
            if re.search(
                rf"(?<![\w.]){re.escape(binding)}(?:\.[A-Za-z_]\w*|\[[^]]+\])+\s*=(?!=|>)",
                _structural_text(content),
            ):
                findings.append(
                    _finding(
                        file_path,
                        line_no,
                        "state-management",
                        "The component assigns directly to a readonly prop.",
                        "Emit an update or copy the value into component-owned reactive state.",
                    )
                )

        if re.search(r"<[A-Za-z][^>]*\bv-if\s*=", content) and re.search(r"\bv-for\s*=", content):
            findings.append(
                _finding(
                    file_path,
                    line_no,
                    "correctness",
                    "The same Vue element combines v-if and v-for, giving the directives "
                    "ambiguous scope and precedence.",
                    "Filter in a computed value or wrap the loop/condition in separate elements.",
                )
            )
    return findings


def _python_findings(file_path: str, rows: list[tuple[int, str]], added: set[int]) -> list[DetectorFinding]:
    if not rows or rows[0][0] != 1 or any(right != left + 1 for (left, _), (right, _) in zip(rows, rows[1:])):
        return []
    try:
        tree = ast.parse("\n".join(content for _line_no, content in rows) + "\n")
    except SyntaxError:
        return []
    return [
        _finding(
            file_path,
            node.lineno,
            "exception-handling",
            "A bare except clause also catches process-control exceptions such as KeyboardInterrupt and SystemExit.",
            "Catch the narrow exception types the operation can recover from.",
        )
        for node in ast.walk(tree)
        if isinstance(node, ast.ExceptHandler) and node.type is None and node.lineno in added
    ]


def _ruby_findings(
    file_path: str,
    patch: str,
    rows: list[tuple[int, str]],
    added: set[int],
) -> list[DetectorFinding]:
    findings: list[DetectorFinding] = []
    for line_no, content in rows:
        if line_no in added and re.match(r"^\s*rescue\s+Exception\b", content):
            findings.append(
                _finding(
                    file_path,
                    line_no,
                    "exception-handling",
                    "rescue Exception catches interrupts and other non-application failures.",
                    "Rescue StandardError or the specific recoverable exception classes.",
                )
            )

    source = "\n".join(content for _line, content in rows)
    if _is_complete_new_file_patch(patch) and not re.search(r"^\s*def\s+respond_to_missing\?\b", source, re.MULTILINE):
        for index, (line_no, content) in enumerate(rows):
            if line_no not in added or not re.match(r"^\s*def\s+method_missing\b", content):
                continue
            definition_indent = len(content) - len(content.lstrip())
            method_body: list[str] = []
            method_complete = False
            for _body_line, body_text in rows[index + 1 :]:
                stripped = body_text.strip()
                indent = len(body_text) - len(body_text.lstrip())
                if stripped == "end" and indent == definition_indent:
                    method_complete = True
                    break
                method_body.append(body_text)
            meaningful = [
                text.strip()
                for text in method_body
                if text.strip() and not text.strip().startswith("#") and text.strip() not in {"end", "super"}
            ]
            if method_complete and meaningful:
                findings.append(
                    _finding(
                        file_path,
                        line_no,
                        "api-contract",
                        "A custom method_missing implementation is added without the matching "
                        "respond_to_missing? contract.",
                        "Implement respond_to_missing? with the same predicate so reflection remains correct.",
                        confidence=0.98,
                    )
                )
            break
    return findings


def _go_loop_blocks(rows: list[tuple[int, str]]) -> list[list[tuple[int, str]]]:
    blocks: list[list[tuple[int, str]]] = []
    for index, (_line_no, content) in enumerate(rows):
        if re.match(r"^\s*for(?:\s|\{)", content) and "{" in content:
            block = _brace_block(rows, index)
            if block:
                blocks.append(block)
    return blocks


def _go_findings(file_path: str, rows: list[tuple[int, str]], added: set[int]) -> list[DetectorFinding]:
    findings: list[DetectorFinding] = []
    source = "\n".join(content for _line, content in rows)
    command_vars = set(re.findall(r"\b([A-Za-z_]\w*)\s*:?=\s*exec\.Command(?:Context)?\s*\(", source))
    loop_blocks = _go_loop_blocks(rows)

    for line_no, content in rows:
        if line_no not in added:
            continue
        for command_var in command_vars:
            if re.match(rf"^\s*{re.escape(command_var)}\.Run\s*\(\s*\){_GO_CALL_TAIL}", content):
                findings.append(
                    _finding(
                        file_path,
                        line_no,
                        "ignored-error",
                        "The error returned by exec.Cmd.Run is discarded.",
                        "Check and propagate or handle the command execution error.",
                    )
                )

        if re.match(
            rf"^\s*(?:_\s*=\s*)?[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*"
            rf"\.Ping(?:Context)?\(.*\){_GO_CALL_TAIL}",
            content,
        ):
            findings.append(
                _finding(
                    file_path,
                    line_no,
                    "ignored-error",
                    "The database Ping error is explicitly discarded.",
                    "Return or handle the connectivity error before continuing.",
                )
            )

        standalone_query = re.match(
            rf"^\s*[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*\.Query(?:Context)?\(.*\){_GO_CALL_TAIL}",
            content,
        )
        ignored_query = re.match(
            r"^\s*(?P<result>[A-Za-z_]\w*)\s*,\s*_\s*:?=\s*.+\.Query(?:Context)?\(",
            content,
        )
        suppress_duplicate = False
        if ignored_query:
            result_name = ignored_query.group("result")
            suppress_duplicate = any(
                block[0][0] <= line_no <= block[-1][0]
                and any(re.search(rf"\bdefer\s+{re.escape(result_name)}\.Close\s*\(", text) for _n, text in block)
                for block in loop_blocks
            )
        if standalone_query or (ignored_query and not suppress_duplicate):
            findings.append(
                _finding(
                    file_path,
                    line_no,
                    "ignored-error",
                    "The database Query error is discarded.",
                    "Check the returned error before consuming query results.",
                )
            )

        if re.search(r"\bdefer\s+[A-Za-z_]\w*\.Close\s*\(\s*\)", content):
            containing_loops = [block for block in loop_blocks if block[0][0] <= line_no <= block[-1][0]]
            safe_inner_function = any(_go_function_literal_contains_line(block, line_no) for block in containing_loops)
            if containing_loops and not safe_inner_function:
                findings.append(
                    _finding(
                        file_path,
                        line_no,
                        "resource-leak",
                        "defer is registered inside a loop, so resource cleanup waits until the "
                        "outer function returns.",
                        "Close each resource at the end of the iteration or move one iteration into a helper function.",
                    )
                )

    for index, (line_no, content) in enumerate(rows):
        if line_no not in added or not re.search(r"\bgo\s+func\s*\([^)]*\)\s*\{", content):
            continue
        block = _brace_block(rows, index)
        if not block:
            continue
        loop_lines = [
            candidate_line
            for candidate_line, candidate in block
            if candidate_line in added and re.match(r"^\s*for\s*\{", candidate)
        ]
        block_text = "\n".join(text for _line, text in block)
        has_unbounded_work = bool(re.search(r"\.(?:Query|Exec|Ping|Do)\s*\(", block_text))
        has_stop_contract = bool(
            re.search(r"\bselect\s*\{|\.Done\s*\(\s*\)|<-\s*(?:done|stop|quit)\b|\b(?:break|return)\b", block_text)
        )
        if loop_lines and has_unbounded_work and not has_stop_contract:
            findings.append(
                _finding(
                    file_path,
                    loop_lines[0],
                    "lifecycle",
                    "A goroutine performs work in an unbounded loop without a visible cancellation or stop path.",
                    "Accept a context/stop signal and terminate the goroutine when its owner shuts down.",
                    confidence=0.98,
                )
            )
    return findings


def _rust_findings(
    file_path: str,
    rows: list[tuple[int, str]],
    added: set[int],
    original_rows: list[tuple[int, str]],
) -> list[DetectorFinding]:
    findings: list[DetectorFinding] = []
    original_by_line = dict(original_rows)
    for line_no, content in rows:
        if line_no not in added:
            continue
        original = original_by_line.get(line_no, content)
        if re.search(r"\.unwrap\s*\(\s*\)", content) and not re.search(
            r"\b(?:Some|Ok)\s*\([^;]*\)\.unwrap\s*\(\s*\)", original
        ):
            literal_match = _LITERAL_REGEX_UNWRAP.search(original)
            literal_regex = bool(
                literal_match and literal_match.start() < len(content) and not content[literal_match.start()].isspace()
            )
            dynamic_parse = bool(
                re.search(r"\.parse(?:::\s*<[^>]+>)?\s*\([^)]*\)\s*\.unwrap\s*\(", original)
                and not re.search(
                    r"(?:\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|r#*\"[^\"]*\"#*|\b\d+)"
                    r"\s*\.parse(?:::\s*<[^>]+>)?\s*\(",
                    original,
                )
            )
            findings.append(
                _finding(
                    file_path,
                    line_no,
                    "panic-risk",
                    (
                        "Regex::new(...).unwrap() will panic if the static pattern is invalid."
                        if literal_regex
                        else "unwrap() can panic on an error or missing value in production code."
                    ),
                    (
                        "Validate the literal with the Rust regex parser or return the construction error."
                        if literal_regex
                        else "Propagate the error with `?` or handle the Result/Option explicitly."
                    ),
                    # Generic unwraps can be protected by invariants outside a
                    # patch. Only a visible parse of runtime data is strong
                    # enough for deterministic confirmation.
                    confidence=0.98 if dynamic_parse and not literal_regex else 0.9,
                )
            )
        if re.search(r"\bpanic!\s*\(", content):
            findings.append(
                _finding(
                    file_path,
                    line_no,
                    "panic-risk",
                    "panic! turns this recoverable runtime failure into process or request termination.",
                    "Return a typed error and let the caller decide how to recover.",
                    confidence=0.9,
                )
            )
    return findings


def _browser_import_findings(file_path: str, rows: list[tuple[int, str]], added: set[int]) -> list[DetectorFinding]:
    source = "\n".join(content for _line, content in rows)
    if re.search(r"^\s*['\"]use server['\"]\s*;?", source, re.MULTILINE):
        return []
    if not re.search(r"\b(?:window|document|localStorage|sessionStorage)\b", source):
        return []
    return [
        _finding(
            file_path,
            line_no,
            "import-error",
            "A browser-facing TSX/JSX module imports Node's child_process API.",
            "Move process execution behind a server API and keep the browser bundle free of Node built-ins.",
            severity="error",
            confidence=0.9,
        )
        for line_no, content in rows
        if line_no in added
        and re.search(
            r"(?:\bfrom\s*['\"](?:node:)?child_process['\"]|\brequire\s*\(\s*['\"](?:node:)?child_process['\"]\s*\))",
            content,
        )
    ]


def detect_quality_findings(diffs: dict[str, str]) -> list[DetectorFinding]:
    """Detect high-signal quality defects on trustworthy RIGHT-side lines."""

    findings: list[DetectorFinding] = []
    for file_path, patch in diffs.items():
        if _is_low_signal_path(file_path):
            continue
        suffix = PurePosixPath(file_path.replace("\\", "/")).suffix.lower()
        if suffix not in {".java", ".vue", ".py", ".rb", ".go", ".rs", ".tsx", ".jsx"}:
            continue
        added_rows = iter_added_lines(patch)
        if not added_rows:
            continue
        rows = iter_right_lines(patch)
        added = {line_no for line_no, _content in added_rows}
        language = {
            ".java": "java",
            ".vue": "typescript",
            ".py": "python",
            ".rb": "ruby",
            ".go": "go",
            ".rs": "rust",
            ".tsx": "typescript",
            ".jsx": "javascript",
        }[suffix]
        code_rows = _masked_rows(rows, language)

        if suffix == ".java":
            findings.extend(_java_findings(file_path, code_rows, added))
        elif suffix == ".vue":
            findings.extend(_vue_findings(file_path, code_rows, added))
        elif suffix == ".py":
            findings.extend(_python_findings(file_path, rows, added))
        elif suffix == ".rb":
            findings.extend(_ruby_findings(file_path, patch, code_rows, added))
        elif suffix == ".go":
            findings.extend(_go_findings(file_path, code_rows, added))
        elif suffix == ".rs":
            findings.extend(_rust_findings(file_path, code_rows, added, rows))
        else:
            findings.extend(
                _browser_import_findings(
                    file_path,
                    _masked_rows(rows, language, comments_only=True),
                    added,
                )
            )
    return dedupe_findings(findings)


def is_deterministic_quality_finding(file_path: str, line: int, category: str, diff: str) -> bool:
    """Prove that the exact finding is reproduced by a local quality rule."""

    if not _is_complete_new_file_patch(diff):
        return False
    return any(
        finding.line == line and finding.category == category for finding in detect_quality_findings({file_path: diff})
    )
