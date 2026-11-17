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
