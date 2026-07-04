"""Security category normalization shared by reviewers, calibration, and cross-PR analysis."""

import re

SECURITY_CATEGORIES = {
    "authentication",
    "authorization",
    "code-injection",
    "command-injection",
    "config-injection",
    "crypto",
    "csrf",
    "data-leak",
    "hardcoded-secrets",
    "insecure-deserialization",
    "open-redirect",
    "path-traversal",
    "rce",
    "security",
    "sql-injection",
    "ssrf",
    "unsafe-block",
    "unsafe-deserialization",
    "unsafe-transmute",
    "unsafe-usage",
    "xss",
    "xss-bypass",
    "xxe",
}

_ALIASES = {
    "arbitrary-code-execution": "rce",
    "client-side-code-execution": "code-injection",
    "directory-traversal": "path-traversal",
    "file-disclosure": "path-traversal",
    "file-path-traversal": "path-traversal",
    "hardcoded-secret": "hardcoded-secrets",
    "hardcoded-token": "hardcoded-secrets",
    "insecure-crypto": "crypto",
    "insecure-randomness": "crypto",
    "insecure-yaml-load": "insecure-deserialization",
    "open-redirection": "open-redirect",
    "os-command-injection": "command-injection",
    "remote-code-execution": "rce",
    "rce-via-deserialization": "insecure-deserialization",
    "secret-exposure": "hardcoded-secrets",
    "secret-leak": "hardcoded-secrets",
    "sensitive-data-exposure": "data-leak",
    "shell-injection": "command-injection",
    "token-leak": "data-leak",
    "token-storage": "data-leak",
    "unsafe-code": "unsafe-block",
    "weak-crypto": "crypto",
    "weak-cryptography": "crypto",
}


def normalize_category(category: str) -> str:
    """Return a stable finding category while preserving non-security labels."""
    raw = str(category or "").strip().lower().replace("_", "-").replace(" ", "-")
    normalized = re.sub(r"-+", "-", raw).strip("-")
    return _ALIASES.get(normalized, normalized)


def is_security_category(category: str) -> bool:
    """Whether a finding category should be treated as security-sensitive."""
    return normalize_category(category) in SECURITY_CATEGORIES
