# 2026-11-17 finding delivery validation

## Outcome

The saved-candidate gate did not pass. The operator stopped a confirmed repeated
source-state cycle at the next model-request boundary and drained the response.
This is an interrupted diagnostic, not a naturally completed review or a quality
score. No small samples or full PR were started, and no real PR comments were sent.

| Measurement | Result |
| --- | --- |
| Complete physical responses | 28 |
| Known input / output tokens | 103,449 / 6,888 |
| Known total tokens | 110,337 |
| Cache-read tokens, included in input | 88,448 |
| Failed / unknown-usage / in-flight requests | 0 / 0 / 0 |
| Persisted verdicts / local comments / real GitHub writes | 0 / 0 / 0 |
| Billing source | Go for all 28 responses |
| Reported balance charge | USD 0 |
| Reported Go allowance consumption | USD 0.00797788 |

These are inference-response receipts, not a reconciled account invoice. The
provider's Go allowance consumption differs from the charged account balance.
The previous supplier's two HTTP 524 failures and unknown usage remain in the
separate November 16 run; they are not overwritten or presented as free requests.

## Frozen scope

- Source revision: `162f6ff270e5ad64c9166a7b38cb18bce8ad01f6`.
- The same saved idle-deadline claim and immutable base/head originals were used.
  No old verdict, prior model conversation, or scoring answer was imported.
- The original saved-gate and nine-small-episode rubric were unchanged.
- Core roles used `deepseek-v4.1-flash` through OpenCode Go. Free assistant routing
  was not introduced into this experiment.
- A separate frozen transport wrapper supplied a truthful ReviewForge User-Agent
  and a stable provider session derived from the product run and persistent
  conversation IDs. It did not change prompts, source projection, tool schemas,
  semantic decisions, or the product session-loop implementation. This wrapper
  is evaluation instrumentation; production Go session support is not claimed.
- Comments used only the local transport substitute. Production configuration
  and the default legacy mode were unchanged.

The first invocation supplied a relative saved-origin path, which the existing
driver rejected before any model request. The invocation was corrected to use
absolute paths; the frozen product source, driver and provider binding were not
changed.

## Evidence and design assumption

The verifier received eight original sources in its independent session.
Read-only auditing confirmed their hashes, bytes, base/head sides and revisions
match the frozen fixture. One additional navigation source was recorded. There
was no source corruption or observed provider HTTP failure.

The model called `read_saved_source` 108 times in 27 batches: the four base
originals 14 times and the four head originals 13 times. Two direct head-source
reads and one diff navigation were also requested. It never called
`retain_source`, `set_progress`, or the judgment tool.

The session loop reprojects only the latest complete tool exchange plus explicitly
retained sources. Without retention, loading the opposite side removes the prior
side from the next request. Removing tool-call IDs and response/usage metadata
from the successfully answered request messages exposes repeated identical input
states; the original prompt and source contents are preserved by that comparison.

`git blame` attributes this projection to `07078b8b`. Its design assumes agents
reliably call `retain_source` and `set_progress`. The verifier prompt already
requests both; neither tool was missing or unavailable. This real execution does
not satisfy that assumption. Prompt instructions alone did not preserve the
cross-batch comparison context.

The runtime's repeated-failure check is also insufficient here: original-source
reads succeed, so the alternating evidence-state cycle is not a failed batch.
The controller continued issuing requests without reaching a persisted judgment.
The stop was diagnostic, not a token/request quota.

The repository Issue API query timed out during this read-only investigation.
This run did not modify product code or declare that no related Issue exists.

## Next acceptance step

Revisit source residency as a controller responsibility: retain the originals
actually read by each independent session by default, deduplicate them, and make
release explicit without changing finding revisions or treating notes as proof.
Also diagnose repeated evidence-state cycles across successful reads. Neither
change should invent a verdict or lower source authenticity/independent judgment
requirements. This is proposed follow-up design work, not an implemented fix.

After a repair, use a new source binding and re-test this same saved claim first.
It must persist independent acceptance, deliver exactly one local comment, and
remain idempotent on restart before running the nine small episodes. The full PR
remains gated on all nine passing.

Local receipts are under
`.reviewforge/benchmarks/finding-delivery-go-20261117-105031/`: `manifest.json`,
`result-summary.json`, `checkpoint.json`, and `saved/` containing the model meter,
transport usage receipts, original turn ledger, result and provenance audit.
These private runtime artifacts are ignored by Git. No credentials are included
in this document.

## First repaired-kernel retest (`95230e3`)

Automatic original residency and same-request body deduplication were effective:
the verifier's current input preserved both sides without any model-authored
retention/progress calls. The eight context regressions and complete Linux CI
passed (4,359 passed, 3 skipped). This did not establish judgment delivery.

The same saved case naturally ended as a reading-cycle partial with **70 complete
responses, 395,568 known tokens, one failed request with unknown usage, no
in-flight request, no verdict and no comment**. The transient failure was a
`RemoteProtocolError`; its retry and unknown usage were retained. Successful
responses reported USD 0 balance charge and USD 0.02434747 Go allowance usage;
this excludes the failed request's unknown usage.

The controller did eventually detect a cycle. Its granularity was too coarse:
it compared complete batches, while the model recombined already completed
diff/search/reference reads into new batches. A stable material basis persisted
across many such combinations. The current context also lacked a compact index
of completed queries, even though their durable receipts were stored. Those are
remaining memory/loop-contract defects, not evidence that a stored verdict failed
to publish. An operator-stop file was written after natural completion and was
not consumed; this run is not labelled operator-terminated.

The follow-up compares individual read actions and projects a deduplicated
session-local navigation index of prior queries, source handles and completeness.
The index is not evidence. Offline replay of the observed actions and recorded
material-basis digests reaches a corrected cycle halt at response 19 instead of
70; this is a deterministic mechanism replay, not a new model/quality result and
not evidence of a 51-response saving in a live run. Original-source and
independent-judgment requirements remain unchanged.

Retest receipts are in
`.reviewforge/benchmarks/finding-delivery-resident-20261117-112631/`. No small
sample or full PR has started. The saved-case acceptance gate remains closed.

## Action-level retest and isolated native protocol diagnostic (`c46eb5c`)

The action-level/query-memory implementation passed full Linux CI (4,360 passed,
3 skipped), but the same saved case still ended naturally as a cycle partial:
**29 complete responses, 170,364 known tokens, no unknown/in-flight requests,
no verdict and no comment**. Its known Go allowance receipt sums to USD
0.01508673 and its reported balance charge to USD 0. This gate did not pass;
small samples and the full PR were not started.

The provider's documented native thinking/tool-call protocol requires carrying
forward its own `reasoning_content` with the conversation. The reused v4.2 SSE
adapter drops that field, and the workspace-only projection does not preserve
the complete native exchanges. Go did not reject these incomplete continuations
with HTTP 400; HTTP success alone therefore did not validate the conversation
contract.

A separately labelled diagnostic kept the same model, case text, originals,
semantic prompts, tools and publication checks, but used the existing native
non-streaming compatibility adapter and complete per-session protocol history.
Provider state was returned only to its originating conversation, never used
as evidence or shared with another role. It produced **one independent accept
and one local comment in 7 complete responses**, with **47,513 known tokens**
and **one additional failed physical attempt with unknown usage** (8 physical
attempts total). Independent original-byte/side/revision/delivery auditing passed.
Known responses report USD 0 balance charge and USD 0.00707154 Go allowance use;
the failed attempt's usage remains unknown.

This is a viable protocol/session-continuation combination, not a single-variable
causal experiment, a holdout score, or product acceptance. The diagnostic changes
both history projection and transport, as well as preserving the native field;
it does not isolate which change caused the behavioral difference. Its files
live separately under `native-protocol-diagnostic/` within the `c46eb5c` cohort.

The new kernel now selects and freezes its own provider conversation policy,
with native-state isolation, exact pending-request recovery, stable Go headers,
original-body deduplication and explicit release. Nine native-protocol offline
tests supplement the nine workspace/cycle regressions. The saved-case gate must
be re-run using this actual product implementation before small samples.

## Product native-session retest (`89af63e`)

Full Linux CI passed with **4,369 passed, 3 skipped**. The actual product path's
saved-case gate persisted one independent accept and one local comment in **9
responses, 102,788 known tokens**, with zero failed/unknown/in-flight requests.
Pinned original provenance and independent delivery passed; restart made zero
new model requests and preserved the finding, verdict and comment identities.

Fresh small episodes A1, B1 and C1 satisfied their frozen finding/source gates:
A1 delivered one accepted defect and one comment (updated under the same finding
identity after a wording correction), B1 correctly closed an equivalent change
with zero findings, and C1 identified the absent authoritative policy with zero
unsupported comments. Their response/token counts were respectively **23/229,451**,
**10/99,014** and **7/42,280**. The fixtures contain no runnable tests; the reports
retain that execution limitation and `partial` status separately from persisted
lead completion and finding delivery. C1's policy gap is itself expected partial.

**A2 failed the original-base evidence gate**: its defect and single comment were
correct, but neither lead nor independent verifier read the original base
`deadlines.py`; the comparison came only from diff navigation. It used **18
responses, 171,722 known tokens**. No unknown/in-flight requests occurred in these
episodes. This cohort totals **67 responses and 645,255 known tokens** including
the saved case. Nine-sample acceptance was not achieved, and no full PR started.

The follow-up repair supplies separately identified pinned original excerpts
alongside requested diff navigation, without promoting navigation to evidence.
It requires a new source binding and a fresh saved-case/nine-sample evaluation;
earlier successful samples will not be combined with the new kernel's results.
All comment transports in these runs are local simulations, with zero real
GitHub writes. These fixture results do not establish holdout quality or a cost
improvement over v3.
