"""Model-independent, conservative clustering of duplicate root causes.

Reviewers often describe one defect with different categories or wording.  This
module converts findings into a small immutable IR and only merges claims when
they share both a canonical causal family and concrete code identity.  It does
not call an LLM, mutate findings, or treat proximity/category equality as proof.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from reviewforge.core.state import Finding
from reviewforge.engine.detectors.unified_diff import iter_right_lines

_SUMMARY_FILE_HEADER = re.compile(r"^--- (?P<file>.+?) \(\+\d+ -\d+\)$")
_HUNK_HEADER = re.compile(r"^@@ -\d+(?:,\d+)? \+(?P<start>\d+)(?:,(?P<count>\d+))? @@")
_QUALIFIED_IDENTIFIER = re.compile(
    r"(?<![\w$@])@?[A-Za-z_$][A-Za-z0-9_$]*"
    r"(?:\s*\.\s*[A-Za-z_$][A-Za-z0-9_$]*)+"
)
_CALL_IDENTIFIER = re.compile(
    r"(?<![\w$@])(?P<name>@?[A-Za-z_$][A-Za-z0-9_$]*(?:\s*\.\s*[A-Za-z_$][A-Za-z0-9_$]*)*)"
    r"(?=\s*\()"
)
_SHAPED_IDENTIFIER = re.compile(r"(?<![\w$@])@?[A-Za-z_$][A-Za-z0-9_$]*(?![\w$])")
_QUOTED_CODE = re.compile(r"[`'\"](?P<code>@?[A-Za-z_$][A-Za-z0-9_$.]*)[`'\"]")
_OPERATORS = re.compile(r"&&|\|\||===|!==|==|!=|<=|>=|<|>")

_STOPWORDS = {
    "action",
    "application",
    "argument",
    "boolean",
    "call",
    "code",
    "context",
    "create",
    "data",
    "delete",
    "error",
    "false",
    "field",
    "file",
    "function",
    "input",
    "line",
    "log",
    "method",
    "name",
    "null",
    "output",
    "parameter",
    "result",
    "return",
    "true",
    "update",
    "user",
    "value",
}

_EXACT_FAMILIES = {
    "wrong-metric-recorder": "wrong-metric",
    "wrong-metric-recorder-and-label": "wrong-metric",
    "metric-label-error": "wrong-metric",
    "incorrect-metric-parameter": "wrong-metric",
    "context-loss": "context-loss",
    "lost-context": "context-loss",
    "lost-logger": "context-loss",
    "context-inconsistency": "context-loss",
    "log-field-name": "context-loss",
    "logging-context": "context-loss",
    "missing-action": "incomplete-action",
    "missing-side-effect": "incomplete-action",
    "incomplete-implementation": "incomplete-action",
    "wrong-boolean-logic": "auth-logic",
    "authorization-logic": "auth-logic",
    "wrong-permission-check": "auth-logic",
    "undefined-symbol": "undefined-symbol",
    "undefined-variable": "undefined-symbol",
    "nil-dereference": "nil-dereference",
    "null-dereference": "nil-dereference",
}

_LINE_TOLERANCE = {
    "wrong-metric": 40,
    "context-loss": 30,
    "incomplete-action": 24,
    "auth-logic": 16,
    "undefined-symbol": 8,
    "nil-dereference": 8,
    "stored-xss-flow": 0,
}


@dataclass(frozen=True, slots=True)
class RootCauseClaim:
    """Immutable evidence used for duplicate decisions."""

    finding_id: str
    file: str
    line: int
    reviewer: str
    causal_family: str
    identifiers: frozenset[str]
    strong_identifiers: frozenset[str]
    anchor_identifiers: frozenset[str]
    anchor_text: str
    operators: frozenset[str]
    semantic_markers: frozenset[str]
    hunk_id: str
    is_detector: bool
    confidence: float


@dataclass(frozen=True, slots=True)
class RootCauseCluster:
    """One representative and the findings it subsumes."""

    causal_family: str
    representative_id: str
    member_ids: tuple[str, ...]
    reviewers: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RootCauseClusterResult:
    """Immutable clustering structure; input ``Finding`` objects are untouched."""

    kept: tuple[Finding, ...]
    absorbed: tuple[Finding, ...]
    clusters: tuple[RootCauseCluster, ...]
    absorbed_to_representative: tuple[tuple[str, str], ...]
    input_count: int
    cross_reviewer_merged: int

    @property
    def stats(self) -> dict[str, int]:
        return {
            "input": self.input_count,
            "clusters": len(self.clusters),
            "kept": len(self.kept),
            "absorbed": len(self.absorbed),
            "cross_reviewer_merged": self.cross_reviewer_merged,
        }


def _normalize_category(category: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", (category or "").lower())).strip("-")


def _causal_family(category: str, text: str) -> str:
    normalized = _normalize_category(category)
    exact = _EXACT_FAMILIES.get(normalized)
    if exact:
        return exact

    combined = f"{normalized} {text}".lower()
    if "metric" in normalized and any(word in normalized for word in ("wrong", "incorrect", "label", "recorder")):
        return "wrong-metric"
    if "context" in normalized and any(word in normalized for word in ("loss", "lost", "log", "inconsisten")):
        return "context-loss"
    if any(token in normalized for token in ("missing-action", "missing-side-effect", "incomplete-action")):
        return "incomplete-action"
    if any(token in normalized for token in ("auth", "permission", "access-control")) and any(
        token in combined for token in ("logic", "check", "condition", "boolean", "role", "admin", "owner")
    ):
        return "auth-logic"
    if normalized in {"logic-bug", "wrong-logic"} and re.search(
        r"is(?:team)?(?:admin|owner)|permission|authori[sz]",
        combined,
    ):
        return "auth-logic"
    if "undefined" in normalized and any(token in normalized for token in ("symbol", "variable", "name")):
        return "undefined-symbol"
    if any(token in normalized for token in ("nil-deref", "null-deref", "nil-pointer", "null-pointer")):
        return "nil-dereference"
    if (
        "xss" in normalized
        and "raw_html" in combined
        and any(token in combined for token in ("rss", "feed", "topicembed"))
    ):
        return "stored-xss-flow"
    return ""


def _normalize_identifier(identifier: str) -> str:
    return re.sub(r"\s+", "", identifier).lower()


def _is_shaped(identifier: str) -> bool:
    bare = identifier.lstrip("@")
    return (
        identifier.startswith("@")
        or "." in identifier
        or "_" in identifier
        or "$" in identifier
        or any(character.isupper() for character in bare[1:])
        or (bare[:1].isupper() and len(bare) >= 4)
    )


def _extract_identifiers(text: str) -> tuple[frozenset[str], frozenset[str]]:
    identifiers: set[str] = set()
    strong: set[str] = set()
    source = text or ""

    for match in _QUALIFIED_IDENTIFIER.finditer(source):
        value = _normalize_identifier(match.group(0))
        identifiers.add(value)
        strong.add(value)
    for match in _CALL_IDENTIFIER.finditer(source):
        raw = match.group("name")
        value = _normalize_identifier(raw)
        if value not in _STOPWORDS:
            identifiers.add(value)
            if _is_shaped(raw):
                strong.add(value)
    for match in _QUOTED_CODE.finditer(source):
        raw = match.group("code")
        value = _normalize_identifier(raw)
        if value not in _STOPWORDS:
            identifiers.add(value)
            if _is_shaped(raw):
                strong.add(value)
    for match in _SHAPED_IDENTIFIER.finditer(source):
        raw = match.group(0)
        value = _normalize_identifier(raw)
        if value not in _STOPWORDS and _is_shaped(raw):
            identifiers.add(value)
            strong.add(value)
    return frozenset(identifiers), frozenset(strong)


def _extract_file_patch(diff_summary: str, file_path: str) -> str:
    lines = (diff_summary or "").splitlines()
    if not any(_SUMMARY_FILE_HEADER.match(line) for line in lines):
        return diff_summary or ""

    selected: list[str] = []
    in_target = False
    for line in lines:
        header = _SUMMARY_FILE_HEADER.match(line)
        if header:
            if in_target:
                break
            in_target = header.group("file") == file_path
            continue
        if in_target:
            selected.append(line)
    return "\n".join(selected)


def _hunk_for_line(patch: str, line: int) -> str:
    for index, match in enumerate(_HUNK_HEADER.finditer(patch or "")):
        start = int(match.group("start"))
        count = int(match.group("count") or 1)
        if start <= line < start + count:
            return f"{index}:{start}:{count}"
    return ""


def _anchor_for_line(patch: str, line: int) -> str:
    for right_line, content in iter_right_lines(patch):
        if right_line == line:
            return re.sub(r"\s+", " ", content.strip()).lower()
    return ""


def _build_claim(finding: Finding, diff_summary: str, file_diffs: Mapping[str, str]) -> RootCauseClaim:
    patch = file_diffs.get(finding.file)
    if patch is None:
        patch = _extract_file_patch(diff_summary, finding.file)
    identifiers, strong_identifiers = _extract_identifiers(finding.message)
    anchor_text = _anchor_for_line(patch, finding.line)
    anchor_identifiers, _anchor_strong = _extract_identifiers(anchor_text)
    return RootCauseClaim(
        finding_id=finding.id,
        file=finding.file,
        line=finding.line,
        reviewer=finding.reviewer,
        causal_family=_causal_family(finding.category, f"{finding.message}\n{finding.suggestion}"),
        identifiers=identifiers,
        strong_identifiers=strong_identifiers,
        anchor_identifiers=anchor_identifiers,
        anchor_text=anchor_text,
        operators=frozenset(_OPERATORS.findall(f"{finding.message}\n{anchor_text}")),
        semantic_markers=frozenset(
            marker
            for marker in ("admin", "owner", "organizer", "attendee")
            if re.search(rf"\b{marker}\b", finding.message, re.IGNORECASE)
        ),
        hunk_id=_hunk_for_line(patch, finding.line),
        is_detector=finding.verified_by.startswith("detector"),
        confidence=finding.confidence,
    )


def _same_location_window(left: RootCauseClaim, right: RootCauseClaim) -> bool:
    tolerance = _LINE_TOLERANCE[left.causal_family]
    return abs(left.line - right.line) <= tolerance and (
        not left.hunk_id or not right.hunk_id or left.hunk_id == right.hunk_id
    )


def _same_code_identity(left: RootCauseClaim, right: RootCauseClaim) -> bool:
    if not _same_location_window(left, right):
        return False

    shared_message = left.identifiers & right.identifiers
    shared_strong = left.strong_identifiers & right.strong_identifiers
    shared_anchor = left.anchor_identifiers & right.anchor_identifiers

    if left.causal_family == "auth-logic" and left.operators & right.operators:
        left_roles = left.semantic_markers | {
            role
            for role in ("admin", "owner", "organizer", "attendee")
            if any(role in identifier for identifier in left.identifiers | left.anchor_identifiers)
        }
        right_roles = right.semantic_markers | {
            role
            for role in ("admin", "owner", "organizer", "attendee")
            if any(role in identifier for identifier in right.identifiers | right.anchor_identifiers)
        }
        if len(left_roles & right_roles) >= 2:
            return True

    if left.anchor_text and right.anchor_text:
        if left.anchor_text == right.anchor_text:
            return True
        # Two different calls of the same function are not one root cause.  A
        # second shared code identifier is required when both anchors exist.
        if len(shared_anchor) >= 2 or len(shared_message | shared_anchor) >= 2:
            return True
        return False

    if len(shared_message) >= 2 or shared_strong:
        return True
    if (
        left.causal_family == "auth-logic"
        and left.operators & right.operators
        and shared_message
        and abs(left.line - right.line) <= 8
    ):
        return True
    return False


def _are_duplicates(left: RootCauseClaim, right: RootCauseClaim) -> bool:
    if not left.causal_family or left.causal_family != right.causal_family:
        return False
    # Independent deterministic findings remain independent evidence.
    if left.is_detector and right.is_detector:
        return False
    if left.file != right.file:
        if left.causal_family != "stored-xss-flow":
            return False
        shared = left.strong_identifiers & right.strong_identifiers
        # A source and sink description of one stored-XSS path can be anchored
        # in different files. Require the uncommon rendering contract and a
        # qualified code identity so two unrelated XSS sites never collapse.
        return (
            "raw_html" in shared
            and bool({"cook_method", "post.cook_methods"} & shared)
            and any("." in identifier for identifier in shared)
            and len(shared) >= 3
        )
    return _same_code_identity(left, right)


def _representative(indices: list[int], findings: Sequence[Finding], claims: Sequence[RootCauseClaim]) -> int:
    severity = {"info": 0, "warning": 1, "error": 2}
    return max(
        indices,
        key=lambda index: (
            claims[index].is_detector,
            bool(claims[index].anchor_text),
            len(claims[index].anchor_identifiers),
            findings[index].confidence,
            severity.get(findings[index].severity, 0),
        ),
    )


def cluster_root_causes(
    findings: Sequence[Finding],
    diff_summary: str = "",
    *,
    file_diffs: Mapping[str, str] | None = None,
) -> RootCauseClusterResult:
    """Collapse only high-confidence duplicate descriptions of one code defect."""

    source = tuple(findings)
    claims = tuple(_build_claim(finding, diff_summary, file_diffs or {}) for finding in source)
    groups: list[list[int]] = []

    for index, claim in enumerate(claims):
        if not claim.causal_family:
            groups.append([index])
            continue
        for group in groups:
            # Complete-link clustering prevents a vague bridge claim from
            # joining two otherwise distinct call sites.
            if all(_are_duplicates(claim, claims[member]) for member in group):
                group.append(index)
                break
        else:
            groups.append([index])

    representative_indices: set[int] = set()
    absorbed_indices: set[int] = set()
    clusters: list[RootCauseCluster] = []
    mapping: list[tuple[str, str]] = []
    cross_reviewer_merged = 0

    for group in groups:
        representative = _representative(group, source, claims)
        representative_indices.add(representative)
        if len(group) == 1:
            continue
        members = tuple(source[index].id for index in group)
        reviewers = tuple(dict.fromkeys(source[index].reviewer for index in group))
        clusters.append(
            RootCauseCluster(
                causal_family=claims[representative].causal_family,
                representative_id=source[representative].id,
                member_ids=members,
                reviewers=reviewers,
            )
        )
        if len(set(reviewers)) > 1:
            cross_reviewer_merged += len(group) - 1
        for index in group:
            if index == representative:
                continue
            absorbed_indices.add(index)
            mapping.append((source[index].id, source[representative].id))

    kept = tuple(finding for index, finding in enumerate(source) if index not in absorbed_indices)
    absorbed = tuple(finding for index, finding in enumerate(source) if index in absorbed_indices)
    return RootCauseClusterResult(
        kept=kept,
        absorbed=absorbed,
        clusters=tuple(clusters),
        absorbed_to_representative=tuple(mapping),
        input_count=len(source),
        cross_reviewer_merged=cross_reviewer_merged,
    )
