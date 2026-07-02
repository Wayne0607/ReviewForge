---
name: python_best_practices
description: Python 代码审查规则。当审查 .py 文件时套用。检查类型注解、异常处理、命名规范、函数复杂度、安全漏洞（eval/exec/pickle/yaml.load/os.system）。
category: style
reviewer_type: style
languages: [python]
---

# Python Best Practices Review

## When to Apply
- 审查 `.py` 文件（非测试文件）
- 审查 Python 项目的安全性、可读性、惯用性

## When NOT to Apply
- **测试文件**（`test_*.py`, `*_test.py`, `tests/`）→ 测试中的 `# noqa`、bare except、mock 模式是合理惯用法
- **生成的代码**（protobuf stubs, gRPC stubs, migration files）→ 不审查
- **Jupyter notebooks** → 探索性代码，规则放宽
- **`__init__.py`** → 仅包含导入的包文件
- **`setup.py` / `conftest.py`** → 配置/构建文件
- **Django/Flask 迁移文件** → 自动生成的
- **`__main__.py`** → CLI 入口点

## Key Areas

### Type Hints
- Public functions should have type hints (parameters and return)
- Use `from __future__ import annotations` for forward refs
- Don't flag: `Any` in highly dynamic code, callback-heavy code

### Error Handling
- Bare `except:` or `except Exception: pass` → **error** (检查是否故意)
- Errors should be logged or re-raised, never silently swallowed
- Use specific exception types (`ValueError`, `KeyError`) not `Exception`

### Naming
- Functions: `snake_case`; Classes: `PascalCase`; Constants: `UPPER_SNAKE_CASE`
- Private: leading underscore `_name`; Internal: double underscore `__name`

### Function Complexity
- Functions > 30 lines should be split
- Nesting > 3 levels is a warning
- Too many parameters (> 5) is a design smell

### Imports
- Unused imports → **warning**
- Circular imports → **error**
- Wildcard imports (`from module import *`) → **warning**

## Security
- `os.system()` / `os.popen()` with user input → **error**
- `pickle.loads()` on untrusted data → **error**
- `yaml.load()` without SafeLoader → **error**
- `eval()` / `exec()` with user input → **error**
- SQL queries via f-strings or `%` formatting → **error**
- Hardcoded API keys/tokens → **error**

## Validation Criteria

**True Positive**: The issue makes the code harder to maintain, understand, or is a security risk. Confidence > 0.7.

**False Positive**:
- A broad except in a top-level error handler (e.g., `main()`, web framework middleware)
- `# type: ignore` or `# noqa` comments indicating intentional deviation
- Generated or auto-formatted code
- `Any` type usage in highly dynamic or callback-heavy code
- Long function that is a well-structured state machine or dispatcher
