# Finding-level delivery architecture

Status: authorized implementation, not quality or cost acceptance. 2026-11-16.

## Accepted product contract

- Each independently confirmed defect produces one separate comment beneath the PR. There is no aggregate findings comment. Retries/restarts reconcile the same finding's comment instead of posting duplicates.
- A finding survives independent attempted refutation before delivery. A different defect discovered during verification is a new finding, not acceptance of the original claim.
- The model chooses investigation, decomposition, evidence interpretation and semantic verdicts. The controller owns identities, immutable source provenance, persistence, transport and delivery.
- Core roles are a lead reviewer and an independent adversarial verifier; focused research assistants are delegated only for concrete questions. They have separate sessions. Shared notes are navigation, never original evidence.
- Coverage/progress and each finding's disposition/delivery are independent. A missing rule or unfinished region cannot erase an unrelated accepted finding. Zero findings is a legitimate outcome.

## Implementation boundary

Implement a single opt-in `review_delivery` mode in `reviewforge.review_delivery`. Preserve legacy and historical v4/v4.2 checkpoint semantics. Reuse byte-preserving pinned source tools and physical request metering, not the old decision/scope/integration state machines. No production-default change, live GitHub posting, paid model benchmark or full-PR evaluation is part of coding acceptance.

The lead sees its task, relevant work and retrieved materials; not a complete historical board plus repeated custom action schemas. Normal reads do not require a progress declaration. Full originals stay durable and are retrievable without rerunning the original operation.

## State and validity

Persist a run binding (repo, PR, comparison base, head, source environment, model/config identity); findings and their explicit semantic revisions; original sources/navigation; session reads; shared research; independent verdicts; durable delivery outbox and receipts. IDs and concurrency revisions are controller-assigned and hidden from ordinary model submissions.

Reading/searching/adding a note must not increment a finding revision. Record read holders separately from the explicit premises used by a verdict. A changed claim or relevant counterevidence reopens only the affected finding. Existing acceptances are not resampled on a normal restart. Preserve old versions and late-response fencing. Shared source storage does not imply the independent verifier received its originals: record successful model-request delivery by session before accepting cited evidence.

Verdicts are `accept`, `reject`, or `needs_evidence`, with a concrete missing fact, attempted sources and recovery condition for the last. The verifier owns attempted refutation and semantic judgment. The controller validates source identity, snapshot binding, hashes and actual delivery. It does not infer a defect from matching a rigid prose template. Acceptance and delivery intent commit atomically; formatting, coverage and unrelated drafts are not semantic vetoes.

## Delivery and recovery

Use a separate PR discussion comment for each accepted finding, with fixed-commit source links; an inline changed-line anchor is optional. The comment contains only the independently approved finding, including its concrete trigger/impact and relevant code locations. No later model may silently rewrite its semantics.

Bind delivery to repo/PR/head/finding identity, freeze a deterministic marker and body, reconcile remote receipts before ambiguous retries, and retain unknown outcomes. Check live PR head before writes. A network failure retries delivery, not investigation or a stored verdict. Changes to an already delivered finding update or explicitly supersede the same comment; do not publish stale revisions.

When an independently rechecked finding is rejected, update its original comment with an explicit withdrawal and retain the previous body. If rechecking ends with missing evidence, mark the same comment as awaiting evidence and show the concrete recovery condition. These corrections to an existing authenticated comment may proceed after the head changes or the PR closes; they cannot create a new comment. New or revised accepted findings still require the current open PR head. GitHub head reads and writes are not atomic; retain the receipt and check the head again after writes.

## Coding acceptance

Tests must exercise the actual new entry/runtime and pinned adapters with deterministic model substitutes and HTTP mocks. Cover: one accepted finding while another is blocked; two distinct findings yield two comments; same finding restart/retry yields one; no inline anchor; refutation and concrete missing policy; source reads/notes do not invalidate a verdict; independently delivered original evidence; source isolation and changed hashes/head; persisted sessions and late responses; ambiguous HTTP writes and reconcile-only recovery; delegated research returning to the original question; legacy dispatch unchanged.

Report offline mechanism results separately from real discovery/false-positive/token results. Retain all physical attempts and unknown usage. No artificial token/request/reading cap; relay latency is not a quality metric. Saved cases and small real samples precede any future full PR.

## Execution ownership

- State worker: `review_delivery/contracts.py`, `review_delivery/store.py`, store-focused tests. Publish a concise Python API early for the other workers.
- Agent worker: `review_delivery/agents.py`, `review_delivery/runtime.py`, `review_delivery/tools.py` as needed, corresponding runtime tests. Coordinate the store API with the state worker.
- Publication worker: `review_delivery/publication.py`, narrow additive GitHub-client methods, publication/HTTP tests. Coordinate outbox interfaces with state worker.
- Main agent: new package entry/product integration, config/dispatch, joint entry tests, this document and final verification. Do not modify the existing uncommitted `docs/v4.2-delivery-plan.md` or user artifacts.

## Operator contract

Select `pipeline_v4.mode: review_delivery` (or `REVIEWFORGE_PIPELINE=review_delivery`) to use this entry. Unlike the historical local-only v4.2 experiment, this explicit mode delivers accepted findings through GitHub discussion comments. It reuses the configured `v42_reviewer` and `v42_verifier` model routes and their relay idle timeout/retry policy. The new mode has no combination of historical v4.2 behavior flags.

State lives next to the service database under `review-delivery`, or in the explicitly selected `REVIEWFORGE_DELIVERY_STATE_DIR`. Reuse the same directory on recovery. Repo/PR/base/head, original snapshot identities, external captures, model configuration, publication actor and kernel digest are frozen. A changed binding is a reported partial result, not automatic checkpoint migration or resampling.

Ordinary tokens resolve the comment author's immutable account ID with `GET /user`. Installations that cannot do that may explicitly supply the trusted account ID as `github.bot_user_id` or `GITHUB_BOT_USER_ID`. Another user's copied comment marker is never a publication receipt.

Completed original-source responses become available to an agent for citation after a successful model request containing them, independently for each session. The shared store is not shared understanding. A small finding index preserves known identities; detailed claims, notes and retained sources are selected or queried. Progress snapshots remain durable and can be read back.

Startup failures write a current partial report. A normal retry preserves accepted verdicts and recovers existing outbox work. Existing reject/needs-evidence corrections for the known frozen run can be recovered before the open/current-head gate for new investigation. Two controllers cannot run the same frozen review concurrently.

HTTP response loss, 408, 5xx and corrupt receipts remain uncertain and reconcile-only. Explicit rejected writes return to intended state; rate-limit and Retry-After delays survive restarts and finding revisions. This deliberately does not promise exactly-once effects across an unknowable remote write: unknown outcomes remain visible until reconciled.

## Session source residency and reading cycles (2026-11-17)

The [Go saved-case diagnostic](finding-delivery-validation-2026-11-17.md) disproved
the assumption that every agent reliably calls `retain_source` and `set_progress`
before an earlier native exchange is omitted. Memory maintenance is therefore a
controller responsibility rather than a prerequisite for investigation.

An original read by a session is automatically resident in that session until
`release_source`; a subsequent read restores it. Residency reconstructs from
durable tool receipts and explicit release/retain events, including after process
restart. It does not import another session's reads or automatically retain
navigation/notes. Preparing resident originals for a request does not prove the
model received them: citation eligibility still requires the successful model
response and its persisted source-delivery receipt. Finding revisions do not
change. Previously persisted pending request messages are replayed unchanged.

Each resident source's full bytes appear once in a projected request. A native
tool receipt whose original is already present keeps its source identity and
provenance and points to the working set instead of duplicating those bytes. The
full unmodified tool receipt remains in the durable session history. Explicit
release removes the source from future working sets; all originals remain
available for authenticated rereading.

Each session also sees a deduplicated navigation index of its prior read/query
operations, source handles and completeness/error receipts, without repeated raw
bodies. Query results remain navigation; this index never grants citation rights.
This keeps agents aware of completed investigation steps without requiring a
separate model-authored memory update.

Individual successful read actions are compared within an unchanged material basis: source
availability, this session's read identities, investigation focus, distinct visible
model observations, findings,
verdicts, research and distinct shared notes. A batch containing only previously
completed actions first adds a
visible correction naming the resident originals and inviting comparison, a new
investigation, or the model's own conclusion/concrete gap. Repetition after that
correction returns an explicit partial result without inventing a semantic
verdict. Recombining previously completed actions does not constitute progress.
Distinct reads are unlimited; this is not a token, request, or evidence
quota. An intervening non-reading exchange or a changed material basis starts a
fresh sequence. Test execution is not classified as a plain source-reading loop.

The warning/halt is persisted with its exchange. Restarting an unchanged halted
session does not silently resume spending. New available material or an updated
investigation focus can reopen it. Existing accepted findings, independent
verdicts and their outbox work survive a reading-cycle halt. Product kernel
digests fence changed implementations from old frozen runs; no historical
checkpoint or interrupted provider attempt is upgraded in place.

## Provider-native conversation continuity (2026-11-17)

Conversation protocol is a per-role, frozen part of the new kernel's model
binding. Generic providers retain the existing visible workspace projection.
OpenCode Go DeepSeek uses the existing OpenAI-compatible non-streaming adapter
that preserves the provider-returned `reasoning_content` field. The provider's
[thinking tool-call contract](https://api-docs.deepseek.com/guides/thinking_mode/)
requires that field to accompany prior assistant/tool exchanges. The historical
v4.2 SSE adapter intentionally strips it; that adapter and historical policies
are unchanged.

For these native sessions, actual model requests persist the ordered assistant
and tool exchanges from that session, including its own returned continuation
state. No other agent's native state is imported. This state is opaque protocol
data: it is not a shared note, original source, semantic verdict or published
comment. Only the independently delivered pinned originals can support citations.
Go requests use a truthful ReviewForge User-Agent and a stable provider session
derived from run plus persistent conversation identity. Other models/hosts do
not inherit DeepSeek's native-state policy.

Resident originals still appear once, referenced from their native tool receipts.
Explicitly released historical source bodies can become source handles only
after successful delivery. Newly requested results remain available in full even
when released in the same batch. Navigation remains navigation and its native
tool results remain in the conversation unless explicitly released; a compact
query index does not replace newly delivered results. Previously persisted
pending requests, including native continuation fields, are replayed unchanged.

Native continuation does not change model, temperature, thinking effort, output
capacity, source gates, semantic tools, retry accounting or publication rules.
It does not enable a provider's thinking mode or fabricate missing provider
state. A protocol change requires a new bound run, not an upgrade of an old
checkpoint. Default legacy mode and production settings remain unchanged.

Lead and research completion gaps must identify the missing fact, checked sources
and the specific authoritative input that allows recovery, including a request
for that input. Merely describing absence does not tell the recipient how to
resume. These remain free-text tool arguments, with no prose-template validator
or extra evidence-gathering step; independent `needs_evidence` verdicts retain
their existing explicit missing-fact/attempt/recovery fields.

## Original excerpts accompanying diff navigation (2026-11-17)

The native-session retest produced valid findings but exposed an evidence-delivery
assumption: a model reading the diff would separately ask for both source sides.
One defect replicate instead compared a head original against base diff navigation.
The frozen original-base acceptance criterion rejected it despite the correct
defect and local comment. No source gate or rubric was relaxed.

Only the new delivery kernel enriches `read_diff`: controller-compiled pinned
hunk ranges select actual `read_source` excerpts from each nonempty base/head
side. Overlapping ranges are merged; an original already resident in this same
session can cover a requested range. Original bytes, side, revision, hashes and
source IDs remain separate from the diff's navigation ID. Missing or structural
material remains a concrete gap, and cannot suppress the navigation result or
become original evidence. Empty hunk sides do not manufacture source lines.

The compound receipt is persisted intact. Workspace-only sessions project its
original bodies once in the working set. Native append sessions keep each body
in its first tool reply, with duplicate references pointing to that reply.
Another session receives no delivery credit or working-set entry from these
reads; only its own successful model request proves delivery. Reading never
changes a finding revision. The old pinned gateway and historical kernels retain
their existing semantics; changed source hashes require a fresh evaluation run.

## Chronological native conversation (`native-append-1`)

The first native projection rebuilt every request with a changing working-set
message before the entire assistant/tool history. The full-PR diagnostic proved
that its append/cache assumption was false: two consecutive requests shared
only their first two complete messages; 780,741 serialized characters had a
shared prefix of only 11,635 characters. The interrupted main review reported
7,057,565 input tokens with just 428,032 cached input tokens (about 6.1%).
This is separate from defect-discovery quality; it does not explain by itself
why the lead deferred its first candidate until late in the exploration.

For new native sessions, requests instead preserve every previously submitted
workspace update at its actual chronological position, then append the returned
provider state, tool results and changed navigation state. Original bodies stay
in their first native tool reply; an identical source ID later points to that
reply. The current working-set update contains added/released source handles
and changed focus/finding/note state, not another copy of bodies or the complete
query history already present in native exchanges. The controller persists both
the state snapshot used to calculate the delta and the exact update messages.

Without an explicit release, each subsequent actual request and provider wire
message array extends the preceding submitted array unchanged. Explicit release
can prune previously delivered historical bodies; newly read material remains
fully deliverable before it can be reduced to a released handle. Restart replays
an already prepared request exactly. Provider continuation remains session-local
and never becomes original evidence. Generic workspace-only sessions and old
frozen kernels retain their earlier policies. This requires a fresh source/run
binding; provider cache hits and quality improvements still need live validation.

## Closed scopes for absence premises (`closed-scope-1`)

The completed `f345926` replay disproved a second assumption: authentic original
citations and independent delivery do not establish sufficient coverage for a
negative claim. Its missing-translation finding was true on post-run complete
inspection, but the verifier relied on an incomplete search and partial catalog.
The previous boundary checked source identity, not declared negative premises.
The authorized repair keeps semantic judgment with the model and makes scope
completeness a controller-checkable obligation.

Every native verdict explicitly supplies `absence_claims` (an empty array when
no essential negative premise is used). Each premise identifies its fact,
bounded authoritative scope, and cited original source IDs. A complete source
file, or contiguous originals covering all its lines, can support a bounded
file-level absence judgment. A partial excerpt, search result, shared note,
unavailable source or ordinary test log cannot. Proof references must also be
verdict citations and have reached this verifier's successful model request.
Validation and the normalized premise record commit with the verdict/outbox;
failure leaves the same finding/revision available for supplementary evidence.

The delivery kernel's `check_text` checks an exact, case-sensitive literal in
every file matching an explicit glob at pinned head/base. It uses the complete
content-hashed snapshot, without search exclusions or result caps. Its immutable
test artifact records the snapshot digest, predicate, selected paths/body hashes,
all match positions and concrete gaps. Binary/unreadable files and an empty
selection cannot prove content absence. A complete zero-match artifact proves
only that literal is absent in those files; it is not proof of missing behavior,
an unknown external requirement, or the authority of the chosen scope. The
verifier must establish those semantic connections from relevant code/config.

The controller does not parse prose to guess whether a model has omitted a
negative premise, nor certify arbitrary programs with literal search. Required
declaration and model instructions address that semantic responsibility; saved
adversarial real-model cases and manual premise review still test adherence.
This is a bounded evidence guarantee, not a formal verifier of all natural
language judgments. Unrelated incomplete navigation does not block positive
evidence or accepted findings. Notes/reads do not change finding versions,
supplementary evidence returns to the same finding, and already accepted old
verdicts are not silently reclassified. New source bindings are required; old
checkpoint and legacy semantics remain unchanged.

Acceptance first exercises late/sibling keys, true absence, unreadable scopes,
independent delivery, same-finding recovery, atomic persistence and restart.
Then fixed real-model small gates precede three preselected PR replays. Comments
remain local, physical failures/unknown usage remain in accounting, and no
main/production-default change is authorized by this validation.
