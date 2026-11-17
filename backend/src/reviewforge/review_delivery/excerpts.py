"""Plan original excerpts from controller-compiled, pinned diff regions.

Hunk headers select inclusive source ranges; diff text never supplies their
contents. Empty sides and structural changes cannot fabricate original lines.
"""

from __future__ import annotations

import re

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def original_ranges(regions, path):
    selected = [region for region in regions if region["path"] == path]
    ranges = {"base": [], "head": []}
    gaps = []
    for region in selected:
        header = _HUNK.match(region.get("patch", ""))
        if region.get("unsupported") or header is None:
            gaps.append({"path": path, "region_id": region["id"], "reason": region.get("reason", "no textual hunk")})
            continue
        for side, offset in (("base", 1), ("head", 3)):
            start, count = int(header[offset]), int(header[offset + 1] or 1)
            if count and start > 0:
                ranges[side].append((start, start + count - 1))
    merged = []
    for side, intervals in ranges.items():
        union = []
        for start, end in sorted(intervals):
            if union and start <= union[-1][1] + 1:
                union[-1] = (union[-1][0], max(union[-1][1], end))
            else:
                union.append((start, end))
        merged.extend({"path": path, "side": side, "start_line": start, "end_line": end} for start, end in union)
    return merged, gaps
