"""Deterministic evidence from repeated sibling implementations.

The engine intentionally recognizes only narrow, high-signal contradictions:

* an added call bypasses a locally enriched logger/context alias even though
  sibling methods pass that alias to the same callee and argument position;
* an added telemetry call is the sole argument outlier while at least three
  independent sibling methods agree on the contract-shaped argument.

It does not infer that arbitrary repeated code is correct.  The resulting
findings still pass through the normal verifier, calibrator and publication
gate.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from typing import Any

from reviewforge.core.state import Finding
from reviewforge.engine.detectors.unified_diff import iter_added_lines
from reviewforge.engine.symbol_extractor import SymbolInfo, extract_definitions

_CALL_START = re.compile(r"(?P<callee>[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*)\s*\(")
_ALIAS_BINDING = re.compile(
    r"\b(?P<alias>log(?:ger)?|ctx|context)\s*(?::=|=(?!=))\s*"
    r"(?P<base>[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*)"
    r"(?:\.[A-Za-z_$][\w$]*\([^;\n]*\))*"
    r"\.(?P<enricher>WithValues|WithFields|WithField|WithName|WithContext|With)\s*\(",
    re.IGNORECASE,
)
_TELEMETRY_CALLEE = re.compile(
    r"(?:record|metric|counter|histogram|observe|duration|latency)",
    re.IGNORECASE,
)
_CONTRACT_ARGUMENT = re.compile(r"^(?:[A-Za-z_$][\w$]*\.)+[A-Za-z_$][\w$]*$|^[A-Za-z_$][\w$]*$")


@dataclass(frozen=True, slots=True)
class SiblingInvariant:
    """Serializable proof that one added expression breaks a repeated contract."""

    kind: str
    file: str
    line: int
    symbol: str
    callee: str
    argument_index: int
    actual: str
    expected: str
    support_lines: tuple[int, ...]
    support_symbols: tuple[str, ...]
    confidence: float

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["support_lines"] = list(self.support_lines)
        payload["support_symbols"] = list(self.support_symbols)
        return payload


@dataclass(frozen=True, slots=True)
class _CallSite:
    callee: str
    args: tuple[str, ...]
    line: int
    symbol: str


def _normalize_expression(expression: str) -> str:
    return re.sub(r"\s+", "", expression or "")


def _split_arguments(text: str) -> tuple[str, ...]:
    """Split a single call's arguments while respecting nested delimiters."""

    args: list[str] = []
    start = 0
    depth = 0
    quote = ""
    escaped = False
    pairs = {"(": ")", "[": "]", "{": "}"}
    closing = set(pairs.values())
    stack: list[str] = []
    for index, character in enumerate(text):
        if quote:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                quote = ""
            continue
        if character in {"'", '"', "`"}:
            quote = character
            continue
        if character in pairs:
            stack.append(pairs[character])
            depth += 1
            continue
        if character in closing:
            if stack and character == stack[-1]:
                stack.pop()
                depth -= 1
            continue
        if character == "," and depth == 0:
            args.append(text[start:index].strip())
            start = index + 1
    tail = text[start:].strip()
    if tail or args:
        args.append(tail)
    return tuple(args)


def _balanced_call(line: str, match: re.Match[str]) -> tuple[str, ...] | None:
    """Return arguments when the complete call is present on one source line."""

    opening = line.find("(", match.start())
    if opening < 0:
        return None
    depth = 0
    quote = ""
    escaped = False
    for index in range(opening, len(line)):
        character = line[index]
        if quote:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                quote = ""
            continue
        if character in {"'", '"', "`"}:
            quote = character
        elif character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
            if depth == 0:
                return _split_arguments(line[opening + 1 : index])
    return None


def _owning_symbol(definitions: list[SymbolInfo], line: int) -> SymbolInfo | None:
    matches = [
        symbol
        for symbol in definitions
        if (symbol.start_line or symbol.line) <= line <= (symbol.end_line or symbol.line)
    ]
    return min(matches, key=lambda symbol: (symbol.end_line or line) - (symbol.start_line or line), default=None)


def _extract_calls(content: str, definitions: list[SymbolInfo]) -> list[_CallSite]:
    calls: list[_CallSite] = []
    for line_number, source_line in enumerate(content.splitlines(), start=1):
        symbol = _owning_symbol(definitions, line_number)
        if symbol is None:
            continue
        for match in _CALL_START.finditer(source_line):
            args = _balanced_call(source_line, match)
            if args is None:
                continue
            calls.append(
                _CallSite(
                    callee=match.group("callee"),
                    args=tuple(_normalize_expression(arg) for arg in args),
                    line=line_number,
                    symbol=symbol.name,
                )
            )
    return calls


def _local_aliases(content: str, definitions: list[SymbolInfo]) -> dict[str, dict[str, str]]:
    """Map symbol -> enriched alias -> base expression."""

    aliases: dict[str, dict[str, str]] = defaultdict(dict)
    for line_number, source_line in enumerate(content.splitlines(), start=1):
        symbol = _owning_symbol(definitions, line_number)
        if symbol is None:
            continue
        for match in _ALIAS_BINDING.finditer(source_line):
            aliases[symbol.name][_normalize_expression(match.group("alias"))] = _normalize_expression(
                match.group("base")
            )
    return dict(aliases)


def _alias_bypass_invariants(
    calls: list[_CallSite],
    aliases: dict[str, dict[str, str]],
    added_lines: set[int],
    file_path: str,
) -> list[SiblingInvariant]:
    groups: dict[tuple[str, int], list[_CallSite]] = defaultdict(list)
    for call in calls:
        for index in range(len(call.args)):
            groups[(call.callee, index)].append(call)

    results: list[SiblingInvariant] = []
    for call in calls:
        if call.line not in added_lines:
            continue
        symbol_aliases = aliases.get(call.symbol, {})
        for index, actual in enumerate(call.args):
            matching_alias = next((alias for alias, base in symbol_aliases.items() if base == actual), "")
            if not matching_alias:
                continue
            siblings = [
                sibling
                for sibling in groups[(call.callee, index)]
                if sibling.symbol != call.symbol
                and index < len(sibling.args)
                and sibling.args[index] in aliases.get(sibling.symbol, {})
            ]
            if len({sibling.symbol for sibling in siblings}) < 2:
                continue
            expected_counts = Counter(sibling.args[index] for sibling in siblings)
            expected, count = expected_counts.most_common(1)[0]
            # Logger aliases commonly share the same short name across methods.
            if count < 2 or expected != matching_alias:
                continue
            support = [sibling for sibling in siblings if sibling.args[index] == expected]
            results.append(
                SiblingInvariant(
                    kind="enriched-alias-bypass",
                    file=file_path,
                    line=call.line,
                    symbol=call.symbol,
                    callee=call.callee,
                    argument_index=index,
                    actual=actual,
                    expected=expected,
                    support_lines=tuple(sibling.line for sibling in support[:4]),
                    support_symbols=tuple(dict.fromkeys(sibling.symbol for sibling in support))[:4],
                    confidence=0.98,
                )
            )
    return results


def _telemetry_argument_invariants(
    calls: list[_CallSite],
    added_lines: set[int],
    file_path: str,
) -> list[SiblingInvariant]:
    groups: dict[tuple[str, int], list[_CallSite]] = defaultdict(list)
    for call in calls:
        if not _TELEMETRY_CALLEE.search(call.callee):
            continue
        for index, argument in enumerate(call.args):
            if _CONTRACT_ARGUMENT.fullmatch(argument):
                groups[(call.callee, index)].append(call)

    results: list[SiblingInvariant] = []
    for (callee, index), members in groups.items():
        for outlier in (member for member in members if member.line in added_lines):
            support_by_value: dict[str, dict[str, _CallSite]] = defaultdict(dict)
            for member in members:
                if member.line == outlier.line or member.symbol == outlier.symbol:
                    continue
                support_by_value[member.args[index]].setdefault(member.symbol, member)
            if not support_by_value:
                continue
            expected, support_by_symbol = max(
                support_by_value.items(),
                key=lambda pair: (len(pair[1]), pair[0]),
            )
            independent_symbols = {member.symbol for member in members if member.symbol != outlier.symbol}
            if (
                expected == outlier.args[index]
                or len(support_by_symbol) < 3
                or len(support_by_symbol) / max(1, len(independent_symbols)) < 0.75
            ):
                continue
            # The proposed outlier must be unique across independent methods;
            # otherwise this is a legitimate second convention, not a broken
            # repeated invariant.
            if any(member.symbol != outlier.symbol and member.args[index] == outlier.args[index] for member in members):
                continue
            support = list(support_by_symbol.values())
            results.append(
                SiblingInvariant(
                    kind="telemetry-argument-outlier",
                    file=file_path,
                    line=outlier.line,
                    symbol=outlier.symbol,
                    callee=callee,
                    argument_index=index,
                    actual=outlier.args[index],
                    expected=expected,
                    support_lines=tuple(member.line for member in support[:4]),
                    support_symbols=tuple(member.symbol for member in support[:4]),
                    confidence=0.94,
                )
            )
    return results


def analyze_sibling_invariants(content: str, file_path: str, patch: str) -> tuple[SiblingInvariant, ...]:
    """Extract conservative sibling contradictions from one complete source file."""

    if not content or not patch:
        return ()
    added_lines = {line for line, _text in iter_added_lines(patch)}
    if not added_lines:
        return ()
    definitions = [symbol for symbol in extract_definitions(content, file_path) if symbol.end_line]
    if len(definitions) < 3:
        return ()
    calls = _extract_calls(content, definitions)
    aliases = _local_aliases(content, definitions)
    candidates = [
        *_alias_bypass_invariants(calls, aliases, added_lines, file_path),
        *_telemetry_argument_invariants(calls, added_lines, file_path),
    ]
    deduped: dict[tuple[str, int, str, int], SiblingInvariant] = {}
    for candidate in candidates:
        deduped.setdefault((candidate.kind, candidate.line, candidate.callee, candidate.argument_index), candidate)
    return tuple(deduped.values())


def findings_from_sibling_invariants(manifest: dict[str, Any] | None) -> list[Finding]:
    """Convert manifest evidence into normal candidates for the review pipeline."""

    findings: list[Finding] = []
    for item in (manifest or {}).get("sibling_invariants", []):
        kind = str(item.get("kind") or "")
        support_symbols = ", ".join(str(value) for value in item.get("support_symbols", [])[:4])
        argument_position = int(item.get("argument_index", 0)) + 1
        if kind == "enriched-alias-bypass":
            message = (
                f"{item.get('symbol')} passes `{item.get('actual')}` to `{item.get('callee')}` "
                f"at argument {argument_position}, bypassing the locally enriched "
                f"`{item.get('expected')}` context. Sibling methods ({support_symbols}) "
                "consistently propagate the enriched alias, so its structured fields are lost here."
            )
            suggestion = f"Pass `{item.get('expected')}` at argument {argument_position}."
            category = "missing-context-field"
            severity = "warning"
        elif kind == "telemetry-argument-outlier":
            message = (
                f"{item.get('symbol')} passes `{item.get('actual')}` to telemetry call "
                f"`{item.get('callee')}` at argument {argument_position}, while independent "
                f"sibling methods ({support_symbols}) consistently pass `{item.get('expected')}`. "
                "This breaks the repeated metric-label contract."
            )
            suggestion = (
                f"Use `{item.get('expected')}` at argument {argument_position}, "
                "or document and test why this operation intentionally uses a different label dimension."
            )
            category = "wrong-argument-contract"
            severity = "error"
        else:
            continue
        findings.append(
            Finding(
                file=str(item["file"]),
                line=max(1, int(item["line"])),
                severity=severity,
                category=category,
                message=message,
                suggestion=suggestion,
                confidence=float(item.get("confidence", 0.9)),
                reviewer="correctness_reviewer",
                status="candidate",
                verified_by="detector-sibling-invariant",
                verify_reason="Derived from repeated independent sibling implementations.",
            )
        )
    return findings
