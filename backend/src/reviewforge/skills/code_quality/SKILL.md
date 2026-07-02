---
name: code_quality
description: 跨语言通用代码质量规则。当没有匹配的语言特定 skill 时作为 fallback 使用。适用于函数复杂度、命名规范、死代码、魔法数字。
category: style
reviewer_type: style
languages: []
---

# Code Quality Review (Universal)

## When to Apply
- 审查任何语言的代码风格和可维护性，且**没有**语言特定的 style skill 可用时
- 作为所有语言特定 style skill 的补充（通用规则）

## When NOT to Apply
- **测试文件** → 测试代码的函数长度、命名规则与业务代码不同
- **配置文件**（`*.json`, `*.yaml`, `*.toml`, `*.xml`）→ 不同规则体系
- **自动生成的代码**（`*.pb.go`, `*_generated.rs`, `*.g.dart`）→ 不审查生成代码
- **vendor/third_party** → 第三方代码
- **已有语言特定 skill 的文件** → 语言特定规则优先，本 skill 只做补充
- **嵌入式 SQL/模板** → 内嵌在字符串中的代码不审查命名/复杂度

## Key Areas

### Function Complexity
- Functions/methods should generally fit on screen (~40 lines); longer functions are a candidate for extraction
- Nesting depth > 3 levels is a warning — consider guard clauses or early returns
- Too many parameters (> 5) is a design smell in most languages

### Naming
- Names should reveal intent: `getUserById()` > `get()`, `isExpired()` > `check()`
- Avoid single-letter variables except in very local scope (loop index `i`, closure param)
- No misleading names: a function called `validate()` shouldn't also mutate state
- Boolean variables should read as questions: `isEmpty`, `hasPermission`, `canEdit`

### Magic Values
- Hardcoded numbers and strings in business logic should be extracted as named constants
- Exception: well-known values like `0`, `1`, `-1`, `""`, `null` don't need names
- Configuration values (timeouts, limits, URLs) must never be inlined

### Dead Code
- Commented-out code blocks are dead code — delete them
- Unused variables, imports, and functions should be removed
- Empty catch/except blocks that silently swallow errors are a red flag

### Single Responsibility
- A function should do one thing well; if the docstring needs "and", it does too much
- A class/module should have one reason to change
- Avoid "god objects" that know about everything — split by concern

## Validation Criteria

**True Positive**: The issue genuinely impacts readability or maintainability. Confidence > 0.6.

**False Positive**:
- The complexity is inherent to the domain (e.g., a state machine, parser, or code generator)
- Naming follows an established project convention that differs from the default
- Magic values are well-known domain constants (e.g., `3.14159` for pi, `86400` for seconds per day)
- "Dead" code is behind a feature flag or conditionally compiled
- Long functions are well-structured with clear section comments
- Generated code, protocol buffer stubs, ORM models
