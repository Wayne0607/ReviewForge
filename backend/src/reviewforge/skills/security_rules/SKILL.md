---
name: security_rules
description: 通用安全漏洞检测规则。适用于所有语言。当代码涉及用户输入处理、认证、序列化、文件操作、加解密、密钥管理时必须套用。
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

## When to Apply
- 代码处理用户输入（表单、API 参数、文件上传、URL 参数）
- 代码涉及认证/授权逻辑
- 代码执行系统命令、文件操作、网络请求
- 代码包含序列化/反序列化
- 代码中有密钥、token、密码等机密信息
- 依赖文件变更（可能引入已知漏洞的依赖）

## When NOT to Apply
- **测试文件**（`*_test.go`, `*Test.java`, `test_*.py`, `*.spec.ts`, `__tests__/`）→ 测试中的硬编码密钥、eval 是正常测试数据，不报告
- **示例/演示代码**（`examples/`, `demo/`, `sample/`）→ 为了演示目的可能故意简化安全措施
- **构建脚本/CI 配置** → CI 中的环境变量引用不是"硬编码"
- **文档中的代码片段**（`*.md`, `README`）→ 文档中的示例代码不审查
- **vendor/third_party 目录** → 第三方代码不改动
- **注释中的代码** → 不审查注释内容

## Attack Surface
Focus on: user input handling, authentication flows, data serialization, file operations, network requests, crypto usage, secret management.

## Key Vulnerabilities

### SQL Injection
- String concatenation or f-strings in SQL queries
- Missing parameterized queries
- ORM raw query usage without escaping

### XSS (Cross-Site Scripting)
- Unsanitized user input rendered in HTML/templates
- `dangerouslySetInnerHTML` in React, `v-html` in Vue, `{@html}` in Svelte, `[innerHTML]` in Angular
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
- `pickle.loads` on untrusted data (Python)
- `yaml.load` without `Loader=SafeLoader`
- `ObjectInputStream` on untrusted input (Java)
- `eval()` / `exec()` on user input (any language)

## Validation Criteria

**True Positive**: The code path is reachable with user-controlled input AND the vulnerability is exploitable. Confidence > 0.7.

**False Positive**:
- The code is in a test file
- Input is already sanitized/validated elsewhere (trace the data flow)
- The pattern appears in a comment or string literal
- The code path is unreachable (dead code, feature-flagged off)
- It's a false match (e.g., `exec` in variable name like `executor.run()`)

## Methodology
1. Read the diff to identify changed code paths
2. Trace input sources: is any part user-controlled?
3. Check if sanitization/validation exists before the sink
4. Consider the context: is this behind auth? Is it a test file?
5. Only report if confidence > 0.7
