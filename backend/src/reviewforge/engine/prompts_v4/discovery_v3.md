# Discovery protocol v3 — {{lens}}

Review only defects introduced by this PR. This is one bounded group of related
changes, not the entire PR. The PR overview and relationship navigation retain
the wider design; navigation, existing candidates and attention cues are not
proof of execution order, a supported input or a defect. Review the supplied
before AND after source for the same input before deciding whether behavior
changes. For a changed conditional, explicitly compare true and false branches,
including which values are written or omitted. Intent alone does not establish
equivalence or correctness.

Shared hunks are supplied once for all the units that use them. `- line [sr_ID] | code`
is before source, `+ line [sr_ID] | code` is after source, and unchanged
`before=LEFT [sr_ID] RIGHT=NEW [sr_OTHER] | code` has two explicit coordinates. A before fact uses
LEFT; an after fact or site uses RIGHT. Do not copy RIGHT into a before fact.
Context lines are separately numbered and require side=context even when the
same text also appears in diff; alternatively cite its exact diff side/line.
Use `{"source_ref":"sr_ID"}` for source_facts, trigger_basis, sites and violation
requirement_basis/input_basis. Copy a
delivered ID exactly; the program resolves its side, path, line and exact source
excerpt. IDs belong only to this block. Long lines show `sr_ID:start-end` column
ranges for short fragments of the same source body; copy only the ID, without
the column suffix. The source text is not repeated in an index. Legacy explicit
side/path/line/excerpt facts also remain valid when the excerpt is an exact
substring of the delivered line. Copy such excerpts from the code after `|`;
preserve any `|` in the code itself. Only after/RIGHT
locations can become `sites`. Facts must cite delivered source, not navigation,
an omitted slice, a hypothetical call, or an invented input.

First state the behavior before and after, and its observable result. A real
observable change may be compatible with the contract or repair an earlier
defect. That comparison alone does not establish a regression. A candidate
also requires a separate violation: the required behavior and its real source
basis, the after behavior incompatible with that requirement, the negative
consequence, and a supported input/lifecycle with its real source basis. A
before image alone does not establish that its behavior was required. References
must support their assigned role; an unrelated caller, resource key or code
line is not a supported input merely because it was delivered in this block.
Do not manufacture a requirement to preserve a previous defect, formatting or
wording. If you cannot establish the consumer requirement or a harmful after
result, explicitly defer that boundary rather than allocate a defect candidate.
For a compatible behavior change, no_issue.checked must explain which supplied
contract remains satisfied and what was compared. Then ask one factual question
whose answer determines the proposed regression, and state what would refute it.
These obligations are still unconfirmed discovery navigation; investigation
must independently prove the requirement, supported input, after violation and
harm in the same direction. Source resolution does not prove semantic support.

Keep independent triggers/impacts distinct. Combine sites only for the same
root cause, trigger and consequence. An existing OPEN candidate is not proven;
when it covers the same risk, explain the concrete check and cite that identity
in `checked`, rather than creating a duplicate. Styles, naming, requests for
more tests/documentation, intended negative fixtures and pre-existing defects
are outside this review. Missing context cannot prove a defect or clean result.

Return one JSON object. Each allowed unit must have a hypothesis, a concrete
`no_issue_units` check, or an explicit `deferred_units` reason. Independent
hypotheses may share a unit. A hypothesis may coexist with deferred_units when
another hunk, supported input or contract remains unchecked; state that boundary
explicitly. The candidate is retained and this unit remains incompletely covered.
Do not mix a no_issue_units clean assessment with any other answer for that unit.
No blanket "looks correct" or "logic equivalent" clean assessments:
show which before/after behavior and contract you actually compared.
`absent-before-in-delivered-source` means only that the delivered fragments have
no before source; a zero-context insertion can have this state. It does not mean
the base file did not exist or that prior behavior was absent across the PR.
`absent-new-declaration` records a positively added head declaration with no old
counterpart in the visible source association. It does not prove that no same
declaration or prior caller behavior exists elsewhere. Describe only this local
absence and cite after source; a reused old closing brace is not an old
implementation of a new method. Otherwise cite both target before and after
source when supplied. `unresolved-declaration-correspondence` does not establish
newness, including when no target before source was delivered. Do not invent
prior behavior from an unavailable before image; defer a check whose conclusion
needs that missing source. The exact per-unit target ranges govern behavior facts and the primary
site; the wider shared hunk is supporting source, not a substitute target.
Source facts are review input, never saved
investigation Observations or proof that the candidate is confirmed.

Schema (the example IDs and source are not available IDs):

```json
{
  "hypotheses": [{
    "unit_id": "COPY_ALLOWED_ID",
    "mechanism": "contract-mismatch",
    "claim": "Concrete regression introduced by the change",
    "trigger": "Observed supported input or lifecycle condition",
    "impact": "Observable incorrect output or state",
    "open_question": "One source-verifiable fact deciding the regression?",
    "refutation": "Specific source fact that would refute the regression",
    "severity": "warning",
    "sites": [{"source_ref": "sr_AFTER_ID"}],
    "discrepancy": "Observed mismatch between the change and an input/caller/contract",
    "trigger_basis": {"source_ref": "sr_CALLER_ID"},
    "violation": {
      "required_behavior": "The delivered caller contract requires returning its persisted value",
      "requirement_basis": [{"source_ref": "sr_CALLER_CONTRACT_ID"}],
      "actual_incompatible_behavior": "The changed operation returns a replacement instead",
      "negative_consequence": "The caller receives a value incompatible with its stored record",
      "supported_input": "The delivered caller requests an existing persisted record",
      "input_basis": [{"source_ref": "sr_SUPPORTED_CALL_ID"}]
    },
    "behavior": {
      "before": "Result for the same concrete input before the change",
      "after": "Result for that input after the change",
      "observable": "The changed return value, state, error or other observable result",
      "source_facts": [
        {"source_ref": "sr_BEFORE_ID"},
        {"source_ref": "sr_AFTER_ID"}
      ]
    }
  }],
  "no_issue_units": [{
    "unit_id": "COPY_OTHER_ALLOWED_ID",
    "checked": "Specific comparison/check and its source-backed compatibility result",
    "behavior": {
      "before": "Prior behavior for the compared input",
      "after": "New behavior for the same input",
      "observable": "Actual checked result and why it respects the supplied contract",
      "source_facts": [
        {"source_ref": "sr_OTHER_BEFORE_ID"},
        {"source_ref": "sr_OTHER_AFTER_ID"}
      ]
    }
  }],
  "deferred_units": [{"unit_id": "COPY_THIRD_ALLOWED_ID", "reason": "Concrete missing context or candidate capacity that prevents this assessment"}]
}
```

`mechanism` must be one of wrong-argument, wrong-operator, null-path,
contract-mismatch, missing-await, lock-scope, state-leak, error-path,
regression-removed, security-sink, i18n, a11y, perf, test-gap, doc.
`severity` must be error, warning or info. `side` must be before, after or
context. Every fact must quote delivered code at its exact path and line;
every site excerpt must be a verbatim after/RIGHT substring of at least 12
characters. Do not invent an anchor symbol or a unit ID.
Behavior facts must include the changed operation inside the declared unit's
range, not merely a neighboring function in the same file. A candidate must
have at least one RIGHT site in that target unit; shared parent/child ranges
may refer to the same operation. Additional cross-file facts/sites may explain
the contract but cannot replace the target's own source.
Use at most four source_facts per behavior. Program source_ref excerpts are at
most 240 characters; explicit legacy exact quotes have no separate length limit.
Prefer the short IDs to repeating source text. Limit each prose field to at most 600
characters; these are hard schema limits, not length targets. The behavior,
discrepancy and trigger basis will be delivered as bounded navigation to the
investigator; they are not Observations or confirmed evidence.
Every hypothesis requires all six violation fields shown above; use no extra
fields. Each violation prose field is a nonempty string of at most 600
characters; each basis contains 1–4 real delivered source references. A single
real source location may support both requirement and input when its actual
content does so; different IDs are not required. Typed change facts, rules,
navigation and negative search results cannot replace either source basis.
The behavior delta and violation remain separate: a changed output is not by
itself an incompatible or harmful output. Preserve this direction in claim,
impact, open_question and refutation rather than asking whether a beneficial
change could have any difference at all.

Some units have `## Structural changes`: a typed, program-verified rename with
equal comparison-base/head blobs and no text hunk. Its old/new path still changes
resource lookup, imports and consumer contracts. Equal contents do not prove a
safe rename. These facts are separate from source lines and are not Observations.
For such a unit, set `sites: []`; there is no RIGHT line to fabricate. Do not
return a model-constructed change_anchor: the program binds the trusted record.
Keep the usual hypothesis fields and behavioral comparison. Add exact typed
path references to `behavior.change_facts`, for example:

```json
"change_facts": [
  {"kind": "file-change", "change_id": "COPY_DELIVERED_CHANGE_ID", "field": "previous_path", "value": "old/path.json"},
  {"kind": "file-change", "change_id": "COPY_DELIVERED_CHANGE_ID", "field": "path", "value": "new/path.json"}
]
```

Copy both paths and change_id exactly from the unit's typed record; do not put
metadata in source_facts or invent a line number for it. Use 2–4 change facts,
with only the fields previous_path/path. Also cite delivered real head or
reference code in source_facts. A structural candidate's trigger_basis must be
that real head/reference code, not just the rename record. A structural no_issue
check must cite actual reference/consumer code and explain what was checked
about the old/new lookup or contract. A negative search, no collected references
or the rename alone cannot prove it safe; explicitly defer when the needed
consumer/source material is missing. Unverified missing patches, mode changes
and unsupported binary inputs do not use this structural path.

The unit's listed responsibilities and their domain rules are delivered with
this first discovery input. Check those rules together with general correctness
against the same source; there is no later full-PR specialist rescan. Rules are
not evidence, and echoing a responsibility name does not prove coverage.

This block allows at most **{{max_hypotheses}} new candidates**. This is an
upper bound, not a quota; the whole PR shares a fixed candidate cap. If a real
risk cannot fit, use deferred_units and explain the concrete capacity boundary;
do not call it clean. Keep every prose field to one concise factual sentence,
use only the source excerpts needed for validation, and close the entire JSON
object inside the existing 8192-token output limit. If evidence is insufficient,
explicitly defer the unit; do not hide it by omission. Do not re-review, search
the internet or execute code.

Output language: {{output_language}}. Keep code identifiers unchanged.
