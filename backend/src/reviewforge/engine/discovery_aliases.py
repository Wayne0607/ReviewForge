"""Short, block-local wire references with unchanged canonical evidence identity."""

from __future__ import annotations

import copy
import re
from typing import Any

from reviewforge.engine.discovery import DiscoveryBlock

_SOURCE_ROW = re.compile(r"^(?:[-+]\s*\d+|\s*before=\s*\d+|\s*\d+)\s+\[")
_REFERENCE = re.compile(r"\bsr_[0-9a-f]{12,64}\b")
_CONTEXT_ALIAS = re.compile(r"^\s*\d+ \[[^\]]+\] = (?:RIGHT \[[^\]]+\]|earlier Context)$")


def source_aliases(refs: dict[str, Any]) -> dict[str, str]:
    return {f"r{index}": reference for index, reference in enumerate(sorted(refs))}


def target_source_choices(block: DiscoveryBlock, unit_id: str, aliases: dict[str, str]) -> list[list[str]]:
    """Offer eligible references, without selecting evidence for the model.

    Mirror the source validator's changed-operation scope. Pure additions need
    no unchanged before citation; deletion-only units may use their delivered
    after context. Structural metadata/consumers retain their separate rules.
    """
    if unit_id in block.structural_changes:
        return [[], []]
    choices = []
    target = block.unit_facts.get(unit_id, [])
    for side in ("before", "after"):
        relevant = [fact for fact in target if fact.side == side]
        changed = any(fact.changed for fact in relevant)
        if side == "before" and not changed:
            choices.append([])
            continue
        eligible = []
        for alias, reference in aliases.items():
            source = block.source_refs[reference]
            raw = {"side": source.side, "path": source.path, "line": source.line, "excerpt": source.text}
            if source.side == side and block.unit_source_matches(unit_id, raw, changed=changed):
                eligible.append(alias)
        choices.append(eligible)
    return choices


def render_source_aliases(text: str, aliases: dict[str, str]) -> str:
    """Replace only coordinate metadata, never source bodies or prose."""
    reverse = {canonical: short for short, canonical in aliases.items()}
    rendered = []
    for line in text.split("\n"):
        prefix, separator, body = line.partition(" | ")
        if separator and _SOURCE_ROW.match(prefix):
            prefix = _REFERENCE.sub(lambda match: reverse.get(match[0], match[0]), prefix)
        elif not separator and _CONTEXT_ALIAS.fullmatch(line):
            prefix = _REFERENCE.sub(lambda match: reverse.get(match[0], match[0]), prefix)
        rendered.append(prefix + separator + body)
    return "\n".join(rendered)


def expand_response_refs(parsed: dict[str, Any], aliases: dict[str, str]) -> dict[str, Any]:
    """Decode only schema-designated reference slots before strict validation.

    Invalid types, unknown aliases and conflicting extra fields are preserved so
    the existing validators reject them. Claim text and source quotes are never
    rewritten; the caller retains the provider's original response separately.
    """
    result = copy.deepcopy(parsed)

    def fact(value):
        if isinstance(value, str):
            return aliases.get(value, value)
        if isinstance(value, dict) and isinstance(value.get("source_ref"), str):
            value["source_ref"] = aliases.get(value["source_ref"], value["source_ref"])
        return value

    def array(owner, name):
        if isinstance(owner, dict) and isinstance(owner.get(name), list):
            owner[name] = [fact(value) for value in owner[name]]

    for name in ("hypotheses", "no_issue_units"):
        for item in result.get(name, []) if isinstance(result.get(name), list) else []:
            if not isinstance(item, dict):
                continue
            array(item, "sites")
            array(item.get("behavior"), "source_facts")
            if "trigger_basis" in item:
                item["trigger_basis"] = fact(item["trigger_basis"])
            # Preserve optional older proof fields if supplied; no invention.
            for field in ("requirement_basis", "input_basis"):
                array(item.get("violation"), field)
    return result
