Judge this recorded claim using only the saved success evidence below. This is
the final request: no tools, navigation requests or exploration. Candidate facts
are the claim under review, and contract_constraints are review rules; neither
is evidence. Author intent, omitted Context, missing files, errors and negative
search results cannot prove a defect or refute one.

Judge the entire supported trigger, mechanism, impact and regression. Confirm
only an introduced contract violation on the same supported input, with exact
saved source for both required and actual behavior. A null/zero possibility,
unread caller or unexplained business requirement is insufficient. A direct
language/script violation is judged against that resource's declared locale;
formatting/parameter claims require the actual consumer/formatter binding.
Naming, style, intended negative fixtures and pre-existing defects are outside
scope. Missing necessary facts remain UNKNOWN.

If positive saved source contradicts ONE necessary premise, return
counterevidence now. An unanswered open_question about an ancillary caller or
another irrelevant fact does not prevent this refutation. Do not demand a
second contract premise when a local guard or explicit test already defeats
the claim. Reason text alone is never a proof object or a verdict.

For structural claims, rename metadata proves paths and blob identity, not
runtime failure, old-path absence or consumer compatibility. Actual confirmation
must cite real saved source, optionally supplemented by metadata. A lookup
claim needs the concrete loader/root/alias/fallback resolution contract; an old
name plus rename alone is insufficient. Do not infer absence from failed reads.

Copy exact obs_N:eM references from observations.evidence. Unsaved, non-success
or another claim's evidence is forbidden. A saved excerpt is only the supplied
window, not proof of an entire implementation. No Context is available to cite.
Return ONE JSON object, no verdict, confidence, fences or surrounding prose:

CONFIRM:
{"answer":"...","severity":"error","assessment":{"expected":"required behavior","actual":"actual consequence","expected_evidence":["obs_N:eM"],"actual_evidence":["obs_N:eM"],"comparison":"conflict"},"counterevidence":null,"reason":"supported contract violation","additional_sites":[]}

REFUTE:
{"answer":"...","severity":"error","assessment":null,"counterevidence":{"premise":"mechanism","statement":"the saved guard prevents the claimed failure","evidence":["obs_N:eM"]},"reason":"necessary premise contradicted","additional_sites":[]}
premise is trigger, mechanism, impact or regression. A compatible assessment
with both evidenced premises also remains valid. Never emit both proof shapes.

UNKNOWN:
{"answer":"established facts","severity":"error","assessment":null,"counterevidence":null,"reason":"specific necessary fact still missing","additional_sites":[]}

additional_sites require real RIGHT {path,line,excerpt}; do not invent a diff
line for a structural claim. Output: {{output_language}}.
