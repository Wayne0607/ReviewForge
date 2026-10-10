# Read-only quality benchmarks

Run on Linux or Windows using the backend environment. The runner reads GitHub PR heads,
intercepts review writes into local receipts, and blocks any other GitHub HTTP
method except GET/HEAD. Results score actual emitted comments rather than the
legacy finding store. `summary.status` retains operational partial/failed state;
the outer `status` describes whether the benchmark execution returned a result.

On a host shared with production, run evaluation inside a separate systemd
resource group with explicit aggregate memory/CPU limits and lower priority.
Read available memory and reserve capacity for production before launching.
Run large-repository context captures sequentially. Source-directory isolation
alone does not isolate CPU or memory; do not launch multiple bare Python context
jobs on the production host. Resource-limit failures must remain failed/partial,
never a zero-finding success.

`launch_isolated.py` is the shared-host entry point for `runner`, `context`, and
`judge`. It requires Linux root, systemd, and cgroup v2; it refuses an unbounded
fallback or the production checkout. Choose the limits from actual host capacity
and production load. Both the memory reserve and disk reserve are required.

```bash
python scripts/benchmarks/launch_isolated.py \
  --repo-root "$EVAL_SNAPSHOT" \
  --python /opt/reviewforge/backend/.venv/bin/python \
  --env-file /opt/reviewforge/.env \
  --settings-dir /opt/reviewforge/.reviewforge \
  --revision "$EVAL_SOURCE_REVISION" \
  --record "$EVAL_SNAPSHOT/results/context/execution.json" \
  --memory-mb "$EVAL_MEMORY_MB" --reserve-mb "$PRODUCTION_RESERVE_MB" \
  --disk-reserve-mb "$PRODUCTION_DISK_RESERVE_MB" \
  --cpu-percent "$EVAL_CPU_PERCENT" --runtime-seconds "$EVAL_RUNTIME_SECONDS" \
  --task context -- --repo keycloak/keycloak --pr 36880 \
  --output "$EVAL_SNAPSHOT/results/context/context.json"
```

The launcher holds a host-wide process lock until the task exits, rejects an
existing evaluation service or known unmanaged evaluation process, and checks
that the production service is active. A transient service sets MemoryHigh,
MemoryMax, zero swap, CPUQuota, TasksMax and RuntimeMaxSec for the entire process
tree. The child verifies the actual kernel memory/swap/CPU limits before importing
the workload. CPUQuota is a percentage of one CPU, as described in the
[systemd resource-control manual](https://github.com/systemd/systemd/blob/main/man/systemd.resource-control.xml).
An execution record and log retain failures; an interrupted launcher stops its
own service. RuntimeMaxSec also bounds an orphaned service. Records are new for
each attempt; an old result file is not proof that a new attempt succeeded.

These checks require a bounded smoke test on the actual host before evaluation.
The disk reserve is a preflight free-space check, not a filesystem quota; monitor
artifact growth and clean only known evaluation workspaces. Unit completion
indicates process exit, not that an inner partial review passed quality gates.

On Windows, use `launch_windows.py` with the same explicit arguments. It holds a
per-user evaluation lock and checks free memory/disk reserves. A named Job Object
caps aggregate committed memory and CPU use for the workload and descendants,
including Python virtual-environment redirectors. The workload assigns itself to
the job and verifies actual limits before loading the benchmark script. The job
uses lower priority and kill-on-close; timeout or launcher death terminates the
workload tree. Unsupported kernel controls cause failure, with no unbounded
fallback. Windows CPU percentage is relative to the system (or a containing job),
as described in [Microsoft's Job Objects documentation](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects)
and [CPU rate control](https://learn.microsoft.com/en-us/windows/win32/api/winnt/ns-winnt-jobobject_cpu_rate_control_information).
Keep both comparison sides on the same host and with identical resource limits.

Environment variables:

- `REVIEWFORGE_REPO_ROOT`: isolated source checkout.
- `REVIEWFORGE_ENV_FILE`: existing credential file read by this process.
- `REVIEWFORGE_SETTINGS_DIR`: existing encrypted settings directory, read only.
- `REVIEWFORGE_SOURCE_REVISION`: exact source revision recorded with results.

Use `--pipeline legacy|shadow|hypothesis`, `--model-override`, and
`--output-language en`. A model override also clears role-specific model/endpoint
overrides and clears legacy profiles, as required by SPEC §7. Record the optional
`--reasoning-effort` identically for both sides. Never compare runs with different
PR head SHAs or change parameters between paired runs.

`--llm-min-interval` defaults to 30 seconds between request starts across all
benchmark processes on the host. The benchmark disables SDK automatic retries;
the zero-retry option is supplied before constructing provider clients, and its
effective HTTP behavior is covered by a real-SDK/mock-transport regression.
rate failures remain visible instead of consuming an unrecorded retry burst.
Tracing wraps the provider below the token wrapper so private `_agenerate`
delegation cannot bypass it. Judge requests must also respect the provider's
quota; do not run the judge while review workers are using the same quota.

`llm-traces/` contains public benchmark source and model responses. Keep credential
files outside the output directory. Each process uses its own SQLite DB. Restart
skips only operationally complete reviews; inner partial/failed/duplicate-skipped
results are reattempted. Saved results require identical code, workload, provider
and run parameters, and the PR head is rechecked before skipping. Changed
provenance requires a new output directory.

The judge requires every requested PR to be operationally complete before it
calls the model or produces scores. It refuses to quietly exclude missing or
partial reviews. Judge resume is tied to exact input hashes and parameters.
Diagnose partial execution before treating a zero-comment result as successful
review coverage. The matching prompts, confidence threshold and one-to-one
matching remain unchanged. All requested judge decisions must also succeed before
aggregate scores are emitted; failed requests stay in `completed` for a same-input
retry, the artifact is `partial`, and no subset metrics are presented as final.

Use `martian_judge.py --ledger-recall` for SPEC Phase 2 diagnostics on complete v4
results. It validates that the ledger is pinned to the result head, reuses
`eval.ledger_recall.candidate_claims`, and applies the same judge/matching algorithm
to three separate pools: CONFIRMED + OPEN + UNKNOWN, CONFIRMED, and REFUTED.
`ledger_metrics` reports generation recall, confirmed recall, and the number of
goldens wrongly refuted. Primary `metrics` still score only published inline
comments and historical Qodo v2 candidates. The flag and helper hash are recorded
in judge provenance; optional diagnostics cannot replace primary publication F1.

For an OpenAI-compatible provider supporting DeepSeek's thinking parameter,
`--thinking disabled` requests the non-thinking model. Apply it identically to
both review sides and the judge, and label this experiment separately from the
provider-default runs. `--profile` enables periodic Python stack dumps; ordinary
runs keep them disabled. SIGTERM cancels the benchmark task and cleans its known
workspaces and connections. Windows also handles Ctrl+Break; forcefully terminating
a Windows process cannot promise Python cleanup. Request pacing uses a portable
process lock and discards stale monotonic timestamps from before a host reboot.

The strict judge preserves the existing matching prompts, threshold, one-to-one
matching and duplicate penalties. Its settings-path adapter permits use of the
same existing provider from an isolated checkout. Record its SHA256 with each
paired report. Do not use holdout results for tuning.

`context_snapshot.py --repo keycloak/keycloak --pr 36880 --output context.json`
captures a pinned workspace, semantic units, every collected source slice and the
bounded rendered pack without invoking an LLM. It records omitted context kinds,
source/head/script hashes and checks repeat rendering for determinism. Use the
same isolated environment variables as the runner; cleanup always releases the
workspace. Its runtime initializes only the repository gateway and database;
model clients and LLM credentials are not required. The snapshot describes
supplied context, not review quality.
Use `--require-tarball` for repository context audits; API fallback still saves a
diagnostic artifact but exits unsuccessfully. The log reports snapshot source,
file/unit/slice counts so a zero-slice degraded pack cannot look like a passed audit.

The v4 delivery recovery protocol matches the hidden marker against submitted
reviews at the same commit, following the [GitHub review listing API](https://docs.github.com/en/rest/pulls/reviews?apiVersion=2022-11-28).
An absent marker after an ambiguous write does not authorize another POST.
Confirmed facts discovered after the first publication use separate immutable
supplement batches. Resume reconciles all existing batches before new model work
or writes, retains run-wide inline limits, and does not rerun the editor LLM.
Historical development outboxes without coverage metadata cannot safely continue
remaining hypotheses; start a new isolated run instead of guessing coverage.
