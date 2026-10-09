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

`llm-traces/` contains public benchmark source and model responses. Keep credential
files outside the output directory. Each process uses its own SQLite DB; completed
results are skipped on restart. Diagnose partial execution before treating a
zero-comment result as successful review coverage.

The strict judge preserves the existing matching prompts, threshold, one-to-one
matching and duplicate penalties. Its settings-path adapter permits use of the
same existing provider from an isolated checkout. Record its SHA256 with each
paired report. Do not use holdout results for tuning.

The v4 delivery recovery protocol matches the hidden marker against submitted
reviews at the same commit, following the [GitHub review listing API](https://docs.github.com/en/rest/pulls/reviews?apiVersion=2022-11-28).
An absent marker after an ambiguous write does not authorize another POST.
