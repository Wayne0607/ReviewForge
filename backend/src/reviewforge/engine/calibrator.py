"""Dynamic Loop Calibrator — multi-round confidence calibration.

Round 1: Reviewer outputs findings (already done by reviewers.py)
Round 2: Adversarial Verifier tries to refute each finding
Round 3 (conditional): Judge rules on disputed findings

Stops early when consensus is reached. Max 3 rounds.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from reviewforge.core.specs import SpecRegistry
from reviewforge.core.state import Finding

logger = logging.getLogger(__name__)


@dataclass
class ChallengeResult:
    """Result of adversarial verification for one finding."""

    finding_id: str
    verdict: str  # confirmed / false_positive
    adjusted_confidence: float
    challenge: str  # reason for the verdict


class DynamicCalibrator:
    """Multi-round confidence calibration with early stopping.

    Rounds:
    1. Reviewer (already done, input is existing findings)
    2. Adversarial Verifier tries to refute each finding
    3. Judge rules on disputed findings (only if Round 2 disagrees with Round 1)

    Stops early when no disputes remain or max_rounds reached.
    """

    def __init__(
        self,
        llm: ChatOpenAI,
        registry: SpecRegistry,
        max_rounds: int = 3,
        consensus_threshold: float = 0.2,
    ) -> None:
        self._llm = llm
        self._registry = registry
        self._max_rounds = max_rounds
        self._consensus_threshold = consensus_threshold

    async def calibrate(self, findings: list[Finding], code_diff: str) -> list[Finding]:
        """Run dynamic calibration loop. Returns calibrated findings."""
        if not findings:
            return []

        current_findings = findings
        rounds = 1

        while rounds < self._max_rounds:
            # Round 2+: Adversarial verification
            logger.info(f"Calibration round {rounds + 1}: adversarial verify ({len(current_findings)} findings)")
            challenged = await self._adversarial_round(current_findings, code_diff)
            rounds += 1

            # Apply adversarial results
            updated = self._apply_challenges(current_findings, challenged)

            # Find disputes
            disputed = self._find_disputed(current_findings, updated)

            if not disputed:
                logger.info(f"Consensus reached after round {rounds}, stopping")
                return updated

            # Round 3 (if needed): Judge disputed findings
            if rounds < self._max_rounds:
                logger.info(f"Round {rounds + 1}: judge {len(disputed)} disputed findings")
                judged = await self._judge_round(disputed, code_diff)
                rounds += 1

                # Merge judged results back
                judged_map = {f.id: f for f in judged}
                for i, f in enumerate(updated):
                    if f.id in judged_map:
                        updated[i] = judged_map[f.id]

                return updated

        return updated

    async def _adversarial_round(
        self, findings: list[Finding], code_diff: str
    ) -> list[ChallengeResult]:
        """Attempt to refute each finding. Returns challenge results."""
        findings_text = "\n".join(
            f"- [{f.id}] {f.file}:{f.line} ({f.severity}) "
            f"category={f.category} confidence={f.confidence:.2f}\n"
            f"  message: {f.message}\n"
            f"  suggestion: {f.suggestion}"
            for f in findings
        )

        system = """你是 ReviewForge 的对抗性验证器。

你的任务是尝试推翻以下每个代码审查发现。
默认立场：这些发现是错误的，除非你能证明它们是对的。

对每个 finding：
- 如果你能找到反驳理由（比如：代码实际上没有这个问题、上下文说明这不是问题、项目惯例允许这种写法），标记为 false_positive 并降低置信度
- 如果你无法找到反驳理由，标记为 confirmed 并保持或提高置信度
- 如果你认为问题存在但严重程度被高估，降低置信度但保持 confirmed

语言要求：challenge 字段使用中文。"""

        user = f"""## 代码 Diff

```
{code_diff}
```

## 待验证的发现

{findings_text}

## 输出格式

对每个 finding 输出 JSON 数组：
```json
[
  {{
    "finding_id": "finding_xxxx",
    "verdict": "confirmed 或 false_positive",
    "adjusted_confidence": 0.0-1.0,
    "challenge": "你的反驳理由或无法推翻的原因（中文）"
  }}
]
```"""

        response = await self._llm.ainvoke([
            SystemMessage(content=system),
            HumanMessage(content=user),
        ])

        return self._parse_challenges(response.content, findings)

    async def _judge_round(
        self, disputed: list[Finding], code_diff: str
    ) -> list[Finding]:
        """Final judgment on disputed findings."""
        disputed_text = "\n".join(
            f"- [{f.id}] {f.file}:{f.line} ({f.severity}) "
            f"category={f.category} confidence={f.confidence:.2f}\n"
            f"  message: {f.message}"
            for f in disputed
        )

        system = """你是 ReviewForge 的最终裁决者。

以下是有争议的代码审查发现。你需要做出最终裁决。

裁决标准：
- 问题是否真实存在于代码中
- 问题是否可操作（开发者能据此修复）
- 严重程度是否合理

语言要求：reason 字段使用中文。"""

        user = f"""## 代码 Diff

```
{code_diff}
```

## 有争议的发现

{disputed_text}

## 输出格式

```json
[
  {{
    "finding_id": "finding_xxxx",
    "verdict": "confirmed 或 false_positive",
    "confidence": 0.0-1.0,
    "reason": "最终裁决理由（中文）"
  }}
]
```"""

        response = await self._llm.ainvoke([
            SystemMessage(content=system),
            HumanMessage(content=user),
        ])

        return self._parse_judgment(response.content, disputed)

    def _parse_challenges(
        self, content: str, findings: list[Finding]
    ) -> list[ChallengeResult]:
        """Parse adversarial verifier output."""
        content = self._strip_code_fences(content)
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            logger.warning("Adversarial verifier returned invalid JSON, keeping original")
            return [
                ChallengeResult(
                    finding_id=f.id,
                    verdict="confirmed",
                    adjusted_confidence=f.confidence,
                    challenge="验证器输出无效，保留原始判断",
                )
                for f in findings
            ]

        results = []
        for item in data:
            results.append(ChallengeResult(
                finding_id=item.get("finding_id", ""),
                verdict=item.get("verdict", "confirmed"),
                adjusted_confidence=item.get("adjusted_confidence", 0.5),
                challenge=item.get("challenge", ""),
            ))
        return results

    def _parse_judgment(self, content: str, findings: list[Finding]) -> list[Finding]:
        """Parse judge output and update findings."""
        content = self._strip_code_fences(content)
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            logger.warning("Judge returned invalid JSON, keeping findings as-is")
            return findings

        judged_map = {item.get("finding_id"): item for item in data}
        updated = []
        for f in findings:
            judgment = judged_map.get(f.id)
            if judgment:
                f.status = judgment.get("verdict", f.status)
                f.confidence = judgment.get("confidence", f.confidence)
                f.verify_reason = judgment.get("reason", "")
                f.verified_by = "judge"
            updated.append(f)
        return updated

    def _apply_challenges(
        self, findings: list[Finding], challenges: list[ChallengeResult]
    ) -> list[Finding]:
        """Apply adversarial challenge results to findings."""
        challenge_map = {c.finding_id: c for c in challenges}
        updated = []
        for f in findings:
            challenge = challenge_map.get(f.id)
            if challenge:
                old_confidence = f.confidence
                f.confidence = challenge.adjusted_confidence
                f.status = challenge.verdict
                f.verify_reason = challenge.challenge
                f.verified_by = "adversarial"
                logger.debug(
                    f"Finding {f.id}: {old_confidence:.2f} -> {f.confidence:.2f} ({challenge.verdict})"
                )
            updated.append(f)
        return updated

    def _find_disputed(
        self, original: list[Finding], updated: list[Finding]
    ) -> list[Finding]:
        """Find findings where Round 1 and Round 2 disagree."""
        orig_map = {f.id: f for f in original}
        disputed = []
        for f in updated:
            orig = orig_map.get(f.id)
            if not orig:
                continue
            # Confidence changed significantly
            if abs(orig.confidence - f.confidence) > self._consensus_threshold:
                disputed.append(f)
            # Verdict changed
            elif orig.status != f.status:
                disputed.append(f)
        return disputed

    @staticmethod
    def _strip_code_fences(content: str) -> str:
        """Strip markdown code fences from LLM output."""
        content = content.strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[1] if "\n" in content else content[3:]
        if content.endswith("```"):
            content = content[:-3]
        return content.strip()
