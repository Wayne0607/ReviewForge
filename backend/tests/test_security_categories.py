from reviewforge.core.specs import build_registry
from reviewforge.engine.mock_llm import MockChatLLM
from reviewforge.engine.reviewers import DependencyReviewer, StyleReviewer
from reviewforge.engine.security_categories import is_security_category, normalize_category
from reviewforge.tools.gateway import ToolGateway
from reviewforge.tools.mock_github import MockGitHubClient


def test_security_category_aliases_normalize():
    assert normalize_category("hardcoded-secret") == "hardcoded-secrets"
    assert normalize_category("remote code execution") == "rce"
    assert normalize_category("client_side_code_execution") == "code-injection"
    assert normalize_category("security-xss") == "xss"
    assert normalize_category("供应链攻击风险") == "supply-chain-risk"
    assert normalize_category("已知漏洞依赖") == "dependency-vulnerability"
    assert normalize_category("known-vulnerability") == "dependency-vulnerability"
    assert normalize_category("version-unpinned") == "dependency-version-range"
    assert normalize_category("version-range") == "dependency-version-range"
    assert normalize_category("secret-leakage") == "data-leak"
    assert normalize_category("dangerously-set-innerHTML") == "xss"
    assert normalize_category("missing-alt-text") == "missing-alt"
    assert normalize_category("alt-text") == "missing-alt"
    assert normalize_category("missing-form-label") == "missing-label"
    assert normalize_category("unsafe-html") == "xss"
    assert normalize_category("dangerous-html") == "xss"
    assert normalize_category("malicious-dependency") == "supply-chain-risk"
    assert normalize_category("insecure-download") == "insecure-download"
    assert is_security_category("unsafe-code")
    assert is_security_category("supply-chain")
    assert is_security_category("dangerous-html")
    assert is_security_category("malicious-dependency")
    assert is_security_category("insecure-download")
    assert not is_security_category("missing-alt-text")
    assert is_security_category("版本范围不安全")


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


def test_dependency_reviewer_keeps_supply_chain_security_findings():
    registry = build_registry()
    reviewer = DependencyReviewer(MockChatLLM(), registry, ToolGateway(registry, MockGitHubClient()))

    findings = reviewer._parse_findings(
        """
        {
          "findings": [
            {
              "file": "package.json",
              "line": 4,
              "severity": "error",
              "category": "供应链攻击风险",
              "message": "postinstall curl bash",
              "suggestion": "pin and verify installers",
              "confidence": 0.95
            }
          ]
        }
        """
    )

    assert [f.category for f in findings] == ["supply-chain-risk"]
