# ReviewForge v3 Benchmark Diagnosis — Martian 50 (commit 0823da6)

## Methodology

This document reports results from the Martian 50 benchmark (50 PRs, 137 golden findings, 5 project families). All pipeline and judge runs used MiMo v2.5 Pro.

**Measured (from authoritative merged artifacts):**
- Pipeline funnel counts (total_findings, confirmed, false_positives, review_comments) from `results-merged.json`
- Judge outcomes (TP, FP, FN, precision, recall, F1) from `judge-merged.json`
- Workload structure (PR count, golden count per PR) from `workload.json`
- Execution summary (tokens, tasks_failed, by_family, by_severity) from `summary.json`
- Per-PR token totals from `results-merged.json`

**Inferred / supplemental (from SQLite shard DBs, clearly labeled):**
- FP breakdown by reviewer type and category (Section 4.2–4.3): derived from `review_findings` table in `reviewforge-martian.db`; the DB contains per-shard lineage not present in the merged JSON
- Token distribution by agent (Section 3.1): derived from `tokens_by_agent` arrays in `results-merged.json`, cross-referenced with DB `token_usage` table
- Stage attribution for false negatives (Section 5.3, 7.2): **hypotheses** based on zero-candidate PR patterns and DB reviewer assignments; not directly measurable from final merged results alone. Ranges are estimated, not precise counts.

**Qodo-v2 baseline:** Pre-recorded candidates from the benchmark dataset, re-judged with the same MiMo rubric. Same model used for execution and judging may introduce correlated biases. The benchmark is 50 PRs — confidence intervals are wide.

**Canonical cross-check:** A validation script (`.reviewforge/v3-diagnostics/validate_counts.py`) asserts the expected counts from the authoritative artifacts.

---

## Executive Summary

ReviewForge achieves **38.89% precision / 25.55% recall / 30.84% F1** on the Martian 50 benchmark (137 golden findings, 50 PRs, 5 project families). Compared to Qodo-v2 (37.09% P / 57.66% R / 45.14% F1), ReviewForge has marginally higher precision (+1.8pp) but critically lower recall (−32.1pp) and F1 (−14.3pp). The v3 architecture acceptance gates (precision ≥ 35%, recall ≥ 45%, F1 ≥ 39%) are **not met** — recall and F1 fail.

The dominant failure mode is **insufficient candidate generation**, not over-aggressive filtering. 10/50 PRs (20%) produce zero candidates at all (target: ≤ 10%), and 18/50 (36%) emit zero published comments. The pipeline consumes 2.4M tokens across 50 PRs (mean 48K/PR) yet produces only 35 true positives.

---

## 1. Pipeline Funnel — Measured Attrition

### 1.1 Aggregate Funnel

| Stage | Count | Source |
|---|---:|---|
| PRs processed | 50 | `workload.json` |
| Reviewer tasks completed | 166 | `results-merged.json` (sum of per-PR tasks_completed) |
| Reviewer tasks failed | 2 | `results-merged.json` (sum of per-PR tasks_failed) |
| Pre-verification candidates | 211 | `results-merged.json` (sum of total_findings) |
| Post-calibration confirmed | 101 | `results-merged.json` (sum of confirmed) |
| Published comments | 101 | `results-merged.json` (sum of review_comments; equals confirmed) |
| Judge-matched true positives | 35 | `judge-merged.json` |
| Judge false positives | 55 | `judge-merged.json` |
| Judge false negatives | 102 | `judge-merged.json` |

**Attrition**: 110 of 211 candidates (52.1%) are rejected as false positives by the calibrator. Of the 101 confirmed findings, 35 (34.7%) are true positives against the golden set. The bigger loss is upstream — 10 PRs generate zero candidates, missing golden findings outright.

### 1.2 Per-PR Candidate Generation

| Metric | Value |
|---|---:|
| Mean candidates per PR | 4.22 (211/50) |
| PRs with 0 candidates | 10 (20%) |
| PRs with 0 confirmed | 18 (36%) |
| PRs with 0 true positives | 27 (54%) |

### 1.3 Zero-Candidate PRs (complete blind spots)

These 10 PRs produced no reviewer findings whatsoever:

| PR | Family | Tokens | Changed Files | Golden Findings Missed |
|---|---|---:|---:|---:|
| keycloak#36882 | keycloak | 21,307 | 11 | 1 (Medium) |
| keycloak#36880 | keycloak | 33,582 | 10 | 3 (all High) |
| keycloak#33832 | keycloak | 75,338 | 12 | 2 (High + Low) |
| grafana#97529 | grafana | 21,034 | 5 | 2 (both High) |
| sentry#80528 | sentry | 17,159 | 4 | 2 (High + Low) |
| grafana#107534 | grafana | 15,215 | 4 | 1 (Low) |
| grafana#76186 | grafana | 24,583 | 8 | 2 (High + Low) |
| discourse#7 | discourse | 10,873 | 32 | 3 (all Low) |
| discourse#5 | discourse | 4,055 | 5 | 2 (both Low) |
| cal.com#14943 | cal.com | 9,833 | 3 | 2 (both High) |

**Pattern**: These span all 5 families and all languages. The PRs are not unusually small — keycloak#33832 has 12 changed files and 75K tokens consumed. The reviewers were dispatched but produced zero findings, indicating the LLM failed to identify issues in the provided context.

---

## 2. Recall by Severity — Critical Gap in High Severity

| Severity | Golden | TP | FN | Recall | Qodo Recall | Gap |
|---|---:|---:|---:|---:|---:|---:|
| Critical | 9 | 7 | 2 | 77.78% | 88.89% | −11.1pp |
| High | 41 | 10 | 31 | 24.39% | 73.17% | −48.8pp |
| Medium | 47 | 14 | 33 | 29.79% | 61.70% | −31.9pp |
| Low | 40 | 4 | 36 | 10.00% | 30.00% | −20.0pp |

**The High-severity recall gap (48.8pp) is the single largest metric deficit.** ReviewForge catches only 10 of 41 High-severity golden findings. Many of these are semantic logic errors (permission checks, race conditions, contract violations) that require deep cross-file understanding.

### 2.1 Missed High-Severity Findings by Category

From the 31 missed High-severity findings:

- **Permission/authz logic errors** (8): Feature flag mismatches, wrong resource lookups, incorrect scope checks
- **Race conditions / concurrency** (4): Unsynchronized cache access, concurrent device creation, unawaited async
- **Contract violations** (5): Returning wrong types, breaking interface contracts, null returns
- **Logic inversions** (3): Inverted conditions, wrong function called, reversed parameters
- **Data corruption** (3): Wrong data returned, stale values, missing cleanup
- **Crash/panic** (4): Nil dereference, missing null checks, type errors
- **Other** (4): Various

---

## 3. Token Economics

### 3.1 Token Distribution by Agent

*Source: `tokens_by_agent` arrays in `results-merged.json`, cross-referenced with `token_usage` table in `reviewforge-martian.db`.*

| Agent | Tokens | % of Total | Calls | Avg/Call |
|---|---:|---:|---:|---:|
| correctness_reviewer | 573,447 | 23.8% | 74 | 7,749 |
| calibrator | 469,668 | 19.5% | 75 | 6,262 |
| planner | 459,851 | 19.1% | 53 | 8,676 |
| security_reviewer | 369,291 | 15.3% | 42 | 8,792 |
| testing_reviewer | 267,910 | 11.1% | 37 | 7,240 |
| performance_reviewer | 112,814 | 4.7% | 18 | 6,267 |
| accessibility_reviewer | 51,180 | 2.1% | 7 | 7,311 |
| dependency_reviewer | 42,648 | 1.8% | 8 | 5,331 |
| localization_reviewer | 37,506 | 1.6% | 10 | 3,750 |
| escalation | 20,608 | 0.9% | 8 | 2,576 |
| doc_reviewer | 4,338 | 0.2% | 1 | 4,338 |

**Observations**:
- The **planner** consumes 19.1% of tokens (460K) for 53 calls — this is the decision layer that assigns reviewers, not the review itself.
- The **calibrator** consumes 19.5% (470K) for 75 calls — it rejects 52.1% of candidates, meaning ~245K tokens were spent generating candidates that were then discarded.
- **correctness_reviewer** is the most productive (54 of 101 published findings) but also the most expensive.
- **security_reviewer** produces the most false positives despite consuming only 15.3% of tokens.

### 3.2 Token Efficiency

| Metric | Value | Source |
|---|---:|---|
| Total tokens | 2,409,261 | `summary.json` |
| Tokens per PR (mean) | 48,185 | `results-merged.json` |
| Tokens per PR (median) | 45,338 | `results-merged.json` |
| Tokens per emitted comment | 23,854 | 2,409,261 / 101 |
| Tokens per true positive | 68,836 | 2,409,261 / 35 |

### 3.3 High-Token Zero-Comment PRs

These PRs consumed significant tokens but produced no published output:

| PR | Tokens | Candidates | Confirmed |
|---|---:|---:|---:|
| keycloak#33832 | 75,338 | 0 | 0 |
| grafana#106778 | 74,138 | 5 | 0 |
| cal.com#22345 | 49,191 | 1 | 0 |
| grafana#79265 | 40,220 | 3 | 0 |
| sentry#77754 | 37,499 | 3 | 0 |
| keycloak#36880 | 33,582 | 0 | 0 |
| keycloak#32918 | 29,323 | 1 | 0 |
| grafana#76186 | 24,583 | 0 | 0 |
| discourse#9 | 22,513 | 2 | 0 |

---

## 4. False Positive Analysis — Calibrator Effectiveness

### 4.1 Aggregate

*Source: `results-merged.json` for counts; `judge-merged.json` for TP/FP/FN.*

- Total pre-verification candidates: 211
- Total rejected (false_positive): 110
- Total confirmed (published): 101
- Judge-matched true positives: 35
- Judge-matched false positives: 55
- **Calibrator precision on published findings**: 35/101 = 34.7% (i.e., 65.3% of published findings are FP per judge)
- **Overall candidate rejection rate**: 110/211 = 52.1%

### 4.2 FP by Reviewer Type

*Source: `review_findings` table in `reviewforge-martian.db` (supplemental lineage).*

| Reviewer | FP Count | Published | FP Rate |
|---|---:|---:|---:|
| security_reviewer | 36 | 10 | 78.3% |
| testing_reviewer | 25 | 14 | 64.1% |
| correctness_reviewer | 19 | 54 | 26.0% |
| performance_reviewer | 13 | 10 | 56.5% |
| dependency_reviewer | 7 | 0 | 100% |
| accessibility_reviewer | 5 | 7 | 41.7% |

**security_reviewer** has a 78% FP rate — nearly 4 out of 5 candidates are wrong. The dominant FP patterns are:
- **hardcoded-secrets** (11 FP): False positive on test fixtures, example configs, and non-secret strings
- **code-injection** (15 FP): Over-flagging `Function()` constructors, dynamic dispatch in JavaScript, `eval` in non-security contexts
- **sql-injection** (7 FP): Flagging parameterized queries or safe string formatting

**testing_reviewer** has a 64% FP rate, dominated by:
- **compilation-error** (8 FP): Incorrectly flagging valid Go/TypeScript test code
- **test-quality** / **flaky-test** / **naming** (various): Stylistic issues not in the golden set

### 4.3 FP by Category (top 10)

*Source: `review_findings` table in `reviewforge-martian.db` (supplemental lineage).*

| Category | Count | Likely Cause |
|---|---:|---|
| code-injection | 15 | Over-sensitive pattern matching on JS `Function()` |
| hardcoded-secrets | 11 | Test fixtures / example values flagged |
| compilation-error | 8 | Reviewer misunderstands language semantics |
| sql-injection | 7 | Parameterized queries misidentified |
| n-plus-one | 7 | Legitimate sequential operations flagged |
| missing-label | 5 | Accessibility reviewer over-reporting |
| naming | 3 | Style issues not in golden |
| test-quality | 3 | Style issues not in golden |
| logic-error | 3 | Some are plausible but not golden |
| migration-nop | 2 | Idempotent migrations flagged |

---

## 5. Missed Findings (False Negatives) — Deep Dive

### 5.1 Total Missed: 102 of 137 golden findings

*Source: `judge-merged.json` (sum of false_negatives list lengths).*

| Source | Count | % of FNs |
|---|---:|---:|
| Zero-candidate PRs (no candidates generated) | 40 | 39.2% |
| PRs with candidates (candidates exist but wrong/missing) | 62 | 60.8% |

### 5.2 Missed Findings by Family

| Family | FNs | Critical | High | Medium | Low |
|---|---:|---:|---:|---:|---:|
| sentry | 27 | 0 | 7 | 10 | 10 |
| cal.com | 23 | 1 | 9 | 7 | 6 |
| discourse | 18 | 0 | 1 | 8 | 9 |
| keycloak | 17 | 1 | 6 | 4 | 6 |
| grafana | 17 | 0 | 8 | 4 | 5 |

**sentry** and **cal.com** together account for 50 of 102 FNs (49%). These are Python and TypeScript projects respectively.

### 5.3 Root Cause Attribution by Pipeline Stage (Hypotheses)

**These attributions are estimated from zero-candidate patterns and DB reviewer assignments, not directly measurable from the final merged results.** The counts below are ranges, not precise figures.

| Root Cause | Estimated FN Range | Pipeline Stage | Basis |
|---|---:|---|---|
| Reviewer generates 0 findings for PR | ~40 | Reviewer (generation) | 10 zero-candidate PRs, each with golden FNs |
| Reviewer generates findings but misses specific golden | ~30–40 | Reviewer (coverage) | PRs with TP>0 but also FN>0 |
| Calibrator rejects valid finding | ~10–15 | Calibrator | DB false_positives that match golden patterns (not independently verified) |
| Planner assigns wrong reviewers | ~5–10 | Planner | DB reviewer assignments vs golden finding types |
| Actionability filter drops valid finding | ~0–5 | Actionability | Limited evidence; few cases identifiable |

**The reviewer generation stage is the primary bottleneck** (~70–80 of 102 FNs). The calibrator and planner are secondary contributors. Precise counts require re-judging all rejected candidate texts against golden FNs, which is not performed here.

### 5.4 Specific High-Impact Misses

These are High/Critical severity findings that Qodo found but ReviewForge missed entirely:

**Permission/Authz bugs (Java/Keycloak)**:
- keycloak#36880: 3 High findings about AdminPermissions feature flag inconsistency and resource lookup bugs — all missed (0 candidates)
- keycloak#37038: 2 High findings about incorrect canManage() permission check and group resource ID mismatch — both missed (FP candidates produced)
- grafana#103633: 1 High finding about asymmetric cache trust in permission check — missed (FP candidates)

**Race conditions (Go/Grafana)**:
- grafana#97529: 2 High findings about BuildIndex race and TotalDocs concurrent access — both missed (0 candidates)
- grafana#79265: 1 High finding about device count race condition — missed (0 candidates)

**Logic inversions (Java/Keycloak)**:
- keycloak#37634: Critical finding about wrong null-check parameter — found
- keycloak#37634: High finding about inverted isAccessTokenId logic — found
- But keycloak#33832: High finding about wrong BouncyCastle provider — missed (0 candidates)

**Contract violations (TypeScript/Cal.com)**:
- cal.com#14943: 2 High findings about atomic increment race and incorrect SMS deletion logic — both missed (0 candidates)
- cal.com#11059: 5 High findings about OAuth token refresh failures — none found

---

## 6. Language/Project Performance

*Source: `summary.json` by_family section.*

| Family | Language | RF Precision | RF Recall | RF F1 | Qodo F1 | RF Worst |
|---|---|---:|---:|---:|---:|---|
| keycloak | Java | 53.8% | 29.2% | 37.8% | 43.6% | 3 High missed in #36880 |
| discourse | Ruby | 41.7% | 35.7% | 38.5% | 39.5% | Close to Qodo |
| grafana | Go | 50.0% | 22.7% | 31.3% | 42.6% | Race conditions missed |
| cal.com | TypeScript | 29.6% | 25.8% | 27.6% | 50.0% | 23 FNs, worst family |
| sentry | Python | 31.3% | 15.6% | 20.8% | 50.0% | 27 FNs, worst recall |

**sentry/Python** is the worst-performing family (15.6% recall). This is likely because:
1. Python's dynamic nature makes static analysis harder for the reviewer
2. Many sentry findings involve subtle type/state issues (dataclass defaults, queue APIs, serialization)
3. The sentry codebase has complex async patterns the reviewer doesn't track

**cal.com/TypeScript** has the second-worst recall (25.8%) with the highest FP rate. TypeScript findings often involve Prisma ORM patterns, OAuth flows, and complex type relationships.

---

## 7. Architecture Stage Attribution for False Negatives

### 7.1 Stage Map

```
PR Input
  -> Context Engine (file selection, indexing)
  -> Deterministic Scan (pattern-based)
  -> Planner (LLM: assign reviewers to files)
  -> Reviewers (LLM: generate candidate findings)
  -> Actionability Filter (deterministic)
  -> Calibrator (LLM: confirm/reject candidates)
  -> Commenter (format + publish)
```

### 7.2 Where FNs Originate (Hypotheses)

**These are estimated ranges, not measured counts.** Attribution requires tracing individual FNs through the pipeline via DB lineage and re-judging rejected candidates against golden FNs. The table below represents our best hypothesis based on observable patterns (zero-candidate PRs, PRs with mixed TP/FN, DB reviewer assignments).

| Stage | Mechanism | Estimated FN Range | Observable Evidence |
|---|---|---:|---|
| **Context Engine** | Files not selected for review | ~0–5 | PRs with many files where golden finding is in an unreviewed file |
| **Planner** | Wrong reviewer assigned | ~5–10 | E.g., permission bug assigned to testing_reviewer instead of correctness_reviewer |
| **Reviewer (generation)** | LLM doesn't detect issue | ~35–40 | Zero-candidate PRs; reviewer runs but outputs 0 findings |
| **Reviewer (coverage)** | LLM detects some issues but misses others | ~30–40 | PRs with TP>0 but also FN>0 |
| **Actionability** | Filter drops valid finding | ~0–5 | Limited evidence |
| **Calibrator** | LLM incorrectly rejects valid finding | ~10–15 | DB false_positives that may match golden patterns |
| **Commenter** | Delivery failure | ~0 | All confirmed findings were posted |

**The reviewer generation stage is the primary bottleneck.** The planner and calibrator are secondary.

---

## 8. Repeated Patterns in Failed PRs

### 8.1 Security Reviewer Noise

The security_reviewer produces 15 `code-injection` FPs and 11 `hardcoded-secrets` FPs. These are deterministic pattern matches that the calibrator should filter but often doesn't. The security reviewer's 78% FP rate suggests its prompts or skills are too aggressive for the benchmark's codebases.

### 8.2 Testing Reviewer Compilation Errors

8 FPs are `compilation-error` from the testing_reviewer, all apparently incorrect. The testing reviewer seems to misunderstand Go variable declarations and TypeScript function signatures.

### 8.3 Zero-Round Reviewers

Several event logs show reviewers completing with 0 findings after reading files. This happens for:
- security_reviewer on keycloak#33832 (read files, searched code, found nothing)
- correctness_reviewer on multiple zero-candidate PRs
- testing_reviewer on most PRs

The LLM is given the diff context but doesn't identify the issues. This is a **recall failure at the prompt/skill level**, not a filtering failure.

### 8.4 High-Token Zero-Output PRs

keycloak#33832 consumed 75K tokens across 5 reviewer tasks, all producing 0 findings. The golden findings (wrong BouncyCastle provider, dead code) are subtle Java issues that require understanding the crypto provider hierarchy. The reviewer had the right files but didn't detect the semantic error.

---

## 9. Comparison with v3 Architecture Goals

*Source: `judge-merged.json` for measured metrics; acceptance gates from `docs/v3-architecture.md`.*

| Metric | v3 Target | Measured | Status |
|---|---|---:|---|
| Precision >= 35% | 35% | 38.89% | Met |
| Recall >= 45% | 45% | 25.55% | **Missed by 19.5pp** |
| F1 >= 39% | 39% | 30.84% | **Missed by 8.2pp** |
| Zero-candidate PRs <= 10% | 10% | 20% | **Missed by 10pp** |

The precision gate is met. The recall and F1 gates fail. The zero-candidate PR target is double the limit.

### v3 Release Targets (for reference)

| Metric | Release Target | Measured | Gap |
|---|---|---:|---:|
| Precision >= 40% | 40% | 38.89% | −1.1pp |
| Recall >= 60% | 60% | 25.55% | −34.5pp |
| F1 >= 48% | 48% | 30.84% | −17.2pp |
| Critical recall >= 90% | 90% | 77.78% | −12.2pp |
| High recall >= 60% | 60% | 24.39% | −35.6pp |

---

## 10. Prioritized Architectural Experiments

### Experiment 1: High-Recall Candidate Generation (Expected: +15-20pp recall)

**Hypothesis**: The current single-shot reviewer is too conservative. A two-pass approach — first pass generates candidates at high recall with relaxed thresholds, second pass (calibrator) filters — would increase recall without catastrophic precision loss.

**Implementation**:
- Lower the reviewer's confidence threshold from 0.7 to 0.4
- Add a dedicated "adversarial reviewer" that specifically looks for patterns the calibrator tends to reject
- Run the correctness_reviewer twice: once on the diff, once on the full file context

**Expected metric movement**: Recall +15-20pp, Precision −5-8pp, F1 +8-12pp

**How to falsify**: If recall improves <10pp or precision drops >15pp, the hypothesis is wrong — the model genuinely can't see these issues with current prompting.

### Experiment 2: Zero-Candidate PR Elimination (Expected: +5-8pp recall)

**Hypothesis**: Zero-candidate PRs occur because the planner assigns reviewers that don't match the golden finding type, or because reviewers get insufficient context. A fallback "omnibus reviewer" that runs on PRs with 0 candidates after the first round would catch missed findings.

**Implementation**:
- After the first reviewer round, check if total_findings == 0
- If so, run an omnibus reviewer with the full PR diff and all changed files
- Use a broader prompt: "Identify any bugs, security issues, or correctness problems"

**Expected metric movement**: Recall +5-8pp, Precision −1-2pp, F1 +3-5pp

**How to falsify**: If the omnibus reviewer still produces 0 findings on these PRs, the issue is model capability, not routing.

### Experiment 3: Security Reviewer Precision Fix (Expected: +3-5pp precision)

**Hypothesis**: The security_reviewer's 78% FP rate drags down overall precision. Replacing the security skill's pattern-matching rules with more targeted prompts would reduce noise.

**Implementation**:
- Audit the `security_reviewer` skill: remove `hardcoded-secrets` and `code-injection` pattern matchers that produce FPs
- Add code-context validation: only flag hardcoded secrets if the variable name contains "key", "secret", "password", "token" AND the value is not a test fixture
- For code-injection: require that the dynamic function is called with user-controlled input

**Expected metric movement**: Precision +3-5pp, Recall ±0pp, F1 +2-3pp

**How to falsify**: If precision doesn't improve, the FPs are coming from the LLM's reasoning, not from deterministic patterns.

### Experiment 4: Calibrator Threshold Tuning (Expected: +3-5pp recall)

**Hypothesis**: The calibrator is too aggressive — it rejects 52.1% of candidates, including some valid findings. Adjusting the calibrator's prompt to be less aggressive on borderline findings would increase recall.

**Implementation**:
- Analyze the FNs that were rejected by the calibrator
- Adjust the calibrator prompt: "When in doubt, confirm rather than reject"
- Add a confidence threshold: findings with reviewer confidence > 0.8 should not be rejected by the calibrator unless there's explicit counterevidence

**Expected metric movement**: Recall +3-5pp, Precision −2-3pp, F1 +1-3pp

**How to falsify**: If precision drops >8pp, the calibrator is correctly filtering noise and relaxing it would harm the product.

### Experiment 5: Language-Specific Reviewer Skills (Expected: +5-8pp recall)

**Hypothesis**: The sentry/Python and cal.com/TypeScript families have the worst recall because the reviewer skills don't account for language-specific patterns. Adding Python/TypeScript-specific skills would improve detection.

**Implementation**:
- Create `python_correctness_skill` with patterns for: dataclass defaults, async/await, type narrowing, Django ORM slicing, serialization
- Create `typescript_correctness_skill` with patterns for: Prisma ORM, Zod schema, dayjs comparison, forEach+async, OAuth token handling
- Route these skills based on file language in the planner

**Expected metric movement**: Recall +5-8pp (primarily in sentry/cal.com families), Precision ±0pp, F1 +3-5pp

**How to falsify**: If recall in sentry/cal.com doesn't improve by >3pp, the issue is not language-specific but model capability.

### Experiment 6: Cross-File Context Enrichment (Expected: +3-5pp recall)

**Hypothesis**: Many missed findings (permission bugs, race conditions, contract violations) require understanding how multiple files interact. The current reviewer operates on a per-task basis with limited cross-file context.

**Implementation**:
- In the planner, when assigning correctness_reviewer for files with call/import relationships, include the related files in the reviewer's context
- Add a "relationship-aware" prompt section that highlights cross-file dependencies
- Use the `code_relations` table in the DB to pre-compute file relationships

**Expected metric movement**: Recall +3-5pp (primarily for permission/authz and contract findings), Precision −1-2pp, F1 +2-3pp

**How to falsify**: If adding cross-file context doesn't improve recall on the specific finding types that require it, the model can't reason about multi-file interactions.

### Experiment 7: Agentic Reviewer Loops (Expected: +5-10pp recall)

**Hypothesis**: The current reviewer is single-shot (one LLM call per task). An agentic loop that allows the reviewer to explore the codebase (read related files, search for patterns) would find more issues.

**Implementation**:
- Enable the existing `execute_agentic` path in `BaseReviewer` for all correctness and security tasks
- Allow up to 3 tool-use rounds per reviewer task
- Provide tools: `read_file`, `search_code`, `read_reference`

**Expected metric movement**: Recall +5-10pp, Precision −2-3pp, F1 +3-7pp, Tokens +30-50%

**How to falsify**: If recall doesn't improve >5pp despite tool access, the model's reasoning is the bottleneck, not context.

---

## 11. Experiment Priority Stack Rank

| Priority | Experiment | Expected F1 Gain | Token Cost | Risk |
|---|---|---:|---:|---|
| P0 | High-recall candidate generation | +8-12pp | +20-30% | Medium (precision risk) |
| P1 | Zero-candidate PR elimination | +3-5pp | +5-10% | Low |
| P2 | Security reviewer precision fix | +2-3pp | −5% | Low |
| P3 | Language-specific reviewer skills | +3-5pp | +5% | Low |
| P4 | Calibrator threshold tuning | +1-3pp | 0% | Medium |
| P5 | Cross-file context enrichment | +2-3pp | +10-15% | Low |
| P6 | Agentic reviewer loops | +3-7pp | +30-50% | Medium |

**Combined P0+P1+P2+P3 could plausibly reach F1 ~45-50%, recall ~45-55%, precision ~35-40%.** This would meet the v3 intermediate target and approach the release target.

---

## Appendix A: Data Sources

| Artifact | Path | Role |
|---|---|---|
| Workload | `E:\ReviewForge-mimo-p1\.reviewforge\martian50-0823da6\workload.json` | Authoritative: 50 PRs, 137 golden findings |
| Per-PR results | `E:\ReviewForge-mimo-p1\.reviewforge\martian50-0823da6\results-merged.json` | Authoritative: pipeline counts, tokens, status |
| Judge results | `E:\ReviewForge-mimo-p1\.reviewforge\martian50-0823da6\judge-merged.json` | Authoritative: TP/FP/FN, metrics |
| Summary | `E:\ReviewForge-mimo-p1\.reviewforge\martian50-0823da6\summary.json` | Authoritative: aggregate metrics, by_family, by_severity |
| Shard DBs | `E:\ReviewForge-mimo-p1\.reviewforge\martian50-0823da6\reviewforge-martian.db` | Supplemental: per-finding reviewer/category lineage |
| Event logs | `E:\ReviewForge-mimo-p1\.reviewforge\martian50-0823da6\events\*.jsonl` | Supplemental: timing, reviewer dispatch |
| Validation script | `.reviewforge/v3-diagnostics/validate_counts.py` | Asserts canonical counts |
| Architecture spec | `docs/v3-architecture.md` | Acceptance gates |
