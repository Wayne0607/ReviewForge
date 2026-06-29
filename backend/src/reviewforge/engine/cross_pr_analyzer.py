"""Cross-PR Analyzer — detects security issues spanning multiple PRs.

Two-stage approach:
  Stage 1 (zero tokens): Extract symbols via regex, query code graph for risks
  Stage 2 (LLM): For suspicious call chains, build context and ask LLM to confirm
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from reviewforge.core.database import Database
from reviewforge.core.state import Finding, StateStore
from reviewforge.engine.symbol_extractor import (
    ImportInfo,
    SymbolInfo,
    detect_language,
    extract_calls,
    extract_definitions,
    extract_diff_symbols,
    extract_imports,
)

logger = logging.getLogger(__name__)

# Security categories that propagate across PRs
SECURITY_CATEGORIES = {
    "sql-injection", "xss", "csrf", "command-injection", "path-traversal",
    "hardcoded-secrets", "insecure-deserialization", "unsafe-deserialization",
    "security", "authentication", "authorization", "crypto", "ssrf", "xxe",
    "rce", "config-injection", "code-injection",
}

# Max propagation depth by risk level
MAX_DEPTH = {
    "critical": 3,
    "high": 2,
    "medium": 1,
    "low": 0,
}


@dataclass
class CrossPRChain:
    """A cross-PR call chain."""
    source_file: str
    source_symbol: str
    target_file: str
    target_symbol: str
    risk_category: str
    risk_level: str
    depth: int
    path: list[dict[str, str]]  # Full chain path


class CrossPRAnalyzer:
    """Detects security issues that span multiple PRs."""

    def __init__(
        self,
        db: Database,
        llm: ChatOpenAI | None = None,
        github_client: Any = None,
    ) -> None:
        self._db = db
        self._llm = llm
        self._github = github_client

    async def analyze(
        self,
        run_id: str,
        state: StateStore,
        existing_findings: list[Finding],
    ) -> list[Finding]:
        """Run cross-PR analysis on the current PR.

        Returns a list of cross-PR findings.
        """
        repo = state.repo
        pr_number = state.pr_number
        diff_summary = state.diff_summary

        # Step 1: Extract symbols from diff (zero tokens)
        all_symbols: list[SymbolInfo] = []
        all_imports: list[ImportInfo] = []

        for file_path in state.files_changed:
            lang = detect_language(file_path)
            # Extract from the diff portion
            file_diff = self._extract_file_diff(diff_summary, file_path)
            if file_diff:
                symbols, imports = extract_diff_symbols(file_diff, file_path)
                all_symbols.extend(symbols)
                all_imports.extend(imports)

        logger.info(f"Cross-PR: extracted {len(all_symbols)} symbols, {len(all_imports)} imports")

        # Step 2: Store symbols and relations in graph
        for sym in all_symbols:
            await self._db.upsert_symbol(
                file_path=sym.file_path,
                symbol_name=sym.name,
                symbol_type=sym.symbol_type,
                run_id=run_id,
                pr_number=pr_number,
                language=detect_language(sym.file_path),
            )

        for imp in all_imports:
            target_file = self._resolve_import_to_file(imp.source, state.files_changed)
            await self._db.upsert_relation(
                run_id=run_id,
                source_file=imp.file_path,
                target_file=target_file or imp.source,
                target_symbol=imp.name,
                relation_type="import",
            )

        # Step 3: Mark symbols with risk from current findings
        await self._mark_symbol_risks(existing_findings, run_id, pr_number)

        # Step 4: Find suspicious cross-PR connections (zero tokens)
        suspicious_chains = await self._find_suspicious_chains(all_imports, state.files_changed)

        if not suspicious_chains:
            logger.info("Cross-PR: no suspicious chains found")
            return []

        logger.info(f"Cross-PR: found {len(suspicious_chains)} suspicious chains")

        # Step 5: LLM confirmation for suspicious chains
        cross_findings = []
        if self._llm and suspicious_chains:
            cross_findings = await self._llm_confirm_chains(
                suspicious_chains, diff_summary, state,
            )

        return cross_findings

    def _extract_file_diff(self, diff_summary: str, file_path: str) -> str:
        """Extract the diff portion for a specific file.

        The diff_summary format from the webhook is:
            --- filename.py (+10 -5)
            <patch content>
        """
        lines = diff_summary.split("\n")
        result = []
        in_target = False

        for line in lines:
            if line.startswith("--- "):
                # Check if this is the target file
                header_file = line.split(" ")[1] if len(line.split(" ")) > 1 else ""
                in_target = file_path.endswith(header_file) or header_file.endswith(file_path)
                if in_target:
                    result.append(line)
            elif in_target:
                # Stop at next file header
                if line.startswith("--- ") and not in_target:
                    break
                result.append(line)

        return "\n".join(result)

    def _resolve_import_to_file(self, import_source: str, known_files: list[str]) -> str | None:
        """Resolve an import path to a file path in the repo."""
        # Convert dots to slashes and try matching
        as_path = import_source.replace(".", "/")

        for f in known_files:
            if as_path in f or f.endswith(f"{as_path}.py") or f.endswith(f"{as_path}.ts"):
                return f

        return None

    async def _mark_symbol_risks(
        self, findings: list[Finding], run_id: str, pr_number: int,
    ) -> None:
        """Link current findings to their corresponding symbols."""
        for finding in findings:
            if finding.category not in SECURITY_CATEGORIES:
                continue

            # Find symbols in the same file
            lang = detect_language(finding.file)
            # We need the full file content to extract definitions,
            # but we can infer from the finding's context
            risk_level = self._category_to_risk(finding.category)

            # Update file risk summary
            existing = await self._db.get_file_risk(finding.file)
            if existing:
                cats = json.loads(existing.get("risk_categories", "[]"))
                if finding.category not in cats:
                    cats.append(finding.category)
                max_risk = self._higher_risk(existing.get("max_risk", "safe"), risk_level)
                await self._db.upsert_file_risk(
                    finding.file, max_risk, cats,
                    existing.get("findings_count", 0) + 1, run_id,
                )
            else:
                await self._db.upsert_file_risk(
                    finding.file, risk_level, [finding.category], 1, run_id,
                )

    async def _find_suspicious_chains(
        self, imports: list[ImportInfo], known_files: list[str],
    ) -> list[CrossPRChain]:
        """Find import chains that connect to historically risky code."""
        chains = []

        for imp in imports:
            # Check if the import target has known risks
            target_file = self._resolve_import_to_file(imp.source, known_files)
            if not target_file:
                # Try fuzzy match in DB
                risky_files = await self._db.find_risky_files_for_import(imp.source)
                if not risky_files:
                    continue
                target_file = risky_files[0]["file_path"]

            file_risk = await self._db.get_file_risk(target_file)
            if not file_risk or file_risk.get("max_risk", "safe") == "safe":
                continue

            # Found a risky target! Build the chain
            risk_categories = json.loads(file_risk.get("risk_categories", "[]"))
            risk_level = file_risk.get("max_risk", "medium")

            # Get risky symbols in the target file
            risky_symbols = await self._db.get_risky_symbols(target_file)

            for sym in risky_symbols:
                sym_categories = json.loads(sym.get("risk_categories", "[]"))
                for cat in sym_categories:
                    if cat not in SECURITY_CATEGORIES:
                        continue

                    chain = CrossPRChain(
                        source_file=imp.file_path,
                        source_symbol=imp.name or "<import>",
                        target_file=target_file,
                        target_symbol=sym["symbol_name"],
                        risk_category=cat,
                        risk_level=sym.get("risk_level", risk_level),
                        depth=1,
                        path=[
                            {"file": imp.file_path, "symbol": imp.name or "<import>"},
                            {"file": target_file, "symbol": sym["symbol_name"], "risk": cat},
                        ],
                    )
                    chains.append(chain)

            # Depth 2: check what the risky file imports
            if risk_level in ("critical", "high"):
                target_imports = await self._db.get_relations_from(target_file)
                for ti in target_imports:
                    sub_target = ti.get("target_file", "")
                    sub_risk = await self._db.get_file_risk(sub_target)
                    if sub_risk and sub_risk.get("max_risk", "safe") != "safe":
                        sub_cats = json.loads(sub_risk.get("risk_categories", "[]"))
                        for cat in sub_cats:
                            if cat not in SECURITY_CATEGORIES:
                                continue
                            chain = CrossPRChain(
                                source_file=imp.file_path,
                                source_symbol=imp.name or "<import>",
                                target_file=sub_target,
                                target_symbol=ti.get("target_symbol", ""),
                                risk_category=cat,
                                risk_level=sub_risk.get("max_risk", "medium"),
                                depth=2,
                                path=[
                                    {"file": imp.file_path, "symbol": imp.name or "<import>"},
                                    {"file": target_file, "symbol": ti.get("target_symbol", "")},
                                    {"file": sub_target, "symbol": ti.get("target_symbol", ""), "risk": cat},
                                ],
                            )
                            chains.append(chain)

        # Deduplicate
        seen = set()
        unique = []
        for c in chains:
            key = (c.source_file, c.target_file, c.risk_category, c.depth)
            if key not in seen:
                seen.add(key)
                unique.append(c)

        return unique

    async def _llm_confirm_chains(
        self,
        chains: list[CrossPRChain],
        diff_summary: str,
        state: StateStore,
    ) -> list[Finding]:
        """Use LLM to confirm whether suspicious chains are actually exploitable."""
        # Build context for LLM
        chain_descriptions = []
        for i, chain in enumerate(chains[:5]):  # Max 5 chains per analysis
            path_str = " → ".join(
                f"{p['file']}:{p['symbol']}" + (f" [{p.get('risk', '')}]" if p.get('risk') else "")
                for p in chain.path
            )
            chain_descriptions.append(f"Chain {i+1}: {path_str}")

        chains_text = "\n".join(chain_descriptions)

        # Get relevant source code for context
        context_parts = []
        for chain in chains[:3]:
            for step in chain.path:
                if self._github and state.repo:
                    try:
                        content = await self._github.get_file_content(
                            state.repo, state.head_sha, step["file"],
                        )
                        # Extract just the relevant function
                        relevant = self._extract_function(content, step["symbol"])
                        if relevant:
                            context_parts.append(f"### {step['file']}:{step['symbol']}\n```\n{relevant}\n```")
                    except Exception:
                        pass

        context_text = "\n\n".join(context_parts[:5])  # Limit context size

        system = """你是 ReviewForge 的跨 PR 安全分析器。

你的任务是判断以下跨 PR 调用链是否真的存在安全风险。

关键判断点：
1. 调用链中是否有安全防护（输入验证、白名单、类型检查）？
2. 危险操作的数据来源是否可控（用户输入 vs 内部数据）？
3. 是否有安全的替代实现？

对每条链，输出：
- exploitable: true/false（是否真的可利用）
- confidence: 0.0-1.0
- reason: 判断理由（中文）

语言要求：reason 字段使用中文。"""

        user = f"""## 跨 PR 调用链

{chains_text}

## 相关代码上下文

{context_text if context_text else "（无法获取代码上下文，请基于调用链本身判断）"}

## 当前 PR Diff（摘要）

{diff_summary[:2000]}

## 输出格式

```json
[
  {{"chain_id": 1, "exploitable": true, "confidence": 0.9, "reason": "..."}},
  ...
]
```"""

        response = await self._llm.ainvoke([
            SystemMessage(content=system),
            HumanMessage(content=user),
        ])

        return self._parse_confirmation(response.content, chains)

    def _parse_confirmation(self, content: str, chains: list[CrossPRChain]) -> list[Finding]:
        """Parse LLM confirmation output into findings."""
        # Strip code fences
        content = content.strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[1] if "\n" in content else content[3:]
        if content.endswith("```"):
            content = content[:-3]
        content = content.strip()

        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            # Try to find JSON array
            import re
            match = re.search(r'\[.*\]', content, re.DOTALL)
            if match:
                try:
                    data = json.loads(match.group())
                except json.JSONDecodeError:
                    logger.warning("Cross-PR: LLM returned invalid JSON")
                    return []
            else:
                logger.warning("Cross-PR: LLM returned invalid JSON")
                return []

        findings = []
        for item in data:
            chain_id = item.get("chain_id", 0) - 1
            exploitable = item.get("exploitable", False)
            confidence = item.get("confidence", 0.5)
            reason = item.get("reason", "")

            if not exploitable or chain_id < 0 or chain_id >= len(chains):
                continue

            chain = chains[chain_id]
            chain_path = " → ".join(f"{p['file']}:{p['symbol']}" for p in chain.path)

            findings.append(Finding(
                file=chain.source_file,
                line=0,
                severity="error",
                category=f"cross-pr-{chain.risk_category}",
                message=f"[跨 PR] {chain.source_symbol}() 调用了 {chain.target_symbol}()，"
                        f"存在 {chain.risk_category} 风险。\n"
                        f"调用链: {chain_path}",
                suggestion=f"检查 {chain.target_symbol}() 的安全性，确保输入经过验证。",
                confidence=confidence,
                reviewer="cross_pr_analyzer",
                status="confirmed",
                verified_by="cross-pr-analysis",
                verify_reason=reason,
            ))

        return findings

    def _extract_function(self, content: str, func_name: str) -> str:
        """Extract a specific function definition from file content."""
        if not func_name or func_name == "<import>":
            return ""

        lines = content.split("\n")
        result = []
        in_target = False
        target_indent = None

        for line in lines:
            if re.search(rf"(?:async\s+)?def\s+{re.escape(func_name)}\s*\(", line):
                in_target = True
                target_indent = len(line) - len(line.lstrip())
                result.append(line)
                continue

            if in_target:
                if line.strip() == "":
                    result.append(line)
                    continue
                current_indent = len(line) - len(line.lstrip())
                if current_indent <= target_indent and line.strip():
                    break
                result.append(line)

        return "\n".join(result[:30])  # Limit to 30 lines

    @staticmethod
    def _category_to_risk(category: str) -> str:
        critical = {"rce", "sql-injection", "command-injection", "insecure-deserialization"}
        high = {"xss", "csrf", "path-traversal", "unsafe-deserialization", "ssrf", "xxe", "code-injection"}
        medium = {"hardcoded-secrets", "authentication", "authorization", "crypto", "config-injection"}
        if category in critical:
            return "critical"
        if category in high:
            return "high"
        if category in medium:
            return "medium"
        return "low"

    @staticmethod
    def _higher_risk(a: str, b: str) -> str:
        order = {"safe": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}
        return a if order.get(a, 0) >= order.get(b, 0) else b
