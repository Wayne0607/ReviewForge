"""Same-root-cause duplicate audit for false positives (§7).

A cluster is keyed by ``(path, mechanism)``.  Within a cluster, every item
after the first counts as one duplicate, so repeated reports of the same root
cause on the same file are not double-counted by the paired judge.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path


def duplicate_count(entries: Iterable[dict]) -> int:
    """Return the number of extra items beyond the first in each cluster."""

    clusters: dict[tuple[str, str], int] = defaultdict(int)
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        path = str(entry.get("path", "") or "").strip()
        mechanism = str(entry.get("mechanism", "") or "").strip()
        if path and mechanism:
            clusters[(path, mechanism)] += 1
    return sum(size - 1 for size in clusters.values() if size > 1)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, help="JSON list of {path, mechanism} FP entries")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    entries = json.loads(Path(args.input).read_text(encoding="utf-8"))
    count = duplicate_count(entries)
    Path(args.output).write_text(json.dumps({"duplicates": count}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(count, flush=True)


if __name__ == "__main__":
    main()
