"""Zero-token deterministic scanning that runs independently of LLM routing.

The security and dependency scanners are intentionally executed before the
Planner. This keeps their coverage available when planning fails, returns no
tasks, or simply omits the corresponding reviewer.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from reviewforge.core.state import Finding, StateStore
from reviewforge.engine.detectors import detect_dependency_findings, detect_security_findings
from reviewforge.engine.security_categories import normalize_category
from reviewforge.tools.gateway import ToolGateway

logger = logging.getLogger(__name__)


def finding_identity(finding: Finding) -> tuple[str, int, str]:
    """Return the stable identity used to merge scanner/reviewer overlap."""

    return (finding.file, finding.line, normalize_category(finding.category))


@dataclass
class Phase0ScanResult:
    """Outcome of reading changed diffs and running both deterministic scanners."""

    findings: list[Finding] = field(default_factory=list)
    files_scanned: int = 0
    file_errors: dict[str, str] = field(default_factory=dict)
    scanner_errors: dict[str, str] = field(default_factory=dict)


async def scan_changed_files(
    gateway: ToolGateway,
    state: StateStore,
    *,
    concurrency: int = 4,
) -> Phase0ScanResult:
    """Read every changed diff and run security/dependency rules without an LLM.

    File reads and scanner families are isolated: one unavailable patch or one
    scanner failure must not suppress findings from the remaining inputs.
    """

    result = Phase0ScanResult()
    semaphore = asyncio.Semaphore(max(1, concurrency))

    async def _read(file_path: str) -> tuple[str, str | None, str | None]:
        try:
            async with semaphore:
                diff = await gateway.invoke(
                    "read_diff",
                    {"file_path": file_path},
                    state,
                    agent_name="orchestrator",
                )
            return file_path, str(diff or ""), None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Phase 0 could not read diff for %s: %s", file_path, exc)
            return file_path, None, str(exc)

    reads = await asyncio.gather(*(_read(file_path) for file_path in state.files_changed))
    diffs: dict[str, str] = {}
    for file_path, diff, error in reads:
        if error is not None:
            result.file_errors[file_path] = error
        else:
            diffs[file_path] = diff or ""
    result.files_scanned = len(diffs)

    scanner_specs = (
        ("security", "security_reviewer", detect_security_findings),
        ("dependency", "dependency_reviewer", detect_dependency_findings),
    )
    deduped: dict[tuple[str, int, str], Finding] = {}
    for scanner_name, reviewer_name, scanner in scanner_specs:
        try:
            detected = scanner(diffs)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Phase 0 %s scanner failed", scanner_name)
            result.scanner_errors[scanner_name] = str(exc)
            continue

        for item in detected:
            try:
                finding = Finding(
                    file=item.file,
                    line=max(1, item.line),
                    severity=item.severity,
                    category=normalize_category(item.category),
                    message=item.message,
                    suggestion=item.suggestion,
                    confidence=item.confidence,
                    reviewer=reviewer_name,
                    status="candidate",
                    verified_by="detector",
                )
            except Exception as exc:
                logger.warning("Phase 0 ignored invalid %s scanner finding: %s", scanner_name, exc)
                result.scanner_errors.setdefault(scanner_name, str(exc))
                continue
            key = finding_identity(finding)
            current = deduped.get(key)
            if current is None or finding.confidence > current.confidence:
                deduped[key] = finding

    result.findings = list(deduped.values())
    return result
