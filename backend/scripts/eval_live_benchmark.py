"""Score exported ReviewForge PR findings against a blind benchmark manifest."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from reviewforge.eval.live_benchmark import score_live_benchmark


def _read_json(path: str):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main() -> None:
    parser = argparse.ArgumentParser(description="Score line-aware end-to-end PR review findings.")
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--findings", required=True)
    parser.add_argument("--tokens", default="")
    parser.add_argument("--line-tolerance", type=int, default=2)
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    result = score_live_benchmark(
        _read_json(args.manifest),
        _read_json(args.findings),
        _read_json(args.tokens) if args.tokens else None,
        line_tolerance=args.line_tolerance,
    )
    text = json.dumps(result, ensure_ascii=False, indent=2)
    print(text)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
