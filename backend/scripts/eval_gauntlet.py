"""Run ReviewForge golden expected-finding evaluation.

Usage:
  python scripts/eval_gauntlet.py --scanner-only
  python scripts/eval_gauntlet.py --actual-findings findings.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from reviewforge.eval.gauntlet import load_golden, run_full_eval, run_scanner_eval, score_findings


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate ReviewForge detection quality against a golden gauntlet.")
    parser.add_argument(
        "--golden",
        default=str(Path(__file__).resolve().parent.parent / "eval" / "golden_expected_findings.json"),
        help="Path to golden expected findings JSON.",
    )
    parser.add_argument("--actual-findings", default="", help="Optional JSON list of findings from a review run.")
    parser.add_argument(
        "--scanner-only", action="store_true", help="Run deterministic scanners against golden fixtures."
    )
    parser.add_argument("--tokens", type=int, default=0, help="Token total to attach when scoring external findings.")
    parser.add_argument("--out", default="", help="Optional output JSON path.")
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parents[2]
    golden = load_golden(args.golden)

    if args.actual_findings:
        findings = json.loads(Path(args.actual_findings).read_text(encoding="utf-8"))
        result = score_findings(golden, findings, token_total=args.tokens, mode="external")
    elif args.scanner_only:
        result = run_scanner_eval(golden, repo_root)
    else:
        result = asyncio.run(run_full_eval(golden, repo_root))

    text = json.dumps(result, indent=2, ensure_ascii=False)
    print(text)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
