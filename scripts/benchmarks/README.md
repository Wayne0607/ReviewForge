# Read-only quality benchmarks

Run on Linux using the backend environment. The runner reads GitHub PR heads,
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

Environment variables:

- `REVIEWFORGE_REPO_ROOT`: isolated source checkout.
- `REVIEWFORGE_ENV_FILE`: existing credential file read by this process.
- `REVIEWFORGE_SETTINGS_DIR`: existing encrypted settings directory, read only.
- `REVIEWFORGE_SOURCE_REVISION`: exact source revision recorded with results.

Use `--pipeline legacy|shadow|hypothesis`, `--model-override`, and
`--output-language en`. A model override also clears role-specific model/endpoint
overrides and aligns every profile with the global provider. Record the optional
`--reasoning-effort` identically for both sides. Never compare runs with different
PR head SHAs or change parameters between paired runs.

`--llm-min-interval` defaults to 30 seconds between request starts across all
benchmark processes on the host. The benchmark disables SDK automatic retries;
rate failures remain visible instead of consuming an unrecorded retry burst.
Tracing wraps the provider below the token wrapper so private `_agenerate`
delegation cannot bypass it. Judge requests must also respect the provider's
quota; do not run the judge while review workers are using the same quota.

`llm-traces/` contains public benchmark source and model responses. Keep credential
files outside the output directory. Each process uses its own SQLite DB; completed
results are skipped on restart. Diagnose partial execution before treating a
zero-comment result as successful review coverage.

For an OpenAI-compatible provider supporting DeepSeek's thinking parameter,
`--thinking disabled` requests the non-thinking model. Apply it identically to
both review sides and the judge, and label this experiment separately from the
provider-default runs. `--profile` enables periodic Python stack dumps; ordinary
runs keep them disabled. SIGTERM cancels the benchmark task and cleans its known
workspaces and connections.

The strict judge preserves the existing matching prompts, threshold, one-to-one
matching and duplicate penalties. Its settings-path adapter permits use of the
same existing provider from an isolated checkout. Record its SHA256 with each
paired report. Do not use holdout results for tuning.

`context_snapshot.py --repo keycloak/keycloak --pr 36880 --output context.json`
captures a pinned workspace, semantic units, every collected source slice and the
bounded rendered pack without invoking an LLM. It records omitted context kinds,
source/head/script hashes and checks repeat rendering for determinism. Use the
same isolated environment variables as the runner; cleanup always releases the
workspace. The snapshot describes supplied context, not review quality.

The v4 delivery recovery protocol matches the hidden marker against submitted
reviews at the same commit, following the [GitHub review listing API](https://docs.github.com/en/rest/pulls/reviews?apiVersion=2022-11-28).
An absent marker after an ambiguous write does not authorize another POST.
