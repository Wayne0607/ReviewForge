---
name: go_best_practices
description: Go 代码审查规则。当审查 .go 文件时套用。检查错误处理、goroutine 生命周期、接口设计、命名惯例、defer 使用。
category: style
reviewer_type: style
languages: [go]
---

# Go Best Practices Review

## When to Apply
- 审查 `.go` 文件（非测试文件）
- 审查 Go 项目的代码风格、惯用性、可维护性

## When NOT to Apply
- **测试文件**（`*_test.go`）→ 测试中的 `_ = err` 有时是故意忽略不重要的错误；表驱动测试的长函数是惯用法
- **生成的代码**（`*.pb.go`, `*_string.go`）→ 不审查
- **vendor/ 目录** → 第三方依赖
- **main.go 初始化代码** → `panic` 和 `os.Exit` 在 CLI 入口是合理的
- **CGo 绑定代码** → 遵循 C 的惯例而非纯 Go 惯例

## Security（必查，最高优先级）

### Command Injection
- `exec.Command()` with user-controlled command name → **error**
- Shell invocation: `exec.Command("bash", "-c", userInput)` → **error**
- Check: is user input passed to exec.Command without whitelisting?

### SQL Injection
- `fmt.Sprintf("SELECT ... WHERE name = '%s'", userInput)` → **error**
- String concatenation in query strings → **error**
- Check: is every variable passed via `?` placeholders, not string formatting?

### Hardcoded Secrets
- API keys, tokens, passwords as string literals → **error**

### XSS (html/template)
- `template.HTML(userInput)` marks untrusted content as safe → **error**
- Using `text/template` for HTML output → **error**

## Key Areas

### Error Handling
- Never ignore errors with `_ = err` — always check and handle
- Don't just log and continue; decide: return, retry, or wrap with context
- Use `fmt.Errorf("...: %w", err)` to wrap errors, preserving the chain
- Avoid `panic` in library code; use it only for truly unrecoverable states

### Goroutine Lifecycle
- Every goroutine must have a clear exit path (context cancellation, channel close)
- Use `context.Context` for cancellation propagation
- Never leak goroutines: verify there's a `<-ctx.Done()` or `defer close(ch)` path
- Always `defer wg.Done()` immediately after `wg.Add(1)`

### Interface Design
- Interfaces should be small (1-3 methods); single-method interfaces preferred
- Define interfaces where consumed, not where implemented
- Accept interfaces, return concrete types

### Naming
- Package: lowercase, single word, no underscores
- Exported: PascalCase, initialisms all-caps (`HTTPServer`, `UserID`)
- Avoid stutter: `user.UserName` → `user.Name`
- Getters: `Owner()` not `GetOwner()`

### Performance
- Pre-allocate slice capacity when size is known: `make([]T, 0, n)`
- `defer` in a loop delays execution until function exit — wrap in closure

## Validation Criteria

**True Positive**: Violates Go conventions and causes bugs, leaks, or maintainability issues. Confidence > 0.7.

**False Positive**:
- `panic` in `init()` for must-parse templates/configs
- `_ = err` in test code or where the error is truly irrelevant (e.g., `defer f.Close()`)
- Long functions in table-driven tests are idiomatic
- Generated code (`*.pb.go`, `*_string.go`, `*_mock.go`)
- Interface defined at implementation site because it mirrors an external API contract
