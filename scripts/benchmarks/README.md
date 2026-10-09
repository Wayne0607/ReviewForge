# Read-only quality benchmarks

Run on Linux using the backend environment. The runner reads GitHub PR heads,
intercepts review writes into local receipts, and blocks any other GitHub HTTP
method except GET/HEAD. Results score actual emitted comments rather than the
legacy finding store. `summary.status` retains operational partial/failed state;
the outer `status` describes whether the benchmark execution returned a result.

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
