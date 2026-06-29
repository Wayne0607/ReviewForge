"""A/B evaluation: single-shot vs agentic security_reviewer.

Usage:
  python scripts/eval_agentic.py --mock    # mock mode (verify pipeline)
  python scripts/eval_agentic.py --real    # real LLM (meaningful comparison)
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

# Load .env
env_path = Path(__file__).parent.parent.parent / ".env"
if env_path.exists():
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def load_fixtures() -> dict[str, str]:
    """Load fixture files and their content."""
    fixtures_dir = Path(__file__).parent.parent.parent / "examples" / "fixtures"
    fixtures = {}
    for f in fixtures_dir.glob("*.py"):
        fixtures[f.name] = f.read_text(encoding="utf-8")
    return fixtures


def load_labels() -> dict[str, list[str]]:
    """Load expected security categories per fixture."""
    labels_path = Path(__file__).parent.parent.parent / "examples" / "fixtures" / "labels.json"
    if not labels_path.exists():
        return {}
    return json.loads(labels_path.read_text(encoding="utf-8"))


async def run_reviewer(llm, gateway, state, mode: str, fixture_name: str, fixture_content: str) -> dict:
    """Run a single review and collect metrics."""
    from reviewforge.core.specs import build_registry
    from reviewforge.core.state import ReviewTask
    from reviewforge.engine.reviewers import SecurityReviewer

    registry = build_registry()

    if mode == "agentic":
        reviewer = SecurityReviewer(llm, registry, gateway, agentic=True, max_tokens=10000)
    else:
        reviewer = SecurityReviewer(llm, registry, gateway, agentic=False)

    task = ReviewTask(
        reviewer="security_reviewer",
        files=[fixture_name],
        rationale="eval",
    )

    start = time.monotonic()
    findings = await reviewer.execute(task, state)
    latency = time.monotonic() - start

    categories = {f.category for f in findings}
    return {
        "mode": mode,
        "fixture": fixture_name,
        "findings_count": len(findings),
        "categories": sorted(categories),
        "latency_s": round(latency, 2),
        "findings": [f.to_dict() for f in findings],
    }


def _match_category(actual: str, expected_set: set[str]) -> bool:
    """Fuzzy match: actual contains any expected keyword, or vice versa."""
    for exp in expected_set:
        if exp in actual or actual in exp:
            return True
    return False


def compute_metrics(results: list[dict], labels: dict[str, list[str]]) -> dict:
    """Compute precision/recall against labeled ground truth (fuzzy match)."""
    tp, fp, fn = 0, 0, 0
    details = []

    for r in results:
        expected = set(labels.get(r["fixture"], []))
        actual = set(r["categories"])

        # Fuzzy matching
        matched = set()
        for a in actual:
            if _match_category(a, expected):
                matched.add(a)
        extra = actual - matched
        # Check which expected categories were covered
        covered_expected = set()
        for e in expected:
            for a in actual:
                if e in a or a in e:
                    covered_expected.add(e)
                    break
        missed = expected - covered_expected

        tp += len(matched)
        fp += len(extra)
        fn += len(missed)

        details.append(
            {
                "fixture": r["fixture"],
                "mode": r["mode"],
                "expected": sorted(expected),
                "actual": sorted(actual),
                "matched": sorted(matched),
                "extra": sorted(extra),
                "missed": sorted(missed),
                "latency_s": r["latency_s"],
            }
        )

    precision = tp / (tp + fp) if (tp + fp) > 0 else 0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0

    return {
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "precision": round(precision, 3),
        "recall": round(recall, 3),
        "f1": round(f1, 3),
        "details": details,
    }


async def main() -> None:
    use_mock = "--mock" in sys.argv

    fixtures = load_fixtures()
    labels = load_labels()

    if not fixtures:
        print("ERROR: No fixtures found in examples/fixtures/")
        sys.exit(1)

    print(f"Fixtures: {list(fixtures.keys())}")
    print(f"Mode: {'mock' if use_mock else 'real'}")

    # Setup LLM
    if use_mock:
        from reviewforge.engine.mock_llm import MockChatLLM

        llm = MockChatLLM()
    else:
        from reviewforge.core.config import ReviewForgeConfig
        from reviewforge.engine.model_router import ModelRouter

        cfg = ReviewForgeConfig.load()
        router = ModelRouter(cfg.llm)
        llm = router.get_llm("security_reviewer")
        print(f"Model: {cfg.llm.model}")

    # Setup mock gateway
    from reviewforge.core.specs import build_registry
    from reviewforge.tools.gateway import ToolGateway
    from reviewforge.tools.mock_github import MockGitHubClient

    registry = build_registry()
    gateway = ToolGateway(registry, MockGitHubClient())

    # Run single-shot and agentic for each fixture
    all_results = {"single_shot": [], "agentic": []}

    for name, content in fixtures.items():
        from reviewforge.core.state import StateStore

        state = StateStore(
            pr_number=0,
            repo="eval/eval",
            files_changed=[name],
            diff_summary=f"--- {name}\n+{content.replace(chr(10), chr(10) + '+')}",
        )

        # Single-shot
        print(f"\n[{name}] single-shot...", end=" ", flush=True)
        ss_result = await run_reviewer(llm, gateway, state, "single_shot", name, content)
        print(f"{ss_result['findings_count']} findings, {ss_result['latency_s']}s")
        all_results["single_shot"].append(ss_result)

        # Agentic
        print(f"[{name}] agentic...", end=" ", flush=True)
        ag_result = await run_reviewer(llm, gateway, state, "agentic", name, content)
        print(f"{ag_result['findings_count']} findings, {ag_result['latency_s']}s")
        all_results["agentic"].append(ag_result)

    # Compute metrics
    ss_metrics = compute_metrics(all_results["single_shot"], labels)
    ag_metrics = compute_metrics(all_results["agentic"], labels)

    # Print comparison table
    print("\n" + "=" * 60)
    print("A/B COMPARISON: single-shot vs agentic")
    print("=" * 60)
    print(f"{'Metric':<20} {'Single-shot':>12} {'Agentic':>12}")
    print("-" * 44)
    print(f"{'Precision':<20} {ss_metrics['precision']:>12.3f} {ag_metrics['precision']:>12.3f}")
    print(f"{'Recall':<20} {ss_metrics['recall']:>12.3f} {ag_metrics['recall']:>12.3f}")
    print(f"{'F1':<20} {ss_metrics['f1']:>12.3f} {ag_metrics['f1']:>12.3f}")
    print(f"{'TP':<20} {ss_metrics['tp']:>12} {ag_metrics['tp']:>12}")
    print(f"{'FP':<20} {ss_metrics['fp']:>12} {ag_metrics['fp']:>12}")
    print(f"{'FN':<20} {ss_metrics['fn']:>12} {ag_metrics['fn']:>12}")

    avg_ss_latency = sum(r["latency_s"] for r in all_results["single_shot"]) / len(all_results["single_shot"])
    avg_ag_latency = sum(r["latency_s"] for r in all_results["agentic"]) / len(all_results["agentic"])
    print(f"{'Avg latency (s)':<20} {avg_ss_latency:>12.2f} {avg_ag_latency:>12.2f}")

    # Save results
    output = {
        "mode": "mock" if use_mock else "real",
        "single_shot": ss_metrics,
        "agentic": ag_metrics,
        "avg_latency": {"single_shot": avg_ss_latency, "agentic": avg_ag_latency},
        "raw": all_results,
    }
    out_path = Path(__file__).parent / "eval_result.json"
    out_path.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nResults saved to {out_path}")


if __name__ == "__main__":
    asyncio.run(main())
