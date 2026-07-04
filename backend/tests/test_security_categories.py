from reviewforge.core.specs import build_registry
from reviewforge.engine.mock_llm import MockChatLLM
from reviewforge.engine.reviewers import StyleReviewer
from reviewforge.engine.security_categories import is_security_category, normalize_category
from reviewforge.tools.gateway import ToolGateway
from reviewforge.tools.mock_github import MockGitHubClient


def test_security_category_aliases_normalize():
    assert normalize_category("hardcoded-secret") == "hardcoded-secrets"
    assert normalize_category("remote code execution") == "rce"
    assert normalize_category("client_side_code_execution") == "code-injection"
    assert is_security_category("unsafe-code")


def test_non_security_reviewer_filters_security_aliases():
    registry = build_registry()
    reviewer = StyleReviewer(MockChatLLM(), registry, ToolGateway(registry, MockGitHubClient()))

    findings = reviewer._parse_findings(
        """
        {
          "findings": [
            {
              "file": "app.py",
              "line": 3,
              "severity": "error",
              "category": "hardcoded-secret",
              "message": "secret",
              "suggestion": "move to env",
              "confidence": 0.9
            },
            {
              "file": "app.py",
              "line": 9,
              "severity": "info",
              "category": "dead-code",
              "message": "unused branch",
              "suggestion": "remove it",
              "confidence": 0.8
            }
          ]
        }
        """
    )

    assert [f.category for f in findings] == ["dead-code"]
