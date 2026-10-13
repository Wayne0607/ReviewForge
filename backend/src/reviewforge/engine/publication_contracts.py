"""Addressable independent contracts and narrow, explicit provenance rules.

This does not decide whether a defect exists. Resource rules are bound to an
observed locale path; exception-type claims need a real type-sensitive consumer,
not an inference that a Java throws declaration excludes unchecked exceptions.
"""

from __future__ import annotations

import hashlib
import json
import re

from reviewforge.engine.symbol_extractor import mask_non_code


def _exception_type_claim(item) -> set[str]:
    if item.mechanism.value != "error-path" or not any(site.path.endswith(".java") for site in item.sites):
        return set()
    names = set(re.findall(r"\b[A-Z]\w*(?:Exception|Error)\b", item.claim + " " + item.impact))
    return names if len(names) >= 2 else set()


def _type_sensitive_consumer(contract, refs, types):
    if not types:
        return True
    names = "(?:" + "|".join(re.escape(name) for name in sorted(types)) + ")"
    for ref in contract["evidence"]:
        code = mask_non_code(refs[ref]["quote"], "java")
        if re.search(r"\bcatch\s*\([^)]*\b" + names + r"\b", code) or re.search(
            r"\b(?:assertThrows|assertThrowsExactly|isInstanceOf)\s*\(\s*" + names + r"\s*\.class\b", code
        ):
            return True
    return False


def contract_catalog(item, ledger):
    from reviewforge.engine.proof_gate import _references

    refs = _references(item)
    types = _exception_type_claim(item)
    contracts = []
    for row in ledger.source_audits.get(item.id, {}).get("facts", {}).get("contracts", []):
        if _type_sensitive_consumer(row, refs, types):
            contracts.append({"origin": "independent-source-audit", **row})
    # A declared-locale rule is already part of the approved review contract.
    # Bind that rule deterministically instead of asking the model to invent a
    # caller to establish it. It imposes no exact wording or formatter semantics.
    if item.mechanism.value == "i18n":
        observed = {observation.id: observation for observation in item.observations}
        targets = {site.path for site in item.sites}
        if item.change_anchor is not None:
            targets.add(item.change_anchor.path)
        for reference, source in refs.items():
            observation = observed[source["observation_id"]]
            match = re.search(
                r"(?:^|/)messages_([a-z]{2,3}(?:[_-][A-Za-z0-9]+)*)\.(?:properties|json)$", observation.path
            )
            if match and observation.path in targets:
                contracts.append(
                    {
                        "origin": "program-bound-localization-rule",
                        "kind": "configuration",
                        "statement": f"The observed resource {observation.path} declares locale {match[1]}; "
                        "localized text must remain coherent in that locale. "
                        "This does not require preserving old wording, "
                        "links, markup, or unverified formatter/runtime behavior.",
                        "evidence": [
                            ref
                            for ref, fact in refs.items()
                            if observed[fact["observation_id"]].path == observation.path
                        ],
                    }
                )
                break
    return {
        item.id
        + "/contract_"
        + hashlib.sha256(json.dumps(row, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:10]: row
        for row in contracts
    }
