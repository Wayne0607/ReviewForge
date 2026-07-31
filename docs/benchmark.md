# ReviewForge v3 benchmark

## Stable 50-PR baseline

Date: 2026-07-30

Review core tag: `v3-final-50pr-baseline-20260730`

Review core commit: `e9827cb944af5e8dd8ef8d9704e8579d7e9ab1c4`

The workload is the fixed 50-PR Martian Code Review Benchmark with 137 golden
issues. ReviewForge and Qodo v2 were evaluated by the same MiniMax-M3 judge
against the same golden set. Under the strict duplicate policy, only the
strongest candidate for a golden receives credit; additional reports remain
false positives. One candidate may cover multiple distinct goldens, so TP plus
FP can be slightly larger than the raw candidate count.

| Metric | ReviewForge v3 | Qodo v2 | Difference |
| --- | ---: | ---: | ---: |
| Evaluated PRs | 50 | 50 | 0 |
| Candidate comments | 151 | 104 | +47 |
| TP | 65 | 62 | +3 |
| FP | 87 | 45 | +42 |
| FN | 72 | 75 | -3 |
| Precision | 42.76% | **57.94%** | -15.18 pp |
| Recall | **47.45%** | 45.26% | +2.19 pp |
| F1 | 44.98% | **50.82%** | -5.84 pp |

ReviewForge finds slightly more golden issues, but publishes too many unmatched
or semantically overlapping candidates. It does not surpass Qodo on strict F1
in this full run.

## Improvement over the previous ReviewForge full run

| Metric | Previous V3 | Stable V3 | Change |
| --- | ---: | ---: | ---: |
| Published comments | 252 | 151 | -40.08% |
| Review tokens | 21,054,909 | 16,577,583 | -21.26% |
| TP | 66 | 65 | -1 |
| FP | 180 | 87 | -93 |
| FN | 71 | 72 | +1 |
| Precision | 26.83% | 42.76% | +15.93 pp |
| Recall | 48.18% | 47.45% | -0.73 pp |
| F1 | 34.46% | 44.98% | +10.52 pp |

The stable baseline substantially improves publication precision and
efficiency while nearly preserving recall.

## Coverage and reliability

- 11 ReviewForge PRs published no comments; those PRs contain 21 golden issues
  and account for 29.17% of ReviewForge false negatives.
- Qodo published no candidates on 2 PRs containing 2 golden issues.
- ReviewForge produced no byte-identical duplicate comment bodies. Semantic
  duplicates and non-golden but potentially valid findings are still strict
  false positives.
- All 50 PR-level reviews completed. There were 21 failed internal reviewer
  tasks, so partial agent failure remains a recall risk.
- Strong clean results include `discourse-graphite#7` (3 TP, 0 FP, 0 FN),
  `keycloak-greptile#1` (2 TP, 0 FP, 0 FN), and `grafana/grafana#94942`
  (2 TP, 0 FP, 0 FN).

## Cost and latency

- Review tokens: 16,577,583
- Average review tokens per PR: 331,552
- MiniMax-M3: 14,288,384 tokens (86.19%)
- MiniMax-M2.7: 2,289,199 tokens (13.81%)
- Cumulative PR duration: 9.40 agent-hours
- Active three-shard wall time: approximately 3 hours 45 minutes
- Judge tokens: 110,329

Largest review-token consumers:

| Agent | Tokens | Share |
| --- | ---: | ---: |
| Security Reviewer | 7,488,045 | 45.17% |
| Correctness Reviewer | 2,747,622 | 16.57% |
| Publication Gate | 1,811,137 | 10.93% |
| Dynamic Calibrator | 1,541,774 | 9.30% |
| Testing Reviewer | 1,187,031 | 7.16% |

## Interpretation

The primary remaining gap is publication precision rather than raw discovery.
The next improvement should target model-independent semantic equivalence,
abstention, failed-reviewer recovery, and empty-review coverage. Increasing
reviewer breadth alone is unlikely to beat Qodo because ReviewForge already has
slightly higher recall.

For a self-hosted tool used asynchronously by a small team, the current result
is usable. It does not support claims of complete detection, zero false
positives, sub-minute feedback, or universal superiority over Qodo.

## Methodology limits

- LLM judging is probabilistic. The same fixed Qodo candidates have received
  materially different scores in separate judge runs.
- The within-run ReviewForge/Qodo comparison is therefore more reliable than
  comparing absolute scores across dates.
- Product-level superiority claims require unseen holdout PRs, repeated runs,
  confidence intervals, and preferably multiple independent judges.
- Benchmark artifacts and credentials are not committed. Local artifacts live
  under ignored `.reviewforge/` directories.
