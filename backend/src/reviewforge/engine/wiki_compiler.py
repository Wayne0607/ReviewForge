"""Compile revision-anchored repository wiki pages from source code.

The compiler deliberately avoids LLM-authored summaries.  It extracts a small
set of contract and state facts verbatim from source, records their line
anchors, and lets the Context Engine retrieve only pages relevant to the
changed symbols.  Reviewers must still verify those facts against raw code.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import Any

_IDENTIFIER = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]{2,}")
_GUARD = re.compile(
    r"\b(?:if|unless|when|guard|require|assert)\b|"
    r"(?:==|!=|===|!==)\s*(?:null|nil|none|false|true)\b",
    re.IGNORECASE,
)
_RETURN = re.compile(r"^\s*(?:return|yield|raise|throw)\b", re.IGNORECASE)
_SIDE_EFFECT = re.compile(
    r"\b(?:save|create|insert|update|delete|remove|send|publish|enqueue|commit|rollback|"
    r"set|write|exec|execute|exit|shutdown|invalidate|cache)\w*\s*\(",
    re.IGNORECASE,
)
_ASYNC_STATE = re.compile(
    r"\b(?:await|async|goroutine|thread|spawn|lock|mutex|atomic|concurrent|promise|future)\b",
    re.IGNORECASE,
)
_DATA_SHAPE = re.compile(
    r"\b(?:json|serialize|deserialize|schema|parse|response|payload|config|metadata|credential|token)\b",
    re.IGNORECASE,
)
_SECURITY = re.compile(
    r"\b(?:auth|permission|scope|role|admin|secret|password|token|redirect|origin|sanitize|escape)\w*\b",
    re.IGNORECASE,
)
_STOP_TERMS = {
    "and",
    "async",
    "await",
    "class",
    "const",
    "def",
    "else",
    "false",
    "from",
    "func",
    "function",
    "import",
    "none",
    "null",
    "return",
    "self",
    "this",
    "true",
}
_MAX_FACTS_PER_PAGE = 8
_MAX_EVIDENCE_CHARS = 220


@dataclass(frozen=True)
class WikiPage:
    page_key: str
    kind: str
    title: str
    content: dict[str, Any]
    search_terms: list[str]
    source_path: str
    source_sha: str
    source_start: int
    source_end: int
    content_hash: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def compile_symbol_pages(
    *,
    path: str,
    language: str,
    content: str,
    changed_symbols: list[dict[str, Any]],
    source_sha: str,
    focus_terms: list[str] | None = None,
) -> list[WikiPage]:
    """Create compact contract pages for changed symbols in one source file."""

    if not content or not changed_symbols:
        return []
    lines = content.splitlines()
    pages: list[WikiPage] = []
    for symbol in changed_symbols:
        name = str(symbol.get("name", "")).strip()
        if not name:
            continue
        start = max(int(symbol.get("start_line") or symbol.get("line") or 1), 1)
        end = min(max(int(symbol.get("end_line") or start), start), len(lines))
        block = list(enumerate(lines[start - 1 : end], start=start))
        facts = _extract_facts(block, focus_terms or [])
        if not facts:
            continue
        terms = _search_terms(name, facts)
        page_content = {
            "language": language,
            "symbol_type": str(symbol.get("type", "symbol")),
            "facts": facts,
            "verification": "Verify every fact against the cited source revision before reporting a finding.",
        }
        canonical = json.dumps(page_content, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        pages.append(
            WikiPage(
                page_key=f"symbol:{path}:{name}",
                kind="symbol-contract",
                title=name,
                content=page_content,
                search_terms=terms,
                source_path=path,
                source_sha=source_sha,
                source_start=start,
                source_end=end,
                content_hash=hashlib.sha256(canonical.encode()).hexdigest(),
            )
        )
    return pages


def render_wiki_pages(pages: list[dict[str, Any]], *, max_chars: int = 4_000) -> list[dict[str, Any]]:
    """Return a bounded prompt representation without persistence metadata."""

    rendered: list[dict[str, Any]] = []
    used = 2
    for page in pages:
        candidate = {
            "title": page.get("title", ""),
            "kind": page.get("kind", ""),
            "source": {
                "path": page.get("source_path", ""),
                "sha": page.get("source_sha", ""),
                "start": page.get("source_start", 0),
                "end": page.get("source_end", 0),
            },
            "facts": page.get("content", {}).get("facts", []),
            "retrieval_score": page.get("retrieval_score", 0),
        }
        size = len(json.dumps(candidate, ensure_ascii=False, separators=(",", ":"))) + 1
        if used + size > max_chars:
            break
        rendered.append(candidate)
        used += size
    return rendered


def _extract_facts(block: list[tuple[int, str]], focus_terms: list[str]) -> list[dict[str, Any]]:
    if not block:
        return []
    facts: list[dict[str, Any]] = []
    signature_line, signature = next(((line, text) for line, text in block if text.strip()), block[0])
    _append_fact(facts, "signature", signature_line, signature)
    for term in focus_terms[:4]:
        pattern = re.compile(rf"\b{re.escape(term)}\s*\(")
        for line, text in block:
            if pattern.search(text):
                _append_fact(facts, "related-call", line, text)
                break
    matchers = (
        ("guard", _GUARD, 2),
        ("return-or-error", _RETURN, 2),
        ("side-effect", _SIDE_EFFECT, 2),
        ("async-state", _ASYNC_STATE, 1),
        ("data-shape", _DATA_SHAPE, 1),
        ("security-boundary", _SECURITY, 1),
    )
    for kind, pattern, limit in matchers:
        matched = 0
        for line, text in block:
            if line == signature_line or not pattern.search(text):
                continue
            _append_fact(facts, kind, line, text)
            matched += 1
            if matched >= limit or len(facts) >= _MAX_FACTS_PER_PAGE:
                break
        if len(facts) >= _MAX_FACTS_PER_PAGE:
            break
    return facts


def _append_fact(facts: list[dict[str, Any]], kind: str, line: int, evidence: str) -> None:
    normalized = " ".join(evidence.strip().split())[:_MAX_EVIDENCE_CHARS]
    if not normalized or any(
        item["kind"] == kind and item["line"] == line and item["evidence"] == normalized for item in facts
    ):
        return
    facts.append({"kind": kind, "line": line, "evidence": normalized})


def _search_terms(symbol: str, facts: list[dict[str, Any]]) -> list[str]:
    terms = [symbol]
    for fact in facts:
        for identifier in _IDENTIFIER.findall(str(fact.get("evidence", ""))):
            if identifier.lower() in _STOP_TERMS or identifier in terms:
                continue
            terms.append(identifier)
            if len(terms) >= 20:
                return terms
    return terms
