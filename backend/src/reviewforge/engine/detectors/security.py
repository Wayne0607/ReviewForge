"""Deterministic high-signal security detectors for core languages."""

from __future__ import annotations

from dataclasses import dataclass

from reviewforge.engine.detectors.base import (
    DetectorFinding,
    dedupe_findings,
    match_lines,
    normalize_category_for_detector,
    safe_confidence,
)
from reviewforge.engine.symbol_extractor import detect_language


@dataclass(frozen=True)
class _Rule:
    pattern: str
    category: str
    severity: str
    message: str
    suggestion: str
    confidence: float


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
        0.9,
    ),
]


_SECURITY_RULES: dict[str, list[_Rule]] = {
    "python": [
        _Rule(
            r"\b(?:os\.(?:system|popen)|subprocess\.(?:call|run|Popen|check_output|check_call))\b",
            "command-injection",
            "error",
            "Command execution API used.",
            "Avoid shell execution or strictly validate and quote every argument.",
            0.95,
        ),
        _Rule(
            r"\b(?:eval|exec)\s*\(",
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
            r"\bopen\([^)]*(?:\.\.|request|user|input|query|path)",
            "path-traversal",
            "warning",
            "Potential path join from user-driven fragments.",
            "Validate and normalize user paths before file access.",
            0.84,
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
            "xss-bypass",
            "warning",
            "Potentially unsafe DOM rendering call.",
            "Sanitize user HTML before rendering.",
            0.9,
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
            r"\[innerHTML\]",
            "xss",
            "warning",
            "Angular innerHTML binding detected.",
            "Sanitize untrusted HTML before binding.",
            0.91,
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
            "xss-bypass",
            "warning",
            "Potentially unsafe DOM rendering call.",
            "Sanitize untrusted markup before render.",
            0.9,
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
            "xss-bypass",
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
            r"\beval\s*\(",
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
            "Limit unsafe scope and add safety assertions.",
            0.72,
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
            r"\bunwrap\(\)",
            "unsafe-usage",
            "warning",
            "unwrap usage in security-sensitive code.",
            "Propagate errors instead of unwrap.",
            0.52,
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
            "xss-bypass",
            "warning",
            "Dynamic component selection from data can expand attack surface.",
            "Allow-list component names before rendering.",
            0.82,
        ),
        _Rule(
            r"\bwindow\.location(?:\.href)?\s*=",
            "open-redirect",
            "warning",
            "Redirect target is assigned dynamically.",
            "Validate redirect destinations against an allow-list.",
            0.86,
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


def detect_security_findings(diffs: dict[str, str]) -> list[DetectorFinding]:
    """Scan modified file diffs for deterministic security findings."""

    findings: list[DetectorFinding] = []

    for file_path, diff in diffs.items():
        language = normalize_language(file_path)
        rules = _rules_for_language(language)

        for rule in _UNIVERSAL_RULES:
            matches = match_lines(diff, rule.pattern)
            for line_no, _ in matches:
                findings.append(
                    DetectorFinding(
                        file=file_path,
                        line=line_no,
                        severity=rule.severity,
                        category=normalize_category_for_detector(rule.category),
                        message=rule.message,
                        suggestion=rule.suggestion,
                        confidence=safe_confidence(rule.confidence, len(matches)),
                    )
                )

        for rule in rules:
            for line_no, _ in match_lines(diff, rule.pattern):
                findings.append(
                    DetectorFinding(
                        file=file_path,
                        line=line_no,
                        severity=rule.severity,
                        category=normalize_category_for_detector(rule.category),
                        message=rule.message,
                        suggestion=rule.suggestion,
                        confidence=safe_confidence(rule.confidence, 1),
                    )
                )

    return dedupe_findings(findings)


def normalize_language(file_path: str) -> str:
    """Normalize file extension to detector language key."""

    lower = (file_path or "").lower()
    if lower.endswith(".vue"):
        return "vue"
    if lower.endswith(".svelte"):
        return "svelte"
    return detect_language(file_path) or "unknown"
