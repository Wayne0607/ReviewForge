"""Offline evaluation inputs and observability; no scoring or model calls."""

from __future__ import annotations

from typing import Any


def prepare_review_input(record: dict[str, Any]) -> dict[str, Any]:
    """Only explicitly allow snapshot inputs; goldens stay with the scorer."""
    required = ("repo", "base", "head")
    if any(not isinstance(record.get(k), str) or not record[k] for k in required):
        raise ValueError("fixed repo, base and head required")
    result = {key: record[key] for key in required}
    for key in ("diff", "description"):
        if isinstance(record.get(key), str):
            result[key] = record[key]
    return result


def summarize_run(report: dict[str, Any], requests: list[dict[str, Any]]) -> dict[str, Any]:
    """Report delivery and physical usage without treating unknown as zero."""
    known = {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0}
    unknown = 0
    unknown_cache = 0
    for request in requests:
        usage = request.get("usage")
        if not isinstance(usage, dict) or any(
            not isinstance(usage.get(k), int) or usage[k] < 0 for k in ("input_tokens", "output_tokens")
        ):
            unknown += 1
            unknown_cache += 1
            continue
        if not isinstance(usage.get("cache_read_tokens"), int) or usage["cache_read_tokens"] < 0:
            unknown_cache += 1
        for key in known:
            value = usage.get(key)
            if isinstance(value, int) and value >= 0:
                known[key] += value
    scopes = [scope for scope in report.get("scopes", []) if scope.get("status") != "superseded"]
    run = report.get("run") or {}
    manifest = (run.get("metadata") or {}).get("changed_regions")
    if run.get("policy") == "v42-units-6" and isinstance(manifest, list):
        # The integration scope is not a primary unit and cannot add coverage.
        # A duplicated or altered receipt cannot count as coverage either.
        scopes = [scope for scope in scopes if scope.get("regions")]
        expected = {
            region["id"]: region
            for region in manifest
            if isinstance(region, dict) and isinstance(region.get("id"), str) and region["id"]
        }
        manifest_counts: dict[str, int] = {}
        for region in manifest:
            if isinstance(region, dict) and isinstance(region.get("id"), str) and region["id"]:
                manifest_counts[region["id"]] = manifest_counts.get(region["id"], 0) + 1
        owners: dict[str, int] = {}
        checked: dict[str, int] = {}
        for scope in scopes:
            for region in scope.get("regions", []):
                if isinstance(region, dict) and expected.get(region.get("id")) == region:
                    owners[region["id"]] = owners.get(region["id"], 0) + 1
            for region in scope.get("checked_regions", []):
                if isinstance(region, dict) and expected.get(region.get("id")) == region and region in scope["regions"]:
                    checked[region["id"]] = checked.get(region["id"], 0) + 1
        region_count = len(manifest)
        checked_count = sum(
            manifest_counts[identity] == owners.get(identity) == checked.get(identity) == 1
            and not any(expected[identity].get(key) for key in ("unsupported", "unavailable", "binary", "parse_failed"))
            for identity in expected
        )
    else:
        region_count = sum(len(s.get("regions", [])) for s in scopes)
        checked_count = sum(len(s.get("checked_regions", [])) for s in scopes)
    verified = report.get("verified", [])
    verdicts = {
        key: sum(v.get("verdict") == key for v in verified)
        for key in ("accept", "reject", "needs_evidence", "unresolved")
    }
    return {
        "status": report.get("status", "partial"),
        "coverage": {
            "scopes": len(scopes),
            "reviewed": sum(s.get("status") == "reviewed" for s in scopes),
            "regions": region_count,
            "checked_regions": checked_count,
        },
        "funnel": {
            "candidates": report.get("candidates"),
            "verified": len(verified),
            "verdicts": verdicts,
            "comments": len(report.get("comments", [])),
            "unresolved": len(report.get("unresolved", [])),
            "duplicates": len(report.get("duplicates", [])),
        },
        "usage": {
            **known,
            "unknown_requests": unknown,
            "unknown_cache_requests": unknown_cache,
            "physical_requests": len(requests),
        },
        "quality": None,
    }
