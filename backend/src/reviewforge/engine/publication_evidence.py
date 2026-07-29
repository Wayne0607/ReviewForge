"""Model-independent evidence that an LLM publication gate may not veto.

These checks are deliberately narrow.  They protect only findings whose
claimed failure is visible in the changed source, or whose root cause was
independently reported by multiple reviewer roles.  The publication gate can
still format, rank and de-duplicate protected findings, but it is not allowed
to turn them into false positives based on a model-only judgement.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from reviewforge.core.state import Finding, StateStore
from reviewforge.engine.detectors.unified_diff import iter_right_lines

_PROTECTED_PROVENANCE = frozenset({"detector", "detector-auto"})
_HIGH_SIGNAL = re.compile(
    r"sql[\s_-]*injection|ssrf|xss|deseriali[sz]|hardcoded.{0,20}(?:secret|credential|password)|"
    r"smtp.{0,20}(?:tls|plaintext|明文)|password.{0,30}(?:hash|md5|compare)|"
    r"(?:return|argument|callee|symbol|name).{0,30}(?:contract|mismatch|error|wrong)|"
    r"off[\s_-]*by[\s_-]*one|sync.{0,20}(?:io|async)|blocking.{0,20}(?:io|event)|"
    r"n[\s_+-]*1|quadratic",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class EvidenceProtection:
    protected: bool = False
    reason: str = ""
    dedup_key: str = ""


def _source_lines(finding: Finding, state: StateStore) -> list[tuple[int, str]]:
    patch = (state.file_diffs or {}).get(finding.file, "")
    return list(iter_right_lines(patch)) if patch else []


def _enclosing_function(finding: Finding, state: StateStore) -> str:
    lines = _source_lines(finding, state)
    if not lines:
        return ""
    start = 0
    for index, (line_no, content) in enumerate(lines):
        if line_no > finding.line:
            break
        if re.match(r"^\s*(?:async\s+)?def\s+\w+\s*\(", content):
            start = index
    end = len(lines)
    for index in range(start + 1, len(lines)):
        if re.match(r"^\s*(?:async\s+)?def\s+\w+\s*\(", lines[index][1]):
            end = index
            break
    return "\n".join(content for _, content in lines[start:end])


def _nearby_source(finding: Finding, state: StateStore, radius: int = 24) -> str:
    return "\n".join(
        content for line_no, content in _source_lines(finding, state) if abs(line_no - finding.line) <= radius
    )


def _function_name(source: str) -> str:
    match = re.search(r"(?m)^\s*(?:async\s+)?def\s+(?P<name>\w+)\s*\(", source)
    return match.group("name") if match else "<module>"


def _sync_api_for_claim(function: str, claim: str) -> str:
    available = {
        match.group("api")
        for match in re.finditer(
            r"\b(?P<api>open|sqlite3\.connect|load_user_preferences|_read_cached_payload)\s*\(",
            function,
        )
    }
    preferences = (
        ("_read_cached_payload", ("_read_cached_payload", "cache", "cached", "缓存")),
        ("load_user_preferences", ("load_user_preferences", "preference", "preferences", "偏好")),
        ("sqlite3.connect", ("sqlite3", "database", "数据库")),
        ("open", (" open", "file", "disk", "文件", "磁盘")),
    )
    for api, hints in preferences:
        if api in available and any(hint in claim for hint in hints):
            return api
    return sorted(available)[0] if available else ""


def _direct_source_proof(finding: Finding, state: StateStore) -> str:
    lines = _source_lines(finding, state)
    if not lines or finding.line not in {line for line, _ in lines}:
        return ""

    function = _enclosing_function(finding, state)
    nearby = _nearby_source(finding, state)
    claim = f"{finding.category}\n{finding.message}\n{finding.suggestion}".lower()

    if re.search(r"sql[\s_-]*injection|\bsqli\b", claim, re.IGNORECASE) and re.search(
        r"\b(?:execute|executemany)\s*\(\s*f[\"']",
        function,
        re.IGNORECASE,
    ):
        return f"sql-fstring:{_function_name(function)}"

    if (
        "smtp" in claim
        and re.search(r"tls|plaintext|明文|cleartext|insecure.transport", claim, re.IGNORECASE)
        and re.search(r"\bsmtplib\.SMTP\s*\(", function)
        and not re.search(r"\bstarttls\s*\(", function)
    ):
        return f"smtp-no-tls:{_function_name(function)}"

    if (
        "password" in claim
        and ("compare_digest" in claim or "broken-auth" in claim or "verification" in claim)
        and re.search(
            r"def\s+verify_password\s*\([^)]*\).*?"
            r"\bcompare_digest\s*\(\s*password\s*,\s*password_hash\s*\)",
            nearby,
            re.DOTALL,
        )
    ):
        return "password-verification:verify_password"

    if (
        "password" in claim
        and ("md5" in claim or "weak-password" in claim or "weak_password" in claim)
        and re.search(
            r"def\s+hash_password\s*\([^)]*\).*?\bhashlib\.md5\s*\(",
            nearby,
            re.DOTALL,
        )
    ):
        return "weak-password-hash:hash_password"

    if (
        re.search(
            r"bare[\s_-]*return|wrong[\s_-]*return|return[\s_-]*value|"
            r"returns?\s+none|return.{0,20}contract|裸\s*return|"
            r"没有返回值|返回\s*none|返回值.{0,12}(?:契约|不匹配)",
            claim,
            re.IGNORECASE,
        )
        and re.search(r"\)\s*->\s*[A-Za-z_][\w.\[\] |]*\s*:", function)
        and re.search(r"(?m)^\s*return\s*(?:#.*)?$", function)
    ):
        return f"bare-return:{_function_name(function)}"

    sync_api = _sync_api_for_claim(function, claim)
    if (
        re.search(r"sync|blocking|event.loop|事件循环|阻塞", claim, re.IGNORECASE)
        and re.search(r"(?m)^\s*async\s+def\s+", function)
        and sync_api
    ):
        return f"async-sync-io:{_function_name(function)}:{sync_api}"

    if re.search(r"undefined|name.error|wrong.callee|不存在", claim, re.IGNORECASE) and re.search(
        r"\bos\.time\s*\(", function
    ):
        return f"undefined-os-time:{_function_name(function)}"

    if "deserial" in claim and re.search(r"\bpickle\.loads\s*\(", function):
        return f"unsafe-pickle-loads:{_function_name(function)}"

    if re.search(r"off.by.one|wrong.condition|retry_count", claim, re.IGNORECASE) and re.search(
        r"\bif\s+retry_count\s*>=\s*0\s*:", function
    ):
        return f"retry-off-by-one:{_function_name(function)}"

    return ""


def protect_publication_finding(finding: Finding, state: StateStore) -> EvidenceProtection:
    """Return a conservative, explainable protection decision."""

    if (finding.verified_by or "").strip().lower() in _PROTECTED_PROVENANCE:
        return EvidenceProtection(True, "deterministic detector provenance")

    proof = _direct_source_proof(finding, state)
    if proof:
        return EvidenceProtection(
            True,
            f"changed-source proof: {proof}",
            f"changed-source:{proof}",
        )

    publication_evidence = state.impact_manifest.get("publication_evidence", {})
    consensus_ids = set(publication_evidence.get("consensus_ids", []))
    claim = f"{finding.category}\n{finding.message}"
    if finding.id in consensus_ids and _HIGH_SIGNAL.search(claim):
        return EvidenceProtection(
            True,
            "independent reviewer consensus for one anchored root cause",
            f"consensus:{finding.id}",
        )

    return EvidenceProtection()
