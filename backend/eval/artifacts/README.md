# Evaluation artifacts

Put generated benchmark inputs, results and logs in this directory.

Typical files include:

- `manifest.json`
- `findings.json`
- `tokens.json`
- `gauntlet-scanner.json`
- `live-benchmark.json`
- `*.log`

Everything in this directory except this file is ignored by Git. Benchmark definitions and reusable fixtures belong in `backend/eval/` or `test_fixtures/`, not here.
