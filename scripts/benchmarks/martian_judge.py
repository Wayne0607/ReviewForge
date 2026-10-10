"""Judge ReviewForge and Qodo against Martian Code Review Bench goldens."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from benchmark_support import require_complete_results, validate_resume_metadata
from dotenv import load_dotenv
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage
from openai import AsyncOpenAI

from reviewforge.core.config import ReviewForgeConfig
from reviewforge.core.json_output import extract_json_value
from reviewforge.core.llm_settings import EncryptedLLMSettingsStore, apply_override

_MIN_MATCH_CONFIDENCE = 0.7


JUDGE_PROMPT = """You are evaluating AI code review tools.
Determine if the candidate issue matches the golden (expected) comment.

Golden Comment (the issue we're looking for):
{golden_comment}

Candidate Issue (from the tool's review):
{candidate}

Instructions:
- Determine if the candidate identifies the SAME underlying issue as the golden comment
- Accept semantic matches - different wording is fine if it's the same problem
- Focus on whether they point to the same bug, concern, or code issue

Respond with ONLY a JSON object:
{{"reasoning": "brief explanation", "match": true/false, "confidence": 0.0-1.0}}"""

BATCH_JUDGE_PROMPT = """You are evaluating an AI code review tool.

Golden comments (expected issues):
{goldens}

Candidate issues (from the tool's review):
{candidates}

For every candidate, determine whether it identifies the SAME underlying issue as
one of the golden comments. Accept semantic matches and different wording, but do
not match comments that merely concern the same file or broad topic. A candidate
may match more than one golden only when it genuinely covers both underlying issues.

Return ONLY a JSON object with a `matches` array. Omit non-matches. Indices are
0-based. Each item must contain golden_index, candidate_index, reasoning, and
confidence (0.0-1.0):
{{"matches": [{{"golden_index": 0, "candidate_index": 0, "reasoning": "...", "confidence": 0.95}}]}}"""


def _atomic_json(path: Path, payload: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _strip_fence(content: str) -> str:
    content = content.strip()
    if content.startswith("```"):
        content = content.split("```", 2)[1]
        if content.startswith("json"):
            content = content[4:]
    return content.strip()


class Judge:
    def __init__(self, concurrency: int, *, thinking: str = "default", min_interval: float = 30.0) -> None:
        repo_root = Path(os.environ.get("REVIEWFORGE_REPO_ROOT", "/opt/reviewforge"))
        load_dotenv(os.environ.get("REVIEWFORGE_ENV_FILE", repo_root / ".env"))
        config = ReviewForgeConfig.load(repo_root / "reviewforge.yaml")
        runtime_dir = Path(os.environ.get("REVIEWFORGE_SETTINGS_DIR", str(Path(config.events_dir).parent)))
        llm = apply_override(
            config.llm,
            EncryptedLLMSettingsStore(runtime_dir).load(),
        )
        api_key = llm.api_key
        base_url = llm.base_url
        self.model = llm.model
        self.thinking = thinking
        self.min_interval = min_interval
        self.is_minimax = bool(base_url and "minimax" in base_url.lower() and self.model.lower().startswith("minimax-"))
        if self.is_minimax:
            anthropic_url = base_url.split("/v1", 1)[0].rstrip("/") + "/anthropic"
            self.client = ChatAnthropic(
                anthropic_api_key=api_key,
                base_url=anthropic_url,
                model=self.model,
                temperature=0.0,
            )
        else:
            self.client = AsyncOpenAI(api_key=api_key, base_url=base_url, max_retries=0)
        self.semaphore = asyncio.Semaphore(concurrency)
        self.input_tokens = 0
        self.output_tokens = 0

    async def close(self) -> None:
        close = getattr(self.client, "close", None)
        if close is not None:
            result = close()
            if asyncio.iscoroutine(result):
                await result

    async def _complete(self, prompt: str, timeout: int) -> object:
        if self.min_interval > 0:
            from martian_runner import _wait_for_llm_slot

            await asyncio.to_thread(_wait_for_llm_slot, self.min_interval)
        system = "You are a precise code review evaluator. Always respond with valid JSON."
        if self.is_minimax:
            response = await asyncio.wait_for(
                self.client.ainvoke([SystemMessage(content=system), HumanMessage(content=prompt)]),
                timeout=timeout,
            )
            usage = response.usage_metadata or {}
            self.input_tokens += int(usage.get("input_tokens", 0))
            self.output_tokens += int(usage.get("output_tokens", 0))
            return response.content
        response = await asyncio.wait_for(
            self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.0,
                **({"extra_body": {"thinking": {"type": self.thinking}}} if self.thinking != "default" else {}),
            ),
            timeout=timeout,
        )
        usage = response.usage
        if usage:
            self.input_tokens += int(usage.prompt_tokens or 0)
            self.output_tokens += int(usage.completion_tokens or 0)
        return response.choices[0].message.content or ""

    async def match(self, golden: str, candidate: str) -> dict[str, Any]:
        prompt = JUDGE_PROMPT.format(golden_comment=golden, candidate=candidate)
        for attempt in range(3):
            try:
                async with self.semaphore:
                    content = await self._complete(prompt, 120)
                result = extract_json_value(content, required_key="match")
                if not isinstance(result, dict):
                    raise ValueError("response has no match field")
                return result
            except Exception as exc:
                if attempt == 2:
                    return {"error": f"{type(exc).__name__}: {exc}"}
                await asyncio.sleep(2**attempt)
        return {"error": "retries exhausted"}

    async def match_all(self, goldens: list[str], candidates: list[str]) -> dict[str, Any]:
        prompt = BATCH_JUDGE_PROMPT.format(
            goldens="\n".join(f"[golden_index={index}] {text}" for index, text in enumerate(goldens)),
            candidates="\n".join(f"[candidate_index={index}] {text}" for index, text in enumerate(candidates)),
        )
        for attempt in range(3):
            try:
                async with self.semaphore:
                    content = await self._complete(prompt, 180)
                result = extract_json_value(content, required_key="matches")
                if not isinstance(result, dict):
                    raise ValueError("response is not a JSON object")
                if not isinstance(result.get("matches"), list):
                    raise ValueError("response has no matches array")
                for item in result["matches"]:
                    golden_index = int(item["golden_index"])
                    candidate_index = int(item["candidate_index"])
                    if not 0 <= golden_index < len(goldens):
                        raise ValueError("golden_index is out of range")
                    if not 0 <= candidate_index < len(candidates):
                        raise ValueError("candidate_index is out of range")
                return result
            except Exception as exc:
                if attempt == 2:
                    return {"error": f"{type(exc).__name__}: {exc}"}
                await asyncio.sleep(2**attempt)
        return {"error": "retries exhausted"}


async def evaluate(
    judge: Judge,
    goldens: list[dict[str, Any]],
    candidates: list[str],
) -> dict[str, Any]:
    # Candidate identity is positional.  Do not deduplicate by text: publishing
    # the same finding twice must leave one duplicate available to count as FP.
    candidates = [text.strip() for text in candidates if text.strip()]
    if not candidates:
        return {
            "tp": 0,
            "fp": 0,
            "fn": len(goldens),
            "total_candidates": 0,
            "total_golden": len(goldens),
            "true_positives": [],
            "false_positives": [],
            "false_negatives": goldens,
            "errors": [],
        }

    comparison = await judge.match_all(
        [str(golden["comment"]) for golden in goldens],
        candidates,
    )
    errors: list[dict[str, str]] = []
    if comparison.get("error"):
        errors.append({"golden": "*", "candidate": "*", "error": str(comparison["error"])})
    # Keep only the strongest report for each exact edge.  The judge can emit
    # duplicate pairs, and those must not affect the graph matching below.
    eligible_pairs: dict[tuple[int, int], dict[str, Any]] = {}
    for result in comparison.get("matches", []):
        try:
            golden_index = int(result["golden_index"])
            candidate_index = int(result["candidate_index"])
            golden = goldens[golden_index]
            candidate = candidates[candidate_index]
            confidence = float(result.get("confidence", 0))
        except (KeyError, TypeError, ValueError, IndexError):
            errors.append({"golden": "*", "candidate": "*", "error": "invalid match indices"})
            continue
        # MiniMax occasionally emits a candidate pair in ``matches`` while its
        # own reasoning says the issues differ and assigns very low confidence.
        # Strict evaluation must not turn that self-rejected pair into a TP.
        if confidence < _MIN_MATCH_CONFIDENCE:
            continue
        pair = (golden_index, candidate_index)
        previous = eligible_pairs.get(pair)
        if previous is None or confidence > float(previous["confidence"]):
            eligible_pairs[pair] = {
                "golden_index": golden_index,
                "candidate_index": candidate_index,
                "golden_comment": golden["comment"],
                "severity": golden.get("severity"),
                "matched_candidate": candidate,
                "confidence": confidence,
                "reasoning": result.get("reasoning", ""),
            }

    # Compute a deterministic maximum-cardinality bipartite matching.  Each
    # golden and each candidate index can therefore earn at most one TP.  Edges
    # are tried by confidence first so equally-sized matchings prefer stronger
    # judge decisions, while the augmenting-path step avoids greedy undercount.
    adjacency: dict[int, list[int]] = {}
    for golden_index, candidate_index in eligible_pairs:
        adjacency.setdefault(golden_index, []).append(candidate_index)
    for golden_index, candidate_indices in adjacency.items():
        candidate_indices.sort(
            key=lambda candidate_index: (
                -float(eligible_pairs[(golden_index, candidate_index)]["confidence"]),
                candidate_index,
            )
        )

    candidate_to_golden: dict[int, int] = {}

    def augment(golden_index: int, seen_candidates: set[int]) -> bool:
        for candidate_index in adjacency.get(golden_index, []):
            if candidate_index in seen_candidates:
                continue
            seen_candidates.add(candidate_index)
            current_golden = candidate_to_golden.get(candidate_index)
            if current_golden is None or augment(current_golden, seen_candidates):
                candidate_to_golden[candidate_index] = golden_index
                return True
        return False

    golden_order = sorted(
        adjacency,
        key=lambda golden_index: (
            -max(
                float(eligible_pairs[(golden_index, candidate_index)]["confidence"])
                for candidate_index in adjacency[golden_index]
            ),
            golden_index,
        ),
    )
    for golden_index in golden_order:
        augment(golden_index, set())

    matched_goldens = {
        golden_index: eligible_pairs[(golden_index, candidate_index)]
        for candidate_index, golden_index in candidate_to_golden.items()
    }
    matched_candidate_indices = set(candidate_to_golden)

    false_negatives = [golden for index, golden in enumerate(goldens) if index not in matched_goldens]
    false_positives = [
        candidate
        for candidate_index, candidate in enumerate(candidates)
        if candidate_index not in matched_candidate_indices
    ]
    return {
        "tp": len(matched_goldens),
        "fp": len(false_positives),
        "fn": len(false_negatives),
        "total_candidates": len(candidates),
        "total_golden": len(goldens),
        "true_positives": list(matched_goldens.values()),
        "false_positives": false_positives,
        "false_negatives": false_negatives,
        "errors": errors,
    }


def _metrics(completed: dict[str, Any], tool: str) -> dict[str, Any]:
    rows = [tools[tool] for tools in completed.values() if tool in tools and not tools[tool]["errors"]]
    tp = sum(row["tp"] for row in rows)
    fp = sum(row["fp"] for row in rows)
    fn = sum(row["fn"] for row in rows)
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "reviews": len(rows),
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "precision": precision,
        "recall": recall,
        "f1": f1,
    }


async def main_async(args: argparse.Namespace) -> None:
    workload = json.loads(Path(args.workload).read_text(encoding="utf-8"))
    selected = workload[: args.limit or None]
    reviewforge = require_complete_results(
        json.loads(Path(args.reviewforge_results).read_text(encoding="utf-8")), selected
    )
    qodo = json.loads(Path(args.qodo_candidates).read_text(encoding="utf-8"))
    output = Path(args.output)
    state = json.loads(output.read_text(encoding="utf-8")) if output.exists() else {"completed": {}}
    completed = state.setdefault("completed", {})
    judge = Judge(args.concurrency, thinking=args.thinking, min_interval=args.llm_min_interval)
    parameters = {
        "model": judge.model,
        "thinking": args.thinking,
        "llm_min_interval": args.llm_min_interval,
        "sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "support_sha256": hashlib.sha256(Path(__file__).with_name("benchmark_support.py").read_bytes()).hexdigest(),
        "workload_sha256": hashlib.sha256(Path(args.workload).read_bytes()).hexdigest(),
        "results_sha256": hashlib.sha256(Path(args.reviewforge_results).read_bytes()).hexdigest(),
        "qodo_sha256": hashlib.sha256(Path(args.qodo_candidates).read_bytes()).hexdigest(),
        "limit": args.limit,
    }
    try:
        validate_resume_metadata(state.get("judge_parameters"), parameters, has_results=bool(completed))
        state["judge_parameters"] = parameters
        batch_size = max(1, min(args.concurrency // 2, 8))
        for start in range(0, len(selected), batch_size):
            jobs = []
            metadata = []
            for offset, item in enumerate(selected[start : start + batch_size], start):
                index = offset + 1
                url = item["golden_url"]
                if url not in reviewforge:
                    continue
                tools = completed.setdefault(url, {})
                sources = {
                    "reviewforge": [comment["body"] for comment in reviewforge[url]["review_comments"]],
                    "qodo-v2": [candidate["text"] for candidate in qodo.get(url, {}).get("qodo-v2", [])],
                }
                pending = [
                    (tool, candidates)
                    for tool, candidates in sources.items()
                    if tool not in tools or tools[tool].get("errors")
                ]
                for tool, candidates in pending:
                    print(
                        f"JUDGE {index}/{len(selected)} {tool} "
                        f"gold={len(item['golden_comments'])} "
                        f"candidates={len(candidates)}",
                        flush=True,
                    )
                    jobs.append(evaluate(judge, item["golden_comments"], candidates))
                    metadata.append((url, tool))
            evaluated = await asyncio.gather(*jobs)
            for (url, tool), result in zip(metadata, evaluated, strict=True):
                completed[url][tool] = result
            state["metrics"] = {name: _metrics(completed, name) for name in ("reviewforge", "qodo-v2")}
            state["judge_tokens"] = {
                "input": judge.input_tokens,
                "output": judge.output_tokens,
                "total": judge.input_tokens + judge.output_tokens,
            }
            _atomic_json(output, state)
            print(json.dumps(state["metrics"], ensure_ascii=False), flush=True)
    finally:
        await judge.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workload", required=True)
    parser.add_argument("--reviewforge-results", required=True)
    parser.add_argument("--qodo-candidates", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--concurrency", type=int, default=20)
    parser.add_argument("--thinking", choices=("default", "enabled", "disabled"), default="default")
    parser.add_argument("--llm-min-interval", type=float, default=30.0)
    args = parser.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
