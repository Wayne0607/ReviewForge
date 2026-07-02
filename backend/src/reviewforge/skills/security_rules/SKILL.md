---
name: security_rules
description: Security vulnerability detection rules for common web application patterns
category: security
reviewer_type: security
languages: []
references:
  - patterns.md
  - go_patterns.md
  - java_patterns.md
  - rust_patterns.md
  - frontend_patterns.md
---

# Security Review Rules

## Attack Surface

Focus on: user input handling, authentication flows, data serialization, file operations, network requests, crypto usage, secret management.

## Key Vulnerabilities

### SQL Injection
- String concatenation or f-strings in SQL queries
- Missing parameterized queries
- ORM raw query usage without escaping

### XSS (Cross-Site Scripting)
- Unsanitized user input rendered in HTML/templates
- `dangerouslySetInnerHTML` in React
- Missing Content-Security-Policy headers

### Path Traversal
- User input in file paths without sanitization
- Missing `..` filtering
- Symlink following in file operations

### Hardcoded Secrets
- API keys, passwords, tokens in source code
- Default credentials in configuration
- Secrets in environment variable defaults

### Insecure Deserialization
- `pickle.loads` on untrusted data
- `yaml.load` without `Loader=SafeLoader`
- `eval()` / `exec()` on user input

## Validation Criteria

**True Positive**: The code path is reachable with user-controlled input and the vulnerability is exploitable.

**False Positive**: The code is behind authentication, input is already sanitized elsewhere, or the pattern is a false match (e.g., string in a comment).

## Methodology

1. Read the diff to identify changed code paths
2. Trace input sources: is any part user-controlled?
3. Check if sanitization/validation exists before the sink
4. Consider the context: is this behind auth? Is it a test file?
5. Only report if confidence > 0.6
