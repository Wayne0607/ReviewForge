"""Deterministic high-signal security detectors for core languages."""

from __future__ import annotations

import ast
import io
import re
import token
import tokenize
from dataclasses import dataclass

from reviewforge.engine.detectors.base import (
    DetectorFinding,
    dedupe_findings,
    match_lines,
    normalize_category_for_detector,
    safe_confidence,
)
from reviewforge.engine.detectors.unified_diff import iter_added_lines
from reviewforge.engine.symbol_extractor import detect_language


@dataclass(frozen=True)
class _Rule:
    pattern: str
    category: str
    severity: str
    message: str
    suggestion: str
    confidence: float
    allow_single_line_string_literal: bool = False


@dataclass(frozen=True)
class _IgnoredSpan:
    start: int
    end: int
    token_type: int
    multiline: bool


# Conservative first-pass rules used for all files.
_UNIVERSAL_RULES: list[_Rule] = [
    _Rule(
        r"\b\w*(?:password|secret|api[_-]?key|token)\w*\s*(?::=|=)\s*[\"'][^\"']{6,}[\"']",
        "hardcoded-secrets",
        "error",
        "Hard-coded credentials detected in added lines.",
        "Move secrets to the platform secret store or environment variables.",
        0.92,
    ),
    _Rule(
        r"\b\w*(?:password|secret|api[_-]?key|token)\w*[\w\s:]*:\s*[\"'][^\"']{6,}[\"']",
        "hardcoded-secrets",
        "error",
        "Hard-coded credentials detected in added lines.",
        "Move secrets to the platform secret store or environment variables.",
        0.88,
    ),
    _Rule(
        r"[\"'](?:ghp_[A-Za-z0-9_]{12,}|sk_(?:live|proj)[A-Za-z0-9_\-]{8,}|Bearer\s+[A-Za-z0-9._\-]{12,})[\"']",
        "hardcoded-secrets",
        "error",
        "Hard-coded token-like secret detected in added lines.",
        "Move secrets to the platform secret store or environment variables.",
        0.97,
        True,
    ),
]


_SECURITY_RULES: dict[str, list[_Rule]] = {
    "python": [
        _Rule(
            r"\bos\.(?:system|popen)\s*\(\s*(?:f[\"']|[^\"'\s][^,)]*|[\"'][^\"']*[\"']\s*(?:\+|%|\.format\s*\())",
            "command-injection",
            "error",
            "Dynamic data is passed to a shell command API.",
            "Avoid shell execution or pass validated arguments to a non-shell API.",
            0.96,
        ),
        _Rule(
            r"\bsubprocess\.(?:call|run|Popen|check_output|check_call)\s*\(\s*(?:f[\"']|[^\"'\s][^,)]*|[\"'][^\"']*[\"']\s*(?:\+|%|\.format\s*\())[^\n]*\bshell\s*=\s*True",
            "command-injection",
            "error",
            "Dynamic command data is executed with shell=True.",
            "Use an argument list with shell=False and validate the executable name.",
            0.97,
        ),
        _Rule(
            r"\b(?:eval|exec)\s*\(\s*(?![rub]*[\"'][^{}\"']*[\"']\s*(?:,|\)))",
            "code-injection",
            "error",
            "Dynamic code execution detected.",
            "Replace with explicit dispatch logic.",
            0.97,
        ),
        _Rule(
            r"\b(?:pickle\.loads?|dill\.loads?|marshal\.loads?)\s*\(",
            "insecure-deserialization",
            "error",
            "Unsafe deserialization API used.",
            "Prefer safe formats with schema validation.",
            0.96,
        ),
        _Rule(
            r"\byaml\.load\s*\((?!\s*Loader=)",
            "insecure-deserialization",
            "warning",
            "YAML load without explicit safe loader.",
            "Use safe_load and validate the loaded schema.",
            0.89,
        ),
        _Rule(
            r"\bopen\([^)]*(?:\.\.|request|user|input|query|params?)",
            "path-traversal",
            "warning",
            "Potential path join from user-driven fragments.",
            "Validate and normalize user paths before file access.",
            0.84,
        ),
        _Rule(
            r"\bopen\s*\(\s*[^,\n]*\+\s*[\"'][/\\\\][\"']\s*\+\s*(?:[A-Za-z_]\w*\.)*(?:file_?name|path|user_?path|input_?path)\b",
            "path-traversal",
            "error",
            "A dynamic filename is concatenated into a filesystem path.",
            "Resolve the candidate path and enforce that it remains under the intended root.",
            0.96,
        ),
        _Rule(
            r"\burllib\.request\.urlopen\s*\(\s*(?:f[\"'][^\"']*\{[^}]+\}[^\"']*[\"']|(?![rub]*[\"'][^\"']*[\"']\s*(?:,|\)))[^)\n]+)",
            "ssrf",
            "error",
            "A dynamic URL is passed to urllib.request.urlopen.",
            "Allow-list destinations and block private, loopback, and link-local address ranges.",
            0.96,
        ),
        _Rule(
            r"\bcursor\.(?:execute|executemany)\s*\([^\n]*\+",
            "sql-injection",
            "error",
            "String concatenation in SQL execution call.",
            "Use parameterized query APIs.",
            0.95,
        ),
        _Rule(
            r"\b\w+\s*=\s*f[\"'][^\n]*(?:SELECT|INSERT|UPDATE|DELETE)[^\n]*\{",
            "sql-injection",
            "error",
            "SQL string is built with f-string interpolation.",
            "Use parameterized query APIs.",
            0.94,
        ),
        _Rule(
            r"\bconn\.execute\s*\(\s*(?:f[\"']|[\"'][^\n]*(?:SELECT|INSERT|UPDATE|DELETE)[^\n]*\+)",
            "sql-injection",
            "error",
            "SQL is built from an interpolated or concatenated string.",
            "Use parameterized query APIs.",
            0.94,
        ),
        _Rule(
            r"\bhashlib\.(?:md5|sha1)\b",
            "crypto",
            "warning",
            "Weak hash algorithm found.",
            "Use SHA-256+ and avoid using raw hash for password security.",
            0.82,
        ),
        _Rule(
            r"\brequests\.(?:get|post|put|delete)\s*\([^,)]*verify\s*=\s*False",
            "insecure-download",
            "warning",
            "HTTP request disables TLS verification.",
            "Keep certificate validation enabled.",
            0.91,
        ),
    ],
    "javascript": [
        _Rule(
            r"\beval\s*\(",
            "code-injection",
            "error",
            "Direct eval usage found.",
            "Avoid eval and use strict parsing.",
            0.98,
        ),
        _Rule(
            r"\bFunction\s*\(",
            "code-injection",
            "warning",
            "Dynamic Function constructor used.",
            "Avoid `Function` constructor.",
            0.88,
        ),
        _Rule(
            r"\b(?:setTimeout|setInterval)\s*\(\s*[`\"']",
            "code-injection",
            "warning",
            "Timer API is invoked with string code.",
            "Pass a function reference instead of a code string.",
            0.95,
        ),
        _Rule(
            r"\b(?:innerHTML|outerHTML)\s*=",
            "xss",
            "warning",
            "Direct HTML assignment detected.",
            "Use safe render path or sanitizer.",
            0.9,
        ),
        _Rule(
            r"\bdocument\.write\s*\(", "xss", "warning", "document.write can inject HTML.", "Use safe DOM APIs.", 0.9
        ),
        _Rule(
            r"\b(?:localStorage|sessionStorage)\.setItem\s*\([^)]*(?:token|secret|password)",
            "data-leak",
            "warning",
            "Sensitive token-like value stored in browser storage.",
            "Keep secrets out of browser storage or use short-lived scoped tokens.",
            0.86,
        ),
        _Rule(
            r"\bdangerouslySetInnerHTML\b",
            "xss",
            "warning",
            "dangerouslySetInnerHTML renders potentially unsafe DOM content.",
            "Sanitize user HTML before rendering.",
            0.9,
        ),
        _Rule(
            r"\bwindow\.location(?:\.href)?\s*=(?!\s*[\"'`])\s*",
            "open-redirect",
            "error",
            "A dynamic value is assigned to the browser location.",
            "Allow-list same-origin destinations before navigating.",
            0.96,
        ),
        _Rule(
            r"\bchild_process\.(?:exec|execSync|spawn)\s*\(",
            "command-injection",
            "warning",
            "Node.js child_process command API used.",
            "Avoid shell-style execution and validate arguments.",
            0.94,
        ),
        _Rule(
            r"\bchild_process\.spawnSync?\s*\(",
            "command-injection",
            "warning",
            "Child process spawn API used.",
            "Avoid untrusted command arguments.",
            0.9,
        ),
        _Rule(
            r"\b(?:db|client|connection|pool)\.(?:query|execute)\s*\([^\n]*(?:\+|`)",
            "sql-injection",
            "warning",
            "SQL query appears to be built dynamically.",
            "Use bound parameters.",
            0.88,
        ),
    ],
    "typescript": [
        _Rule(
            r"\beval\s*\(",
            "code-injection",
            "error",
            "Direct eval usage found.",
            "Avoid eval and use explicit parser logic.",
            0.98,
        ),
        _Rule(
            r"\b(?:innerHTML|outerHTML)\s*=",
            "xss",
            "warning",
            "Direct HTML assignment detected.",
            "Use safe rendering.",
            0.9,
        ),
        _Rule(
            r"\bbypassSecurityTrust(?:Html|Script|Style|Url|ResourceUrl)\s*\(",
            "xss-bypass",
            "warning",
            "Angular sanitizer bypass API used.",
            "Avoid bypass APIs for untrusted data.",
            0.94,
        ),
        _Rule(
            r"\bdangerouslySetInnerHTML\b",
            "xss",
            "warning",
            "dangerouslySetInnerHTML renders potentially unsafe DOM content.",
            "Sanitize untrusted markup before render.",
            0.9,
        ),
        _Rule(
            r"\bwindow\.location(?:\.href)?\s*=(?!\s*[\"'`])\s*",
            "open-redirect",
            "error",
            "A dynamic value is assigned to the browser location.",
            "Allow-list same-origin destinations before navigating.",
            0.96,
        ),
        _Rule(
            r"\bchild_process\.(?:exec|execSync|spawn|spawnSync)\s*\(",
            "command-injection",
            "warning",
            "Node.js child_process command API used.",
            "Avoid shell-style execution paths.",
            0.94,
        ),
        _Rule(
            r"\b(?:localStorage|sessionStorage)\.setItem\s*\([^)]*(?:token|secret|password)",
            "data-leak",
            "warning",
            "Sensitive token-like value stored in browser storage.",
            "Keep secrets out of browser storage or use short-lived scoped tokens.",
            0.86,
        ),
    ],
    "go": [
        _Rule(
            r"\bos/exec\.Command\(",
            "command-injection",
            "error",
            "os/exec used to execute external commands.",
            "Prefer safe wrappers and explicit args.",
            0.96,
        ),
        _Rule(
            r"\bexec\.Command\(",
            "command-injection",
            "warning",
            "Go exec.Command is used.",
            "Validate command names and arguments before execution.",
            0.9,
        ),
        _Rule(
            r"\btemplate\.HTML\(",
            "xss",
            "warning",
            "Template HTML injection risk.",
            "Keep template HTML explicit and data sanitized.",
            0.9,
        ),
        _Rule(
            r"\bexec\.Command\(.+\"sh\",\s*\"-c\"",
            "command-injection",
            "error",
            "Shell-based command execution.",
            "Avoid `sh -c` and build explicit command args.",
            0.98,
        ),
        _Rule(
            r"\bos\.ReadFile\(",
            "path-traversal",
            "warning",
            "Potential file path from variable used in io call.",
            "Validate and constrain path inputs.",
            0.82,
        ),
        _Rule(
            r"\bhttp\.Get\s*\(\s*(?![\"'`][^\"'`]*[\"'`]\s*\))[^)\n]+\)",
            "ssrf",
            "error",
            "A dynamic URL is passed to http.Get.",
            "Allow-list destinations and block private, loopback, and link-local address ranges.",
            0.96,
        ),
        _Rule(
            r"\b(?:db|tx)\.(?:Exec|Query|QueryRow)\s*\([^\n]*(?:fmt\.Sprintf|\+)",
            "sql-injection",
            "error",
            "SQL query is built dynamically before execution.",
            "Use placeholders with bound parameters.",
            0.93,
        ),
        _Rule(
            r"\bfmt\.Sprintf\s*\([^\n]*(?:SELECT|INSERT|UPDATE|DELETE)",
            "sql-injection",
            "warning",
            "SQL string is built with fmt.Sprintf.",
            "Use placeholders with bound parameters.",
            0.9,
        ),
    ],
    "java": [
        _Rule(
            r"\bRuntime\.getRuntime\(\)\.exec\(",
            "command-injection",
            "error",
            "Runtime.exec command execution detected.",
            "Prefer allow-listed command execution or avoid runtime execution.",
            0.96,
        ),
        _Rule(
            r"\bProcessBuilder\s*\(",
            "command-injection",
            "warning",
            "ProcessBuilder is used.",
            "Validate all arguments and avoid shell metacharacters.",
            0.88,
        ),
        _Rule(
            r"\b(?:ObjectInputStream|ObjectOutputStream)\b",
            "insecure-deserialization",
            "error",
            "Native Java serialization API used.",
            "Validate signatures and avoid unsafe serialized input.",
            0.93,
        ),
        _Rule(
            r"\b(?:ScriptEngineManager|javax\.script)\b",
            "code-injection",
            "error",
            "Dynamic script evaluation detected.",
            "Avoid eval style scripting APIs.",
            0.95,
        ),
        _Rule(
            r"\bStatement\s+\w+\s*=|\bcreateStatement\s*\(",
            "sql-injection",
            "warning",
            "Raw JDBC Statement is used.",
            "Use PreparedStatement with bound parameters.",
            0.88,
        ),
        _Rule(
            r"\bnew\s+File\s*\([^)]*(?:user|path|request|param)",
            "path-traversal",
            "warning",
            "File path is built from user-controlled input.",
            "Normalize and constrain file paths.",
            0.84,
        ),
    ],
    "ruby": [
        _Rule(
            r"\beval\s*\(\s*(?![\"'][^#{}\"']*[\"']\s*\))",
            "code-injection",
            "error",
            "Ruby eval usage detected.",
            "Avoid eval and parse structured input safely.",
            0.98,
        ),
        _Rule(
            r"`[^`]*`",
            "command-injection",
            "warning",
            "Backtick command execution detected.",
            "Prefer `open3` with fixed arguments.",
            0.9,
        ),
        _Rule(
            r"\b(?:instance_eval|class_eval|send)\s*\(",
            "code-injection",
            "warning",
            "Dynamic dispatch/runtime execution API used.",
            "Use explicit method calls where possible.",
            0.88,
        ),
        _Rule(
            r"\bKernel\.(?:system|`)\s*\(",
            "command-injection",
            "error",
            "Kernel command execution API used.",
            "Avoid command execution from user-controlled input.",
            0.95,
        ),
        _Rule(
            r"\bsystem\s*\(",
            "command-injection",
            "warning",
            "Shell command execution API used.",
            "Avoid command execution from user-controlled input.",
            0.92,
        ),
        _Rule(
            r"\bMarshal\.load\(",
            "insecure-deserialization",
            "error",
            "Marshal.load deserializes attacker-controlled data.",
            "Use signed, versioned formats and validate input.",
            0.96,
        ),
        _Rule(
            r"\bYAML\.load\s*\(",
            "insecure-deserialization",
            "warning",
            "YAML.load may instantiate unsafe objects.",
            "Use YAML.safe_load with explicit permitted classes.",
            0.9,
        ),
        _Rule(
            r"\bFile\.(?:read|binread|open)\s*\(\s*(?:params|user_input|request)(?:\s*\[|\.)",
            "path-traversal",
            "error",
            "A filesystem API receives a path directly from request data.",
            "Resolve the path under an allow-listed root before opening it.",
            0.96,
        ),
        _Rule(
            r"\bOpen3\.(?:capture|popen)",
            "command-injection",
            "warning",
            "Open3 command execution API used.",
            "Pass arguments as an array and validate inputs.",
            0.9,
        ),
    ],
    "rust": [
        _Rule(
            r"\bstd::process::Command\(",
            "command-injection",
            "error",
            "Process spawn API used.",
            "Avoid shell-like command construction.",
            0.95,
        ),
        _Rule(
            r"\bCommand::new\s*\(",
            "command-injection",
            "warning",
            "Process spawn API used.",
            "Validate command names and arguments before execution.",
            0.9,
        ),
        _Rule(
            r"\bunsafe\s*(?:\{|fn\b)",
            "unsafe-block",
            "warning",
            "Unsafe block or function used.",
            "Limit unsafe scope, document the # Safety contract for public APIs, and add safety assertions.",
            0.96,
        ),
        _Rule(
            r"\btransmute(?:::)?\s*<",
            "unsafe-transmute",
            "warning",
            "transmute usage found.",
            "Avoid transmute unless ABI requirements are strict.",
            0.9,
        ),
        _Rule(
            r"\bfs::(?:read|read_to_string|read_dir)\s*\(\s*&?path",
            "path-traversal",
            "warning",
            "Filesystem access uses a variable path.",
            "Normalize and constrain file paths before filesystem access.",
            0.8,
        ),
    ],
    "vue": [
        _Rule(
            r"v-html",
            "xss",
            "warning",
            "Vue `v-html` binding can inject HTML.",
            "Use text binding or strict sanitizer for untrusted input.",
            0.9,
        ),
        _Rule(
            r"<component\s+[^>]*:is=",
            "xss",
            "warning",
            "Dynamic component selection from data can expand attack surface.",
            "Allow-list component names before rendering.",
            0.82,
        ),
        _Rule(
            r"\bwindow\.location(?:\.href)?\s*=(?!\s*[\"'`])\s*",
            "open-redirect",
            "error",
            "Redirect target is assigned dynamically.",
            "Validate redirect destinations against an allow-list.",
            0.96,
        ),
        _Rule(
            r"@click\.native",
            "xss",
            "warning",
            "Native event handler may bypass component boundary checks.",
            "Prefer component event binding and keep inputs validated.",
            0.7,
        ),
    ],
    "svelte": [
        _Rule(
            r"\{@html",
            "xss",
            "warning",
            "Svelte raw HTML injection (`{@html}`) used.",
            "Use {@html} only with trusted content and sanitizer.",
            0.91,
        ),
        _Rule(
            r"\bdocument\.cookie\b",
            "data-leak",
            "warning",
            "Cookie value read in frontend code.",
            "Avoid sending cookie data to untrusted sinks.",
            0.6,
        ),
    ],
}


def _rules_for_language(language: str) -> tuple[_Rule, ...]:
    if language == "javascript":
        return tuple(_SECURITY_RULES.get("javascript", []))
    if language == "typescript":
        return tuple(_SECURITY_RULES.get("typescript", []))
    if language == "vue":
        return tuple(_SECURITY_RULES.get("vue", []))
    if language == "svelte":
        return tuple(_SECURITY_RULES.get("svelte", []))
    return tuple(_SECURITY_RULES.get(language, []))


_TEST_PATH_PARTS = {"test", "tests", "testing", "spec", "specs", "fixtures", "examples"}
_PLACEHOLDER_SECRET_MARKERS = (
    "not-a-real",
    "not_real",
    "example",
    "dummy",
    "placeholder",
    "fake",
    "changeme",
    "test-password",
    "test-secret",
    "test-token",
    "test-api",
    "sk-test",
)


def _is_test_path(file_path: str) -> bool:
    """Return whether a path is test/example code where context is essential."""

    normalized = (file_path or "").replace("\\", "/").lower()
    parts = [part for part in normalized.split("/") if part]
    name = parts[-1] if parts else ""
    return (
        any(part in _TEST_PATH_PARTS for part in parts[:-1])
        or name.startswith(("test_", "spec_"))
        or bool(re.search(r"(?:^|[._-])(?:test|spec)(?:[._-]|$)", name))
    )


def _looks_like_placeholder_secret(matched_text: str) -> bool:
    """Recognize explicit non-secret values without hiding realistic tokens."""

    text = (matched_text or "").lower()
    return any(marker in text for marker in _PLACEHOLDER_SECRET_MARKERS)


def _detector_confidence(file_path: str, confidence: float) -> float:
    """Route findings in tests/examples through contextual calibration."""

    if _is_test_path(file_path):
        return min(confidence, 0.75)
    return confidence


def _is_comment_only(line: str) -> bool:
    """Ignore rule names that only occur in source-code comments."""

    stripped = (line or "").lstrip()
    return stripped.startswith(("#", "//", "/*", "*", "<!--", "--"))


def _is_import_only(line: str) -> bool:
    """Ignore dangerous API names that only select a module or type."""

    stripped = (line or "").lstrip()
    return bool(
        re.match(
            r"^(?:from\s+\S+\s+import\b|import\s+|use\s+|using\s+|#include\b|require\s*\()",
            stripped,
            re.IGNORECASE,
        )
    )


def _match_starts_inside_string(line: str, start: int) -> bool:
    """Best-effort lexical guard against API names embedded in quoted text."""

    quote = ""
    escaped = False
    for char in (line or "")[:start]:
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if quote:
            if char == quote:
                quote = ""
        elif char in {"'", '"', "`"}:
            quote = char
    return bool(quote)


def _starts_inside_javascript_template_expression(line: str, start: int) -> bool:
    """Return whether ``start`` is executable code inside a JS ``${...}`` block."""

    text = line or ""
    index = 0
    in_template = False
    expression_depth = 0
    code_quote = ""
    escaped = False

    while index < min(start, len(text)):
        char = text[index]
        next_char = text[index + 1] if index + 1 < len(text) else ""

        if not in_template:
            if code_quote:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == code_quote:
                    code_quote = ""
            elif char in {"'", '"'}:
                code_quote = char
            elif char == "`":
                in_template = True
            index += 1
            continue

        if expression_depth == 0:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == "`":
                in_template = False
            elif char == "$" and next_char == "{":
                expression_depth = 1
                index += 2
                continue
            index += 1
            continue

        if code_quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == code_quote:
                code_quote = ""
        elif char in {"'", '"'}:
            code_quote = char
        elif char == "{":
            expression_depth += 1
        elif char == "}":
            expression_depth -= 1
        index += 1

    return in_template and expression_depth > 0 and not code_quote


def _is_static_python_eval(line: str, match_start: int) -> bool:
    """Return whether eval/exec receives only a compile-time string literal."""

    source = (line or "").lstrip()
    indent = len(line or "") - len(source)
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False
    adjusted_start = max(0, match_start - indent)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        if node.func.id not in {"eval", "exec"} or node.func.col_offset != adjusted_start:
            continue
        return bool(node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str))
    return False


_POSTIMAGE_HUNK_HEADER = re.compile(
    r"^@@ -\d+(?:,(?P<old_count>\d+))? \+(?P<new_start>\d+)(?:,(?P<new_count>\d+))? @@(?:.*)$"
)


def _postimage_hunks(diff: str) -> list[list[tuple[int, str, bool]]]:
    """Return post-image hunk lines as ``(RIGHT line, content, added)`` tuples."""

    hunks: list[list[tuple[int, str, bool]]] = []
    current: list[tuple[int, str, bool]] = []
    new_line = 0
    old_remaining = 0
    new_remaining = 0
    in_hunk = False

    for raw_line in (diff or "").splitlines():
        header = _POSTIMAGE_HUNK_HEADER.match(raw_line)
        if header:
            if current:
                hunks.append(current)
            current = []
            new_line = int(header.group("new_start"))
            old_remaining = int(header.group("old_count") or 1)
            new_remaining = int(header.group("new_count") or 1)
            in_hunk = True
            continue

        if raw_line.startswith("@@") or raw_line.startswith("diff --git "):
            if current:
                hunks.append(current)
            current = []
            in_hunk = False
            continue
        if not in_hunk or raw_line.startswith("\\ No newline at end of file"):
            continue
        if not raw_line:
            in_hunk = False
            continue

        prefix = raw_line[0]
        if prefix == "+" and new_remaining > 0:
            current.append((new_line, raw_line[1:], True))
            new_line += 1
            new_remaining -= 1
        elif prefix == " " and old_remaining > 0 and new_remaining > 0:
            current.append((new_line, raw_line[1:], False))
            new_line += 1
            old_remaining -= 1
            new_remaining -= 1
        elif prefix == "-" and old_remaining > 0:
            old_remaining -= 1
        else:
            in_hunk = False

        if old_remaining == 0 and new_remaining == 0:
            in_hunk = False

    if current:
        hunks.append(current)
    return hunks


def _has_named_child_process_exec(diff: str) -> bool:
    postimage = "\n".join(content for hunk in _postimage_hunks(diff) for _line, content, _added in hunk)
    imports = re.finditer(
        r"import\s*\{(?P<names>[^}]*)\}\s*from\s*[\"'](?:node:)?child_process[\"']",
        postimage,
        re.IGNORECASE | re.DOTALL,
    )
    if any(re.search(r"(?:^|,)\s*exec(?:Sync)?\s*(?:,|$)", match.group("names")) for match in imports):
        return True
    return bool(
        re.search(
            r"(?:const|let|var)\s*\{[^}]*\bexec(?:Sync)?\b[^}]*\}\s*=\s*require\s*\(\s*[\"'](?:node:)?child_process[\"']\s*\)",
            postimage,
            re.IGNORECASE | re.DOTALL,
        )
    )


def _python_ignored_spans(diff: str) -> dict[int, list[_IgnoredSpan]]:
    """Map Python STRING/COMMENT token spans to actual RIGHT-side lines.

    Tokenization runs on each hunk's post-image (context plus additions), while
    returned coordinates remain the post-image line/column coordinates used by
    GitHub. This catches triple-quoted prompt/documentation text that starts in
    unchanged context as well as strings added entirely by the patch.
    """

    ignored: dict[int, list[_IgnoredSpan]] = {}
    for group in _postimage_hunks(diff):
        source_lines = [content for _, content, _added in group]
        source = "\n".join(source_lines) + "\n"
        try:
            tokens = tokenize.generate_tokens(io.StringIO(source).readline)
            for item in tokens:
                if item.type not in {token.STRING, token.COMMENT}:
                    continue
                start_line, start_col = item.start
                end_line, end_col = item.end
                for synthetic_line in range(start_line, end_line + 1):
                    if not 1 <= synthetic_line <= len(group):
                        continue
                    right_line = group[synthetic_line - 1][0]
                    left = start_col if synthetic_line == start_line else 0
                    right = end_col if synthetic_line == end_line else len(source_lines[synthetic_line - 1])
                    ignored.setdefault(right_line, []).append(
                        _IgnoredSpan(
                            start=left,
                            end=right,
                            token_type=item.type,
                            multiline=start_line != end_line,
                        )
                    )
        except (IndentationError, tokenize.TokenError):
            # Incomplete hunks can be syntactically partial. Tokens yielded
            # before the failure have already populated the trustworthy spans.
            continue
    return ignored


def _ignored_span_at(line_no: int, match_start: int, ignored: dict[int, list[_IgnoredSpan]]) -> _IgnoredSpan | None:
    return next(
        (span for span in ignored.get(line_no, ()) if span.start <= match_start < span.end),
        None,
    )


_PYTHON_PATH_BUILDERS = {"os.path.join", "pathlib.Path", "Path"}
_PYTHON_PATH_SINKS = {"open", "os.open"}
_PYTHON_PATH_SANITIZERS = {"os.path.basename", "secure_filename"}


def _python_call_name(node: ast.Call) -> str:
    parts: list[str] = []
    target: ast.expr = node.func
    while isinstance(target, ast.Attribute):
        parts.append(target.attr)
        target = target.value
    if isinstance(target, ast.Name):
        parts.append(target.id)
    return ".".join(reversed(parts))


def _contains_name(node: ast.AST, names: set[str]) -> bool:
    return any(isinstance(child, ast.Name) and child.id in names for child in ast.walk(node))


def _assigned_names(node: ast.Assign | ast.AnnAssign) -> set[str]:
    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    return {child.id for target in targets for child in ast.walk(target) if isinstance(child, ast.Name)}


def _python_added_tree(diff: str) -> ast.Module | None:
    additions = iter_added_lines(diff or "")
    if not additions:
        return None
    last_line = max(line for line, _content in additions)
    if last_line > 20_000:
        return None
    source = [""] * last_line
    for line_no, content in additions:
        source[line_no - 1] = content
    try:
        return ast.parse("\n".join(source) + "\n")
    except SyntaxError:
        return None


def _python_dynamic_path_sinks(diff: str) -> list[int]:
    """Find unguarded request-derived path construction reaching a file sink.

    This deliberately requires a data-flow edge through a path builder. A
    generic helper that merely accepts ``path`` and opens it is not enough to
    claim traversal; direct request data joined beneath a root is.
    """

    tree = _python_added_tree(diff)
    if tree is None:
        return []

    sinks: set[int] = set()
    scopes: list[ast.AST] = [
        tree,
        *(node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))),
    ]
    for scope in scopes:
        if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
            parameters = {
                arg.arg
                for arg in (*scope.args.posonlyargs, *scope.args.args, *scope.args.kwonlyargs)
                if re.search(r"(?:user|input|request|param|file|path|name)", arg.arg, re.IGNORECASE)
            }
            if scope.args.vararg:
                parameters.add(scope.args.vararg.arg)
            if scope.args.kwarg:
                parameters.add(scope.args.kwarg.arg)
            nodes = [node for node in ast.walk(scope) if node is not scope]
        else:
            parameters = set()
            nodes = list(ast.walk(scope))

        tainted = set(parameters)
        built_paths: set[str] = set()
        guarded: set[str] = set()
        for node in sorted(nodes, key=lambda item: (getattr(item, "lineno", 0), getattr(item, "col_offset", 0))):
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                value = node.value
                if value is None:
                    continue
                names = _assigned_names(node)
                call_name = _python_call_name(value) if isinstance(value, ast.Call) else ""
                if call_name in _PYTHON_PATH_SANITIZERS:
                    guarded.update(names)
                    tainted.difference_update(names)
                    built_paths.difference_update(names)
                elif _contains_name(value, tainted):
                    tainted.update(names)
                    if call_name in _PYTHON_PATH_BUILDERS or isinstance(value, (ast.BinOp, ast.JoinedStr)):
                        built_paths.update(names)

            if isinstance(node, ast.Call):
                call_name = _python_call_name(node)
                method_name = call_name.rsplit(".", 1)[-1]
                if method_name in {"is_relative_to", "relative_to", "validate_path"}:
                    guarded.update(
                        child.id for child in ast.walk(node) if isinstance(child, ast.Name) and child.id in tainted
                    )
                first_arg = node.args[0] if node.args else None
                direct_built_path = (
                    isinstance(first_arg, ast.Call)
                    and _python_call_name(first_arg) in _PYTHON_PATH_BUILDERS
                    and _contains_name(first_arg, tainted)
                )
                tainted_built_name = bool(first_arg is not None and _contains_name(first_arg, built_paths - guarded))
                method_sink = method_name in {
                    "read_text",
                    "read_bytes",
                    "write_text",
                    "write_bytes",
                    "unlink",
                }
                receiver = node.func.value if isinstance(node.func, ast.Attribute) else None
                receiver_built_path = bool(
                    method_sink
                    and receiver is not None
                    and (
                        _contains_name(receiver, built_paths - guarded)
                        or (
                            isinstance(receiver, ast.Call)
                            and _python_call_name(receiver) in _PYTHON_PATH_BUILDERS
                            and _contains_name(receiver, tainted)
                        )
                    )
                )
                if (call_name in _PYTHON_PATH_SINKS or method_sink) and (
                    direct_built_path or tainted_built_name or receiver_built_path
                ):
                    sinks.add(node.lineno)
    return sorted(sinks)


def _python_open_redirect_sinks(diff: str) -> list[int]:
    """Find redirect helpers that return an unvalidated destination argument."""

    tree = _python_added_tree(diff)
    if tree is None:
        return []
    sinks: set[int] = set()
    for function in ast.walk(tree):
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not re.search(
            r"(?:build|make|create|validate|safe).*redirect|redirect.*(?:url|uri)",
            function.name,
            re.IGNORECASE,
        ):
            continue
        destination_args = {
            arg.arg
            for arg in (*function.args.posonlyargs, *function.args.args, *function.args.kwonlyargs)
            if re.search(r"(?:next|redirect|return|target|dest|url|location)", arg.arg, re.IGNORECASE)
        }
        for destination in destination_args:
            guard_names = {destination}
            for assignment in (node for node in ast.walk(function) if isinstance(node, (ast.Assign, ast.AnnAssign))):
                value = assignment.value
                if (
                    value is not None
                    and _contains_name(value, {destination})
                    and isinstance(value, ast.Call)
                    and _python_call_name(value).rsplit(".", 1)[-1].lower() == "urlparse"
                ):
                    guard_names.update(_assigned_names(assignment))
            guarded = False
            for conditional in (node for node in ast.walk(function) if isinstance(node, ast.If)):
                if not _contains_name(conditional.test, guard_names):
                    continue
                guard_calls = {
                    _python_call_name(node).rsplit(".", 1)[-1].lower()
                    for node in ast.walk(conditional.test)
                    if isinstance(node, ast.Call)
                }
                membership_guard = any(
                    any(isinstance(operator, (ast.In, ast.NotIn)) for operator in comparison.ops)
                    for comparison in ast.walk(conditional.test)
                    if isinstance(comparison, ast.Compare)
                )
                if (
                    guard_calls
                    & {
                        "startswith",
                        "is_relative_to",
                        "is_safe_redirect",
                        "validate_redirect",
                        "urlparse",
                    }
                    or membership_guard
                ):
                    guarded = True
                    break
            if guarded:
                continue
            for returned in (node for node in ast.walk(function) if isinstance(node, ast.Return)):
                if isinstance(returned.value, ast.Name) and returned.value.id == destination:
                    sinks.add(returned.lineno)
    return sorted(sinks)


def _hunk_lines_for_right_line(diff: str, line_no: int) -> tuple[list[tuple[int, str, bool]], int] | None:
    for hunk in _postimage_hunks(diff):
        for index, (right_line, _content, _added) in enumerate(hunk):
            if right_line == line_no:
                return hunk, index
    return None


def _rust_unsafe_has_safety_evidence(diff: str, line_no: int) -> bool:
    location = _hunk_lines_for_right_line(diff, line_no)
    if location is None:
        return False
    hunk, index = location
    context = "\n".join(content for _line, content, _added in hunk[max(0, index - 4) : index])
    return bool(re.search(r"\bSAFETY\s*:", context, re.IGNORECASE))


def _browser_redirect_is_guarded(diff: str, line_no: int, source_line: str) -> bool:
    assignment = re.search(r"=\s*(?P<target>[A-Za-z_$][\w$]*)", source_line)
    if not assignment:
        return False
    target = assignment.group("target")
    if re.search(r"=\s*(?:safe|validate|allow)\w*\s*\(", source_line, re.IGNORECASE):
        return True
    location = _hunk_lines_for_right_line(diff, line_no)
    if location is None:
        return False
    hunk, index = location
    context = "\n".join(content for _line, content, _added in hunk[max(0, index - 10) : index])
    escaped = re.escape(target)
    guards = (
        rf"(?:allow(?:ed|list)?|safe\w*)\s*\.\s*(?:includes|has)\s*\(\s*{escaped}\s*\)",
        rf"{escaped}\s*\.\s*startsWith\s*\(",
        rf"(?:isSafeRedirect|validateRedirect|isAllowedUrl)\s*\(\s*{escaped}\s*\)",
    )
    return any(re.search(pattern, context, re.IGNORECASE) for pattern in guards)


def detect_security_findings(diffs: dict[str, str]) -> list[DetectorFinding]:
    """Scan modified file diffs for deterministic security findings."""

    findings: list[DetectorFinding] = []

    for file_path, diff in diffs.items():
        language = normalize_language(file_path)
        rules = list(_rules_for_language(language))
        if language in {"javascript", "typescript"} and _has_named_child_process_exec(diff):
            rules.append(
                _Rule(
                    r"\bexec(?:Sync)?\s*\(\s*(?:`[^`]*\$\{[^}]+\}[^`]*`|(?![\"'`][^\"'`]*[\"'`]\s*(?:,|\)))[^)\n]+)",
                    "command-injection",
                    "error",
                    "A dynamic command is passed to child_process exec.",
                    "Use a fixed executable with an argument array and validate every dynamic argument.",
                    0.96,
                )
            )
        python_ignored = _python_ignored_spans(diff) if language == "python" else {}

        for rule in _UNIVERSAL_RULES:
            matches = match_lines(diff, rule.pattern)
            for line_no, match in matches:
                if language == "python":
                    ignored_span = _ignored_span_at(line_no, match.start(), python_ignored)
                    allow_literal = (
                        ignored_span is not None
                        and rule.allow_single_line_string_literal
                        and ignored_span.token_type == token.STRING
                        and not ignored_span.multiline
                        and match.start() == ignored_span.start
                    )
                    if ignored_span is not None and not allow_literal:
                        continue
                if rule.category == "hardcoded-secrets" and _looks_like_placeholder_secret(match.group(0)):
                    continue
                findings.append(
                    DetectorFinding(
                        file=file_path,
                        line=line_no,
                        severity=rule.severity,
                        category=normalize_category_for_detector(rule.category),
                        message=rule.message,
                        suggestion=rule.suggestion,
                        confidence=_detector_confidence(file_path, safe_confidence(rule.confidence, 1)),
                    )
                )

        for rule in rules:
            for line_no, match in match_lines(diff, rule.pattern):
                if language == "python" and _ignored_span_at(line_no, match.start(), python_ignored) is not None:
                    continue
                if _is_import_only(match.string):
                    continue
                inside_string = _match_starts_inside_string(match.string, match.start())
                if language in {"javascript", "typescript"} and _starts_inside_javascript_template_expression(
                    match.string, match.start()
                ):
                    inside_string = False
                if language != "python" and (_is_comment_only(match.string) or inside_string):
                    continue
                if (
                    language == "python"
                    and rule.category == "code-injection"
                    and _is_static_python_eval(match.string, match.start())
                ):
                    continue
                if (
                    language == "rust"
                    and rule.category == "unsafe-block"
                    and _rust_unsafe_has_safety_evidence(diff, line_no)
                ):
                    continue
                if rule.category == "open-redirect" and _browser_redirect_is_guarded(diff, line_no, match.string):
                    continue
                findings.append(
                    DetectorFinding(
                        file=file_path,
                        line=line_no,
                        severity=rule.severity,
                        category=normalize_category_for_detector(rule.category),
                        message=rule.message,
                        suggestion=rule.suggestion,
                        confidence=_detector_confidence(file_path, safe_confidence(rule.confidence, 1)),
                    )
                )

        if language == "python":
            for line_no in _python_dynamic_path_sinks(diff):
                findings.append(
                    DetectorFinding(
                        file=file_path,
                        line=line_no,
                        severity="error",
                        category="path-traversal",
                        message="Request-derived path data reaches a filesystem sink after dynamic path construction.",
                        suggestion="Resolve the candidate path and enforce that it remains below the intended root.",
                        confidence=_detector_confidence(file_path, 0.96),
                    )
                )
            for line_no in _python_open_redirect_sinks(diff):
                findings.append(
                    DetectorFinding(
                        file=file_path,
                        line=line_no,
                        severity="error",
                        category="open-redirect",
                        message="A redirect helper returns an unvalidated destination argument.",
                        suggestion=(
                            "Allow-list local destinations or validate scheme, host, and origin before redirecting."
                        ),
                        confidence=_detector_confidence(file_path, 0.96),
                    )
                )

    deduped = dedupe_findings(findings)
    transmute_lines: dict[str, set[int]] = {}
    for finding in deduped:
        if finding.category == "unsafe-transmute":
            transmute_lines.setdefault(finding.file, set()).add(finding.line)
    return [
        finding
        for finding in deduped
        if not (
            finding.category == "unsafe-block"
            and any(abs(finding.line - line) <= 1 for line in transmute_lines.get(finding.file, set()))
        )
    ]


def normalize_language(file_path: str) -> str:
    """Normalize file extension to detector language key."""

    lower = (file_path or "").lower()
    if lower.endswith(".vue"):
        return "vue"
    if lower.endswith(".svelte"):
        return "svelte"
    return detect_language(file_path) or "unknown"
