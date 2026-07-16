"""Conservative repair of reviewer anchors for structured markup findings.

Review models sometimes identify the correct accessibility defect but anchor the
finding on the surrounding ``return``/container line.  Inline GitHub comments and
benchmark matching both need the concrete changed element.  This module only
reanchors when the added markup exposes an unambiguous, still-unlabelled sink; it
does not create or confirm findings.
"""

from __future__ import annotations

import ast
import re
from collections import defaultdict

from reviewforge.core.state import Finding
from reviewforge.engine.detectors.unified_diff import iter_added_lines, iter_right_lines
from reviewforge.engine.security_categories import normalize_category

_SUMMARY_FILE_HEADER = re.compile(r"^--- (?P<file>.+?) \(\+\d+ -\d+\)$")
_ALT_TAG_START = re.compile(r"<(?:img|Image)\b")
_NATIVE_ALT_TAG_START = re.compile(r"<img\b", re.IGNORECASE)
_DETECTOR_NATIVE_ALT_TAG_START = re.compile(r"<img\b")
_CONTROL_TAG_START = re.compile(r"<(?:input|select|textarea)\b")
_ALT_ATTRIBUTE = re.compile(r"(?:\balt|:alt|v-bind:alt|\[alt\]|\[attr\.alt\])\s*=", re.IGNORECASE)
_ACCESSIBLE_NAME = re.compile(
    r"(?:aria-label|aria-labelledby|:aria-label|v-bind:aria-label|"
    r"\[attr\.aria-label\]|\[attr\.aria-labelledby\])\s*=",
    re.IGNORECASE,
)
_DYNAMIC_PROPS = re.compile(r"\{\s*\.\.\.|\bv-bind\s*=", re.IGNORECASE)
_DECORATIVE_IMAGE = re.compile(
    r"\brole\s*=\s*['\"](?:presentation|none)['\"]|"
    r"\baria-hidden\s*=\s*(?:['\"]?true['\"]?|\{true\})",
    re.IGNORECASE,
)
_NON_TEXT_INPUT = re.compile(
    r"\btype\s*=\s*['\"](?:hidden|submit|reset|button|image)['\"]",
    re.IGNORECASE,
)
_STATIC_ID = re.compile(r"\bid\s*=\s*['\"](?P<id>[A-Za-z][\w:.-]*)['\"]", re.IGNORECASE)
_DETECTOR_PROVENANCE = {"detector", "detector-auto"}
_CODE_IDENTIFIER = re.compile(r"(?<![A-Za-z0-9_$])(?:[A-Za-z_$][A-Za-z0-9_$]*)(?:\s*\.\s*[A-Za-z_$][A-Za-z0-9_$]*)*")
_EXPLICIT_CALL_IDENTIFIER = re.compile(
    r"(?<![A-Za-z0-9_$])(?P<name>[A-Za-z_$][A-Za-z0-9_$]*(?:\s*\.\s*[A-Za-z_$][A-Za-z0-9_$]*)*)(?=\s*\()"
)
_IDENTIFIER_STOPWORDS = {
    "allow",
    "application",
    "code",
    "command",
    "data",
    "database",
    "directly",
    "execute",
    "file",
    "function",
    "input",
    "method",
    "password",
    "query",
    "secret",
    "shell",
    "sql",
    "string",
    "token",
    "user",
    "value",
}
_SECURITY_ANCHOR_CATEGORY_PAIRS = {
    frozenset({"hardcoded-secrets", "data-leak"}),
    frozenset({"command-injection", "ci-security"}),
}
_REMOTE_DOWNLOAD_CATEGORIES = {"insecure-download", "supply-chain-risk"}
_WORKFLOW_INDEPENDENT_EXECUTION_SINK = re.compile(
    r"\b(?:ba|da|z|k)?sh\s+-[A-Za-z]*c\b|"
    r"\bcmd(?:\.exe)?\s+/[ck]\b|"
    r"\b(?:powershell|pwsh)(?:\.exe)?\s+-(?:c|command|encodedcommand)\b|"
    r"\b(?:python(?:\d+(?:\.\d+)?)?|node|ruby|perl|lua|php)\s+(?:-c|-e|-r|--eval)\b|"
    r"^\s*run\s*:[^\n]*\$\{\{|"
    r"\beval\s+",
    re.IGNORECASE,
)
_PYTHON_REDIRECT_CALLS = {
    "httpresponseredirect",
    "httpresponseredirectpermanent",
    "redirect",
    "redirect_to",
    "redirectresponse",
    "sendredirect",
}


def _extract_file_patch(diff_summary: str, file_path: str) -> str:
    """Extract a file patch from ReviewForge's concatenated diff summary."""

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


def _is_complete_new_file_patch(patch: str) -> bool:
    """Whether a patch is one complete post-image beginning at line one."""

    headers = list(re.finditer(r"^@@ .* @@", patch or "", re.MULTILINE))
    new_file = re.search(r"^@@ -0,0 \+1(?:,(?P<count>\d+))? @@", patch or "", re.MULTILINE)
    if new_file is None or len(headers) != 1 or headers[0].start() != new_file.start():
        return False
    expected = int(new_file.group("count") or 1)
    additions = iter_added_lines(patch)
    return len(additions) == expected and [line for line, _content in additions] == list(range(1, expected + 1))


def _added_tags(patch: str, start_pattern: re.Pattern[str]) -> list[tuple[int, str]]:
    """Return added opening tags, joining short contiguous multiline tags."""

    added = iter_added_lines(patch)
    tags: list[tuple[int, str]] = []
    for index, (line_no, content) in enumerate(added):
        match = start_pattern.search(content)
        if match is None:
            continue

        parts = [content[match.start() :]]
        previous_line = line_no
        cursor = index + 1
        while ">" not in "\n".join(parts) and cursor < len(added) and len(parts) < 8:
            next_line, next_content = added[cursor]
            if next_line != previous_line + 1:
                break
            parts.append(next_content)
            previous_line = next_line
            cursor += 1
        tag = "\n".join(parts)
        if ">" in tag:
            tags.append((line_no, tag.split(">", 1)[0] + ">"))
    return tags


def _missing_alt_candidates(patch: str) -> list[int]:
    candidates: list[int] = []
    for line, tag in _added_tags(patch, _ALT_TAG_START):
        if _DYNAMIC_PROPS.search(tag):
            continue
        if _ALT_ATTRIBUTE.search(tag) or _ACCESSIBLE_NAME.search(tag) or _DECORATIVE_IMAGE.search(tag):
            continue
        candidates.append(line)
    return candidates


def _has_associated_label(control_tag: str, patch: str, control_line: int) -> bool:
    identifier = _STATIC_ID.search(control_tag)
    if identifier is not None:
        value = re.escape(identifier.group("id"))
        label_for = re.compile(
            rf"<label\b[^>]*(?:for|htmlFor)\s*=\s*['\"]{value}['\"]",
            re.IGNORECASE,
        )
        if label_for.search("\n".join(content for _line, content in iter_right_lines(patch))):
            return True

    nearby = [content for line, content in iter_right_lines(patch) if control_line - 4 <= line <= control_line + 2]
    before_control = "\n".join(nearby).split(control_tag.split("\n", 1)[0], 1)[0]
    if before_control.lower().rfind("<label") > before_control.lower().rfind("</label"):
        return True
    # ``mat-label`` names a control only through Angular Material's
    # ``mat-form-field``/``matInput`` contract.  The caller already exempts an
    # actual ``matInput`` tag; an unrelated nearby ``mat-label`` must not hide a
    # native input that still lacks a label.
    return False


def _missing_label_candidates(patch: str) -> list[int]:
    candidates: list[int] = []
    for line, tag in _added_tags(patch, _CONTROL_TAG_START):
        if _DYNAMIC_PROPS.search(tag) or _ACCESSIBLE_NAME.search(tag) or _NON_TEXT_INPUT.search(tag):
            continue
        if "matinput" in tag.lower() or _has_associated_label(tag, patch, line):
            continue
        candidates.append(line)
    return candidates


def _assign_nearest(findings: list[Finding], candidates: list[int]) -> list[Finding]:
    """Assign unique sinks to nearby findings without guessing through ties."""

    if not findings or not candidates:
        return []

    available = set(candidates)
    changed: list[Finding] = []

    # Preserve correct anchors first so another drifted finding cannot claim them.
    for finding in findings:
        if finding.line in available:
            available.remove(finding.line)

    proposals: list[tuple[int, int, str, Finding]] = []
    for finding in findings:
        if finding.line in candidates:
            continue
        ranked = sorted((abs(line - finding.line), line) for line in available)
        if not ranked:
            continue
        if len(ranked) > 1 and ranked[0][0] == ranked[1][0]:
            continue
        # A single sink is intrinsically unambiguous. With several sinks, keep
        # the repair local to the surrounding JSX/template block.
        if len(candidates) > 1 and ranked[0][0] > 12:
            continue
        proposals.append((ranked[0][0], ranked[0][1], finding.id, finding))

    assigned_findings: set[str] = set()
    for _distance, target, _finding_id, finding in sorted(proposals):
        if finding.id in assigned_findings or target not in available:
            continue
        finding.line = target
        available.remove(target)
        assigned_findings.add(finding.id)
        changed.append(finding)
    return changed


def reanchor_accessibility_findings(findings: list[Finding], diff_summary: str) -> list[Finding]:
    """Move known accessibility findings onto their concrete added markup sink.

    The supplied findings are mutated and the subset whose line changed is
    returned so callers can persist the repaired coordinate in their state store.
    """

    grouped: dict[tuple[str, str], list[Finding]] = defaultdict(list)
    for finding in findings:
        category = normalize_category(finding.category)
        if category in {"missing-alt", "missing-label"}:
            finding.category = category
            grouped[(finding.file, category)].append(finding)

    changed: list[Finding] = []
    for (file_path, category), group in grouped.items():
        patch = _extract_file_patch(diff_summary, file_path)
        candidates = _missing_alt_candidates(patch) if category == "missing-alt" else _missing_label_candidates(patch)
        changed.extend(_assign_nearest(group, candidates))
        if category != "missing-alt" or len(candidates) != 1 or not _is_complete_new_file_patch(patch):
            continue
        target = candidates[0]
        # Reusing an occupied target is safe only when it is a native ``img``
        # sink covered by the deterministic detector, not a framework component
        # such as ``Image`` whose accessibility contract is unknown.
        covered_native_candidates = {line for line, _tag in _added_tags(patch, _NATIVE_ALT_TAG_START)}
        detector_native_candidates = {line for line, _tag in _added_tags(patch, _DETECTOR_NATIVE_ALT_TAG_START)}
        detector_claims = [
            finding
            for finding in group
            if finding.line == target and finding.verified_by.strip().lower() in _DETECTOR_PROVENANCE
        ]
        if covered_native_candidates != {target} or detector_native_candidates != {target} or len(detector_claims) != 1:
            continue
        already_changed = {finding.id for finding in changed}
        for finding in group:
            if finding.line == target or finding.id in already_changed or abs(finding.line - target) > 12:
                continue
            reviewer_text = f"{finding.message}\n{finding.suggestion}"
            if not re.search(r"<\s*img\b|\bimg\s+(?:tag|element)\b|\bimg\s*标签", reviewer_text, re.IGNORECASE):
                # A Reviewer that names another component (for example Avatar)
                # may describe an independent accessibility contract that the
                # native-img detector deliberately does not cover.
                continue
            finding.line = target
            changed.append(finding)
    return changed


def _quality_issue_family(finding: Finding) -> str:
    """Return a narrow semantic family for deterministic quality duplicates."""

    category = normalize_category(finding.category)
    text = f"{category}\n{finding.message}\n{finding.suggestion}".lower()
    file_path = finding.file.lower()

    if file_path.endswith(".java"):
        if category in {"exception-handling", "error-handling"} and re.search(
            r"\bcatch\b.*(?:empty|silent|swallow)|(?:empty|silent|swallow).*\bcatch\b|空\s*catch|吞.{0,12}异常",
            text,
        ):
            return "java-empty-catch"
        if category in {"null-safety", "optional-misuse", "correctness"} and (
            category == "optional-misuse" or "optional" in text
        ):
            return "java-optional-get"
        if category in {"resource-leak", "resource-management"}:
            # JDBC resources in one method are independent lifetimes.  Keep
            # their concrete type in the family so a Statement detector cannot
            # absorb a separate Connection or ResultSet review finding.
            for resource_type in ("preparedstatement", "callablestatement", "resultset", "statement", "connection"):
                if re.search(rf"\b{resource_type}\b", text):
                    return f"java-jdbc-resource:{resource_type}"

    if file_path.endswith(".vue"):
        if category in {"exception-handling", "error-handling"} and re.search(
            r"\bcatch\b.*(?:empty|silent|swallow)|(?:empty|silent|swallow).*\bcatch\b|空\s*catch|吞.{0,12}异常",
            text,
        ):
            return "vue-empty-catch"
        if category in {"computed-side-effect", "side-effect-in-computed"} or (
            "computed" in text and re.search(r"side.?effect|副作用", text)
        ):
            return "vue-computed-side-effect"
        if category == "v-for-v-if-misuse" or ("v-for" in text and "v-if" in text):
            return "vue-v-if-for"
        if category in {"timer-leak", "memory-leak", "resource-leak"} and re.search(
            r"setinterval|clearinterval|\binterval\b|定时器",
            text,
        ):
            return "vue-interval-lifecycle"

    if file_path.endswith(".go"):
        if (
            category in {"performance", "resource-exhaustion", "resource-leak"}
            and re.search(r"\bdefer\b", text)
            and re.search(r"\b(?:for|loop|iteration)\b|循环|迭代", text)
        ):
            return "go-defer-rows-close-loop"
        if category in {"goroutine-leak", "infinite-loop", "lifecycle", "performance", "resource-leak"} and re.search(
            r"goroutine.{0,80}(?:unbounded|infinite|loop|no\s+(?:stop|exit|cancel)|without\s+cancellation)|"
            r"(?:unbounded|infinite|loop|no\s+(?:stop|exit|cancel)|without\s+cancellation).{0,80}goroutine|"
            r"goroutine.{0,40}(?:无限|退出|取消)",
            text,
        ):
            return "go-goroutine-lifecycle"
        if category in {"error-handling", "ignored-error"} and re.search(
            r"only logged|log.{0,24}continue|return nil|仅.{0,8}(?:日志|打印).{0,16}继续",
            text,
        ):
            return "go-log-and-continue"

    if (
        file_path.endswith(".py")
        and category in {"resource-leak", "resource-management"}
        and re.search(
            r"sqlite|connection|\bconn\b",
            text,
        )
    ):
        return "python-sqlite-resource"
    return ""


def _calls_in_braced_scopes(
    patch_lines: list[tuple[int, str]],
    scope_start: re.Pattern[str],
    call: re.Pattern[str],
) -> set[int]:
    """Return call lines only from complete, balanced lexical brace scopes."""

    found: set[int] = set()
    for index, (_line_no, content) in enumerate(patch_lines):
        start = scope_start.search(content)
        if start is None:
            continue
        depth = 0
        opened = False
        scoped: set[int] = set()
        for line_no, scoped_content in patch_lines[index:]:
            # Braces in ordinary quoted strings are not lexical scope.  This is
            # deliberately a small fail-closed scanner rather than a JS parser;
            # an unbalanced/template-heavy scope simply yields no candidates.
            structural = re.sub(r"(['\"])(?:\\.|(?!\1).)*\1", "", scoped_content)
            if not opened:
                brace = structural.find("{", start.start() if line_no == patch_lines[index][0] else 0)
                if brace < 0:
                    continue
                structural = structural[brace:]
                opened = True
            if call.search(scoped_content):
                scoped.add(line_no)
            depth += structural.count("{") - structural.count("}")
            if opened and depth <= 0:
                found.update(scoped)
                break
    return found


def _quality_family_sink_lines(file_path: str, family: str, diff_summary: str) -> set[int]:
    """Return conservative concrete sink lines for one quality family."""

    patch = _extract_file_patch(diff_summary, file_path)
    patch_lines = iter_right_lines(patch)
    if not patch_lines:
        return set()

    if family == "java-empty-catch":
        return {line for line, content in patch_lines if re.search(r"\bcatch\s*\([^)]*\)\s*\{", content)}
    if family == "java-optional-get":
        return {line for line, content in patch_lines if re.search(r"\.\s*get\s*\(", content)}
    if family.startswith("java-jdbc-resource:"):
        resource_type = family.rsplit(":", 1)[1]
        declaration = re.compile(rf"\b{re.escape(resource_type)}\s+[A-Za-z_]\w*\s*=", re.IGNORECASE)
        return {line for line, content in patch_lines if declaration.search(content)}
    if family == "vue-empty-catch":
        return {line for line, content in patch_lines if re.search(r"\bcatch\s*(?:\([^)]*\))?\s*\{", content)}
    if family == "vue-computed-side-effect":
        # A component can legitimately fetch elsewhere in a watcher, event
        # handler, or helper.  Only calls inside a complete computed getter are
        # candidates for this semantic family.
        scoped = _calls_in_braced_scopes(
            patch_lines,
            re.compile(r"\bcomputed\s*\(", re.IGNORECASE),
            re.compile(r"\b[A-Za-z_$]*fetch\w*\s*\(", re.IGNORECASE),
        )
        if scoped:
            return scoped
        # Some gateway summaries contain only the concrete sink line rather
        # than its surrounding computed declaration.  Preserve the established
        # conservative fallback only when that summary has one fetch-like call.
        all_fetches = {
            line for line, content in patch_lines if re.search(r"\b[A-Za-z_$]*fetch\w*\s*\(", content, re.IGNORECASE)
        }
        return all_fetches if len(all_fetches) == 1 else set()
    if family == "vue-v-if-for":
        return {
            line
            for line, content in patch_lines
            if re.search(r"\bv-if\s*=", content) and re.search(r"\bv-for\s*=", content)
        }
    if family == "vue-interval-lifecycle":
        return {line for line, content in patch_lines if re.search(r"\bsetInterval\s*\(", content)}
    if family == "go-goroutine-lifecycle":
        starts = {line for line, content in patch_lines if re.search(r"\bgo\s+(?:func\b|[A-Za-z_]\w*\s*\()", content)}
        loops = _calls_in_braced_scopes(
            patch_lines,
            re.compile(r"\bgo\s+func\s*\([^)]*\)\s*\{"),
            re.compile(r"^\s*for\s*\{"),
        )
        if len(starts) == 1 and len(loops) == 1:
            # The detector may anchor either the goroutine declaration or the
            # concrete unbounded loop, depending on scanner version.  A unique
            # complete pair proves they represent the same lifecycle defect.
            return starts | loops
        # Preserve support for a deliberately abbreviated gateway summary that
        # contains one goroutine declaration but omits its body.
        return starts if len(starts) == 1 and not loops and not _is_complete_new_file_patch(patch) else set()
    if family == "go-log-and-continue":
        return {line for line, content in patch_lines if re.search(r"\bif\s+[A-Za-z_]\w*\s*!=\s*nil\s*\{", content)}
    if family == "go-defer-rows-close-loop":
        return {line for line, content in patch_lines if re.search(r"\bdefer\s+rows\s*\.\s*Close\s*\(\s*\)", content)}
    if family == "python-sqlite-resource":
        return {line for line, content in patch_lines if re.search(r"\bsqlite3\s*\.\s*connect\s*\(", content)}
    return set()


def reanchor_quality_detector_duplicates(findings: list[Finding], diff_summary: str) -> list[Finding]:
    """Move one semantic Reviewer duplicate onto one deterministic quality sink.

    Families are deliberately syntax-specific.  A repair is made only when the
    file contains exactly one deterministic candidate for that family; adjacent
    independent sinks remain ambiguous and untouched. JDBC resources additionally
    require a unique concrete declaration in the changed source.
    """

    grouped: dict[tuple[str, str], dict[str, list[Finding]]] = defaultdict(lambda: {"detectors": [], "reviewers": []})
    for finding in findings:
        family = _quality_issue_family(finding)
        if not family:
            continue
        bucket = "detectors" if finding.verified_by.strip().lower() in _DETECTOR_PROVENANCE else "reviewers"
        grouped[(finding.file, family)][bucket].append(finding)

    proposals: list[tuple[int, str, Finding, Finding]] = []
    for (file_path, family), group in grouped.items():
        detectors = group["detectors"]
        if len(detectors) != 1:
            continue
        detector = detectors[0]
        sink_lines = _quality_family_sink_lines(file_path, family, diff_summary)
        # A unique detector is not proof that the file has only one defect: a
        # second sink may simply be outside the deterministic rule's coverage.
        # Repair only when the changed source itself has one concrete sink for
        # this narrow family and the detector is anchored on it.
        if family == "go-goroutine-lifecycle":
            if detector.line not in sink_lines:
                continue
        elif sink_lines != {detector.line}:
            continue
        for finding in group["reviewers"]:
            if family.startswith("java-jdbc-resource:"):
                detector_symbols = {value.lower() for value in re.findall(r"`([A-Za-z_]\w*)`", detector.message)}
                finding_symbols = {value.lower() for value in re.findall(r"`([A-Za-z_]\w*)`", finding.message)}
                if detector_symbols and finding_symbols and not detector_symbols.intersection(finding_symbols):
                    continue
            proposals.append((abs(finding.line - detector.line), finding.id, finding, detector))

    changed: list[Finding] = []
    claimed_detectors: set[str] = set()
    for _distance, _finding_id, finding, detector in sorted(proposals):
        if detector.id in claimed_detectors:
            continue
        finding.line = detector.line
        finding.category = normalize_category(detector.category)
        claimed_detectors.add(detector.id)
        changed.append(finding)
    return changed


def _message_code_identifiers(message: str, patch_lines: list[tuple[int, str]]) -> set[str]:
    """Return code-like message terms that are actually present in the patch.

    A useful identifier often appears once in a declaration and again at its
    sink (for example ``backupPath`` or ``query``).  Requiring one textual
    occurrence discarded that evidence.  Ambiguity is instead resolved against
    eligible detector windows below: an identifier may repeat inside one owning
    symbol, but must not select two independent sinks.
    """

    code = "\n".join(content for _line, content in patch_lines)
    identifiers: set[str] = set()
    for match in _CODE_IDENTIFIER.finditer(message or ""):
        raw = re.sub(r"\s+", "", match.group(0))
        lowered = raw.lower()
        if (len(raw) < 4 and lowered not in {"md5", "sha1"}) or lowered in _IDENTIFIER_STOPWORDS:
            continue
        occurrence = re.compile(rf"(?<![A-Za-z0-9_$]){re.escape(raw)}(?![A-Za-z0-9_$])", re.IGNORECASE)
        if occurrence.search(code):
            identifiers.add(lowered)
    return identifiers


def _message_explicit_call_identifiers(message: str, patch_lines: list[tuple[int, str]]) -> set[str]:
    """Return APIs explicitly written as calls in a reviewer message.

    Ruby reviews commonly name both a tainted variable and the concrete sink,
    such as ``system()``.  The sink API is stronger ownership evidence than a
    variable reused by an earlier, independent command execution.
    """

    code = "\n".join(content for _line, content in patch_lines)
    identifiers: set[str] = set()
    for match in _EXPLICIT_CALL_IDENTIFIER.finditer(message or ""):
        raw = re.sub(r"\s+", "", match.group("name"))
        occurrence = re.compile(rf"(?<![A-Za-z0-9_$]){re.escape(raw)}\s*\(", re.IGNORECASE)
        if occurrence.search(code):
            identifiers.add(raw.lower())
    return identifiers


def _window_contains_identifier(patch_lines: list[tuple[int, str]], detector_line: int, identifiers: set[str]) -> bool:
    window = "\n".join(content for line, content in patch_lines if abs(line - detector_line) <= 3).lower()
    return any(
        re.search(rf"(?<![a-z0-9_$]){re.escape(identifier)}(?![a-z0-9_$])", window, re.IGNORECASE)
        for identifier in identifiers
    )


def _security_anchor_categories_compatible(
    detector_category: str,
    reviewer_category: str,
    patch_lines: list[tuple[int, str]],
    detector_line: int,
    detector_message: str = "",
    file_path: str = "",
) -> bool:
    if detector_category == reviewer_category:
        return True
    if frozenset({detector_category, reviewer_category}) in _SECURITY_ANCHOR_CATEGORY_PAIRS:
        return True
    categories = {detector_category, reviewer_category}
    if detector_category == "code-injection" and reviewer_category in {
        "unsafe-dynamic-call",
        "unsafe-reflection",
    }:
        if not file_path.lower().endswith(".rb"):
            return False
        detector_text = detector_message.lower()
        if not re.search(r"dynamic\s+dispatch|\b(?:send|public_send|instance_eval|class_eval)\b", detector_text):
            return False
        return any(
            line == detector_line and re.search(r"\b(?:send|public_send|instance_eval|class_eval)\s*\(", content)
            for line, content in patch_lines
        )
    if "unsafe-script" in categories and categories.intersection(_REMOTE_DOWNLOAD_CATEGORIES):
        window = "\n".join(content for line, content in patch_lines if abs(line - detector_line) <= 3)
        return bool(re.search(r"\b(?:curl|wget)\b[^\n]*\|\s*(?:sh|bash)\b", window, re.IGNORECASE))
    # Code-injection versus remote-download category drift needs the Reviewer's
    # own anchor or explicit pipe evidence.  The workflow fallback below owns
    # that check; a remote pipe near the detector alone cannot prove that a
    # shared input identifier does not feed a second eval/command sink.
    return False


def reanchor_security_detector_duplicates(findings: list[Finding], diff_summary: str) -> list[Finding]:
    """Move an LLM duplicate onto a detector line only with unique diff evidence.

    Reviewer line numbers often point at a method declaration while a detector
    points at the concrete sink a few lines later.  A detector is eligible only
    when a code identifier/API named by the reviewer occurs exactly once in the
    file patch and inside that detector's three-line context window.  Candidate
    matching is one-to-one, so adjacent independent sinks are never consumed by
    one deterministic finding.
    """

    grouped: dict[str, dict[str, list[Finding]]] = defaultdict(lambda: {"detectors": [], "reviewers": []})
    for finding in findings:
        bucket = "detectors" if finding.verified_by.strip().lower() in _DETECTOR_PROVENANCE else "reviewers"
        grouped[finding.file][bucket].append(finding)

    proposals: list[tuple[int, int, str, Finding, Finding]] = []
    for file_path, group in grouped.items():
        detectors = group["detectors"]
        reviewers = group["reviewers"]
        if not detectors or not reviewers:
            continue
        patch_lines = iter_right_lines(_extract_file_patch(diff_summary, file_path))
        if not patch_lines:
            continue
        for finding in reviewers:
            identifiers = _message_code_identifiers(finding.message, patch_lines)
            matching = [
                detector
                for detector in detectors
                if _security_anchor_categories_compatible(
                    normalize_category(detector.category),
                    normalize_category(finding.category),
                    patch_lines,
                    detector.line,
                    detector.message,
                    file_path,
                )
                and identifiers
                and _window_contains_identifier(patch_lines, detector.line, identifiers)
            ]
            if file_path.lower().endswith(".rb"):
                explicit_calls = _message_explicit_call_identifiers(finding.message, patch_lines)
                preferred = [
                    detector
                    for detector in matching
                    if _window_contains_identifier(patch_lines, detector.line, explicit_calls)
                ]
                if preferred:
                    matching = preferred
            # Secret-output reviews often say only "GitHub token" and anchor on
            # a nearby expression.  When the diff has exactly one concrete
            # secret-print sink, that sink is stronger evidence than a generic
            # identifier (``github`` occurs throughout workflow expressions).
            if not matching and re.search(r"\.ya?ml$", file_path, re.IGNORECASE):
                message_is_remote_execution = bool(
                    re.search(
                        r"(?:curl|wget|remote|external|\burl\b|download|\u8fdc\u7a0b|\u4e0b\u8f7d|\u5916\u90e8)",
                        finding.message,
                        re.IGNORECASE,
                    )
                    and re.search(
                        r"(?:execute|executed|execution|run|script|shell|bash|command|执行|运行|脚本|命令)",
                        finding.message,
                        re.IGNORECASE,
                    )
                )
                if message_is_remote_execution:
                    remote_pipe_lines = {
                        line
                        for line, content in patch_lines
                        if re.search(r"\b(?:curl|wget)\b[^\n]*\|\s*(?:sh|bash)\b", content, re.IGNORECASE)
                    }
                    explicit_pipe_evidence = bool(
                        re.search(r"\b(?:curl|wget)\b", finding.message, re.IGNORECASE)
                        and re.search(r"(?:\bpipe[sd]?\b|\|\s*(?:sh|bash)\b)", finding.message, re.IGNORECASE)
                    )
                    if len(remote_pipe_lines) == 1:
                        pipe_line = next(iter(remote_pipe_lines))
                        reviewer_is_near_pipe = abs(finding.line - pipe_line) <= 3
                        reviewer_anchor_has_independent_sink = any(
                            line != pipe_line
                            and abs(line - finding.line) <= 1
                            and _WORKFLOW_INDEPENDENT_EXECUTION_SINK.search(content)
                            for line, content in patch_lines
                        )
                        proximity_supports_pipe = reviewer_is_near_pipe and not reviewer_anchor_has_independent_sink
                        if proximity_supports_pipe or explicit_pipe_evidence:
                            matching = [
                                detector
                                for detector in detectors
                                if detector.line == pipe_line
                                and normalize_category(detector.category) in _REMOTE_DOWNLOAD_CATEGORIES
                                and normalize_category(finding.category)
                                in {"code-injection", "command-injection", "unsafe-script"}
                            ]

            if not matching and re.search(r"\.ya?ml$", file_path, re.IGNORECASE):
                message_is_secret_output = bool(
                    re.search(r"(?:print|echo|log|output|write|打印|日志)", finding.message, re.IGNORECASE)
                    and re.search(r"(?:secret|token|password|key|密钥|令牌)", finding.message, re.IGNORECASE)
                )
                if message_is_secret_output:
                    matching = [
                        detector
                        for detector in detectors
                        if frozenset(
                            {
                                normalize_category(detector.category),
                                normalize_category(finding.category),
                            }
                        )
                        == frozenset({"hardcoded-secrets", "data-leak"})
                        and abs(detector.line - finding.line) <= 6
                        and any(
                            line == detector.line
                            and re.search(
                                r"\b(?:echo|printf|printenv|write-output)\b.*(?:\$\{\{\s*secrets\.|\$[A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|KEY)\b)",
                                content,
                                re.IGNORECASE,
                            )
                            for line, content in patch_lines
                        )
                    ]
            if len(matching) != 1:
                continue
            detector = matching[0]
            shared_count = sum(
                1 for identifier in identifiers if _window_contains_identifier(patch_lines, detector.line, {identifier})
            )
            proposals.append((-shared_count, abs(finding.line - detector.line), finding.id, finding, detector))

    changed: list[Finding] = []
    claimed_detectors: set[str] = set()
    claimed_reviewers: set[str] = set()
    for _score, _distance, _finding_id, finding, detector in sorted(proposals):
        if finding.id in claimed_reviewers or detector.id in claimed_detectors:
            continue
        finding.line = detector.line
        finding.category = normalize_category(detector.category)
        claimed_reviewers.add(finding.id)
        claimed_detectors.add(detector.id)
        changed.append(finding)
    return changed


def _complete_python_tree(patch: str) -> tuple[ast.Module, list[tuple[int, str]]] | None:
    """Parse a complete post-image beginning at line one, otherwise fail open."""

    new_file_hunk = re.search(r"^@@ -0,0 \+1(?:,(?P<count>\d+))? @@", patch or "", re.MULTILINE)
    if new_file_hunk is None:
        return None
    patch_lines = iter_right_lines(patch)
    if not patch_lines:
        return None
    line_numbers = [line for line, _content in patch_lines]
    declared_count = int(new_file_hunk.group("count") or 1)
    if line_numbers != list(range(1, declared_count + 1)):
        return None
    try:
        return ast.parse("\n".join(content for _line, content in patch_lines)), patch_lines
    except SyntaxError:
        return None


def _python_function_has_redirect_call(function: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Whether a function body itself invokes a known redirect API."""

    stack: list[ast.AST] = list(function.body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                call_name = node.func.id.lower()
            elif isinstance(node.func, ast.Attribute):
                call_name = node.func.attr.lower()
            else:
                call_name = ""
            if call_name in _PYTHON_REDIRECT_CALLS:
                return True
        stack.extend(ast.iter_child_nodes(node))
    return False


def _python_tree_has_redirect_call(tree: ast.Module) -> bool:
    """Whether a complete Python post-image invokes a known redirect API."""

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name):
            call_name = node.func.id.lower()
        elif isinstance(node.func, ast.Attribute):
            call_name = node.func.attr.lower()
        else:
            call_name = ""
        if call_name in _PYTHON_REDIRECT_CALLS:
            return True
    return False


def unsupported_python_open_redirect_findings(findings: list[Finding], diff_summary: str) -> list[Finding]:
    """Reject LLM open-redirect claims whose Python function has no redirect sink.

    The gate intentionally requires a complete, parseable post-image and one
    unambiguous function scope.  Truncated patches and uncertain anchors remain
    untouched so a URL builder is rejected only when the diff proves it never
    performs a redirect.
    """

    rejected: list[Finding] = []
    parsed_files: dict[str, tuple[ast.Module, list[tuple[int, str]]] | None] = {}
    for finding in findings:
        if finding.verified_by.strip().lower() in _DETECTOR_PROVENANCE:
            continue
        if normalize_category(finding.category) != "open-redirect" or not finding.file.lower().endswith(".py"):
            continue
        if finding.file not in parsed_files:
            patch = _extract_file_patch(diff_summary, finding.file)
            parsed_files[finding.file] = _complete_python_tree(patch)
        parsed = parsed_files[finding.file]
        if parsed is None:
            continue
        tree, patch_lines = parsed
        functions = [node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
        # A complete new file with no redirect API cannot support an
        # open-redirect finding, even when the model anchored it on whitespace
        # or otherwise outside the URL-builder function it describes.
        if not _python_tree_has_redirect_call(tree):
            rejected.append(finding)
            continue
        scopes = [
            function
            for function in functions
            if function.lineno <= finding.line <= (function.end_lineno or function.lineno)
        ]
        if not scopes:
            identifiers = _message_code_identifiers(finding.message, patch_lines)
            scopes = [function for function in functions if function.name.lower() in identifiers]
        if len(scopes) != 1:
            continue
        if not _python_function_has_redirect_call(scopes[0]):
            rejected.append(finding)
    return rejected
