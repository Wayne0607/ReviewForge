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
