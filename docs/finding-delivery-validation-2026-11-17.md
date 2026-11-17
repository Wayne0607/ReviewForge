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

## Companion-original retest (`94abc22`)

Full Linux CI passed (**4,376 passed, 3 skipped**). The saved-case gate passed
with **6 responses, 64,771 known tokens**, one independent accept and one local
comment; exact original-byte/side/delivery auditing and zero-request restart
reconciliation passed. Fresh A1 and A2 both passed the previously failing
original-base comparison requirement, with one accepted defect and one local
comment each (**15/125,984** and **16/137,788** responses/tokens). B1 correctly
closed the equivalent implementation with no finding (**13/152,398**).

C1 correctly identified the absent authoritative commercial policy and produced
no unsupported finding/comment (**7/45,612**), but did not request the missing
input or state a concrete recovery condition. The frozen rubric requires that
recovery step, so **C1 failed**. No full PR started; unrun replicates will not be
represented as passing. This cohort totals **57 responses, 526,553 known tokens,
zero unknown/in-flight requests**. All comments remained local simulations.

The main-review completion prompt/tool description asked only to name gaps,
whereas independent-verifier gaps already explicitly require recovery conditions.
The follow-up aligns the lead/research free-text completion instructions: state
the missing fact, checked sources, and request the specific authoritative input
that permits recovery. It adds no schema fields, prose matcher, semantic veto or
extra read requirement. This instruction change requires a fresh run binding;
the original truth rubric and all earlier failed records stay unchanged.

## Recovery-instruction retest (`ae850d6`)

Full Linux CI passed (**4,376 passed, 3 skipped**). The actual saved-case gate
passed with **10 responses, 103,006 known tokens**, one independent accept and
one local comment; exact original-byte/side/delivery auditing and zero-request
restart reconciliation passed. All **nine fresh small episodes passed their
frozen core truth/source/identity/delivery criteria**:

| Fixture | Complete responses | Known tokens | Result |
| --- | ---: | ---: | --- |
| A, three defect replicates | 51 | 523,350 | One independent accept/comment each |
| B, three equivalent-change replicates | 28 | 266,423 | Zero false findings/comments |
| C, three missing-policy replicates | 19 | 126,664 | Concrete recovery input, zero unsupported comments |

The small cohort used **98 complete responses, 916,437 known tokens**, plus one
failed physical request with unknown usage (99 physical attempts). Fixture test
limitations and expected policy partials remain in the original reports. This
does not certify every phrase: C1 misattributes the policy-absence sentence to
a nonexistent base code comment (it is actually in README); A1's core regression
is supported but its downstream refresh/renewal impact wording is speculative.
These report-quality limitations are recorded explicitly in the gate summary.

The subsequent Cal.com #14740 historical full-source replay was interrupted at
a request boundary after proving the native-prefix architecture gap, not at an
artificial call/token quota. At drain it had **58 complete responses, 7,091,593
known tokens**, one failed unknown-usage attempt, zero in-flight calls, one
persisted candidate, no verdict/comment. A prepared verifier request was saved
before dispatch. The stopped main review is not a full-PR quality result.

At a representative point, original source bodies totaled about 119 KB while
the submitted request serialized to 747,444 characters. A later pair shared only
two complete leading messages and 11,635 of 780,741 serialized characters. Final
main-review input cache hits were **428,032 / 7,057,565 tokens (about 6.1%)**.
Actual calls had distinct arguments; repeated EventManager reads used different
ranges. The evidence therefore does not support blaming this diagnostic on the
earlier same-read loop. It exposes broad exploration without early delivery,
plus repeated processing of a reconstructed native context with a mutable prefix.

The existing candidate's verifier is being resumed alone under its original
frozen source/configuration, without resuming the lead or importing a verdict.
The new `native-append-1` repair preserves chronological workspace deltas and
original tool replies. Its invariant test fails on `ae850d6` at the second
request and passes for both stored messages and the actual provider wire message
array in the new implementation. Live cache, saved-case, small-cohort and full
replay acceptance remain pending for that repair.

The stopped review's existing verifier subsequently completed alone under the
original `ae850d6` binding: **one accept and one local comment** for the team
ADMIN/OWNER `&&` authorization error. Both archive hashes, exact original ranges,
actual successful own-session input bodies, finding/verdict versions and local
delivery receipts passed auditing. It matches the selected benchmark's permission
reference issue. Restart added zero model calls and preserved the comment body
and all identities. The stopped lead was not resumed. Combined main/verification
usage is **78 complete responses, 8,100,001 known tokens, 3 failed unknown-usage
attempts** (81 physical attempts), with known Go allowance receipts totaling
USD **0.99531659** and known balance charge USD 0. The interrupted main review
does not provide a complete quality/recall score for the five reference issues.

## Chronological-append retest (`f345926`)

The chronological prefix invariant fails on the previous source at the second
request. New offline tests cover actual stored and provider-wire message-array
prefixes, changing focus, repeated originals, independent finding/verdict
conversations, restart and explicit release. Selected regressions **92 passed**;
full Linux CI **4,378 passed, 3 skipped**.

The saved gate passed with **6 responses, 50,194 known tokens**, one accept and
one local comment, exact successful-request original-body proof and zero-request
restart. Its measured cached input is **35,968 / 47,442 (75.8%)**. Its known Go
allowance receipt sum is USD **0.00417624**; this single case does not establish
a general savings ratio.

All **nine fresh small episodes passed the same frozen core truth, source,
identity and delivery gates**, with **89 complete responses, 659,238 known
tokens**, one failed unknown-usage attempt (90 physical attempts). Defect
replicates delivered one independent accept/comment each; equivalent and
missing-policy replicates produced no unsupported comments, and missing policy
included concrete recovery input. Actual input bodies are checked independently
of delivery rows. Report limitations remain: C1 unnecessarily questions the
dollars/cents contract already given in README; B3 unnecessarily raises a
counterfactual unreported-config caveat despite verified fixed snapshots.

The fresh historical Cal.com #14740 full-source replay ended naturally as
**partial**, with **158 complete responses, 9,278,989 known tokens** (9,167,765
input and 111,224 output), **9 failed unknown-usage attempts**, 167 physical
attempts and **zero in-flight requests**. Its six current independently accepted
findings produced **six separate local simulated comments**. Seven historical
accept verdicts include a correction and independent reverification of the same
finding, not a seventh finding or duplicate comment. The lead's first candidate
was submitted in response 44; delivery worked but initial discovery remains slow.

| Current finding | Result and review limitation |
| --- | --- |
| ADMIN/OWNER authorization uses `&&` | Non-owner team admins are denied; matches reference issue |
| Raw guest list passed to confirmation sender | Existing attendees receive another confirmation in mixed old/new requests; matches reference issue |
| Email normalization differs from existing booking policy | Case/alias blacklist bypass and case-sensitive attendee duplication; matches reference issue. Stripping `+` tags indiscriminately for attendee identity is an overly broad fix suggestion |
| Duplicate addresses within one request | Duplicate attendee rows and emails; matches reference issue |
| Add-guests sender ignores standard-email controls | Explicitly corrected at revision 2 to remove unsupported team-member recipients, independently reverified, then updated the same comment |
| Missing permission translation key | Complete English-catalog inspection corroborates the fact, but the model's negative-evidence proof is insufficient, as detailed below |

The selected record in the existing 50-PR pool contains five reference comments.
This run matches four and does not report its initial-empty-string suggestion;
the email-control and translation findings are additional. Reference matching
is not a formal recall/precision measurement: the reference is not exhaustive,
this PR has been reused heavily during development, and no unseen holdout or v3
comparison was run. Some manual line numbers and initial toast wording are
imprecise; canonical source-range references remain available.

All **184** original-source records passed exact frozen archive, side, revision,
range and body checks. The audit also checked actual successful own-session
model inputs, finding/task revisions, outbox records and local delivery receipts.
All **150 consecutive request pairs** preserved prior native messages unchanged,
with no changed prefix or explicit release. Restart through the same frozen
product entry produced **zero new requests**, identical report state, and the
same six local comment IDs and bodies. These checks establish mechanical
integrity; they do not establish that every cited source proves the claim.

Input cache hits were **8,348,800 / 9,167,765 tokens (91.1%)**. Known provider
Go allowance receipts total **USD 0.2575507**, with known balance charge USD 0.
The nine failed attempts' usage remains unknown; these are not invoice totals.
Do not calculate a savings ratio against the previous interrupted review, which
performed different work and never finished its main review.

The lead explicitly ended with missing-product-spec gaps: permitted booking
states and seat-based-event scope need the excluded issue/design text, and no
add-guests-specific exception to the general email-disable setting was found.
These gaps remain visible rather than being converted into unsupported defects
or a clean completion result.

### Strict evidence gap and next acceptance boundary

The translation-key verifier treated a navigation search with **`complete=false`**
and **314 skipped paths**, together with partial English-catalog excerpts, as
proof that the key was globally absent. Its source IDs, bytes and independent
delivery are authentic, but it did not receive the complete canonical catalog.
This violates the separation between navigation and evidence for an absence
claim. An independent post-run parse of every English JSON catalog in the
verified head archive (one file) confirms the key is absent. That check supports
the fact; it is **not retroactively credited as model input**. The frozen verdict
and outbox have not been edited to conceal the gap.

Consequently **strict evidence acceptance is false**, and this run is **not ready
for the main/production quality gate**. Six accepted local comments demonstrate
delivery, not six fully certified publication decisions. Default legacy and
`main` remain unchanged; **zero real GitHub comments** were posted.

The next bounded task is to design and verify negative evidence over an explicit
closed scope: a complete canonical original, or an auditable closed-scope check,
must support an absence claim. Incomplete navigation and partial excerpts can
locate evidence, but cannot prove absence; supplementary evidence or a specific
gap must return to the same finding. This should not block a positive claim
because an unrelated query is incomplete, and should not introduce prose regex
gates, arbitrary call quotas, or automatic upgrades of old checkpoints.

Before another full replay, add adversarial saved cases with a key outside the
excerpt or inside a skipped file, a genuinely absent key, and a positive claim
with an unrelated incomplete query. Verify same-finding recovery, independent
judgment and restart/no-duplicate delivery, then repeat the small comparison and
use an unseen PR. A full 50-PR rollout and production publication remain pending.

### Tracked Go validation usage

Across this day's tracked Go evaluation lineage, including interrupted diagnostics
and successive retests, the private physical ledgers total **713 attempts,
697 complete responses, 21,003,455 known tokens, 16 failed unknown-usage attempts,
zero in flight**, and **USD 1.52212977 known Go allowance consumption**. Known
balance charge is USD 0. This scope includes the initial saved-case run and the
isolated native-protocol diagnostic; it excludes setup probes and earlier
non-Go supplier calls. It is not an invoice, a production cost estimate, or the
cost of the final full PR alone. The latest saved, nine-small and full cohort
accounts for 253 responses and 9,988,421 known tokens within that total.

Private audit artifacts are under
`.reviewforge/benchmarks/finding-delivery-resident-20261117-142539/`, notably
`full/provenance-audit.json`, `full/native-prefix-audit.json`,
`full/restart-gate.json`, `full/quality-review.json`,
`full/manual-complete-locale-check.json` and `full/final-usage.json`.
The lineage rollup is
`.reviewforge/eval-private/opencode-go-20261117/evaluation-usage-rollup.json`.
