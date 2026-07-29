# ReviewForge v3 benchmark

## Final short-term release result

Date: 2026-07-29

ReviewForge commit: `fc7f964`

Workload: 10 fixed representative PRs selected from the 50-PR Martian Code Review Benchmark

ReviewForge and Qodo v2 were evaluated against the same golden findings. Both candidate sets were passed through the same MiniMax judge and the same strict duplicate policy. Duplicate reports of one root cause count as false positives.

| Metric | ReviewForge v3 | Qodo v2 |
| --- | ---: | ---: |
| TP | 19 | 20 |
| FP | 14 | 15 |
| FN | 20 | 19 |
| Precision | **57.58%** | 57.14% |
| Recall | 48.72% | **51.28%** |
| F1 | 52.78% | **54.05%** |
| Candidate comments | 32 | 35 |

ReviewForge has slightly higher precision and one fewer false positive. Qodo has one more true positive, one fewer false negative, and a 1.27 percentage-point F1 advantage. This is a near-peer result on the selected sample, not evidence that either product is universally superior.

## Per-PR outcome

| PR | ReviewForge TP / FP / FN | Qodo TP / FP / FN | ReviewForge comments |
| --- | ---: | ---: | ---: |
| grafana/grafana#90045 | 3 / 1 / 0 | 2 / 4 / 1 | 4 |
| keycloak/keycloak#37429 | 3 / 0 / 1 | 1 / 1 / 3 | 3 |
| keycloak/keycloak#36882 | 0 / 0 / 1 | 0 / 0 / 1 | 0 |
| getsentry/sentry#93824 | 1 / 1 / 4 | 1 / 1 / 4 | 2 |
| sentry-greptile#1 | 1 / 0 / 3 | 2 / 1 / 2 | 1 |
| grafana/grafana#97529 | 1 / 0 / 1 | 2 / 2 / 0 | 1 |
| discourse-graphite#10 | 0 / 3 / 4 | 2 / 2 / 2 | 3 |
| discourse-graphite#4 | 4 / 3 / 2 | 4 / 1 / 2 | 7 |
| cal.com#14740 | 4 / 4 / 1 | 4 / 0 / 1 | 7 |
| cal.com#10967 | 2 / 2 / 3 | 2 / 3 / 3 | 4 |

## Cost and latency

- Review pipeline tokens: 3,343,534
- Judge tokens: 27,581
- MiniMax-M3 tokens: 2,781,889
- MiniMax-M2.7 tokens: 561,645
- Three-shard wall time: approximately 67 minutes
- Slowest PR: approximately 52 minutes

Largest token consumers:

| Agent | Tokens |
| --- | ---: |
| Publication Gate | 1,054,761 |
| Security Reviewer | 578,145 |
| Correctness Reviewer | 547,425 |
| Dynamic Calibrator | 356,553 |
| Testing Reviewer | 303,686 |

## Interpretation

The current strength is precision: ReviewForge is conservative enough to keep false positives close to Qodo while still finding substantial correctness, security, race, and cross-file issues.

The remaining quality gap is primarily recall. Hard cross-file data flow, implicit business contracts, and issues that require deep repository context are still missed. The remaining operational weakness is tail latency on large PRs when the provider performs many tool-loop and publication-gate calls.

For a self-hosted tool used asynchronously by a small team, the result is usable. It is not suitable for a product promise of complete detection or sub-minute feedback.

## Methodology limits

- This final iteration used 10 representative PRs, not the complete 50-PR suite.
- The selected PRs participated in optimization, so this is not a clean holdout result.
- LLM judging is probabilistic and can contain correlated model bias.
- Product-level superiority claims require an unseen holdout set, repeated runs, confidence intervals, and ideally more than one independent judge.
- Benchmark artifacts and credentials are intentionally not committed to Git. Local artifacts live under ignored `.reviewforge/` directories.
