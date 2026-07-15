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
from reviewforge.engine.detectors.unified_diff import iter_added_lines, iter_right_lines
from reviewforge.engine.symbol_extractor import detect_language, mask_comments, mask_non_code


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
            r"(?<![\w.])\b(?:eval|exec)\s*\(\s*(?![rub]*[\"'][^{}\"']*[\"']\s*(?:,|\)))",
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
            r"(?<![\w.])\beval\s*\(",
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
            r"\b(?:localStorage|sessionStorage)\.setItem\s*\(\s*[\"'`](?:access[-_]?token|refresh[-_]?token|auth[-_]?token|token|secret|password)[\"'`]\s*,\s*(?![\"'`]|null\b|undefined\b|true\b|false\b)[A-Za-z_$][\w$]*\s*\)",
            "data-leak",
            "error",
            "A sensitive credential value is persisted in browser storage.",
            "Keep credentials out of browser storage or use a narrowly scoped, short-lived session mechanism.",
            0.96,
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
            r"\bdangerouslySetInnerHTML\s*=\s*\{\s*\{\s*__html\s*:\s*[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*\s*\}\s*\}",
            "xss",
            "error",
            "A dynamic value is rendered through dangerouslySetInnerHTML.",
            "Sanitize the value with a proven HTML sanitizer before rendering it.",
            0.96,
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
            r"(?<![\w.])\beval\s*\(",
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
            r"\bdangerouslySetInnerHTML\s*=\s*\{\s*\{\s*__html\s*:\s*[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*\s*\}\s*\}",
            "xss",
            "error",
            "A dynamic value is rendered through dangerouslySetInnerHTML.",
            "Sanitize the value with a proven HTML sanitizer before rendering it.",
            0.96,
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
            r"\b(?:localStorage|sessionStorage)\.setItem\s*\(\s*[\"'`](?:access[-_]?token|refresh[-_]?token|auth[-_]?token|token|secret|password)[\"'`]\s*,\s*(?![\"'`]|null\b|undefined\b|true\b|false\b)[A-Za-z_$][\w$]*\s*\)",
            "data-leak",
            "error",
            "A sensitive credential value is persisted in browser storage.",
            "Keep credentials out of browser storage or use a narrowly scoped, short-lived session mechanism.",
            0.96,
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
            r"(?<![\w.])\beval\s*\(\s*(?![\"'][^#{}\"']*[\"']\s*\))",
            "code-injection",
            "error",
            "Ruby eval usage detected.",
            "Avoid eval and parse structured input safely.",
            0.98,
        ),
        _Rule(
            r"`[^`]*#\{[^}]+\}[^`]*`",
            "command-injection",
            "error",
            "A Ruby backtick command contains dynamic interpolation.",
            "Avoid shell interpolation; invoke a fixed executable with explicit arguments.",
            0.96,
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


def _masked_right_line_map(
    diff: str,
    language: str,
    *,
    comments_only: bool = False,
    preserve_ruby_commands: bool = False,
) -> dict[int, str]:
    """Return coordinate-preserving lexical masks for visible post-image groups."""

    lexer_language = "typescript" if language in {"vue", "svelte"} else language
    rows = iter_right_lines(diff)
    masked: dict[int, str] = {}
    group: list[tuple[int, str]] = []

    def flush() -> None:
        if not group:
            return
        source = "\n".join(content for _line, content in group)
        code = (
            mask_comments(source, lexer_language)
            if comments_only
            else mask_non_code(
                source,
                lexer_language,
                preserve_ruby_commands=preserve_ruby_commands,
            )
        ).split("\n")
        masked.update((row[0], code[index]) for index, row in enumerate(group))
        group.clear()

    for row in rows:
        if group and row[0] != group[-1][0] + 1:
            flush()
        group.append(row)
    flush()
    return masked


def _match_starts_in_mask(line_no: int, match: re.Match[str], masked: dict[int, str]) -> bool:
    line = masked.get(line_no, "")
    return match.start() < len(line) and not line[match.start()].isspace()


def _is_complete_new_file_patch(diff: str) -> bool:
    """Require one exact ``-0,0 +1,N`` hunk before bypassing calibration."""

    headers = list(re.finditer(r"^@@ .* @@", diff, re.MULTILINE))
    new_file = re.search(r"^@@ -0,0 \+1(?:,(?P<count>\d+))? @@", diff, re.MULTILINE)
    if new_file is None or len(headers) != 1 or headers[0].start() != new_file.start():
        return False
    expected = int(new_file.group("count") or 1)
    additions = iter_added_lines(diff)
    return len(additions) == expected and [line for line, _content in additions] == list(range(1, expected + 1))


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


def _is_dynamic_code_declaration(language: str, line: str) -> bool:
    """Reject method/function declarations named like builtin execution APIs."""

    if language == "python":
        return bool(re.match(r"^\s*(?:async\s+)?def\s+(?:eval|exec)\b", line))
    if language == "ruby":
        return bool(re.match(r"^\s*def\s+(?:self\.)?(?:eval|exec)\b", line))
    if language in {"javascript", "typescript", "vue", "svelte"}:
        return bool(
            re.search(
                r"(?:^|[,{;])\s*(?:(?:public|private|protected|static|async)\s+)*"
                r"(?:eval|exec)\s*\([^)]*\)\s*\{",
                line,
            )
        )
    return False


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


def _template_expression_match_is_code(line: str, start: int) -> bool:
    """Distinguish executable template expressions from comments/regex inside them."""

    if not _starts_inside_javascript_template_expression(line, start):
        return False
    opening = (line or "")[:start].rfind("${")
    if opening < 0:
        return False
    expression = (line or "")[opening + 2 :]
    offset = start - opening - 2
    masked = mask_non_code(expression, "typescript")
    return offset < len(masked) and not masked[offset].isspace()


def _starts_in_markup_text(line: str, start: int) -> bool:
    """Return true for JSX/Vue text nodes rather than executable expressions."""

    prefix = (line or "")[:start]
    # Find a real tag terminator.  The ``>`` in an event-handler arrow (``=>``)
    # or comparison lives inside a JSX expression and must not make subsequent
    # executable code look like a text node.
    opening = -1
    closing = -1
    quote = ""
    escaped = False
    expression_depth = 0
    for index, char in enumerate(prefix):
        if opening < 0:
            if (
                char == "<"
                and index + 1 < len(prefix)
                and prefix[index + 1] in "/ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
            ):
                opening = index
            continue
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            continue
        if char in {'"', "'", "`"}:
            quote = char
        elif char == "{":
            expression_depth += 1
        elif char == "}" and expression_depth:
            expression_depth -= 1
        elif char == ">" and expression_depth == 0:
            closing = index
            opening = -1
    if closing < 0:
        return False
    tag_opening = prefix.rfind("<", 0, closing + 1)
    if tag_opening < 0 or not re.match(r"</?[A-Za-z][^>]*>$", prefix[tag_opening : closing + 1].strip()):
        return False
    after_tag = prefix[closing + 1 :]
    return after_tag.rfind("{") <= after_tag.rfind("}")


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


def _is_static_javascript_eval(line: str, match_start: int) -> bool:
    """Return whether a direct eval receives one fixed literal string."""

    return bool(
        re.match(
            r"eval\s*\(\s*(?:\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|`[^`$]*`)\s*\)",
            (line or "")[match_start:],
        )
    )


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


_PYTHON_REDIRECT_APIS = {
    "redirect",
    "redirectresponse",
    "httpresponseredirect",
    "httpresponseredirectpermanent",
}
_PYTHON_SAFE_REDIRECT_GUARDS = {
    "is_relative_to",
    "is_safe_redirect",
    "isallowedredirect",
    "validate_redirect",
}


@dataclass(frozen=True)
class _RedirectGuardFacts:
    """Facts guaranteed by one truth value of a redirect condition."""

    local_path: bool = False
    rejects_scheme_relative: bool = False
    allowlisted: bool = False
    unsafe: bool = False

    @property
    def polarity(self) -> str:
        if self.allowlisted or (self.local_path and self.rejects_scheme_relative):
            return "safe"
        return "unsafe" if self.unsafe else ""


def _merge_redirect_facts(facts: list[_RedirectGuardFacts], *, all_branches: bool) -> _RedirectGuardFacts:
    """Combine guarantees for conjunctions or alternative control-flow paths."""

    if not facts:
        return _RedirectGuardFacts()
    combine = all if all_branches else any
    return _RedirectGuardFacts(
        local_path=combine(item.local_path for item in facts),
        rejects_scheme_relative=combine(item.rejects_scheme_relative for item in facts),
        allowlisted=combine(item.allowlisted for item in facts),
        unsafe=(all(item.unsafe for item in facts) if all_branches else any(item.unsafe for item in facts)),
    )


def _python_redirect_guard_facts(test: ast.expr, names: set[str], *, truth: bool) -> _RedirectGuardFacts:
    """Return guarantees when *test* evaluates to the requested truth value."""

    if not _contains_name(test, names):
        return _RedirectGuardFacts()
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        return _python_redirect_guard_facts(test.operand, names, truth=not truth)
    if isinstance(test, ast.BoolOp):
        # ``A and B`` being true (or ``A or B`` being false) guarantees every
        # child fact. The opposite result has alternative short-circuit paths,
        # so only a guarantee shared by every alternative can be retained.
        all_children_hold = (isinstance(test.op, ast.And) and truth) or (isinstance(test.op, ast.Or) and not truth)
        children = [_python_redirect_guard_facts(value, names, truth=truth) for value in test.values]
        return _merge_redirect_facts(children, all_branches=not all_children_hold)
    if isinstance(test, ast.Call):
        call_name = _python_call_name(test).rsplit(".", 1)[-1].lower()
        if call_name in _PYTHON_SAFE_REDIRECT_GUARDS:
            return _RedirectGuardFacts(allowlisted=truth, unsafe=not truth)
        if call_name != "startswith" or not isinstance(test.func, ast.Attribute):
            return _RedirectGuardFacts()
        if not _contains_name(test.func.value, names) or not test.args:
            return _RedirectGuardFacts()
        prefix = test.args[0]
        if not isinstance(prefix, ast.Constant) or not isinstance(prefix.value, str):
            return _RedirectGuardFacts()
        value = prefix.value
        if value == "//":
            return _RedirectGuardFacts(unsafe=truth, rejects_scheme_relative=not truth)
        if not value.startswith("/"):
            # ``startswith('https')`` and other arbitrary prefixes are not a
            # redirect validation policy.
            return _RedirectGuardFacts()
        if value == "/":
            return _RedirectGuardFacts(local_path=truth, unsafe=not truth)
        # A concrete application route (for example ``/app/``) cannot match a
        # scheme-relative ``//host`` destination.
        return _RedirectGuardFacts(
            local_path=truth,
            rejects_scheme_relative=truth,
            unsafe=not truth,
        )
    if isinstance(test, ast.Compare):
        if len(test.ops) == 1 and isinstance(test.ops[0], ast.In):
            return _RedirectGuardFacts(allowlisted=truth, unsafe=not truth)
        if len(test.ops) == 1 and isinstance(test.ops[0], ast.NotIn):
            return _RedirectGuardFacts(allowlisted=not truth, unsafe=truth)
        netloc = isinstance(test.left, ast.Attribute) and test.left.attr.lower() == "netloc"
        empty_string = any(isinstance(node, ast.Constant) and node.value == "" for node in test.comparators)
        if netloc and empty_string and len(test.ops) == 1:
            equal = isinstance(test.ops[0], (ast.Eq, ast.Is))
            hostless = truth == equal
            return _RedirectGuardFacts(rejects_scheme_relative=hostless, unsafe=not hostless)
        return _RedirectGuardFacts()
    if isinstance(test, ast.Attribute) and test.attr.lower() in {"hostname", "netloc"}:
        return _RedirectGuardFacts(rejects_scheme_relative=not truth, unsafe=truth)
    return _RedirectGuardFacts()


def _python_redirect_guard_polarity(test: ast.expr, names: set[str], *, truth: bool) -> str:
    """Return ``safe``/``unsafe`` only when the selected branch proves it."""

    return _python_redirect_guard_facts(test, names, truth=truth).polarity


def _python_block_terminates(statements: list[ast.stmt]) -> bool:
    if not statements:
        return False
    final = statements[-1]
    if isinstance(final, (ast.Raise, ast.Return)):
        return True
    return bool(
        isinstance(final, ast.If)
        and final.orelse
        and _python_block_terminates(final.body)
        and _python_block_terminates(final.orelse)
    )


def _python_unguarded_redirects(
    statements: list[ast.stmt], names: set[str], *, destination_safe: bool = False
) -> set[int]:
    """Walk one function's control flow while carrying a dominating guard fact."""

    sinks: set[int] = set()
    safe = destination_safe
    for statement in statements:
        if isinstance(statement, ast.If):
            true_polarity = _python_redirect_guard_polarity(statement.test, names, truth=True)
            false_polarity = _python_redirect_guard_polarity(statement.test, names, truth=False)
            sinks.update(
                _python_unguarded_redirects(
                    statement.body,
                    names,
                    destination_safe=safe or true_polarity == "safe",
                )
            )
            sinks.update(
                _python_unguarded_redirects(
                    statement.orelse,
                    names,
                    destination_safe=safe or false_polarity == "safe",
                )
            )
            if false_polarity == "safe" and _python_block_terminates(statement.body):
                safe = True
            elif true_polarity == "safe" and statement.orelse and _python_block_terminates(statement.orelse):
                safe = True
            continue

        if not safe:
            for call in (node for node in ast.walk(statement) if isinstance(node, ast.Call)):
                call_name = _python_call_name(call).rsplit(".", 1)[-1].lower()
                keyword_destinations = [
                    keyword.value
                    for keyword in call.keywords
                    if keyword.arg is not None and keyword.arg.lower() in {"url", "location", "destination"}
                ]
                destinations = ([call.args[0]] if call.args else []) + keyword_destinations
                if call_name in _PYTHON_REDIRECT_APIS and any(
                    _contains_name(destination, names) for destination in destinations
                ):
                    sinks.add(call.lineno)
    return sinks


def _python_open_redirect_sinks(diff: str) -> list[int]:
    """Find unguarded function parameters reaching an actual redirect API."""

    tree = _python_added_tree(diff)
    if tree is None:
        return []
    sinks: set[int] = set()
    for function in ast.walk(tree):
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        parameters = {arg.arg for arg in (*function.args.posonlyargs, *function.args.args, *function.args.kwonlyargs)}
        for destination in parameters:
            names = {destination}
            changed = True
            assignments = [node for node in ast.walk(function) if isinstance(node, (ast.Assign, ast.AnnAssign))]
            while changed:
                changed = False
                for assignment in assignments:
                    if assignment.value is None or not _contains_name(assignment.value, names):
                        continue
                    additions = _assigned_names(assignment) - names
                    if additions:
                        names.update(additions)
                        changed = True
            sinks.update(_python_unguarded_redirects(function.body, names))
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


def _rust_line_for_braces(line: str) -> str:
    without_comment = line.split("//", 1)[0]
    return re.sub(r'r?#*"(?:\\.|[^"\\])*"#*', '""', without_comment)


def _rust_function_blocks(diff: str) -> list[list[tuple[int, str, bool]]]:
    """Return Rust functions visible in post-image hunks.

    Context lines are essential here: a PR commonly adds only the filesystem
    sink to an existing handler. The ``added`` bit is retained so context can
    establish provenance without ever reporting an unchanged sink.
    """

    blocks: list[list[tuple[int, str, bool]]] = []
    function_start = re.compile(r"\bfn\s+[A-Za-z_]\w*\b")
    for raw_hunk in _postimage_hunks(diff):
        masked_lines = mask_comments("\n".join(content for _line, content, _added in raw_hunk), "rust").split("\n")
        hunk = [(line_no, masked_lines[index], added) for index, (line_no, _content, added) in enumerate(raw_hunk)]
        current: list[tuple[int, str, bool]] = []
        depth = 0
        saw_open = False
        for line_no, content, added in hunk:
            starts_function = function_start.search(content) is not None
            if not current:
                if not starts_function:
                    continue
                depth = 0
                saw_open = False
            elif starts_function and not saw_open:
                # A truncated/malformed signature must not donate parameters to
                # the next function in the same hunk.
                current = []
                depth = 0
            current.append((line_no, content, added))
            structural = _rust_line_for_braces(content)
            opens = structural.count("{")
            closes = structural.count("}")
            saw_open = saw_open or opens > 0
            depth += opens - closes
            if saw_open and depth <= 0:
                blocks.append(current)
                current = []
                depth = 0
                saw_open = False
        # GitHub hunks can end before the closing brace. The visible partial
        # function is still safe to analyze, but it never spans into a later
        # hunk or function.
        if current and saw_open:
            blocks.append(current)
    return blocks


_RUST_ASSIGNMENT = re.compile(r"\blet(?:\s+mut)?\s+(?P<name>[A-Za-z_]\w*)[^=]*=\s*(?P<expr>.+?);?\s*$")
_RUST_FS_READ_START = re.compile(
    r"\b(?:std::)?fs::(?:read|read_to_string|read_dir)\s*\(",
    re.IGNORECASE,
)
_RUST_AXUM_PATH_BINDING = re.compile(
    r"(?:\b(?:axum\s*::\s*extract|web)\s*::\s*)?\bPath\s*\(\s*(?:mut\s+)?(?P<name>[A-Za-z_]\w*)\s*\)"
    r"\s*:\s*(?:\b(?:axum\s*::\s*extract|web)\s*::\s*)?Path\s*<",
    re.IGNORECASE,
)


def _rust_references(expression: str, names: set[str]) -> bool:
    return any(re.search(rf"\b{re.escape(name)}\b", expression) for name in names)


def _rust_fs_read_calls(source_line: str) -> list[tuple[int, int, str]]:
    """Extract same-line fs read arguments with balanced parentheses."""

    calls: list[tuple[int, int, str]] = []
    for match in _RUST_FS_READ_START.finditer(source_line):
        argument_start = match.end()
        depth = 1
        quote = ""
        escaped = False
        for index in range(argument_start, len(source_line)):
            char = source_line[index]
            if quote:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == quote:
                    quote = ""
                continue
            if char in {'"', "'"}:
                quote = char
            elif char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    calls.append((match.start(), index + 1, source_line[argument_start:index]))
                    break
    return calls


def _rust_function_parameters(block: list[tuple[int, str, bool]]) -> tuple[set[str], set[str]]:
    """Return ordinary parameters and structurally request-derived Axum bindings."""

    body = "\n".join(content for _line, content, _added in block)
    signature = body.split("{", 1)[0]
    request_parameters = {match.group("name") for match in _RUST_AXUM_PATH_BINDING.finditer(signature)}
    ordinary_parameters = {
        match.group("name") for match in re.finditer(r"\b(?P<name>[A-Za-z_]\w*)\s*:\s*(?!:)", signature)
    }
    # The binding inside ``Path(filename): Path<String>`` is not an ordinary
    # library parameter: the extractor itself proves request provenance.
    ordinary_parameters.difference_update(request_parameters)
    return ordinary_parameters, request_parameters


def _rust_function_is_public(block: list[tuple[int, str, bool]]) -> bool:
    body = "\n".join(content for _line, content, _added in block)
    signature = body.split("{", 1)[0]
    return bool(re.search(r"\bpub(?:\s*\([^)]*\))?\s+(?:async\s+)?fn\b", signature))


def _rust_constructs_path_from(expression: str, sources: set[str]) -> bool:
    """Require a source to occupy a path-fragment position, not merely share a line."""

    if not sources:
        return False

    # For ``base.join(fragment)`` only the joined fragment establishes dynamic
    # path construction. A fixed module/local constant therefore stays clean
    # even when the base itself is passed into a helper.
    for match in re.finditer(r"\.join\s*\(\s*(?P<fragment>[^)]*)\)", expression):
        if _rust_references(match.group("fragment"), sources):
            return True

    format_call = re.search(
        r"format!\s*\(\s*[\"'][^\"']*[/\\][^\"']*[\"'](?P<arguments>[^)]*)\)",
        expression,
        re.IGNORECASE,
    )
    if format_call and _rust_references(format_call.group("arguments"), sources):
        return True

    if re.search(r"(?:\+\s*[\"'][/\\]|[\"'][/\\][\"']\s*\+)", expression) and _rust_references(expression, sources):
        return True
    return False


def _rust_has_contextual_path_construction(expression: str) -> bool:
    """Recognize an inline dynamic path when a function signature is outside the diff hunk.

    Without the surrounding signature we cannot prove request provenance, so
    callers must keep this evidence contextual rather than auto-confirming it.
    Literal and conventional all-caps constant fragments stay quiet.
    """

    for match in re.finditer(r"\.join\s*\(\s*(?P<fragment>[^)]*)\)", expression):
        fragment = match.group("fragment").strip()
        if re.fullmatch(r"(?:r?#*)?[\"'].*[\"']#*", fragment):
            continue
        identifiers = re.findall(r"\b[A-Za-z_]\w*\b", fragment)
        if any(not identifier.isupper() for identifier in identifiers):
            return True

    format_call = re.search(
        r"format!\s*\(\s*(?:r#*)?[\"'](?:\\.|[^\"'\\])*[\"']#*\s*,(?P<arguments>.*)\)",
        expression,
        re.IGNORECASE,
    )
    if format_call:
        identifiers = re.findall(r"\b[A-Za-z_]\w*\b", format_call.group("arguments"))
        if any(not identifier.isupper() for identifier in identifiers):
            return True
    return False


def _rust_guard_block_rejects(
    block: list[tuple[int, str, bool]], guard_index: int, sink_index: int, condition_end: int
) -> bool:
    """Require an unconditional terminating statement inside the guard body."""

    direct_body: list[str] = []
    depth = 0
    opened = False
    for index in range(guard_index, sink_index):
        structural = _rust_line_for_braces(block[index][1])
        start = condition_end if index == guard_index else 0
        for char in structural[start:]:
            if char == "{":
                depth += 1
                opened = True
                continue
            if char == "}":
                depth -= 1
                if opened and depth == 0:
                    return bool(
                        re.search(
                            r"\b(?:return|continue|break)\b|\b(?:bail|ensure)\s*!",
                            "".join(direct_body),
                            re.IGNORECASE,
                        )
                    )
                continue
            if opened and depth == 1:
                direct_body.append(char)
        if opened and depth == 1:
            direct_body.append("\n")
    return False


def _rust_sink_has_prior_confinement_guard(
    block: list[tuple[int, str, bool]], sink_index: int, argument: str, sink_start: int
) -> bool:
    """Recognize a guard only for the canonicalized value read by this sink.

    Ordering and variable identity are deliberate: an unrelated validation or
    a check placed after the read cannot make the filesystem operation safe.
    """

    argument_names = set(re.findall(r"\b[A-Za-z_]\w*\b", argument))
    if not argument_names:
        return False

    canonicalized: set[str] = set()
    canonicalized_before: list[set[str]] = []
    for _line_no, content, _added in block:
        canonicalized_before.append(set(canonicalized))
        assignment = _RUST_ASSIGNMENT.search(content)
        if assignment and re.search(r"\.canonicali[sz]e\s*\(", assignment.group("expr"), re.IGNORECASE):
            canonicalized.add(assignment.group("name"))

    depths: list[int] = []
    depth = 0
    for _line_no, content, _added in block:
        depths.append(depth)
        structural = _rust_line_for_braces(content)
        depth += structural.count("{") - structural.count("}")
    sink_depth = depths[sink_index]

    def rejects_bad_candidate(index: int, condition_end: int) -> bool:
        if depths[index] != sink_depth:
            return False
        return _rust_guard_block_rejects(block, index, sink_index, condition_end)

    for index, (_line_no, content, _added) in enumerate(block[:sink_index]):
        negative_prefix = re.search(
            r"\bif\s*!\s*(?P<name>[A-Za-z_]\w*)\s*\.\s*starts_with\s*\(",
            content,
            re.IGNORECASE,
        )
        if negative_prefix is not None:
            guarded_name = negative_prefix.group("name")
            if (
                guarded_name in argument_names
                and guarded_name in canonicalized_before[index]
                and rejects_bad_candidate(index, negative_prefix.end())
            ):
                return True

        failed_strip = re.search(
            r"\bif\s+(?P<name>[A-Za-z_]\w*)\s*\.\s*strip_prefix\s*\([^)]*\)\s*\.\s*is_err\s*\(",
            content,
            re.IGNORECASE,
        )
        if (
            failed_strip is not None
            and failed_strip.group("name") in argument_names
            and failed_strip.group("name") in canonicalized_before[index]
            and rejects_bad_candidate(index, failed_strip.end())
        ):
            return True

    # A read inside the true branch of ``candidate.starts_with(base)`` is
    # dominated by that check. The candidate must still be canonicalized so a
    # lexical ``..`` prefix cannot masquerade as confinement.
    sink_content = block[sink_index][1]
    for index, (_line_no, content, _added) in enumerate(block[: sink_index + 1]):
        positive_prefix = re.search(
            r"\bif\s+(?P<name>[A-Za-z_]\w*)\s*\.\s*starts_with\s*\(",
            content,
            re.IGNORECASE,
        )
        if positive_prefix is None:
            continue
        guarded_name = positive_prefix.group("name")
        if guarded_name not in argument_names or guarded_name not in canonicalized_before[index]:
            continue
        if index == sink_index:
            if positive_prefix.end() < sink_start:
                return True
            continue
        if any(re.search(r"\belse\b", item) for _right, item, _added in block[index + 1 : sink_index + 1]):
            continue
        prefix = sink_content[:sink_start]
        depth_at_sink_call = (
            sink_depth + _rust_line_for_braces(prefix).count("{") - _rust_line_for_braces(prefix).count("}")
        )
        if depth_at_sink_call > depths[index]:
            return True
    return False


def _rust_dynamic_path_sinks(diff: str) -> list[int]:
    """Find fs reads reached through explicit path construction/request data.

    A bare ``fs::read(path)`` is intentionally insufficient: a library path
    parameter has no implied attacker provenance or confinement contract.
    """

    sink_lines: list[int] = []

    for block in _rust_function_blocks(diff):
        ordinary_parameters, request_parameters = _rust_function_parameters(block)
        assignments: list[tuple[str, str]] = []
        for _line_no, content, _added in block:
            match = _RUST_ASSIGNMENT.search(content)
            if match:
                assignments.append((match.group("name"), match.group("expr")))

        request_vars = set(request_parameters)
        dynamic_vars: set[str] = set()
        changed = True
        while changed:
            changed = False
            for name, expression in assignments:
                if name not in request_vars and _rust_references(expression, request_vars):
                    request_vars.add(name)
                    changed = True
                construction_sources = ordinary_parameters | request_vars | dynamic_vars
                if name not in dynamic_vars and (
                    _rust_references(expression, dynamic_vars)
                    or _rust_constructs_path_from(expression, construction_sources)
                ):
                    dynamic_vars.add(name)
                    changed = True

        for sink_index, (line_no, content, added) in enumerate(block):
            if not added:
                continue
            for sink_start, _sink_end, argument in _rust_fs_read_calls(content):
                if (
                    _rust_references(argument, request_vars | dynamic_vars)
                    or _rust_constructs_path_from(argument, ordinary_parameters | request_vars | dynamic_vars)
                ) and not _rust_sink_has_prior_confinement_guard(block, sink_index, argument, sink_start):
                    sink_lines.append(line_no)
                    break
    return sink_lines


def _rust_command_program_expression(content: str) -> str:
    """Return the first same-line ``Command::new`` program expression."""

    match = re.search(r"\bCommand::new\s*\(", content)
    if match is None:
        return ""
    start = match.end()
    depth = 0
    quote = ""
    escaped = False
    for index in range(start, len(content)):
        character = content[index]
        if quote:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                quote = ""
            continue
        if character in {'"', "'"}:
            quote = character
        elif character in "([{":
            depth += 1
        elif character in ")]}":
            if character == ")" and depth == 0:
                return content[start:index].strip()
            depth = max(0, depth - 1)
        elif character == "," and depth == 0:
            return content[start:index].strip()
    return ""


def _rust_dynamic_command_sinks(diff: str) -> list[tuple[int, tuple[str, ...]]]:
    """Find function parameters that directly choose a spawned executable."""

    sinks: list[tuple[int, tuple[str, ...]]] = []
    for block in _rust_function_blocks(diff):
        ordinary_parameters, request_parameters = _rust_function_parameters(block)
        # Ordinary parameters prove an external source only on a public API.
        # Private helpers may be called exclusively with fixed programs. Axum
        # Path bindings retain request provenance regardless of visibility.
        tainted = set(request_parameters)
        if _rust_function_is_public(block):
            tainted.update(ordinary_parameters)
        assignments: list[tuple[str, str]] = []
        for _line_no, content, _added in block:
            assignment = _RUST_ASSIGNMENT.search(content)
            if assignment:
                assignments.append((assignment.group("name"), assignment.group("expr")))
        changed = True
        while changed:
            changed = False
            for name, expression in assignments:
                if name not in tainted and _rust_references(expression, tainted):
                    tainted.add(name)
                    changed = True

        for index, (line_no, content, added) in enumerate(block):
            if not added:
                continue
            if "Command::new" not in content:
                continue
            # Rust formatting commonly places the program expression on the
            # following line. Join only a small contiguous statement window;
            # the balanced extractor stops at Command::new's closing paren.
            statement = "\n".join(row[1] for row in block[index : index + 12])
            expression = _rust_command_program_expression(statement)
            if not expression:
                continue
            sources = tuple(sorted(name for name in tainted if re.search(rf"\b{re.escape(name)}\b", expression)))
            if sources:
                sinks.append((line_no, sources))
    return sinks


def _rust_contextual_path_sinks(diff: str, *, excluded: set[int]) -> list[int]:
    """Find added inline path construction when the enclosing Rust function is not visible."""

    sink_lines: list[int] = []
    code_by_line = _masked_right_line_map(diff, "rust", comments_only=True)
    for line_no, _content in iter_added_lines(diff):
        if line_no in excluded:
            continue
        content = code_by_line.get(line_no, "")
        if any(
            _rust_has_contextual_path_construction(argument) for _start, _end, argument in _rust_fs_read_calls(content)
        ):
            sink_lines.append(line_no)
    return sink_lines


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


_JAVASCRIPT_STATIC_VALUE = re.compile(
    r"(?:\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|`[^`$]*`|null|undefined|true|false|[-+]?\d+(?:\.\d+)?)",
    re.IGNORECASE,
)
_JAVASCRIPT_STRONG_HTML_SOURCE = re.compile(
    r"(?:"
    r"\b(?:req|request)\s*\.\s*(?:body|query|params)\b|"
    r"\bctx\s*\.\s*request\s*\.\s*(?:body|query)\b|"
    r"\b(?:response|res)\s*\.\s*(?:data|body|json)\b|"
    r"\b(?:response|res)\s*\.\s*(?:json|text)\s*\(|"
    r"\blocation\s*\.\s*(?:search|hash)\b|"
    r"\bdocument\s*\.\s*cookie\b|"
    r"\b(?:event|e)\s*\.\s*(?:target|currentTarget)\s*\.\s*value\b|"
    r"\bdocument\s*\.\s*(?:getElementById|querySelector)\s*\([^)]*\)\s*\.\s*value\b|"
    r"\bURLSearchParams\s*\("
    r")",
    re.IGNORECASE,
)


def _javascript_prior_context(diff: str, line_no: int, limit: int = 60) -> str:
    location = _hunk_lines_for_right_line(diff, line_no)
    if location is None:
        return ""
    hunk, index = location
    return "\n".join(content for _line, content, _added in hunk[max(0, index - limit) : index])


def _javascript_latest_assignment(context: str, variable: str) -> str:
    escaped = re.escape(variable)
    matches = list(
        re.finditer(
            rf"(?:^|[;\n])\s*(?:(?:const|let|var)\s+)?{escaped}"
            rf"(?:\s*:\s*[^=;\n]+)?\s*=\s*(?P<rhs>[^;\n]+)",
            context,
            re.IGNORECASE,
        )
    )
    return matches[-1].group("rhs").strip() if matches else ""


def _javascript_storage_value_is_static(diff: str, line_no: int, source_line: str) -> bool:
    """Trace a browser-storage value to a compile-time/logout placeholder."""

    sink = re.search(
        r"\b(?:localStorage|sessionStorage)\.setItem\s*\(\s*[\"'`][^\"'`]+[\"'`]\s*,\s*"
        r"(?P<value>[A-Za-z_$][\w$]*)\s*\)",
        source_line,
        re.IGNORECASE,
    )
    if sink is None:
        return False
    rhs = _javascript_latest_assignment(
        _javascript_prior_context(diff, line_no),
        sink.group("value"),
    )
    return bool(rhs and _JAVASCRIPT_STATIC_VALUE.fullmatch(rhs))


def _javascript_top_level_positions(source: str, targets: set[str]) -> list[int]:
    """Return target delimiters outside nested parameter syntax and strings."""

    depths = {"(": 0, "[": 0, "{": 0, "<": 0}
    pairs = {")": "(", "]": "[", "}": "{", ">": "<"}
    positions: list[int] = []
    quote = ""
    escaped = False
    for index, char in enumerate(source):
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            continue
        if char in {'"', "'", "`"}:
            quote = char
        elif char in depths:
            depths[char] += 1
        elif char in pairs:
            opener = pairs[char]
            depths[opener] = max(0, depths[opener] - 1)
        elif not any(depths.values()) and char in targets:
            positions.append(index)
    return positions


def _javascript_parameter_fragment(parameter_source: str, name: str) -> str:
    """Return the top-level parameter fragment that binds ``name``."""

    commas = _javascript_top_level_positions(parameter_source, {","})
    starts = [0, *(position + 1 for position in commas)]
    ends = [*commas, len(parameter_source)]
    for start, end in zip(starts, ends, strict=True):
        fragment = parameter_source[start:end]
        boundaries = _javascript_top_level_positions(fragment, {":", "="})
        binding = fragment[: boundaries[0]] if boundaries else fragment
        if re.search(rf"\b{re.escape(name)}\b", binding):
            return fragment
    return ""


def _javascript_parameter_is_defaulted(fragment: str, name: str) -> bool:
    """Return whether the target binding or its whole fragment has a default."""

    if _javascript_top_level_positions(fragment, {"="}):
        return True
    return bool(re.search(rf"\b{re.escape(name)}\b\s*=", fragment))


def _javascript_html_is_function_parameter(diff: str, line_no: int, expression: str) -> bool:
    """Prove that a raw-HTML value is an explicit parameter of the active function."""

    location = _hunk_lines_for_right_line(diff, line_no)
    if location is None:
        return False
    hunk, index = location
    context = "\n".join(content for _line, content, _added in hunk[max(0, index - 16) : index + 1])
    context = mask_comments(context, "typescript")
    sink_offset = context.rfind("dangerouslySetInnerHTML")
    if sink_offset < 0:
        return False
    prefix = context[:sink_offset]
    signatures = [
        *re.finditer(
            r"\bfunction\b[^\n(]*\((?P<params>.*?)\)\s*(?:\:[^={\n]+)?\s*\{",
            prefix,
            re.DOTALL,
        ),
        *re.finditer(
            r"(?:^|[;\n])\s*(?:export\s+)?(?:const|let|var)\s+[A-Za-z_$][\w$]*\s*=\s*"
            r"\((?P<params>.*?)\)\s*(?:\:[^=\n]+)?=>\s*\{",
            prefix,
            re.DOTALL,
        ),
    ]
    if not signatures:
        return False
    signature = max(signatures, key=lambda match: match.start())
    structural_body = re.sub(
        r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|`(?:\\.|[^`\\])*`', "", prefix[signature.end() - 1 :]
    )
    if structural_body.count("{") <= structural_body.count("}"):
        return False
    parameter_source = signature.group("params")
    root = expression.split(".", 1)[0]
    parameter_fragment = _javascript_parameter_fragment(parameter_source, root)
    if not parameter_fragment:
        return False

    # Defaults can be arbitrarily nested expressions and a lexical signature
    # is not a full JavaScript parser. Keep every defaulted parameter
    # contextual instead of trying to prove that the initializer is dynamic.
    if _javascript_parameter_is_defaulted(parameter_fragment, root):
        return False

    # Only an exact branded safe type is a safety contract. Substring checks
    # would incorrectly trust names such as UntrustedHTML, and a union with
    # string/any/unknown does not preserve the contract.
    type_matches = re.findall(
        rf"\b{re.escape(root)}\b\s*\??\s*:\s*(?P<type>[A-Za-z_$][\w$]*"
        rf"(?:\s*\.\s*[A-Za-z_$][\w$]*)*(?:\s*<[^,}}]+>)?)\s*(?=[,}}]|$)",
        parameter_fragment,
    )
    alias = re.search(rf"\b(?P<property>[A-Za-z_$][\w$]*)\s*:\s*{re.escape(root)}\b", parameter_fragment)
    if alias is not None:
        type_matches.extend(
            re.findall(
                rf"\b{re.escape(alias.group('property'))}\b\s*\??\s*:\s*"
                rf"(?P<type>[A-Za-z_$][\w$]*(?:\s*\.\s*[A-Za-z_$][\w$]*)*"
                rf"(?:\s*<[^,}}]+>)?)\s*(?=[,}}]|$)",
                parameter_fragment,
            )
        )
    if any(
        re.fullmatch(
            r"(?:(?:globalThis|window)\.)?(?:TrustedHTML|SafeHTML|SafeHtml|SanitizedHTML|SanitizedHtml)",
            re.sub(r"\s+", "", item),
        )
        for item in type_matches
    ):
        return False
    return True


def _javascript_html_has_strong_source(diff: str, line_no: int, source_line: str) -> bool:
    """Require explicit request/DOM/network provenance before XSS auto-confirm."""

    sink = re.search(
        r"\b__html\s*:\s*(?P<value>[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*)\b",
        source_line,
    )
    if sink is None:
        return False
    expression = sink.group("value")
    if re.search(r"(?:^|\.)(?:safe|trusted|saniti[sz]ed|clean)(?:Html|HTML|Markup)?$", expression, re.IGNORECASE):
        return False
    if _JAVASCRIPT_STRONG_HTML_SOURCE.search(expression):
        return True
    if _javascript_html_is_function_parameter(diff, line_no, expression):
        return True

    context = _javascript_prior_context(diff, line_no)
    current = expression.split(".", 1)[0]
    seen: set[str] = set()
    for _depth in range(4):
        if not re.fullmatch(r"[A-Za-z_$][\w$]*", current) or current in seen:
            return False
        seen.add(current)
        rhs = _javascript_latest_assignment(context, current)
        if not rhs or _JAVASCRIPT_STATIC_VALUE.fullmatch(rhs):
            return False
        if re.match(
            r"(?:(?:DOMPurify\s*\.\s*)?(?:sanitize|purify)|"
            r"(?:sanitize|escape|purify|clean)(?:Html|HTML|Markup))\s*\(",
            rhs,
            re.IGNORECASE,
        ):
            return False
        if _JAVASCRIPT_STRONG_HTML_SOURCE.search(rhs):
            return True
        alias = re.fullmatch(r"(?P<name>[A-Za-z_$][\w$]*)", rhs)
        if alias is None:
            return False
        current = alias.group("name")
    return False


def _ruby_backtick_interpolation_is_escaped(diff: str, line_no: int, source_line: str) -> bool:
    """Return true when every interpolation is escaped or structurally scalar.

    Numeric/scalar interpolation cannot introduce shell metacharacters.  As
    with explicit Shellwords escaping, retain only the lower-confidence generic
    command finding so contextual review can still challenge the assumption.
    """

    expressions = re.findall(r"#\{(?P<expression>[^}]+)\}", source_line)
    location = _hunk_lines_for_right_line(diff, line_no)
    if not expressions or location is None:
        return False
    hunk, index = location
    context = "\n".join(content for _line, content, _added in hunk[max(0, index - 40) : index])

    for expression in expressions:
        if re.fullmatch(r"\s*Shellwords\s*\.\s*(?:escape|shellescape)\s*\([^)]*\)\s*", expression):
            continue
        if re.fullmatch(
            r"\s*(?:[-+]?\d+(?:\.\d+)?|true|false|nil|:[A-Za-z_]\w*[!?=]?|"
            r"[A-Za-z_]\w*\.to_i|\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*')\s*",
            expression,
        ):
            continue
        variable = re.fullmatch(r"\s*(?P<name>[A-Za-z_]\w*)\s*", expression)
        if variable is None:
            return False
        escaped = re.escape(variable.group("name"))
        safe_assignment = re.search(
            rf"(?:^|[;\n])\s*{escaped}\s*=\s*(?:"
            rf"Shellwords\s*\.\s*(?:escape|shellescape)\s*\(|"
            rf"[-+]?\d+(?:\.\d+)?\s*(?:$|[;\n])|"
            rf"(?:true|false|nil)\s*(?:$|[;\n])|"
            rf":[A-Za-z_]\w*[!?=]?\s*(?:$|[;\n])|"
            rf"[A-Za-z_]\w*\.to_i\s*(?:$|[;\n])|"
            rf"[\"'][A-Za-z0-9_./:@+-]*[\"']\s*(?:$|[;\n]))",
            context,
            re.IGNORECASE | re.MULTILINE,
        )
        if safe_assignment is None:
            return False
    return True


def _java_runtime_exec_is_static(source_line: str) -> bool:
    """Return true when Runtime.exec receives only fixed string literals."""

    match = re.search(
        r"\bRuntime\.getRuntime\(\)\.exec\(\s*(?P<argument>.*?)\s*\)\s*;?\s*$",
        source_line,
    )
    if match is None:
        return False
    argument = match.group("argument").strip()
    if re.fullmatch(r'"(?:\\.|[^"\\])*"', argument):
        return True
    array = re.fullmatch(r"new\s+String\s*\[\s*]\s*\{(?P<items>.*)}", argument)
    if array is None:
        return False
    items = [item.strip() for item in array.group("items").split(",")]
    return bool(items) and all(re.fullmatch(r'"(?:\\.|[^"\\])*"', item) for item in items)


def detect_security_findings(diffs: dict[str, str]) -> list[DetectorFinding]:
    """Scan modified file diffs for deterministic security findings."""

    findings: list[DetectorFinding] = []

    for file_path, diff in diffs.items():
        language = normalize_language(file_path)
        code_by_line = _masked_right_line_map(diff, language)
        comments_by_line = _masked_right_line_map(diff, language, comments_only=True)
        ruby_commands_by_line = (
            _masked_right_line_map(diff, language, preserve_ruby_commands=True) if language == "ruby" else {}
        )
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
                if not _match_starts_in_mask(line_no, match, comments_by_line):
                    continue
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
                template_expression = language in {"javascript", "typescript", "vue", "svelte"} and (
                    _template_expression_match_is_code(match.string, match.start())
                )
                ruby_command = rule.category == "command-injection" and _match_starts_in_mask(
                    line_no,
                    match,
                    ruby_commands_by_line,
                )
                if (
                    not _match_starts_in_mask(line_no, match, code_by_line)
                    and not template_expression
                    and not ruby_command
                ):
                    continue
                if language == "python" and _ignored_span_at(line_no, match.start(), python_ignored) is not None:
                    continue
                if _is_import_only(match.string):
                    continue
                svelte_raw_html_directive = (
                    language == "svelte" and rule.category == "xss" and match.group(0).lstrip().startswith("{@html")
                )
                if (
                    language in {"javascript", "typescript", "vue", "svelte"}
                    and not svelte_raw_html_directive
                    and _starts_in_markup_text(match.string, match.start())
                ):
                    continue
                if rule.category == "code-injection" and _is_dynamic_code_declaration(language, match.string):
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
                if (
                    language in {"javascript", "typescript"}
                    and rule.confidence >= 0.96
                    and rule.category == "xss"
                    and not _javascript_html_has_strong_source(diff, line_no, match.string)
                ):
                    continue
                if (
                    language in {"javascript", "typescript"}
                    and rule.confidence >= 0.96
                    and rule.category == "data-leak"
                    and _javascript_storage_value_is_static(diff, line_no, match.string)
                ):
                    continue
                if (
                    language == "ruby"
                    and rule.confidence >= 0.96
                    and rule.category == "command-injection"
                    and _ruby_backtick_interpolation_is_escaped(diff, line_no, match.string)
                ):
                    continue
                if (
                    language in {"javascript", "typescript", "vue", "svelte"}
                    and rule.category == "code-injection"
                    and _is_static_javascript_eval(match.string, match.start())
                ):
                    continue
                if (
                    language == "java"
                    and rule.category == "command-injection"
                    and _java_runtime_exec_is_static(match.string)
                ):
                    continue
                # Rust's Command API does not invoke a shell.  A fixed program
                # with dynamic argv is therefore not command injection.  The
                # relational pass below reports only when a function parameter
                # itself selects the executable.
                if language == "rust" and rule.category == "command-injection":
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
                        message="An unvalidated destination reaches a framework redirect response.",
                        suggestion=(
                            "Allow-list local destinations or validate scheme, host, and origin before redirecting."
                        ),
                        confidence=_detector_confidence(file_path, 0.96),
                    )
                )
        elif language == "rust":
            for line_no, sources in _rust_dynamic_command_sinks(diff):
                source_text = ", ".join(f"`{source}`" for source in sources)
                findings.append(
                    DetectorFinding(
                        file=file_path,
                        line=line_no,
                        severity="error",
                        category="command-injection",
                        message=(
                            f"Function parameter {source_text} directly selects the executable passed to Command::new."
                        ),
                        suggestion=(
                            "Map an allow-listed operation name to fixed executable paths; do not let callers choose "
                            "the spawned program."
                        ),
                        confidence=_detector_confidence(file_path, 0.97),
                    )
                )
            rust_path_lines = set(_rust_dynamic_path_sinks(diff))
            for line_no in sorted(rust_path_lines):
                findings.append(
                    DetectorFinding(
                        file=file_path,
                        line=line_no,
                        severity="error",
                        category="path-traversal",
                        message=(
                            "A function/request parameter is interpolated or joined into this path and reaches "
                            "a Rust filesystem read without a canonical containment guard."
                        ),
                        suggestion="Resolve the path under an intended root and reject candidates that escape it.",
                        confidence=_detector_confidence(file_path, 0.96),
                    )
                )
            for line_no in _rust_contextual_path_sinks(diff, excluded=rust_path_lines):
                findings.append(
                    DetectorFinding(
                        file=file_path,
                        line=line_no,
                        severity="warning",
                        category="path-traversal",
                        message=(
                            "An inline dynamic path reaches a Rust filesystem read "
                            "without visible confinement evidence."
                        ),
                        suggestion="Resolve the path under an intended root and reject candidates that escape it.",
                        confidence=_detector_confidence(file_path, 0.90),
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


def is_deterministic_security_finding(file_path: str, line: int, category: str, diff: str) -> bool:
    """Security regex/data-flow candidates always require independent calibration."""

    del file_path, line, category, diff
    return False


def normalize_language(file_path: str) -> str:
    """Normalize file extension to detector language key."""

    lower = (file_path or "").lower()
    if lower.endswith(".vue"):
        return "vue"
    if lower.endswith(".svelte"):
        return "svelte"
    return detect_language(file_path) or "unknown"
