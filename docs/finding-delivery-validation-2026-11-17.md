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
