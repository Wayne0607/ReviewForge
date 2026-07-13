"""Test the per-reviewer finding cap that cuts low-value nitpick noise."""

import json

from reviewforge.core.specs import build_registry
from reviewforge.engine.mock_llm import MockChatLLM
from reviewforge.engine.reviewers import DocumentationReviewer, SecurityReviewer
from reviewforge.tools.gateway import ToolGateway
from reviewforge.tools.mock_github import MockGitHubClient


def _reviewer(cls):
    reg = build_registry()
    return cls(MockChatLLM(), reg, ToolGateway(reg, MockGitHubClient()))


def _many(category, n, severity="info"):
    return json.dumps(
        {
            "findings": [
                {
                    "file": "a.py",
                    "line": i,
                    "severity": severity,
                    "category": category,
                    "message": "m",
                    "confidence": 0.9,
                }
                for i in range(n)
            ]
        }
    )


def test_doc_findings_capped_low():
    rv = _reviewer(DocumentationReviewer)  # reviewer_type "documentation" → cap 4
    out = rv._parse_findings(_many("missing-docstring", 12))
    assert len(out) == 4


def test_security_findings_capped_higher():
    rv = _reviewer(SecurityReviewer)  # reviewer_type "security" → cap 15
    out = rv._parse_findings(_many("sql-injection", 12, severity="error"))
    assert len(out) == 12  # under the cap → all kept


def test_detector_findings_survive_when_llm_fills_cap():
    """Deterministic detector findings must not be capped away by verbose LLM output."""
    rv = _reviewer(SecurityReviewer)
    # LLM emits 20 high-confidence findings → capped to the security limit of 15.
    llm_findings = rv._parse_findings(_many("sql-injection", 20, severity="error"))
    assert len(llm_findings) == 15  # cap fully consumed by LLM output

    # A diff the zero-token detector flags (hardcoded secret + command injection).
    diffs = {
        "svc.py": (
            "@@ -0,0 +1,3 @@\n"
            "+API_TOKEN = 'ghp_abcdefghijklmnopqrstuvwxyz0123456789'\n"
            "+import os\n"
            "+os.system(user_input)\n"
        )
    }
    merged = rv._merge_detector_findings(llm_findings, diffs)
    detector = [f for f in merged if f.verified_by == "detector"]
    assert detector, "detector findings were dropped by the cap"
    assert "hardcoded-secrets" in {f.category for f in detector}
    # The merged set exceeds the cap — detector findings are additive, not truncated.
    assert len(merged) > 15


def test_cap_keeps_highest_severity_first():
    rv = _reviewer(DocumentationReviewer)
    payload = {
        "findings": [
            {"file": "a.py", "line": 1, "severity": "info", "category": "x", "message": "m", "confidence": 0.5},
            {"file": "a.py", "line": 2, "severity": "error", "category": "y", "message": "m", "confidence": 0.6},
            {"file": "a.py", "line": 3, "severity": "warning", "category": "z", "message": "m", "confidence": 0.9},
            {"file": "a.py", "line": 4, "severity": "info", "category": "w", "message": "m", "confidence": 0.99},
            {"file": "a.py", "line": 5, "severity": "info", "category": "v", "message": "m", "confidence": 0.4},
        ]
    }
    out = rv._parse_findings(json.dumps(payload))
    assert len(out) == 4  # doc cap
    assert out[0].severity == "error"  # highest severity first
    assert out[1].severity == "warning"
