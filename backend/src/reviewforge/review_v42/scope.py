"""Compile changed regions into explicit, non-overlapping review ownership.

Earlier policies assign the whole PR to one reviewer. The units-6 policy groups
regions by file and explicit transaction identity; separate integration reviews
cross-unit behavior. Grouping hints are not evidence of a call path.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from reviewforge.review_v42.contracts import ReviewScope, Run


def compile_scopes(run: Run, regions: list[dict[str, Any]]) -> list[ReviewScope]:
    """Assign every distinct changed region once, including unavailable regions.

    Input should include deletion, binary and parsing failures as region records.
    Identical records are deduplicated; overlapping regions remain distinguishable
    for coverage while belonging to the same task.
    """
    rows = []
    seen = set()
    ids = set()
    for item in regions:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str) or not item["path"]:
            raise ValueError("each changed region needs a path")
        row = dict(item)
        identity = json.dumps({k: v for k, v in row.items() if k != "id"}, sort_keys=True, ensure_ascii=False)
        region_id = row.get("id") or "region_" + hashlib.sha256(identity.encode()).hexdigest()[:20]
        if (region_id, identity) in seen:
            if run.policy == "v42-units-6":
                raise ValueError("duplicate region in changed manifest")
            continue
        seen.add((region_id, identity))
        if not isinstance(region_id, str) or (run.policy == "v42-units-6" and not region_id) or region_id in ids:
            raise ValueError("duplicate or invalid region id")
        ids.add(region_id)
        row["id"] = region_id
        rows.append(row)
    if not rows:
        return []
    if run.policy == "v42-units-6":
        # A path is a conservative boundary, not evidence that paths are independent.
        # Explicit transaction IDs can only merge ownership; they never suppress
        # the separate cross-unit investigation of all resulting units.
        parent = list(range(len(rows)))

        def find(i: int) -> int:
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        def union(a: int, b: int) -> None:
            a, b = find(a), find(b)
            if a != b:
                parent[b] = a

        paths: dict[str, int] = {}
        transactions: dict[str, int] = {}
        for i, row in enumerate(rows):
            if row["path"] in paths:
                union(i, paths[row["path"]])
            else:
                paths[row["path"]] = i
            transaction = row.get("transaction_id")
            if transaction is not None:
                if not isinstance(transaction, str) or not transaction.strip():
                    raise ValueError("transaction_id must be a nonempty string")
                if transaction in transactions:
                    union(i, transactions[transaction])
                else:
                    transactions[transaction] = i
        groups: dict[int, list[dict[str, Any]]] = {}
        for i, row in enumerate(rows):
            groups.setdefault(find(i), []).append(row)
        compiled = []
        for group in groups.values():
            group.sort(key=lambda row: row["id"])
            identity = json.dumps([run.id, "units-6", [row["id"] for row in group]], ensure_ascii=False)
            compiled.append(
                ReviewScope(
                    id="scope_" + hashlib.sha256(identity.encode()).hexdigest()[:20],
                    run_id=run.id,
                    title="Changed behavior: " + ", ".join(sorted({row["path"] for row in group})),
                    regions=group,
                )
            )
        return sorted(compiled, key=lambda scope: scope.id)
    return [
        ReviewScope(
            id="scope_" + hashlib.sha256(run.id.encode()).hexdigest()[:20],
            run_id=run.id,
            title="PR changed behavior",
            regions=rows,
        )
    ]
