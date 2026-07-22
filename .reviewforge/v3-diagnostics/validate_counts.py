"""
Validate canonical counts against authoritative Martian 50 benchmark artifacts.

Assertions (from the diagnosis document):
  - 50 PRs in workload
  - 137 golden findings total
  - 101 review_comments (confirmed = published)
  - 211 total_findings (pre-verification candidates)
  - 110 false_positives
  - 166 tasks_completed (sum of per-PR)
  - 2 tasks_failed (sum of per-PR)
  - Overall: TP=35, FP=55, FN=102, precision~38.89%, recall~25.55%, F1~30.84%

Exit 0 on success, 1 on any mismatch.
"""
import json
import sys
import os

BASE = os.environ.get(
    "BENCHMARK_DIR",
    "E:/ReviewForge-mimo-p1/.reviewforge/martian50-0823da6",
)


def load(name):
    with open(os.path.join(BASE, name), encoding="utf-8") as f:
        return json.load(f)


def approx(a, b, tol=0.005):
    return abs(a - b) < tol


errors = []

# ── workload.json ──
workload = load("workload.json")
n_prs = len(workload)
golden_total = sum(len(pr.get("golden_comments", [])) for pr in workload)

if n_prs != 50:
    errors.append(f"workload PR count: expected 50, got {n_prs}")
if golden_total != 137:
    errors.append(f"workload golden findings: expected 137, got {golden_total}")

# ── results-merged.json ──
results = load("results-merged.json")
n_results = len(results)
total_findings = sum(r["summary"]["total_findings"] for r in results)
confirmed = sum(r["summary"]["confirmed"] for r in results)
false_positives = sum(r["summary"]["false_positives"] for r in results)
review_comments = sum(len(r.get("review_comments", [])) for r in results)
tasks_completed = sum(r["summary"].get("tasks_completed", 0) for r in results)
tasks_failed = sum(r["summary"].get("tasks_failed", 0) for r in results)

if n_results != 50:
    errors.append(f"results-merged PR count: expected 50, got {n_results}")
if total_findings != 211:
    errors.append(f"total_findings: expected 211, got {total_findings}")
if confirmed != 101:
    errors.append(f"confirmed: expected 101, got {confirmed}")
if false_positives != 110:
    errors.append(f"false_positives: expected 110, got {false_positives}")
if review_comments != 101:
    errors.append(f"review_comments: expected 101, got {review_comments}")
if tasks_completed != 166:
    errors.append(f"tasks_completed: expected 166, got {tasks_completed}")
if tasks_failed != 2:
    errors.append(f"tasks_failed: expected 2, got {tasks_failed}")

# cross-check: confirmed + false_positives == total_findings
if confirmed + false_positives != total_findings:
    errors.append(
        f"cross-check: confirmed({confirmed}) + fp({false_positives}) != total_findings({total_findings})"
    )

# ── judge-merged.json ──
judge = load("judge-merged.json")
completed = judge.get("completed", {})
tp = sum(e.get("reviewforge", {}).get("tp", 0) for e in completed.values())
fp = sum(e.get("reviewforge", {}).get("fp", 0) for e in completed.values())
fn = sum(e.get("reviewforge", {}).get("fn", 0) for e in completed.values())

if len(completed) != 50:
    errors.append(f"judge-merged completed entries: expected 50, got {len(completed)}")
if tp != 35:
    errors.append(f"judge TP: expected 35, got {tp}")
if fp != 55:
    errors.append(f"judge FP: expected 55, got {fp}")
if fn != 102:
    errors.append(f"judge FN: expected 102, got {fn}")

precision = tp / (tp + fp) if (tp + fp) > 0 else 0
recall = tp / (tp + fn) if (tp + fn) > 0 else 0
f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0

if not approx(precision, 0.3889, 0.005):
    errors.append(f"precision: expected ~0.3889, got {precision:.4f}")
if not approx(recall, 0.2555, 0.005):
    errors.append(f"recall: expected ~0.2555, got {recall:.4f}")
if not approx(f1, 0.3084, 0.005):
    errors.append(f"F1: expected ~0.3084, got {f1:.4f}")

# ── summary.json ──
summary = load("summary.json")
rf_overall = summary.get("overall", {}).get("reviewforge", {})
if not approx(rf_overall.get("precision", 0), 0.3889, 0.005):
    errors.append(f"summary RF precision mismatch: {rf_overall.get('precision')}")
if not approx(rf_overall.get("recall", 0), 0.2555, 0.005):
    errors.append(f"summary RF recall mismatch: {rf_overall.get('recall')}")
if not approx(rf_overall.get("f1", 0), 0.3084, 0.005):
    errors.append(f"summary RF F1 mismatch: {rf_overall.get('f1')}")

summary_tokens = summary.get("execution", {}).get("tokens", 0)
if summary_tokens != 2409261:
    errors.append(f"summary tokens: expected 2409261, got {summary_tokens}")

# ── Report ──
if errors:
    print("VALIDATION FAILED:")
    for e in errors:
        print(f"  - {e}")
    sys.exit(1)
else:
    print("VALIDATION PASSED — all canonical counts match:")
    print(f"  workload:     {n_prs} PRs, {golden_total} golden findings")
    print(f"  results:      {total_findings} candidates, {confirmed} confirmed, {false_positives} FP")
    print(f"  comments:     {review_comments}")
    print(f"  tasks:        {tasks_completed} completed, {tasks_failed} failed")
    print(f"  judge:        TP={tp}, FP={fp}, FN={fn}")
    print(f"  metrics:      P={precision:.4f}, R={recall:.4f}, F1={f1:.4f}")
    print(f"  tokens:       {summary_tokens:,}")
    sys.exit(0)
