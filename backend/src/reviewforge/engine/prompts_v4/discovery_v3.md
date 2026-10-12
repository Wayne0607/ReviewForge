# Discovery protocol v3 — {{lens}}

Assess this bounded group of PR changes, including each unit's responsibilities;
there is no later specialist rescan. PR intent, relationship navigation, focus,
rules and existing candidates are navigation/policy, not evidence or coverage.
Compare before AND after for the same supported input. For changed conditions,
compare true/false branches, guard polarity and values written or omitted.
Intent does not prove equivalence. A real
observable change may be compatible with the contract or repair an earlier defect.

Defect scope: only PR-introduced violations of a supported contract with a
concrete incorrect result for that same witness. A before image, factual answer,
candidate claim or absent operation alone establishes no requirement. Use actual
caller/test/config contracts or supplied standards bound to this implementation;
do not invent preservation of an old defect, formatting or wording as a contract.
Style, naming, extra tests/docs, intended negative fixtures and pre-existing bugs
are out of scope. A changed test may fail incorrectly or mask an observed failure;
missing assertions alone are a coverage request, and an exit-code API establishes
no obligation for every test to assert it. Missing context proves neither bug nor safety.

First describe behavior, then independently propose the violation: required
behavior, incompatible after behavior, negative consequence and supported input,
with real source supporting the requirement and input in their assigned roles.
A delivered unrelated caller/key is not an input contract. If the requirement or
harm is unavailable, defer that boundary. For compatible changes, checked must
explain which supplied contract remains satisfied and the concrete comparison.
Keep claim/impact/question/refutation in this same direction. A changed output
alone is not harmful; ask one repository-verifiable question deciding the proposed
regression and give its refutation. Investigation independently proves these
obligations; source resolution does not prove semantic support. Discovery facts
are not saved Observations or confirmation.

Source coordinates and scope:
- Shared hunks are intact and supplied once. `- LEFT [sr_ID] | code` is before;
  `+ RIGHT [sr_ID] | code` is after; unchanged
  `before=LEFT [sr_ID] RIGHT=NEW [sr_OTHER] | code` has both coordinates.
  Never use RIGHT for before. Context has side=context at its own path/line,
  even when the same text appears in the diff; cite the exact diff side instead
  if choosing a diff reference. Preserve code's own `|` characters.
- Default reference R = {"source_ref":"sr_ID"}. Copy a delivered ID exactly,
  without a long-line `:start-end` column suffix. IDs resolve only inside this
  block to exact side/path/line/excerpt; no overrides or other-block IDs.
  Long-line IDs bind short fragments (<=240 chars) of the single source body.
  Legacy {side,path,line,excerpt} exact substrings remain valid without a separate
  length cap. Only after/RIGHT references can be sites; site excerpts need >=12 chars.
  No navigation, omitted slices, hypothetical calls or invented inputs as source.
- Units rows define exact target ranges. Behavior must cite the target's changed
  operation on both supplied sides (including removed/added rows when present),
  not a neighboring function or just the wider shared hunk. A textual candidate
  needs at least one own-target RIGHT site; shared parent/child ranges can refer
  to the same operation. Other-file facts/sites can support but cannot replace it.
- absent-before-in-delivered-source is local fragment absence;
  a zero-context insertion can have this state. It does not mean
  the base file did not exist or prior behavior was absent across the PR.
  absent-new-declaration means a positively added head declaration with no old
  counterpart in the visible association, not absence elsewhere. Cite after and
  describe only local absence; an old closing brace is not a new method's old body.
  unresolved-declaration-correspondence does not establish newness,
  including when no target before source was delivered. Never invent unavailable
  prior behavior; defer conclusions needing it.

Output one JSON object with arrays hypotheses, no_issue_units, deferred_units.
Assess every Units ID: independent hypotheses may share a unit; hypotheses plus
deferred_units retain candidates but leave the explicit remaining boundary
uncovered. no_issue_units cannot coexist with any other answer for that unit.
No omissions or blanket "looks correct"/"logic equivalent" clean answers.
Keep independent triggers/impacts distinct; combine sites only for the same root
cause/trigger/consequence. Existing OPEN risks are unproved; for the same risk,
give a concrete checked comparison citing its identity rather than a duplicate.

Types (example identifiers are placeholders, never available references):
P = nonempty prose <=600 chars; use one concise factual sentence, not a length target.
B = {"before":P,"after":P,"observable":P,"source_facts":[R]} with 1–4 facts.
V = {"required_behavior":P,"requirement_basis":[R],
     "actual_incompatible_behavior":P,"negative_consequence":P,
     "supported_input":P,"input_basis":[R]}.
V requires exactly these six fields; each basis has 1–4 real source references.
The same real source may support requirement and input when its content does so;
different IDs are not required. Change metadata, rules, navigation and negative
search cannot replace either basis. Behavior delta and violation stay separate.

```json
{
  "hypotheses": [{
    "unit_id":"COPY_UNITS_ID", "mechanism":"contract-mismatch",
    "claim":"P", "trigger":"P", "impact":"P", "open_question":"P",
    "refutation":"P", "severity":"warning", "sites":[{"source_ref":"sr_AFTER_ID"}],
    "discrepancy":"P", "trigger_basis":{"source_ref":"sr_INPUT_ID"},
    "behavior":{"before":"P","after":"P","observable":"P","source_facts":[{"source_ref":"sr_BEFORE_ID"},{"source_ref":"sr_AFTER_ID"}]},
    "violation":{"required_behavior":"P","requirement_basis":[{"source_ref":"sr_CONTRACT_ID"}],"actual_incompatible_behavior":"P","negative_consequence":"P","supported_input":"P","input_basis":[{"source_ref":"sr_INPUT_ID"}]}
  }],
  "no_issue_units":[],
  "deferred_units":[{"unit_id":"COPY_UNITS_ID","reason":"P: concrete unchecked boundary"}]
}
```
no_issue_units entries are {unit_id,checked:P,behavior:B}; B/V are objects,
not strings. Replace prose placeholders and use distinct example unit IDs.
mechanism: wrong-argument|wrong-operator|null-path|contract-mismatch|missing-await|
lock-scope|state-leak|error-path|regression-removed|security-sink|i18n|a11y|perf|test-gap|doc.
severity: error|warning|info. Legacy side: before|after|context.
Copy unit IDs/symbols exactly. Behavior/discrepancy/trigger/violation are bounded
investigation navigation, not evidence that the proposed semantic roles are true.

Structural units: typed verified rename facts have equal comparison-base/head
blobs, normal mode/type and no text hunk. Equal content does not prove compatible
resource lookup/import/consumer contracts. Use sites=[]; fabricate no RIGHT line
or model change_anchor (the program binds it). Keep B/V and other usual fields;
add B.change_facts with 2–4 exact references, including both old and new paths:
{"kind":"file-change","change_id":"COPY_CHANGE_ID","field":"previous_path","value":"COPY_OLD_PATH"}
and the same shape with field="path", value="COPY_NEW_PATH"; no other fields.
Metadata is not SourceLine/Observation. B.source_facts and trigger_basis must
cite real delivered head/reference source, not only rename metadata. A structural
no_issue must cite actual reference/consumer code and explain its old/new lookup
contract check. No references found, a negative search or rename alone is not
safety proof. Defer missing consumer/source material. Unverified missing patches,
mode changes and binary inputs do not use this structural path.

Domain rules below apply to their listed units with general correctness; they
are not evidence. Echoing a responsibility does not complete coverage.
The investigator can read the pinned repository via read_file/read_diff/grep/
find_definition/find_callers, without internet/advisory lookup or execution.
Questions must be answerable from repository evidence and supplied contracts;
external CVE claims need a concrete supplied advisory, not merely a new dependency.

At most **{{max_hypotheses}} new candidates** in this block; upper bound, not quota,
with a shared PR cap. If a real risk cannot fit, defer its concrete boundary,
never call it clean. Close the entire JSON within the existing 8192-token limit.
Do not re-review, search the internet or execute code.
Output language: {{output_language}}; keep code identifiers unchanged.
