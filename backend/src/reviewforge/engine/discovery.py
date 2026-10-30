"""Deterministic, bounded discovery inputs for protocol v3.

Semantic units remain coverage identities. A discovery block is only a delivery
group: shared hunks and collected source relationships put related units next
to one another, without claiming type resolution, execution order or a defect.
The soft budgets never truncate a changed hunk. A large indivisible change can
use the original input hard cap and is explicitly labelled oversize.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field, replace
from pathlib import PurePosixPath

from reviewforge.core.change_manifest import FileChange, is_verified_rename, render_change_fact
from reviewforge.engine.context_pack import ContextPack, ContextSlice, UnitContext
from reviewforge.engine.declarations_v4 import extract_code_definitions
from reviewforge.engine.semantic_diff import SemanticChangeSet, SemanticUnit

_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_CONDITION = re.compile(r"\b(if|elif|unless|while|case|switch)\b|&&|\|\||\?|(?<!=)!(?!=)")
_OPERATIONS = (
    ("condition", _CONDITION, "Compare both true and false branches, guard polarity and the value written/returned."),
    (
        "state",
        re.compile(r"\b(set|reset|clear|delete|remove|close|release|dispose|commit|rollback)\w*\s*\("),
        "Trace the same object's state before/after the changed operation and its next observable read.",
    ),
    (
        "call",
        re.compile(r"[\w.]\s*\([^)]*\)"),
        "Compare argument values, call counts and ordering against the collected caller/callee contract.",
    ),
    (
        "operator",
        re.compile(r"(?:==|!=|<=|>=|\+=|-=|\*|/|\breturn\b)"),
        "Compare the returned/computed result for the same supported input, including the changed boundary.",
    ),
)


def _source_segments(text: str) -> list[tuple[int, str]]:
    """Bind short excerpts to columns without rendering source a second time."""
    if not text.strip():
        return []
    count = math.ceil(len(text) / 240)
    width = math.ceil(len(text) / count)
    return [
        (start, text[start : start + width])
        for start in range(0, len(text), width)
        if text[start : start + width].strip()
    ]


def _reference_labels(spans: list[tuple[str, int, int]]) -> str:
    if not spans:
        return ""
    return (
        "[" + " ".join(ref_id if len(spans) == 1 else f"{ref_id}:{start}-{end}" for ref_id, start, end in spans) + "]"
    )


def _reference_overhead(text: str) -> int:
    spans = [("sr_" + "0" * 12, start + 1, start + len(excerpt)) for start, excerpt in _source_segments(text)]
    # One separating space is added even for an unreferenced blank source line.
    return len(_reference_labels(spans)) + 1


@dataclass(frozen=True)
class DiscoveryLimits:
    target_input_chars: int = 32_000
    hard_input_chars: int = 120_000
    diff_max_chars: int = 20_000
    context_max_chars: int = 8_000
    global_context_max_chars: int | None = 40_000
    max_units: int = 12
    overview_max_chars: int = 2_000
    navigation_max_chars: int = 2_000


@dataclass(frozen=True)
class SourceLine:
    side: str
    path: str
    line: int
    text: str
    right_line: int = 0
    changed: bool = False
    kind: str = ""
    unit_id: str = ""


def compact_source_coordinates(text: str) -> str:
    """Remove coordinate padding only; preserve source bodies and reference IDs.

    Run after reference registration so delivery signatures, source selection
    and reference resolution remain independent of presentation whitespace.
    """
    lines = []
    for line in text.split("\n"):
        line = re.sub(
            r"^ before=\s*(\d+)( \[[^\]]*\])? RIGHT=\s*(\d+)( \[[^\]]*\])? \| ",
            r" before=\1\2 RIGHT=\3\4 | ",
            line,
        )
        line = re.sub(r"^([-+])\s+(\d+)( \[[^\]]*\])? \| ", r"\1\2\3 | ", line)
        line = re.sub(r"^ +(\d+)( \[[^\]]*\])? \| ", r"\1\2 | ", line)
        lines.append(line)
    return "\n".join(lines)


def compact_overview(text: str) -> str:
    """Group only contiguous same-file inventory rows, without dropping a row.

    The original bounded overview is the input: no extra inventory selection or
    reordering happens here. Unrecognized/truncated rows remain verbatim.
    """
    lines, rendered = text.split("\n"), []
    index = 0
    while index < len(lines):
        match = re.fullmatch(r"- (\S+) (.+):(\d+-\d+) (.*)", lines[index])
        if match is None:
            rendered.append(lines[index])
            index += 1
            continue
        group = [match]
        following = index + 1
        while following < len(lines):
            next_match = re.fullmatch(r"- (\S+) (.+):(\d+-\d+) (.*)", lines[following])
            if next_match is None or next_match[2] != match[2]:
                break
            group.append(next_match)
            following += 1
        original = "\n".join(lines[index:following])
        compact = "File " + match[2] + " (inventory: unit_id | RIGHT range | symbol):\n"
        compact += "\n".join(f"{row[1]} | {row[3]} | {row[4]}" for row in group)
        rendered.append(compact if len(compact) < len(original) else original)
        index = following
    return "\n".join(rendered)


@dataclass
class _Hunk:
    key: str
    path: str
    start: int
    end: int
    text: str
    facts: list[SourceLine]
    changed_text: str
    complete: bool


@dataclass
class DiscoveryBlock:
    id: str
    units: list[SemanticUnit]
    diff_text: str
    context_text: str
    overview: str
    navigation: str
    focus: str
    facts: list[SourceLine] = field(default_factory=list)
    unchecked: list[str] = field(default_factory=list)
    oversize: bool = False
    failure: str = ""
    unit_facts: dict[str, list[SourceLine]] = field(default_factory=dict)
    unit_before_states: dict[str, str] = field(default_factory=dict)
    structural_changes: dict[str, FileChange] = field(default_factory=dict)
    change_text: str = ""
    source_refs: dict[str, SourceLine] = field(default_factory=dict)
    reference_spans: dict[tuple[str, str, int], list[tuple[str, int, int]]] = field(default_factory=dict)
    source_hunks: list[_Hunk] = field(default_factory=list, repr=False)
    unit_hunk_keys: dict[str, set[str]] = field(default_factory=dict, repr=False)
    delivered_context: ContextPack = field(default_factory=ContextPack, repr=False)

    def source_matches(self, raw: object) -> bool:
        if not isinstance(raw, dict):
            return False
        if (
            not isinstance(raw.get("side"), str)
            or raw["side"] not in {"before", "after", "context"}
            or not isinstance(raw.get("line"), int)
        ):
            return False
        if isinstance(raw["line"], bool):
            return False
        excerpt = raw.get("excerpt")
        return (
            isinstance(excerpt, str)
            and bool(excerpt.strip())
            and any(
                item.side == raw["side"]
                and item.path == raw.get("path")
                and item.line == raw["line"]
                and excerpt in item.text
                for item in self.facts
            )
        )

    def resolve_source_fact(self, raw: object) -> dict[str, object] | None:
        reference = raw if isinstance(raw, str) else raw.get("source_ref") if isinstance(raw, dict) else None
        if reference is not None:
            if (
                not isinstance(reference, str)
                or reference not in self.source_refs
                or isinstance(raw, dict)
                and set(raw) != {"source_ref"}
            ):
                return None
            source = self.source_refs[reference]
            return {"side": source.side, "path": source.path, "line": source.line, "excerpt": source.text}
        return dict(raw) if isinstance(raw, dict) and "source_ref" not in raw else None

    def register_source_refs(self) -> None:
        for fact in self.facts:
            key = (fact.side, fact.path, fact.line)
            spans = self.reference_spans.setdefault(key, [])
            for start, excerpt in _source_segments(fact.text):
                identity = json.dumps([self.id, fact.side, fact.path, fact.line, excerpt], ensure_ascii=False)
                digest = hashlib.sha256(identity.encode()).hexdigest()
                ref_id = "sr_" + digest[:12]
                existing = self.source_refs.get(ref_id)
                if existing is not None and (existing.side, existing.path, existing.line, existing.text) != (
                    fact.side,
                    fact.path,
                    fact.line,
                    excerpt,
                ):
                    ref_id = "sr_" + digest
                self.source_refs.setdefault(ref_id, replace(fact, text=excerpt))
                span = (ref_id, start + 1, start + len(excerpt))
                if span not in spans:
                    spans.append(span)

    def reference_labels(self, side: str, path: str, line: int) -> str:
        return _reference_labels(self.reference_spans.get((side, path, line), []))

    def annotate_sources(self) -> None:
        rendered = []
        path = ""
        for raw in self.diff_text.splitlines():
            if raw.startswith("### Shared before/after diff: "):
                path = raw.removeprefix("### Shared before/after diff: ")
            both = re.match(r"^ before=\s*(\d+) RIGHT=\s*(\d+) \| (.*)$", raw)
            single = re.match(r"^([-+])\s*(\d+) \| (.*)$", raw)
            if both:
                old, new = int(both[1]), int(both[2])
                raw = (
                    f" before={old:>5} {self.reference_labels('before', path, old)} "
                    f"RIGHT={new:>5} {self.reference_labels('after', path, new)} | {both[3]}"
                )
            elif single:
                side, line = ("before" if single[1] == "-" else "after"), int(single[2])
                raw = f"{single[1]}{line:>5} {self.reference_labels(side, path, line)} | {single[3]}"
            rendered.append(raw)
        self.diff_text = "\n".join(rendered)
        rendered = []
        for raw in self.context_text.splitlines():
            for unit in self.units:
                prefix = f"### {unit.id} "
                if raw.startswith(prefix):
                    # Unit IDs and repository paths may both contain spaces.
                    path = raw[len(prefix) :].split(" ", 1)[1].rsplit(":", 1)[0]
                    break
            line = re.match(r"^\s*(\d+) \| (.*)$", raw)
            if line:
                number = int(line[1])
                raw = f"{number:>5} {self.reference_labels('context', path, number)} | {line[2]}"
            rendered.append(raw)
        self.context_text = "\n".join(rendered)

    def unit_source_matches(self, unit_id: str, raw: object, *, changed: bool = False) -> bool:
        if not self.source_matches(raw):
            return False
        assert isinstance(raw, dict)
        return any(
            fact.side == raw["side"]
            and fact.path == raw["path"]
            and fact.line == raw["line"]
            and raw["excerpt"] in fact.text
            and (not changed or fact.changed)
            for fact in self.unit_facts.get(unit_id, [])
        )

    def structural_source_matches(self, unit_id: str, raw: object, *, consumer: bool = False) -> bool:
        change = self.structural_changes.get(unit_id)
        if change is None or not self.source_matches(raw):
            return False
        assert isinstance(raw, dict)
        tokens = {
            change.path,
            change.previous_path,
            PurePosixPath(change.path).name,
            PurePosixPath(change.previous_path).name,
        }
        return any(
            fact.unit_id == unit_id
            and fact.side == raw["side"]
            and fact.path == raw["path"]
            and fact.line == raw["line"]
            and raw["excerpt"] in fact.text
            and (
                fact.kind == "reference"
                and any(token and token in fact.text for token in tokens)
                or not consumer
                and fact.kind == "head"
                and fact.path == change.path
            )
            for fact in self.facts
        )

    def valid_change_facts(self, unit_id: str, raw: object) -> bool:
        change = self.structural_changes.get(unit_id)
        if change is None or not isinstance(raw, list) or not 2 <= len(raw) <= 4:
            return False
        seen = set()
        for fact in raw:
            if (
                not isinstance(fact, dict)
                or fact.get("kind") != "file-change"
                or fact.get("change_id") != change.id
                or not isinstance(fact.get("field"), str)
                or fact["field"] not in {"previous_path", "path"}
                or fact.get("value") != getattr(change, fact["field"])
            ):
                return False
            seen.add(fact["field"])
        return seen == {"previous_path", "path"}


def _hunks(path: str, diff: str) -> list[_Hunk]:
    chunks: list[list[str]] = []
    active: list[str] | None = None
    for raw in (diff or "").splitlines():
        if _HEADER.match(raw):
            chunks.append([raw])
            active = chunks[-1]
        elif raw.startswith(("@@", "diff --git ")):
            active = None
        elif active is not None and raw.startswith((" ", "+", "-", "\\ No newline")):
            active.append(raw)
        elif active is not None:
            active = None
    result = []
    for index, chunk in enumerate(chunks):
        match = _HEADER.match(chunk[0])
        assert match is not None
        old, new = int(match[1]), int(match[3])
        old_left, new_left = int(match[2] or 1), int(match[4] or 1)
        rendered = [chunk[0]]
        facts = []
        changed = []
        for raw in chunk[1:]:
            prefix, text = raw[:1], raw[1:]
            if prefix == "-" and old_left > 0:
                facts.append(SourceLine("before", path, old, text, new, True))
                rendered.append(f"-{old:>5} | {text}")
                changed.append(text)
                old, old_left = old + 1, old_left - 1
            elif prefix == "+" and new_left > 0:
                facts.append(SourceLine("after", path, new, text, new, True))
                rendered.append(f"+{new:>5} | {text}")
                changed.append(text)
                new, new_left = new + 1, new_left - 1
            elif prefix == " " and old_left > 0 and new_left > 0:
                facts.extend([SourceLine("before", path, old, text, new), SourceLine("after", path, new, text, new)])
                rendered.append(f" before={old:>5} RIGHT={new:>5} | {text}")
                old, new = old + 1, new + 1
                old_left, new_left = old_left - 1, new_left - 1
            else:
                rendered.append(raw)
        first = int(match[3])
        result.append(
            _Hunk(
                f"{path}:{index}",
                path,
                first,
                first + max(1, int(match[4] or 1)) - 1,
                "\n".join(rendered),
                facts,
                "\n".join(changed),
                old_left == 0 and new_left == 0,
            )
        )
    return result


def _visible_declarations(facts: list[SourceLine], side: str, path: str) -> list[tuple[str, int, int]]:
    """Parse only contiguous delivered source; preserve its absolute coordinates.

    Unseen lines never become padding for a fabricated full-file source. A
    missing declaration is useful only with a positively added head declaration
    and no attributable removed operation; absence alone does not prove newness.
    """
    records = {fact.line: fact.text for fact in facts if fact.side == side and fact.path == path}
    runs: list[list[tuple[int, str]]] = []
    for line, text in sorted(records.items()):
        if not runs or line != runs[-1][-1][0] + 1:
            runs.append([])
        runs[-1].append((line, text))
    return [
        (declaration.name, declaration.start_line + run[0][0] - 1, declaration.line + run[0][0] - 1)
        for run in runs
        for declaration in extract_code_definitions("\n".join(text for _, text in run), path)
    ]


def _before_states(units: list[SemanticUnit], hunks: dict[str, _Hunk]) -> dict[str, str]:
    by_path: dict[str, list[SourceLine]] = {}
    for hunk in hunks.values():
        by_path.setdefault(hunk.path, []).extend(hunk.facts)
    declarations = {
        path: (_visible_declarations(facts, "before", path), _visible_declarations(facts, "after", path))
        for path, facts in by_path.items()
    }
    states = {}
    for unit in units:
        facts = by_path.get(unit.path, [])
        target = [fact for fact in facts if not unit.start_line or unit.start_line <= fact.right_line <= unit.end_line]
        old, new = declarations.get(unit.path, ([], []))
        name = unit.symbol.rsplit(".", 1)[-1]
        head = [line for symbol, start, line in new if symbol == name and unit.start_line in {start, line}]
        added_declaration = bool(head) and any(
            fact.side == "after" and fact.changed and fact.line in head for fact in target
        )
        old_name = any(symbol == name for symbol, _, _ in old)
        removed_operation = any(fact.side == "before" and fact.changed for fact in target)
        if facts and not any(fact.side == "before" for fact in facts):
            states[unit.id] = "absent-before-in-delivered-source"
        elif added_declaration and not old_name and not removed_operation:
            states[unit.id] = "absent-new-declaration"
        elif old_name:
            states[unit.id] = "present-declaration"
        else:
            states[unit.id] = "unresolved-declaration-correspondence"
    return states


def _bounded_lines(lines: list[str], limit: int, label: str) -> str:
    kept: list[str] = []
    size = 0
    for line in lines:
        if size + len(line) + 1 > max(0, limit) - 70:
            break
        kept.append(line)
        size += len(line) + 1
    omitted = len(lines) - len(kept)
    if omitted and limit >= 70:
        kept.append(f"({omitted} {label} omitted from this bounded navigation; not assessed here.)")
    return "\n".join(kept)[: max(0, limit)]


def _overview(changeset: SemanticChangeSet, pack: ContextPack, limit: int) -> str:
    intent = (pack.pr_intent or "(no supplied intent)")[: min(1_000, max(0, limit // 2))]
    header = f"PR head={changeset.head_sha}; {len(changeset.units)} semantic units.\nIntent: {intent}\n"
    inventory = [
        f"- {u.id} {u.path}:{u.start_line}-{u.end_line} {u.symbol or '(resource)'}"
        for u in sorted(changeset.units, key=lambda u: (-u.risk_score, u.id))
    ]
    return (header + _bounded_lines(inventory, max(0, limit - len(header)), "PR units"))[:limit]


def _context_header(unit_id: str, slice_: ContextSlice, start: int, end: int) -> str:
    return (
        f"### {unit_id} {slice_.kind} {slice_.path}:{start}-{end}\nNavigation reason (not evidence): {slice_.reason}\n"
    )


def _focused_context_bounds(unit_id: str, slice_: ContextSlice, lines: list[str], budget: int) -> tuple[int, int]:
    """Choose complete consecutive collected lines around a known source hit.

    Return half-open offsets in the original slice. A line that cannot fit is
    omitted whole; no prefix fallback may pretend the hit was delivered.
    """
    focus = slice_.focus_line - slice_.start_line
    prefix = [0]
    for offset, text in enumerate(lines):
        number = slice_.start_line + offset
        prefix.append(prefix[-1] + len(f"{number:>5} | {text}") + _reference_overhead(text) + 1)

    def fits(start: int, end: int) -> bool:
        header = _context_header(unit_id, slice_, slice_.start_line + start, slice_.start_line + end - 1)
        return len(header) + 2 + prefix[end] - prefix[start] <= budget

    if fits(0, len(lines)):
        return 0, len(lines)
    start, end = focus, focus + 1
    if not fits(start, end):
        return focus, focus
    blocked = set()
    while True:
        choices = []
        if start > 0 and "before" not in blocked:
            choices.append((focus - start + 1, "before", start - 1, end))
        if end < len(lines) and "after" not in blocked:
            choices.append((end - focus, "after", start, end + 1))
        if not choices:
            return start, end
        _, side, left, right = min(choices, key=lambda choice: (choice[0], choice[1] != "before"))
        if fits(left, right):
            start, end = left, right
        else:
            blocked.add(side)


def _context(
    units: list[SemanticUnit], pack: ContextPack, budget: int
) -> tuple[str, list[SourceLine], list[str], ContextPack]:
    sections, facts, unchecked = [], [], []
    seen: dict[tuple, str] = {}
    seen_facts: dict[tuple, list[SourceLine]] = {}
    delivered = ContextPack(protocol_version=pack.protocol_version)
    per_unit = max(0, budget // max(1, len(units)))
    for unit in units:
        ctx = pack.units.get(unit.id)
        if ctx is None:
            continue
        remaining = per_unit
        delivered.units[unit.id] = UnitContext(unit.id)
        for slice_ in ctx.slices:
            key = (slice_.path, slice_.start_line, slice_.end_line, slice_.sha, slice_.text)
            header = _context_header(unit.id, slice_, slice_.start_line, slice_.end_line)
            if key in seen:
                piece = header + f"Same delivered source as {seen[key]}."
                if len(piece) + 2 <= remaining:
                    sections.append(piece)
                    remaining -= len(piece) + 2
                    facts.extend(
                        SourceLine(item.side, item.path, item.line, item.text, kind=slice_.kind, unit_id=unit.id)
                        for item in seen_facts[key]
                    )
                    delivered.units[unit.id].slices.append(replace(slice_))
                else:
                    unchecked.append(f"{unit.id}: context {slice_.kind} omitted by context budget")
                continue
            lines = slice_.text.splitlines()
            focused = (
                pack.protocol_version >= 4
                and type(slice_.focus_line) is int
                and slice_.start_line <= slice_.focus_line <= slice_.end_line
                and slice_.focus_line - slice_.start_line < len(lines)
            )
            first, last = 0, len(lines)
            if focused:
                lines = lines[: slice_.end_line - slice_.start_line + 1]
                first, last = _focused_context_bounds(unit.id, slice_, lines, remaining)
                if first == last:
                    unchecked.append(
                        f"{unit.id}: context {slice_.kind} focus line {slice_.focus_line} "
                        "and surrounding source omitted by context budget"
                    )
                    continue
                header = _context_header(unit.id, slice_, slice_.start_line + first, slice_.start_line + last - 1)
                if first:
                    unchecked.append(
                        f"{unit.id}: context {slice_.kind} lines {slice_.start_line}-{slice_.start_line + first - 1} "
                        "omitted before focused window"
                    )
                if last < len(lines):
                    unchecked.append(
                        f"{unit.id}: context {slice_.kind} lines {slice_.start_line + last}-{slice_.end_line} "
                        "omitted after focused window"
                    )
            rendered, selected = [], []
            size = len(header) + 2
            for offset in range(first, last):
                text = lines[offset]
                line = slice_.start_line + offset
                if line > slice_.end_line:
                    break
                numbered = f"{line:>5} | {text}"
                source_size = len(numbered) + _reference_overhead(text)
                if size + source_size + 1 > remaining:
                    break
                rendered.append(numbered)
                selected.append(SourceLine("context", slice_.path, line, text, kind=slice_.kind, unit_id=unit.id))
                size += source_size + 1
            if rendered:
                piece = header + "\n".join(rendered)
                sections.append(piece)
                remaining -= size
                facts.extend(selected)
                delivered.units[unit.id].slices.append(
                    replace(
                        slice_,
                        start_line=selected[0].line,
                        end_line=selected[-1].line,
                        text="\n".join(item.text for item in selected) + ("\n" if selected[-1].text == "" else ""),
                    )
                )
                # A shared reference can only reuse the actually delivered prefix.
                if len(rendered) == len(lines):
                    seen[key] = f"{unit.id} {slice_.kind} {slice_.path}:{slice_.start_line}-{slice_.end_line}"
                    seen_facts[key] = selected
            if len(rendered) < len(lines):
                unchecked.append(f"{unit.id}: context {slice_.kind} only {len(rendered)}/{len(lines)} lines delivered")
        unchecked.extend(f"{unit.id}: unavailable context {kind}" for kind in ctx.truncated_kinds)
    return "\n\n".join(sections), facts, unchecked, delivered


def _block(
    units: list[SemanticUnit],
    pack: ContextPack,
    overview: str,
    selected: list[_Hunk],
    limits: DiscoveryLimits,
    context_budget: int,
    before_states: dict[str, str] | None = None,
    structural_changes: dict[str, FileChange] | None = None,
    unit_hunk_keys: dict[str, set[str]] | None = None,
) -> DiscoveryBlock:
    diffs = "\n\n".join(f"### Shared before/after diff: {h.path}\n{h.text}" for h in selected)
    context, context_facts, unchecked, delivered_context = _context(units, pack, context_budget)
    nav = []
    for unit in units:
        ctx = pack.units.get(unit.id)
        if ctx is not None:
            nav.extend(
                f"- {unit.id} -> {s.kind} {s.path}:{s.start_line}-{s.end_line} {s.symbol}: {s.reason}"
                for s in ctx.slices
            )
        nav.extend(f"- {unit.id} -> candidate test {path}" for path in unit.candidate_tests)
    changed = "\n".join(h.changed_text for h in selected)
    focus = "\n".join(f"- {kind}: {guide}" for kind, pattern, guide in _OPERATIONS if pattern.search(changed))
    signature = "\n".join(u.id for u in units) + "\n" + "\n".join(h.key + ":" + h.text for h in selected)
    changes = {unit.id: structural_changes[unit.id] for unit in units if unit.id in (structural_changes or {})}
    change_text = "\n\n".join(
        f"### Structural file change for {unit_id}\n{render_change_fact(change)}" for unit_id, change in changes.items()
    )
    if changes:
        focus += (
            "\n- path-contract: Compare old/new paths with actual consumers and relative/resource lookup; "
            "identical blobs do not prove compatible path behavior."
        )
    if change_text:
        signature += "\n" + change_text
    identity = "db_" + hashlib.sha256(signature.encode()).hexdigest()[:16]
    missing = [
        f"{u.id}: no source diff hunk was supplied"
        for u in units
        if not any(h.path == u.path for h in selected) and u.id not in changes
    ]
    material_missing = [
        unit_id
        for unit_id in changes
        if not any(fact.kind in {"head", "reference"} for fact in context_facts if fact.unit_id == unit_id)
    ]
    block = DiscoveryBlock(
        identity,
        units,
        diffs,
        context,
        overview,
        _bounded_lines(nav, limits.navigation_max_chars, "source relationships"),
        focus,
        [fact for h in selected for fact in h.facts] + context_facts,
        unchecked + missing,
        oversize=len(diffs) + len(change_text) > limits.diff_max_chars,
        failure=(
            "missing-diff"
            if missing
            else "incomplete-diff"
            if any(not h.complete for h in selected)
            else "missing-structural-material"
            if material_missing
            else ""
        ),
    )
    block.structural_changes = changes
    block.change_text = change_text
    block.source_hunks = selected
    block.unit_hunk_keys = {unit.id: set((unit_hunk_keys or {}).get(unit.id, [])) for unit in units}
    block.delivered_context = delivered_context
    # Source identity and coverage identity are separate. Quotes from a nearby
    # function in the same file cannot complete this unit's behavior check.
    # Before deletions use their post-image boundary, since old coordinates
    # need not match the head symbol coordinates after preceding insertions.
    for unit in units:
        state = (before_states or {}).get(unit.id, "unresolved-declaration-correspondence")
        if unit.id in changes:
            state = "structural-rename"
        block.unit_before_states[unit.id] = state
        block.unit_facts[unit.id] = [
            fact
            for h in selected
            for fact in h.facts
            if fact.path == unit.path
            and (not unit.start_line or unit.start_line <= fact.right_line <= unit.end_line)
            and (fact.side != "before" or not state.startswith("absent-"))
        ]
    block.register_source_refs()
    block.annotate_sources()
    block.oversize = len(block.diff_text) + len(block.change_text) > limits.diff_max_chars
    return block


def plan_discovery(
    changeset: SemanticChangeSet,
    pack: ContextPack,
    diffs: dict[str, str],
    *,
    limits: DiscoveryLimits,
    overview_changeset: SemanticChangeSet | None = None,
    file_changes: dict[str, FileChange] | None = None,
) -> list[DiscoveryBlock]:
    """Cover each unit exactly once, preferring shared hunks then source relations.

    A hunk unmatched by symbol coordinates is assigned to the nearest unit in
    its file. This preserves changes outside parsed declarations, too. Relation
    edges are navigation only and never used as proof of supported input.
    """
    remaining = sorted(changeset.units, key=lambda u: (-u.risk_score, u.id))
    by_path: dict[str, list[SemanticUnit]] = {}
    for unit in remaining:
        by_path.setdefault(unit.path, []).append(unit)
    all_hunks = {h.key: h for path in by_path for h in _hunks(path, diffs.get(path, ""))}
    structural_changes = {
        unit.id: change
        for unit in remaining
        if isinstance((change := (file_changes or {}).get(unit.path)), FileChange)
        and change.path == unit.path
        and change.repo == changeset.repo
        and is_verified_rename(change, changeset.head_sha)
        and not any(hunk.path == unit.path for hunk in all_hunks.values())
    }
    change_chars = {
        unit_id: len(f"### Structural file change for {unit_id}\n") + len(render_change_fact(change))
        for unit_id, change in structural_changes.items()
    }
    before_states = _before_states(remaining, all_hunks)
    hunk_chars = {key: len(f"### Shared before/after diff: {h.path}\n") + len(h.text) for key, h in all_hunks.items()}
    keys: dict[str, set[str]] = {u.id: set() for u in remaining}
    for h in all_hunks.values():
        matches = [
            u
            for u in by_path[h.path]
            if not u.start_line or h.start <= u.end_line + 3 and h.end >= max(1, u.start_line - 3)
        ]
        if not matches:
            matches = [min(by_path[h.path], key=lambda u: (abs(u.start_line - h.start), u.id))]
        for unit in matches:
            keys[unit.id].add(h.key)
    related: dict[str, set[str]] = {u.id: set() for u in remaining}
    for unit in remaining:
        ctx = pack.units.get(unit.id)
        paths = {s.path for s in ctx.slices} if ctx is not None else set()
        paths.update(unit.candidate_tests)
        for other in remaining:
            if other.path in paths and other.id != unit.id:
                related[unit.id].add(other.id)
                related[other.id].add(unit.id)
    overview = _overview(overview_changeset or changeset, pack, limits.overview_max_chars)
    context_left = None if limits.global_context_max_chars is None else max(0, limits.global_context_max_chars)
    blocks = []

    def make(units: list[SemanticUnit]) -> DiscoveryBlock:
        selected_keys = set().union(*(keys[u.id] for u in units))
        selected = [
            all_hunks[k] for k in sorted(selected_keys, key=lambda k: (all_hunks[k].path, all_hunks[k].start, k))
        ]
        return _block(
            units,
            pack,
            overview,
            selected,
            limits,
            limits.context_max_chars if context_left is None else min(limits.context_max_chars, context_left),
            before_states,
            structural_changes,
            keys,
        )

    while remaining:
        group = [remaining.pop(0)]
        block = make(group)
        while remaining and not block.failure and len(group) < max(1, limits.max_units):
            group_keys = set().union(*(keys[u.id] for u in group))
            group_relations = set().union(*(related[u.id] for u in group))
            choices = sorted(
                remaining,
                key=lambda u: (-len(keys[u.id] & group_keys), u.id not in group_relations, -u.risk_score, u.id),
            )
            chosen = None
            for unit in choices:
                if (
                    not keys[unit.id]
                    and unit.id not in structural_changes
                    or any(not all_hunks[key].complete for key in keys[unit.id])
                ):
                    continue
                if unit.id in structural_changes and not any(
                    slice_.kind in {"head", "reference"}
                    for slice_ in (pack.units[unit.id].slices if unit.id in pack.units else [])
                ):
                    continue
                candidate_keys = group_keys | keys[unit.id]
                diff_chars = sum(hunk_chars[key] for key in candidate_keys) + max(0, len(candidate_keys) - 1) * 2
                diff_chars += sum(change_chars.get(item.id, 0) for item in [*group, unit])
                # Reject impossible unions before rendering source, context,
                # unit fact maps and their stable hash. The key union preserves
                # exactly the shared-hunk test used by the full renderer.
                shares_oversize = block.oversize and candidate_keys == group_keys and unit.id not in structural_changes
                if diff_chars > limits.diff_max_chars and not shares_oversize:
                    continue
                candidate = make([*group, unit])
                if candidate.failure:
                    continue
                estimate = len(candidate.diff_text) + len(candidate.context_text) + len(candidate.overview)
                estimate += len(candidate.change_text)
                estimate += (
                    len(candidate.navigation)
                    + len(candidate.focus)
                    + 1_000
                    + sum(len(u.id) + 60 for u in candidate.units)
                )
                # An oversize hunk can still be shared without expanding it.
                shared_oversize = (
                    block.oversize and candidate_keys == group_keys and candidate.change_text == block.change_text
                )
                if (
                    len(candidate.diff_text) + len(candidate.change_text) <= limits.diff_max_chars
                    and estimate <= limits.target_input_chars
                    or shared_oversize
                    and estimate <= limits.hard_input_chars
                ):
                    chosen, block = unit, candidate
                    break
            if chosen is None:
                break
            group.append(chosen)
            remaining.remove(chosen)
        if context_left is not None:
            context_left -= len(block.context_text)
        blocks.append(block)
    return blocks


def split_discovery(block: DiscoveryBlock, limits: DiscoveryLimits) -> list[DiscoveryBlock]:
    """Bisect unit delivery without removing any attributable source material.

    No source collection runs here. Children retain their complete original
    hunks and all actually delivered context, including shared reference bodies.
    The caller enforces the aggregate context and physical request budgets.
    """
    if len(block.units) < 2 or block.failure:
        return []
    midpoint = (len(block.units) + 1) // 2
    children = []
    for units in (block.units[:midpoint], block.units[midpoint:]):
        keys = set().union(*(block.unit_hunk_keys.get(unit.id, set()) for unit in units))
        if any(unit.id not in block.structural_changes and not block.unit_hunk_keys.get(unit.id) for unit in units):
            return []
        # The generous renderer allowance prevents a second clipping of the
        # saved slices. The resulting text still must fit the original cap.
        child = _block(
            units,
            block.delivered_context,
            block.overview,
            [hunk for hunk in block.source_hunks if hunk.key in keys],
            limits,
            max(limits.context_max_chars, len(block.context_text)) * len(units) + 2_000,
            block.unit_before_states,
            block.structural_changes,
            block.unit_hunk_keys,
        )
        ids = {unit.id for unit in units}
        required = {
            (f.side, f.path, f.line, f.text, f.kind, f.unit_id)
            for f in block.facts
            if f.side == "context" and f.unit_id in ids
        }
        actual = {(f.side, f.path, f.line, f.text, f.kind, f.unit_id) for f in child.facts if f.side == "context"}
        if child.failure or len(child.context_text) > limits.context_max_chars or not required <= actual:
            return []
        child.navigation = block.navigation
        child.unchecked = list(
            dict.fromkeys(
                child.unchecked
                + [item for item in block.unchecked if any(item.startswith(unit.id + ":") for unit in units)]
            )
        )
        children.append(child)
    return children


__all__ = ["DiscoveryBlock", "DiscoveryLimits", "SourceLine", "plan_discovery", "split_discovery"]
