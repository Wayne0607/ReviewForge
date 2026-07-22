# ReviewForge v3 architecture

## Objective

Increase full-benchmark recall without trading away actionable precision. The
current GitHub integration, persistence, event bus, scheduler, and comment
delivery remain the product shell. V3 replaces the review decision core with a
coverage-driven, evidence-producing pipeline.

## Pipeline

1. Compile the PR into a `SemanticChangeSet` whose units are changed symbols or
   bounded resource/config regions.
2. Build a `CoverageLedger` over semantic units and required review dimensions.
3. Generate high-recall candidate findings for pending coverage cells.
4. Investigate each candidate into an `EvidenceCapsule` with supporting and
   refuting evidence.
5. Resolve capsules to confirmed/rejected/abstain. An operational or contract
   failure must be retryable and must never silently become a rejection.
6. Rank and publish confirmed findings, then run coverage closure for uncovered
   high-risk cells.

## Stable contracts

### Semantic context

`SemanticChangeSet` contains repository, PR, head SHA, semantic units, and
unresolved changed files. `SemanticUnit` contains a stable id, path, language,
kind, symbol, changed-line range, added lines, calls/imports, live references,
candidate tests, risk signals, wiki facts, and source provenance. Compilation
is deterministic and side-effect free over `StateStore` plus the existing
impact manifest.

### Coverage

Dimensions are correctness, contract, error-handling, security, testing,
localization, performance, compatibility, and cross-PR. A `CoverageCell`
tracks unit, dimension, risk, status, attempts, assigned task ids, finding ids,
and closure reason. `CoverageLedger` is deterministic, serializable, and owns
the completion rule. Planner output may prioritize or add work but cannot mark
mandatory coverage complete.

**Canonical field mapping.** `CoverageLedger.from_change_set` reads
`risk_score` (float, [0.0, 1.0]) and `start_line` (int) from the
`SemanticUnit.to_dict()` shape.  Legacy `risk` (int) and `line` (int) keys
are accepted as fallback but `risk_score` and `start_line` always take
precedence.

**Signal vocabulary.** Risk signals drive mandatory dimension creation via
`_RISK_SIGNAL_MAP`:

| Signal                          | Dimension        |
|---------------------------------|------------------|
| `security-sensitive-symbol`     | security         |
| `security-sensitive`            | security         |
| `localization-resource`         | localization     |
| `localization`                  | localization     |
| `cross-PR` / `cross-pr`        | cross-PR         |
| `error-handling` / `error_handling` | error-handling |
| `contract-surface` / `contract` | contract         |
| `testing-scope` / `testing`    | testing          |
| `test-evidence-not-discovered`  | testing          |

`test-evidence-not-discovered` is emitted by `SemanticChangeSet` compilation
when no candidate test is found for a changed symbol.  It maps to the
`testing` dimension so the reviewer explicitly verifies whether the gap is
acceptable.

### Evidence

An `EvidenceCapsule` is tied to one candidate finding and stores typed evidence
items with path/SHA/line provenance, an explicit trigger or execution path,
the violated contract, supporting and refuting evidence, independent verdicts,
and a final confirmed/rejected/abstain status. Missing evidence and provider
failures produce abstain/retry, not false-positive suppression.

## Compatibility constraints

- Existing `Finding`, `ReviewTask`, and `StateStore` remain valid while V3 is
  introduced behind configuration flags.
- New structures provide `to_dict`/`from_dict` or equivalent stable JSON forms.
- Pure compilation and ledger logic must not call tools or models.
- Every heuristic must carry an explicit reason and have deterministic tests.
- No fixed global cap may silently drop mandatory high-risk coverage.
- Existing behavior remains the fallback when V3 is disabled.

## Initial integration ownership

- `engine/semantic_diff.py`: semantic change contracts and compiler.
- `engine/coverage_ledger.py`: coverage cells, policy, lifecycle, persistence.
- `engine/evidence_verifier.py`: evidence contracts and independent verdict
  orchestration.
- `engine/orchestrator.py`: integration is performed centrally after all three
  modules are reviewed.

## Acceptance gates

- Unit and integration tests pass with V3 both enabled and disabled.
- No provider/JSON/budget error is converted to an empty successful review.
- Every high-risk semantic unit has a terminal coverage reason.
- Martian 50 PR intermediate target: precision >= 35%, recall >= 45%, F1 >=
  39%, zero-candidate PRs <= 10%.
- V3 release target on the same judge: precision >= 40%, recall >= 60%, F1 >=
  48%, Critical recall >= 90%, High recall >= 60%.
- Final superiority claims require an unseen holdout and independent judges.
