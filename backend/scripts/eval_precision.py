"""Precision/recall A/B eval: single-shot vs agentic security_reviewer on a labeled fixture set.

Unlike the old eval, this serves the fixture files to the agentic tools (read_file/
search_code) so the tool loop actually investigates real code, counts tokens per run,
and normalizes category synonyms before scoring. Clean fixtures measure false positives.

Usage: python scripts/eval_precision.py --real   (real MiMo)   |   --mock
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import time
from pathlib import Path

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.outputs import ChatResult

FIX_DIR = Path(__file__).resolve().parent.parent.parent / "examples" / "fixtures"

# Canonical security categories + synonyms the models tend to emit.
_ALIASES = {
    "sql-injection": ["sql-injection", "sqli", "sql"],
    "command-injection": [
        "command-injection",
        "os-command-injection",
        "shell-injection",
        "command-execution",
        "command",
    ],
    "code-injection": ["code-injection", "eval-injection", "rce", "arbitrary-code-execution", "code-exec"],
    "insecure-deserialization": ["insecure-deserialization", "unsafe-deserialization", "deserialization", "pickle"],
    "hardcoded-secrets": [
        "hardcoded-secrets",
        "hardcoded-secret",
        "hardcoded-credentials",
        "hardcoded-password",
        "hardcoded-api-key",
        "secret",
        "credential",
    ],
    "weak-crypto": ["weak-crypto", "weak-hash", "insecure-crypto", "weak-cryptography", "md5", "insecure-hash"],
}


def canon(category: str) -> str | None:
    """Map a model-emitted category to a canonical security category, or None."""
    c = (category or "").lower().strip().replace(" ", "-").replace("_", "-")
    for canonical, names in _ALIASES.items():
        if any(n == c or n in c for n in names):
            return canonical
    return None


class FixtureGitHubClient:
    """Serves fixture file contents to the Tool Gateway so agentic tools read real code."""

    def __init__(self, fixtures: dict[str, str]) -> None:
        self._f = fixtures

    def _lookup(self, fp: str) -> str:
        return self._f.get(fp) or self._f.get(fp.split("/")[-1]) or ""

    async def get_file_diff(self, repo, pr_number, file_path):
        c = self._lookup(file_path)
        return "@@ fixture @@\n" + "\n".join("+" + ln for ln in c.splitlines()) if c else ""

    async def get_file_content(self, repo, ref, file_path):
        return self._lookup(file_path)

    async def search_code(self, repo, pattern, file_glob=""):
        try:
            rx = re.compile(pattern)
        except re.error:
            rx = None
        hits = []
        for name, content in self._f.items():
            for i, ln in enumerate(content.splitlines(), 1):
                if rx.search(ln) if rx else pattern in ln:
                    hits.append(f"{name}:{i}: {ln.strip()[:80]}")
        return "\n".join(hits[:20]) or "No results"

    async def post_review_comment(self, **kw):
        return {"id": 0}

    async def close(self):
        pass


class CountingLLM(BaseChatModel):
    """Wraps an LLM to accumulate token usage (handles flat ChatResult.generations)."""

    _inner: BaseChatModel
    _acc: dict

    class Config:
        arbitrary_types_allowed = True

    def __init__(self, inner, acc):
        super().__init__()
        self._inner = inner
        self._acc = acc

    async def _agenerate(self, messages, stop=None, run_manager=None, **kw) -> ChatResult:
        res = await self._inner._agenerate(messages, stop, run_manager, **kw)
        self._acc["calls"] += 1
        usage = (res.llm_output or {}).get("token_usage", {})
        tot = usage.get("total_tokens", 0)
        if not tot and res.generations:
            msg = getattr(res.generations[0], "message", None)
            um = getattr(msg, "usage_metadata", None) or {}
            tot = um.get("total_tokens", 0)
        self._acc["tokens"] += tot or 0
        return res

    def _generate(self, messages, stop=None, run_manager=None, **kw):
        return self._inner._generate(messages, stop, run_manager, **kw)

    @property
    def _llm_type(self):
        return "counting"

    @property
    def _identifying_params(self):
        return self._inner._identifying_params

    def bind_tools(self, tools, **kw):
        from langchain_core.utils.function_calling import convert_to_openai_tool

        return self.bind(tools=[convert_to_openai_tool(t) for t in tools], **kw)


def _load_fixtures() -> dict[str, str]:
    return {f.name: f.read_text(encoding="utf-8") for f in FIX_DIR.glob("*.py")}


async def _run_one(llm_inner, registry, skill, fixtures, fname, content, agentic):
    from reviewforge.core.state import ReviewTask, StateStore
    from reviewforge.engine.reviewers import SecurityReviewer
    from reviewforge.tools.gateway import ToolGateway

    acc = {"tokens": 0, "calls": 0}
    gw = ToolGateway(registry, FixtureGitHubClient(fixtures))
    rv = SecurityReviewer(CountingLLM(llm_inner, acc), registry, gw)
    rv._agentic = agentic
    if skill:
        rv._skill_body, rv._skill_name, rv._skill_refs = skill
    state = StateStore(pr_number=0, repo="eval/eval", head_sha="x", files_changed=[fname], diff_summary=content)
    t0 = time.monotonic()
    findings = await rv.execute(ReviewTask(reviewer="security_reviewer", files=[fname]), state)
    dt = round(time.monotonic() - t0, 1)
    cats = {canon(f.category) for f in findings} - {None}
    return {
        "fixture": fname,
        "categories": sorted(cats),
        "tokens": acc["tokens"],
        "calls": acc["calls"],
        "latency_s": dt,
    }


def _score(runs, labels):
    tp = fp = fn = 0
    detail = []
    for r in runs:
        expected = {canon(c) or c for c in labels.get(r["fixture"], [])}
        actual = set(r["categories"])
        matched = expected & actual
        tp += len(matched)
        fp += len(actual - expected)
        fn += len(expected - actual)
        detail.append(
            {
                **r,
                "expected": sorted(expected),
                "matched": sorted(matched),
                "missed": sorted(expected - actual),
                "extra": sorted(actual - expected),
            }
        )
    prec = tp / (tp + fp) if tp + fp else 0.0
    rec = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
    return {
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "precision": round(prec, 3),
        "recall": round(rec, 3),
        "f1": round(f1, 3),
        "tokens": sum(r["tokens"] for r in runs),
        "latency_s": round(sum(r["latency_s"] for r in runs), 1),
        "detail": detail,
    }


async def main():
    use_mock = "--mock" in sys.argv
    fixtures = _load_fixtures()
    labels = json.loads((FIX_DIR / "labels.json").read_text(encoding="utf-8"))
    from reviewforge.core.specs import build_registry

    registry = build_registry()

    if use_mock:
        from reviewforge.engine.mock_llm import MockChatLLM

        llm_inner = MockChatLLM()
    else:
        from reviewforge.core.config import ReviewForgeConfig
        from reviewforge.engine.model_router import ModelRouter

        cfg = ReviewForgeConfig.load()
        llm_inner = ModelRouter(cfg.llm).get_llm("security_reviewer")
        print(f"model={cfg.llm.model}")

    skill = None
    try:
        from reviewforge.skills.loader import SkillLoader

        loader = SkillLoader(Path(__file__).resolve().parent.parent / "src" / "reviewforge" / "skills")
        for m in loader.discover():
            if m.reviewer_type == "security":
                skill = (loader.load(m.name).body, m.name, list(m.references or []))
                break
    except Exception as e:
        print("skill load skipped:", e)

    print(f"fixtures: {len(fixtures)} | labels: {len(labels)}")
    out = {}
    for mode, agentic in [("single_shot", False), ("agentic", True)]:
        runs = []
        for fname, content in fixtures.items():
            r = await _run_one(llm_inner, registry, skill, fixtures, fname, content, agentic)
            print(f"  [{mode:11}] {fname:24} cats={r['categories']} tok={r['tokens']} {r['latency_s']}s")
            runs.append(r)
        out[mode] = _score(runs, labels)

    print("\n" + "=" * 60)
    print(f"{'metric':<16}{'single_shot':>14}{'agentic':>14}")
    for k in ("precision", "recall", "f1", "tp", "fp", "fn", "tokens", "latency_s"):
        print(f"{k:<16}{out['single_shot'][k]:>14}{out['agentic'][k]:>14}")
    Path(__file__).parent.joinpath("eval_precision_result.json").write_text(
        json.dumps({"mode": "mock" if use_mock else "real", **out}, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print("\nsaved -> scripts/eval_precision_result.json")


if __name__ == "__main__":
    asyncio.run(main())
