"""Compile changed regions into explicit, non-overlapping review ownership.

The conservative initial implementation assigns the whole PR to one reviewer;
region relationships are navigation hints, not evidence of a call path.
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
            continue
        seen.add((region_id, identity))
        if not isinstance(region_id, str) or region_id in ids:
            raise ValueError("duplicate or invalid region id")
        ids.add(region_id)
        row["id"] = region_id
        rows.append(row)
    if not rows:
        return []
    return [
        ReviewScope(
            id="scope_" + hashlib.sha256(run.id.encode()).hexdigest()[:20],
            run_id=run.id,
            title="PR changed behavior",
            regions=rows,
        )
    ]
